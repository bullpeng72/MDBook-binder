"""로컬 Ollama로 마크다운 코퍼스를 번역한다 — `mdbook-binder translate`.

manifest.resolve()가 이미 아는 챕터 순서/exclude 규칙을 그대로 재사용해
걷고, 코드/mermaid/raw-HTML 블록은 render.py가 HTML 렌더링 전에 쓰는 것과
동일한 정규식 경계로 보호했다가 그대로 복원한다 — 번역 대상에서 제외해야
할 블록의 정의를 두 곳에서 따로 유지하지 않기 위해서다.

실제 Ollama 호출(`make_ollama_translate_fn`)은 이 모듈에서 `import ollama`가
일어나는 유일한 지점이다 — 나머지 함수는 전부 `translate_fn: Callable[[str], str]`을
주입받는 순수/의사-순수 함수라, Ollama 설치 없이도 청킹·블록 보호·진행
출력·book.yaml 재작성 로직을 단위 테스트할 수 있다.
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


def translate_chapter(
    text: str,
    chunk_chars: int,
    translate_fn: Callable[[str], str],
    *,
    on_chunk_start: Callable[[int, int], None] | None = None,
) -> str:
    """챕터 하나를 번역한다 — 코드/mermaid/raw-HTML을 보호한 뒤 청크 단위로
    translate_fn을 호출하고 재조립·복원한다.

    on_chunk_start(j, n)은 청크(1-indexed) 번역을 시작하기 직전 호출된다 —
    로컬 LLM은 청크 하나에도 수십 초가 걸릴 수 있어, 호출부(translate_corpus)가
    이 훅으로 진행 상황을 찍지 않으면 멈춘 것처럼 보이기 쉽다.
    """
    masked, blocks = protect_blocks(text)
    chunks = chunk_paragraphs(masked, chunk_chars)

    translated: list[str] = []
    total = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        if on_chunk_start:
            on_chunk_start(i, total)
        translated.append(translate_fn(chunk) if chunk.strip() else chunk)

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
        config.translation.chunk_chars if (config and config.translation) else TranslationConfig().chunk_chars
    )

    chapters = resolve(root, config)
    total = len(chapters)
    for i, chap in enumerate(chapters, start=1):
        rel = chap.path.relative_to(root)
        print(f"\U0001f4c4 [{i}/{total}] {rel}")

        text = chap.path.read_text(encoding="utf-8")
        out_text = translate_chapter(
            text, effective_chunk_chars, translate_fn,
            on_chunk_start=lambda j, n: print(f"  청크 {j}/{n} 번역 중..."),
        )

        dest = out_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(out_text, encoding="utf-8")

    _write_translated_book_yaml(root, out_dir, target_language)
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
    dest_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


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
    available = [m.model for m in client.list().models]
    resolved_model = resolve_model_name(cfg.model, available)
    if resolved_model is None:
        available_desc = ", ".join(available) if available else "없음"
        raise RuntimeError(f"Ollama 모델을 찾을 수 없습니다: {cfg.model!r} (설치된 모델: {available_desc})")
    if resolved_model != cfg.model:
        print(f"  ℹ️  요청한 모델 '{cfg.model}'을 찾지 못해 '{resolved_model}'을 대신 씁니다")

    def _translate(chunk: str) -> str:
        response = client.generate(model=resolved_model, prompt=_build_prompt(chunk, target_language))
        return response.response.strip()

    return _translate
