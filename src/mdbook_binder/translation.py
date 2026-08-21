"""로컬 Ollama로 마크다운 코퍼스를 번역한다 — `mdbook-binder translate`.

manifest.resolve()가 이미 아는 챕터 순서/exclude 규칙을 그대로 재사용해
걷고, 코드/mermaid/raw-HTML 블록은 render.py가 HTML 렌더링 전에 쓰는 것과
동일한 정규식 경계로 보호했다가 그대로 복원한다 — 번역 대상에서 제외해야
할 블록의 정의를 두 곳에서 따로 유지하지 않기 위해서다.

실제 Ollama 호출(`make_ollama_translate_fn`)은 이 모듈에서 `import ollama`가
일어나는 유일한 지점이다 — 나머지 함수는 전부 `translate_fn: Callable[[str], str]`을
주입받는 순수/의사-순수 함수라, Ollama 설치 없이도 청킹·블록 보호·진행
출력·book.yaml 재작성 로직을 단위 테스트할 수 있다.

k2e(한→영) 방향은 청크당 has_residual_korean()으로 번역 결과를 검증하고
실패 시 자동 재시도한다 — 소형 로컬 모델이 밀도 높은 청크에서 지시를
놓치고 원문을 그대로 돌려주는 경우가 실사용에서 잦았는데, 이전에는 그런
결과를 검증 없이 그대로 통과시켰다. e2k는 검증하지 않는다(§translate_chapter
docstring 참고 — 정상적인 영문 잔존과 번역 실패를 구분할 신호가 없음).

재시도 후에도 미완료였던 챕터는 out_dir에 `.incomplete.json` 마커를 남긴다
(§translate_corpus docstring). `--resume`은 이 마커가 있는 챕터를 "이미
번역됨"으로 건너뛰지 않고 자동으로 삭제 후 재번역하므로, 같은 코퍼스를
--resume으로 반복 실행하면(LLM 출력이 비결정적이므로) 미완료 챕터 수가
점차 줄어드는 방향으로 수렴하는 것을 기대할 수 있다.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

import markdown as md_lib
import yaml

from mdbook_binder import render
from mdbook_binder.manifest import BookConfig, TranslationConfig, resolve, write_minimal_book_yaml

# render.py가 HTML 렌더링 직전 쓰는 것과 동일한 블록 경계 + 번역 청킹에서만
# 필요한 일반 코드 펜스(마크다운 파서의 fenced_code 확장이 별도 전처리 없이도
# 처리하므로 render.py에는 없던 패턴). 적용 순서가 중요하다 — HTML 블록을
# 먼저 떼어내야 그 안에 우연히 들어있는 ```로 다른 패턴이 오작동하지 않는다.
_GENERIC_FENCE_RE = re.compile(r"^```(\w*)\n.*?^```\s*$", re.MULTILINE | re.DOTALL)
_PROTECTED_PATTERNS = [
    render.HTML_BLOCK_RE,
    render.MERMAID_FENCE_RE,
    render.BQ_CODE_FENCE_RE,
    _GENERIC_FENCE_RE,
]

_PLACEHOLDER_RE = re.compile(r"@@TBLOCK_(\d+)@@")


def protect_blocks(text: str) -> tuple[str, list[str]]:
    """번역 대상에서 코드/mermaid/raw-HTML 블록을 @@TBLOCK_N@@으로 치환한다.

    render.md_to_html()의 restore_block()과 달리 HTML로 래핑할 필요가 없다
    (번역 후에도 여전히 마크다운이어야 하므로) — 원문을 그대로 붙여넣기만
    하면 되는 훨씬 단순한 복원이라 별도의 얕은 추출/복원 쌍을 둔다.
    """
    blocks: list[str] = []

    def _extract(m: re.Match) -> str:
        blocks.append(m.group(0))
        return f"@@TBLOCK_{len(blocks) - 1}@@"

    for pattern in _PROTECTED_PATTERNS:
        text = pattern.sub(_extract, text)
    return text, blocks


def restore_blocks(text: str, blocks: list[str]) -> str:
    def _restore(m: re.Match) -> str:
        return blocks[int(m.group(1))]

    return _PLACEHOLDER_RE.sub(_restore, text)


def chunk_paragraphs(text: str, chunk_chars: int) -> list[str]:
    """빈 줄 기준 단락을 chunk_chars까지 그리디하게 채운다.

    문장 중간에서 자르면 번역 문맥이 끊기므로 항상 단락 경계에서만 자른다.
    단락 하나가 chunk_chars보다 길어도 그 단락 혼자 청크가 된다(잘라내지
    않음 — 문맥이 끊기는 것보다 청크 하나가 좀 커지는 편이 낫다).
    """
    paragraphs = [p for p in text.split("\n\n") if p.strip() != ""]
    if not paragraphs:
        return [text] if text else []

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for para in paragraphs:
        para_len = len(para)
        added_len = para_len if not current else para_len + 2  # "\n\n" 구분자
        if current and current_len + added_len > chunk_chars:
            chunks.append("\n\n".join(current))
            current = [para]
            current_len = para_len
        else:
            current.append(para)
            current_len += added_len
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def reassemble_chunks(translated_chunks: list[str]) -> str:
    return "\n\n".join(translated_chunks)


_HANGUL_RE = re.compile(r"[가-힣]")
# k2e(한→영) 청크 하나가 그리디 청킹으로 2000자 안팎씩 묶이는데, 로컬
# 소형 모델(기본 exaone3.5:7.8b)은 이 정도 크기·마크다운 밀도의 청크에서
# 가끔 지시를 놓치고 원문을 그대로(또는 앞부분만) 돌려준다 — 실사용
# 코퍼스에서 섹션 전체가 한글로 고스란히 남는 형태로 재현됨. 성공적으로
# 번역된 청크도 고유명사·acronym 한두 개 정도는 한글로 남을 수 있으므로
# "0%"가 아니라 "그 청크가 사실상 번역되지 않았다"로 볼 수 있는 임계값을
# 쓴다 — 원문 한국어 산문의 한글 비율(보통 30%+)과는 확실히 구분되면서도
# 정상 번역의 잔여 고유명사는 오탐하지 않는 지점.
_RESIDUAL_KOREAN_THRESHOLD = 0.05
_MAX_RETRIES = 2


def residual_korean_ratio(text: str) -> float:
    """텍스트에서 한글 문자가 차지하는 비율(0~1)을 반환한다(순수 함수)."""
    return len(_HANGUL_RE.findall(text)) / len(text) if text else 0.0


def has_residual_korean(text: str, *, threshold: float = _RESIDUAL_KOREAN_THRESHOLD) -> bool:
    """k2e 번역 결과가 사실상 번역되지 않은 채로 남아있는지 판정한다(순수 함수)."""
    return residual_korean_ratio(text) > threshold


# render.py가 HTML 렌더링에 이미 쓰는 것과 같은 "tables" 확장을 그대로 재사용한다
# — 표 인식/셀 분리 규칙을 translation.py에서 정규식으로 새로 짜면 render.py와
# 판정이 갈릴 위험이 있고, 인라인 코드 안의 `|`를 이스케이프까지 고려해 안전하게
# 셀 경계를 찾는 로직(_split_row)을 직접 재구현하는 것도 불필요하다. 파서 자체가
# 상태를 갖는 인스턴스라(test()가 self.border/self.separator를 채워야 다음
# _split_row 호출이 맞게 동작함) 모듈 레벨에서 한 번만 만들어 공유한다.
_TABLE_PROCESSOR = md_lib.Markdown(extensions=["tables"]).parser.blockprocessors["table"]


def is_table_chunk(chunk: str) -> bool:
    """청크 전체가 마크다운 표 블록 하나로만 이뤄져 있는지 판정한다(순수 함수).

    chunk_paragraphs()는 여러 원본 단락을 "\\n\\n"으로 그리디하게 합칠 수 있다.
    표가 다른 단락과 섞여 청크 안에 빈 줄이 남아 있으면(표만 있는 게 아니면)
    행 단위 재조립이 안전하지 않으므로 표 전용 처리 대상에서 제외하고 기존
    방식(청크 전체를 translate_fn에 통째로 넘김)으로 넘긴다.
    """
    if "\n\n" in chunk:
        return False
    # test()의 타입 스텁은 parent: Element를 요구하지만 실제 구현은 이 인자를
    # 쓰지 않는다(render.py가 실제 렌더링에 쓰는 것과 동일한 인스턴스로 직접
    # 확인함) — None을 넘겨도 안전하다.
    return bool(_TABLE_PROCESSOR.test(None, chunk))  # type: ignore[arg-type]


_CELL_HALLUCINATION_LEN_MULTIPLIER = 4
_CELL_HALLUCINATION_LEN_SLACK = 40


def _looks_like_cell_hallucination(original: str, result: str) -> bool:
    """짧은 표 셀 번역 결과가 원문 셀이 아니라 프롬프트가 언급한 마크다운
    요소(제목·리스트·표 등)의 예시를 지어낸 환각인지 어림짐작한다(순수 함수).

    실사용 실패 사례에서 이 환각은 두 신호로 거의 항상 구분됐다 — (1) 셀
    안에 개행이 생긴다(정상 셀 번역은 항상 한 줄), (2) 원문 길이 대비 결과가
    비정상적으로 길다(3단어 셀이 수백 자짜리 체크리스트로 부풀려지는 식).
    두 신호 중 하나만 봐도 표 구조를 깨뜨리므로(줄 단위 재조립을 가정하는
    translate_table_chunk) 임계값을 넉넉히 잡아 과탐지보다 놓치는 쪽을
    택하지 않는다.
    """
    if "\n" in result:
        return True
    return len(result) > len(original) * _CELL_HALLUCINATION_LEN_MULTIPLIER + _CELL_HALLUCINATION_LEN_SLACK


def _translate_cell(cell: str, translate_fn: Callable[[str], str]) -> str:
    """셀 하나를 번역하고, 결과가 환각으로 의심되면 한 번 더 시도한 뒤 그래도
    환각이면 원문 셀을 그대로 돌려준다.

    로컬 모델은 temperature>0이라 같은 셀을 한 번 더 물어보면 정상적으로
    번역되는 경우가 실사용에서 흔했다 — 그래도 환각이 반복되면, 표 구조를
    깨뜨리는 다중 라인 결과를 그대로 끼워 넣는 것보다 셀을 미번역 상태로
    남겨(has_residual_korean이 챕터 단위 재시도 대상으로 잡아줌) 사람이
    나중에 확인할 수 있게 하는 편이 안전하다.
    """
    result = translate_fn(cell)
    if _looks_like_cell_hallucination(cell, result):
        result = translate_fn(cell)
    if _looks_like_cell_hallucination(cell, result):
        return cell
    return result


def translate_table_chunk(chunk: str, translate_fn: Callable[[str], str]) -> str:
    """표 청크를 행의 셀 단위로 번역해 파이프(|) 구조를 코드가 그대로 보존한
    채 재조립한다.

    표 전체를 청크 하나로 통째로 로컬 소형 모델에 넘기면 뒤쪽 행을 통째로
    빠뜨리거나 파이프 구조 자체를 깨뜨리는 사례가 실사용에서 반복 확인됐다
    (§translate_chapter docstring 참고) — 마크다운 표 문법을 모델이 그대로
    지켜주길 기대하는 대신, 셀 텍스트만 개별로 모델에 보내고 파이프로
    재조립하는 일은 코드가 맡는다. 구분선 행(|---|---|)과 한글이 없는 셀
    (숫자·기호·영문 전용 — 참조 번호 `§14`, 체크마크 등)은 번역할 내용이
    없으므로 모델을 거치지 않고 그대로 둔다.
    """
    if not _TABLE_PROCESSOR.test(None, chunk):  # type: ignore[arg-type]
        return translate_fn(chunk)  # 방어적 폴백 — 호출부는 항상 is_table_chunk()로 먼저 확인한다

    out_rows: list[str] = []
    for i, row in enumerate(chunk.split("\n")):
        if i == 1:
            out_rows.append(row)  # 구분선은 셀이 아니라 번역하지 않고 그대로 유지
            continue
        # _split_row는 밑줄로 시작하는 내부 메서드라 타입 스텁에 없지만, 인라인
        # 코드 속 이스케이프된 `|`까지 안전하게 처리하는 로직을 정규식으로 새로
        # 짜지 않기 위해 재사용한다.
        cells = _TABLE_PROCESSOR._split_row(row)  # type: ignore[attr-defined]
        translated_cells = [
            _translate_cell(cell.strip(), translate_fn) if _HANGUL_RE.search(cell) else cell.strip()
            for cell in cells
        ]
        out_rows.append("| " + " | ".join(translated_cells) + " |")
    return "\n".join(out_rows)


def translate_chunk(chunk: str, translate_fn: Callable[[str], str]) -> str:
    """청크 하나를 번역한다 — 표 단락은 translate_table_chunk로, 나머지
    산문 단락은 (연속된 것끼리 묶어) translate_fn으로 넘긴다.

    chunk_paragraphs()는 원본 단락을 "\\n\\n" 기준 그리디하게 묶으므로,
    표 뒤에 짧은 소제목·문단이 그대로 이어붙어 한 청크가 되는 경우가
    실사용 코퍼스에서 흔했다(예: 표 바로 뒤 "### 그룹 B ..." 같은 다음
    절 제목). is_table_chunk()가 청크 안에 빈 줄이 하나라도 있으면 표
    전용 처리를 포기하던 예전 방식은 바로 이 흔한 경우를 전부 놓쳤다 —
    그래서 여기서는 청크를 원래 단락 단위로 다시 쪼개 표 단락만 골라
    처리하고, 나머지는 원래 묶여 있던 순서·인접 관계를 그대로 살려
    (표가 아닌 단락들끼리는 계속 하나로 묶어) translate_fn을 호출한다.
    """
    paragraphs = chunk.split("\n\n")
    if len(paragraphs) == 1:
        return (
            translate_table_chunk(chunk, translate_fn)
            if is_table_chunk(chunk)
            else translate_fn(chunk)
        )

    parts: list[str] = []
    prose_run: list[str] = []

    def _flush_prose() -> None:
        if prose_run:
            parts.append(translate_fn("\n\n".join(prose_run)))
            prose_run.clear()

    for para in paragraphs:
        if is_table_chunk(para):
            _flush_prose()
            parts.append(translate_table_chunk(para, translate_fn))
        else:
            prose_run.append(para)
    _flush_prose()

    return "\n\n".join(parts)


def translate_chapter(
    text: str,
    chunk_chars: int,
    translate_fn: Callable[[str], str],
    *,
    target_language: str | None = None,
    max_retries: int = _MAX_RETRIES,
    on_chunk_start: Callable[[int, int], None] | None = None,
    on_incomplete: Callable[[int, int], None] | None = None,
    on_chunk_done: Callable[[int, str], None] | None = None,
    previous_chunks: list[str] | None = None,
    chunks_to_retranslate: set[int] | None = None,
) -> str:
    """챕터 하나를 번역한다 — 코드/mermaid/raw-HTML을 보호한 뒤 청크 단위로
    translate_fn을 호출하고 재조립·복원한다.

    on_chunk_start(j, n)은 청크(1-indexed) 번역을 시작하기 직전 호출된다 —
    로컬 LLM은 청크 하나에도 수십 초가 걸릴 수 있어, 호출부(translate_corpus)가
    이 훅으로 진행 상황을 찍지 않으면 멈춘 것처럼 보이기 쉽다. on_chunk_done(j,
    result)는 청크 하나의 최종 결과(마스킹된 채로 — restore_blocks 이전)가
    정해질 때마다 호출된다(재사용으로 건너뛴 청크 포함, 항상 i=1..총
    청크수 순서로 한 번씩) — 호출부가 청크별 결과를 다음 --resume에
    캐시해두려 할 때 쓴다(§translate_corpus의 마커 파일 참고).

    target_language="en"(k2e)일 때만 has_residual_korean()으로 각 청크
    결과를 검증하고, 여전히 한글이 남아있으면 translate_fn을 최대
    max_retries회 다시 호출한다(동일 청크를 모델에 다시 물어보는 것 —
    LLM 출력은 비결정적이라 재시도로 통과하는 경우가 실사용에서 흔하다).
    e2k(target_language="ko")는 검증하지 않는다 — 한국어 기술 문서에
    영문 고유명사·약어가 정상적으로 섞이는 게 흔해 "번역 실패"와
    "정상적인 영문 잔존"을 신뢰성 있게 구분할 신호가 없다. 재시도 후에도
    실패하면 on_incomplete(j, n)를 호출해 호출부가 어떤 청크가 여전히
    한글로 남았는지 알 수 있게 한다 — 조용히 안 좋은 출력을 그대로
    통과시키던 이전 동작과 달리, 최소한 사람이 나중에 찾아볼 수 있게 한다.

    청크 안에 마크다운 표 단락이 있으면(translate_chunk 참고) 청크 전체를
    translate_fn에 통째로 넘기는 대신 표 단락만 행의 셀 단위로 나눠
    번역한다 — 표는 밀도 높은 반복 구조라 로컬 소형 모델이 청크 전체를
    한 번에 번역할 때 뒤쪽 행을 통째로 빠뜨리거나 파이프 구조를 깨뜨리는
    실패가 실사용에서 반복 확인됐다.

    previous_chunks/chunks_to_retranslate는 청크 단위 --resume을 위한
    것이다: previous_chunks가 이번에 계산된 청크 개수와 정확히 같은
    길이면(같은 소스 텍스트·chunk_chars로 이전에 한 번 청킹된 결과라는
    뜻 — 다르면 청크 경계 자체가 달라져 재사용이 안전하지 않으므로 무시
    하고 전부 새로 번역한다) 그 값을 초기 translated 리스트로 삼는다.
    chunks_to_retranslate가 주어지면 그 안에 있는 청크(1-indexed)만
    translate_fn을 다시 타고, 나머지는 이전 결과를 그대로 재사용해
    on_chunk_start도 호출하지 않는다(이미 성공한 청크를 매번 다시
    모델에 물어보면 비결정성 때문에 오히려 새로 실패할 수도 있고,
    무엇보다 낭비다).
    """
    masked, blocks = protect_blocks(text)
    chunks = chunk_paragraphs(masked, chunk_chars)
    total = len(chunks)

    if previous_chunks is not None and len(previous_chunks) == total:
        reuse_previous = True
        translated: list[str] = list(previous_chunks)
    else:
        reuse_previous = False
        translated = [""] * total

    for i, chunk in enumerate(chunks, start=1):
        if reuse_previous and chunks_to_retranslate is not None and i not in chunks_to_retranslate:
            if on_chunk_done:
                on_chunk_done(i, translated[i - 1])
            continue

        if on_chunk_start:
            on_chunk_start(i, total)
        if not chunk.strip():
            translated[i - 1] = chunk
            if on_chunk_done:
                on_chunk_done(i, chunk)
            continue

        result = translate_chunk(chunk, translate_fn)
        if target_language == "en" and has_residual_korean(result):
            for _ in range(max_retries):
                result = translate_chunk(chunk, translate_fn)
                if not has_residual_korean(result):
                    break
            else:
                if on_incomplete:
                    on_incomplete(i, total)
        translated[i - 1] = result
        if on_chunk_done:
            on_chunk_done(i, result)

    return restore_blocks(reassemble_chunks(translated), blocks)


def _incomplete_marker_path(dest: Path) -> Path:
    """dest 옆에 두는 미완료 마커 경로. out_dir을 resolve()가 다시 걷지
    않으므로(*.md만 스캔) .json 사이드카가 있어도 이후 빌드/번역에 영향이
    없다."""
    return dest.with_name(dest.name + ".incomplete.json")


def translate_corpus(
    root: Path,
    out_dir: Path,
    config: BookConfig | None,
    target_language: str,
    translate_fn: Callable[[str], str],
    *,
    chunk_chars: int | None = None,
    resume: bool = False,
) -> Path:
    """manifest.resolve()로 걷은 챕터를 순서대로 번역해 out_dir에 동일한
    디렉토리 구조로 미러링하고, book.yaml의 language를 target_language로
    재작성한다(book.yaml이 없던 코퍼스면 최소 book.yaml을 새로 만든다).

    resume=True면 out_dir에 이미 결과 파일이 있는 챕터는 건너뛴다 — 대용량
    코퍼스 번역 중 네트워크/타임아웃으로 중간에 실패했을 때, 같은 명령을
    `--resume`으로 다시 실행하면 이미 끝난 챕터를 재번역하지 않고 이어서
    진행할 수 있다. 청크 하나라도 재시도 후에도 미완료였던 챕터는 dest
    옆에 `<파일명>.incomplete.json` 마커를 남긴다 — 이 마커는 실패한
    청크 번호뿐 아니라 그 시점의 청크별 번역 결과 전체(`chunks`)와
    청킹에 쓰인 `chunk_chars`도 함께 담는다. 다음 --resume 실행은 이
    마커를 읽어 **실패했던 청크만** 다시 번역하고 나머지는 캐시된 결과를
    그대로 재사용한다(§translate_chapter의 previous_chunks/
    chunks_to_retranslate 참고) — 파일 전체를 매번 처음부터 다시 번역하던
    이전 방식은 이미 통과한 청크까지 매번 새로 무작위 샘플링해 오히려
    새로 실패하는 청크가 생기기도 했다. 캐시된 `chunk_chars`가 이번 실행의
    유효 chunk_chars와 다르면(청킹 경계 자체가 달라져 재사용이 안전하지
    않으므로) 통째로 무시하고 챕터 전체를 처음부터 다시 번역한다 — 0.5.3
    이전 버전이 남긴 마커(chunks 필드 자체가 없음)도 같은 경로로 전체
    재번역된다. LLM 출력이 비결정적이라 같은 챕터를 --resume으로 반복
    실행할수록 미완료 청크 수가 점차 줄어드는 것이 기대 동작이다(수렴
    보장은 없음 — 특정 청크가 매 시도 실패할 수도 있다). 이 마커는
    on_incomplete가 실제로 호출될 때만 쓰이므로, 현재는 k2e
    (has_residual_korean 검증이 있는 방향)에서만 의미 있게 동작한다 — e2k는
    청크 검증 자체가 없어(§translate_chapter docstring) 마커가 생기지 않고,
    기존과 동일하게 파일 존재 여부만으로 건너뛴다.
    """
    effective_chunk_chars = chunk_chars or (
        config.translation.chunk_chars
        if (config and config.translation)
        else TranslationConfig().chunk_chars
    )

    chapters = resolve(root, config)
    total = len(chapters)
    incomplete_chapters: list[str] = []
    for i, chap in enumerate(chapters, start=1):
        rel = chap.path.relative_to(root)
        dest = out_dir / rel
        marker = _incomplete_marker_path(dest)

        previous_chunks: list[str] | None = None
        chunks_to_retranslate: set[int] | None = None

        if resume and dest.exists():
            if marker.exists():
                marker_data = json.loads(marker.read_text(encoding="utf-8"))
                cached_chunks = marker_data.get("chunks")
                incomplete = marker_data.get("incomplete_chunks", [])
                if cached_chunks is not None and marker_data.get("chunk_chars") == effective_chunk_chars:
                    previous_chunks = cached_chunks
                    chunks_to_retranslate = set(incomplete)
                    print(
                        f"\U0001f501 [{i}/{total}] {rel} — 이전 실행에서 미완료였던 청크 {incomplete}만 재번역"
                    )
                else:
                    # chunks 필드가 없는 구버전(0.5.3 이전) 마커이거나 chunk_chars가
                    # 바뀌어 청킹 경계가 달라졌다 — 부분 재사용이 안전하지 않으므로
                    # 챕터 전체를 처음부터 다시 번역한다(아래 공통 경로로 그대로 진행).
                    print(f"\U0001f501 [{i}/{total}] {rel} — 이전 실행에서 미완료 청크가 남아 재번역")
            else:
                print(f"⏭️  [{i}/{total}] {rel} — 이미 번역됨, 건너뜀")
                continue

        print(f"\U0001f4c4 [{i}/{total}] {rel}")

        text = chap.path.read_text(encoding="utf-8")
        incomplete_chunks: list[int] = []
        translated_chunks: list[str] = []

        def _on_chunk_start(j: int, n: int) -> None:
            print(f"  청크 {j}/{n} 번역 중...")

        def _on_incomplete(j: int, n: int, _sink: list[int] = incomplete_chunks) -> None:
            _sink.append(j)

        def _on_chunk_done(j: int, result: str, _sink: list[str] = translated_chunks) -> None:
            _sink.append(result)

        out_text = translate_chapter(
            text,
            effective_chunk_chars,
            translate_fn,
            target_language=target_language,
            on_chunk_start=_on_chunk_start,
            on_incomplete=_on_incomplete,
            on_chunk_done=_on_chunk_done,
            previous_chunks=previous_chunks,
            chunks_to_retranslate=chunks_to_retranslate,
        )

        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(out_text, encoding="utf-8")

        if incomplete_chunks:
            print(f"  ⚠️  재시도 후에도 한글이 남은 청크: {incomplete_chunks} — 수동 확인 필요")
            incomplete_chapters.append(f"{rel} (청크 {incomplete_chunks})")
            marker.write_text(
                json.dumps(
                    {
                        "incomplete_chunks": incomplete_chunks,
                        "total_chunks": len(translated_chunks),
                        "chunk_chars": effective_chunk_chars,
                        "chunks": translated_chunks,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        elif marker.exists():
            marker.unlink()

    _write_translated_book_yaml(root, out_dir, target_language)

    if incomplete_chapters:
        print(
            f"\n⚠️  총 {len(incomplete_chapters)}개 챕터에 재시도 후에도 번역되지 않은 청크가 있습니다:"
        )
        for item in incomplete_chapters:
            print(f"   - {item}")
        print(
            "   해당 구간을 직접 열어 확인하거나, translate를 다시 실행해보세요"
            "(로컬 LLM 출력은 비결정적이라 재실행 시 통과하는 경우가 있습니다)."
        )

    return out_dir


def _write_translated_book_yaml(root: Path, out_dir: Path, target_language: str) -> None:
    """원본 book.yaml이 있으면 language만 target_language로 바꿔 옮기고, 없으면
    최소 book.yaml(language만)을 새로 만든다.

    PyYAML의 safe_load/safe_dump 왕복은 주석을 보존하지 못한다 — 원본
    book.yaml에 주석이 있었다면 번역된 코퍼스에서는 사라진다(Phase 1의
    알려진 한계로 남겨둔다).
    """
    src_path = root / "book.yaml"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest_path = out_dir / "book.yaml"

    if not src_path.exists():
        write_minimal_book_yaml(dest_path, language=target_language)
        return

    data = yaml.safe_load(src_path.read_text(encoding="utf-8")) or {}
    data["language"] = target_language
    dest_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def _build_prompt(chunk: str, target_language: str) -> str:
    """마크다운 문법을 그대로 유지하라는 지시를 포함한 번역 프롬프트.

    "Preserve all Markdown syntax (headings, lists, links, emphasis, tables)"처럼
    마크다운 요소를 나열해 지시하던 이전 문구는 exaone3.5:7.8b 같은 소형 로컬
    모델에서 역효과를 냈다 — 실사용 실패 청크를 까보면 모델이 실제 원문을
    번역하는 대신 그 나열된 카테고리(제목·리스트·링크·강조·표)의 "예시"를
    스스로 지어내 돌려주는 사례가 반복 확인됐다(예: 표 셀 "코드 리뷰
    체크리스트"(3단어) 하나에 가짜 "## Code Review Checklist" 체크리스트
    전체를 창작해서 반환). 나열식 지시 대신 "이미 있는 것만 유지하고 새로
    지어내지 마라"는 금지형 문구로 바꾸고, "예시·설명·코멘트를 추가하지
    마라"를 명시적으로 덧붙여 이 환각 패턴을 직접 겨냥한다.
    """
    target_name = {"ko": "Korean", "en": "English"}.get(target_language, target_language)
    return (
        f"Translate the following text to {target_name}. "
        "Keep the Markdown formatting exactly as in the input — do not add, remove, or invent "
        "any Markdown elements (headings, lists, links, tables, etc.) that are not already there. "
        "Do not add examples, explanations, or commentary of your own — translate only what is given. "
        "Output only the translated text, with no preamble.\n\n"
        f"{chunk}"
    )


def resolve_model_name(requested: str, available: list[str]) -> str | None:
    """요청한 모델명이 실제 설치된 모델 중 무엇에 해당하는지 찾는다(순수 함수).

    Ollama 서버가 돌려주는 모델 이름은 버전/빌드에 따라 태그(:)가 다를 수
    있다(예: book.yaml엔 "qwen3.6:35b"라고 적어도 서버엔 "qwen3.6:35b-mlx"만
    있는 경우 — 실사용 중 확인된 사례). 정확히 일치하는 이름이 있으면
    그것을 우선하고, 없으면 태그 앞 베이스 이름이 같은 첫 항목으로 완화
    매칭한다. check_ollama()의 "설치 여부 판정"과 make_ollama_translate_fn()의
    "실제 generate() 호출에 쓸 이름 결정"이 반드시 같은 매칭 규칙을 써야
    한다 — 그러지 않으면 점검은 통과했는데 실제 호출은 서버가 모르는
    이름이라 404로 실패하는 불일치가 생긴다(실사용 중 재현된 버그).
    """
    if requested in available:
        return requested
    base_name = requested.split(":")[0]
    for name in available:
        if name.split(":")[0] == base_name:
            return name
    return None


def make_ollama_translate_fn(cfg: TranslationConfig, target_language: str) -> Callable[[str], str]:
    """실제 Ollama 클라이언트를 감싼 translate_fn을 만든다 — 이 모듈에서
    `import ollama`가 일어나는 유일한 지점이다(지연 임포트라 translation.py
    자체는 ollama 미설치 상태에서도 임포트 가능하다 — 호출 시에만 필요).

    cfg.model을 그대로 generate()에 넘기지 않고 resolve_model_name()으로
    실제 서버에 있는 정확한 이름으로 바꿔 쓴다 — check_ollama()가 "완화
    매칭으로 뭔가는 설치돼 있다"고 판정해 사전 점검을 통과시켜놓고, 정작
    호출은 서버에 없는 이름 그대로 나가 404로 실패하는 불일치를 막는다.

    generate()에 options로 cfg.temperature/cfg.num_ctx를 명시 전달한다 —
    안 넘기면 모델의 Modelfile 기본값(exaone3.5:7.8b는 temperature 1)과
    서버의 기본 컨텍스트 창이 쓰이는데, 표처럼 밀도 높은 청크에서 이게
    청크 뒷부분이 잘리거나 모델이 지시를 놓치는 실패로 이어지는 사례가
    실사용에서 반복 확인됐다(§TranslationConfig 참고).
    """
    from ollama import Client

    client = Client(host=cfg.host, timeout=cfg.timeout)
    # ollama 클라이언트의 ListResponse.Model.model은 스텁상 str | None이지만
    # 서버가 실제로 이름 없는 모델을 보고하는 경우는 없다 — None만 걸러낸다.
    available = [m.model for m in client.list().models if m.model is not None]
    resolved_model = resolve_model_name(cfg.model, available)
    if resolved_model is None:
        available_desc = ", ".join(available) if available else "없음"
        raise RuntimeError(
            f"Ollama 모델을 찾을 수 없습니다: {cfg.model!r} (설치된 모델: {available_desc})"
        )
    if resolved_model != cfg.model:
        print(f"  ℹ️  요청한 모델 '{cfg.model}'을 찾지 못해 '{resolved_model}'을 대신 씁니다")

    def _translate(chunk: str) -> str:
        response = client.generate(
            model=resolved_model,
            prompt=_build_prompt(chunk, target_language),
            options={"temperature": cfg.temperature, "num_ctx": cfg.num_ctx},
        )
        return (response.response or "").strip()

    return _translate
