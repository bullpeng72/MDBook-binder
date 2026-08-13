"""영문 PDF → 단일 평면 마크다운 코퍼스 추출 — `mdbook-binder import pdf`.

챕터 분리(Part_/Chapter_ 명명 규칙에 맞춘 자동 분할)와 이미지 추출은 Phase 1
스코프 밖이다 — PDF 전체를 파일 하나로 뽑아낸다. 그래도 manifest.py의 3순위
자연정렬 폴백이 단일 파일 코퍼스를 그대로 받아들이므로, 이 모듈의 산출물은
manifest.py를 전혀 건드리지 않고도 그 자체로 유효한 mdbook-binder 코퍼스다
(`build html`/`build pdf`/`translate` 모두 바로 이어받을 수 있다).
"""

from __future__ import annotations

import re
from pathlib import Path

from mdbook_binder.manifest import write_minimal_book_yaml

# 문장이 끝나는 문장부호로 안 끝나는 줄은 하드랩(물리적 줄바꿈)일 뿐 단락의
# 끝이 아니라고 본다 — 다음 줄과 공백으로 이어붙인다.
_SENTENCE_END_RE = re.compile(r"[.!?:]$")
# 단어 중간 하이픈 개행("exam-\nple") — 단어 문자 바로 뒤에 하이픈으로 줄이
# 끝나고 다음 줄이 소문자로 시작하면 하이픈 없이 그대로 이어붙인다. 대문자로
# 시작하면(고유명사 등) 의도적 하이픈일 가능성이 있어 건드리지 않는다.
_HYPHEN_BREAK_RE = re.compile(r"\w-$")


def extract_pdf_text(pdf_path: Path) -> str:
    """pypdf(PdfReader)로 페이지별 텍스트를 추출해 이어붙인다."""
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(pages)


def clean_paragraphs(raw_text: str) -> str:
    """PDF 텍스트 추출 특유의 잡음을 정리해 마크다운 단락으로 되돌린다(순수 함수).

    - 문장부호(.!?:)로 안 끝나는 줄은 다음 줄과 공백으로 합친다(하드랩 해제)
    - 단어 중간 하이픈 개행은 하이픈 없이 그대로 합친다
    - 빈 줄(몇 개가 연속이든)은 단락 구분 하나로 정규화된다

    페이지 경계를 넘어 이어지는 문장(러닝 헤더/푸터 포함)은 다루지 않는다 —
    extract_pdf_text()가 페이지 텍스트를 단순 이어붙이기만 해 페이지 경계
    정보 자체가 여기 도달하는 시점엔 이미 사라져 있다(Phase 2 후보).
    """
    paragraphs: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            paragraphs.append(" ".join(current))
            current.clear()

    for raw_line in raw_text.split("\n"):
        line = raw_line.strip()
        if not line:
            flush()
            continue

        if current and _HYPHEN_BREAK_RE.search(current[-1]) and line[:1].islower():
            current[-1] = current[-1][:-1] + line
        else:
            current.append(line)

        if _SENTENCE_END_RE.search(line):
            flush()

    flush()
    return "\n\n".join(paragraphs)


def import_pdf(pdf_path: Path, out_dir: Path, *, title: str | None = None) -> Path:
    """PDF_PATH를 단일 평면 마크다운 + 최소 book.yaml(language: en)로 out_dir에 쓴다.

    챕터 분리·이미지 추출 없음(Phase 2/3). 반환값은 생성된 .md 파일 경로.
    """
    raw = extract_pdf_text(pdf_path)
    body = clean_paragraphs(raw)

    stem = title or pdf_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{stem}.md"
    md_path.write_text(f"# {stem}\n\n{body}\n", encoding="utf-8")

    write_minimal_book_yaml(out_dir / "book.yaml", language="en")
    return md_path
