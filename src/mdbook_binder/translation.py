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
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

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


def translate_chapter(
    text: str,
    chunk_chars: int,
    translate_fn: Callable[[str], str],
    *,
    target_language: str | None = None,
    max_retries: int = _MAX_RETRIES,
    on_chunk_start: Callable[[int, int], None] | None = None,
    on_incomplete: Callable[[int, int], None] | None = None,
) -> str:
    """챕터 하나를 번역한다 — 코드/mermaid/raw-HTML을 보호한 뒤 청크 단위로
    translate_fn을 호출하고 재조립·복원한다.

    on_chunk_start(j, n)은 청크(1-indexed) 번역을 시작하기 직전 호출된다 —
    로컬 LLM은 청크 하나에도 수십 초가 걸릴 수 있어, 호출부(translate_corpus)가
    이 훅으로 진행 상황을 찍지 않으면 멈춘 것처럼 보이기 쉽다.

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
    """
    masked, blocks = protect_blocks(text)
    chunks = chunk_paragraphs(masked, chunk_chars)

    translated: list[str] = []
    total = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        if on_chunk_start:
            on_chunk_start(i, total)
        if not chunk.strip():
            translated.append(chunk)
            continue

        result = translate_fn(chunk)
        if target_language == "en" and has_residual_korean(result):
            for _ in range(max_retries):
                result = translate_fn(chunk)
                if not has_residual_korean(result):
                    break
            else:
                if on_incomplete:
                    on_incomplete(i, total)
        translated.append(result)

    return restore_blocks(reassemble_chunks(translated), blocks)


def translate_corpus(
    root: Path,
    out_dir: Path,
    config: BookConfig | None,
    target_language: str,
    translate_fn: Callable[[str], str],
    *,
    chunk_chars: int | None = None,
) -> Path:
    """manifest.resolve()로 걷은 챕터를 순서대로 번역해 out_dir에 동일한
    디렉토리 구조로 미러링하고, book.yaml의 language를 target_language로
    재작성한다(book.yaml이 없던 코퍼스면 최소 book.yaml을 새로 만든다).
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
        print(f"\U0001f4c4 [{i}/{total}] {rel}")

        text = chap.path.read_text(encoding="utf-8")
        incomplete_chunks: list[int] = []

        def _on_chunk_start(j: int, n: int) -> None:
            print(f"  청크 {j}/{n} 번역 중...")

        def _on_incomplete(j: int, n: int, _sink: list[int] = incomplete_chunks) -> None:
            _sink.append(j)

        out_text = translate_chapter(
            text,
            effective_chunk_chars,
            translate_fn,
            target_language=target_language,
            on_chunk_start=_on_chunk_start,
            on_incomplete=_on_incomplete,
        )

        if incomplete_chunks:
            print(f"  ⚠️  재시도 후에도 한글이 남은 청크: {incomplete_chunks} — 수동 확인 필요")
            incomplete_chapters.append(f"{rel} (청크 {incomplete_chunks})")

        dest = out_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(out_text, encoding="utf-8")

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
    """마크다운 문법을 그대로 유지하라는 지시를 포함한 번역 프롬프트."""
    target_name = {"ko": "Korean", "en": "English"}.get(target_language, target_language)
    return (
        f"Translate the following text to {target_name}. "
        "Preserve all Markdown syntax (headings, lists, links, emphasis, tables) exactly as-is — "
        "translate only the natural-language content. "
        "Output only the translated Markdown, with no preamble or explanation.\n\n"
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
            model=resolved_model, prompt=_build_prompt(chunk, target_language)
        )
        return (response.response or "").strip()

    return _translate
