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
# 슬라이드/프레젠테이션류 PDF는 불릿 항목이 문장부호로 안 끝나는 경우가
# 많다("Interaction: Agent interacts..." 등) — 새 불릿 마커로 시작하는 줄을
# 만나면 이전 줄이 문장부호로 안 끝났어도 그 자리에서 단락을 끊는다. 특정
# PDF의 커스텀 불릿 폰트가 글리프를 알파벳 문자(예: "q")로 잘못 매핑해
# 내보내는 경우까지는 다루지 않는다 — 그런 매핑은 문서마다 달라 일반화할
# 수 없는 그 PDF 고유의 아티팩트다.
_BULLET_MARKER_RE = re.compile(r"^(?:[•\-*◦‣]|\d+[.)]|[a-zA-Z][.)])\s")


def extract_pdf_text(pdf_path: Path) -> list[str]:
    """pypdf(PdfReader)로 페이지별 텍스트를 추출한다(페이지 1개당 문자열 1개).

    기본 추출 모드는 슬라이드/프레젠테이션류 PDF에서 제목·불릿·푸터처럼
    서로 다른 텍스트박스를 줄바꿈 없이 그대로 이어붙여버리는 경우가 흔하다
    (실사용 PDF로 직접 확인된 문제). extraction_mode="layout"은 각 텍스트의
    x/y 좌표를 이용해 실제 시각적 줄 위치를 재구성해 훨씬 안정적이다.
    """
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    return [page.extract_text(extraction_mode="layout") or "" for page in reader.pages]


def strip_repeated_lines(pages: list[str], *, min_fraction: float = 0.5) -> list[str]:
    """여러 페이지에 걸쳐 동일하게 반복되는 줄(러닝 헤더/푸터)을 제거한다(순수 함수).

    페이지의 min_fraction 이상에서 정확히 같은 문자열로 등장하는 줄을 헤더/
    푸터로 간주해 모든 페이지에서 지운다. 페이지가 2장 미만이면(반복 여부를
    판단할 표본이 없음) 그대로 반환한다. 같은 페이지 안에서 같은 줄이 여러 번
    나와도 그 페이지는 1회로만 센다 — 문서 전체 등장 횟수가 아니라 "몇 개
    페이지에 나오는가"를 기준으로 해야 러닝 헤더/푸터를 정확히 잡아낸다.
    """
    if len(pages) < 2:
        return pages

    counts: dict[str, int] = {}
    for page in pages:
        for line in {ln.strip() for ln in page.split("\n") if ln.strip()}:
            counts[line] = counts.get(line, 0) + 1

    threshold = max(2, round(len(pages) * min_fraction))
    repeated = {line for line, count in counts.items() if count >= threshold}
    if not repeated:
        return pages

    return ["\n".join(ln for ln in page.split("\n") if ln.strip() not in repeated) for page in pages]


def clean_paragraphs(raw_text: str) -> str:
    """PDF 텍스트 추출 특유의 잡음을 정리해 마크다운 단락으로 되돌린다(순수 함수).

    - 문장부호(.!?:)로 안 끝나는 줄은 다음 줄과 공백으로 합친다(하드랩 해제)
    - 새 불릿 마커(•/-/*/숫자./영문.)로 시작하는 줄은 그 앞에서 단락을 끊는다
      (이전 줄이 문장부호로 안 끝났어도)
    - 단어 중간 하이픈 개행은 하이픈 없이 그대로 합친다
    - 빈 줄(몇 개가 연속이든)은 단락 구분 하나로 정규화된다

    페이지 경계를 넘어 이어지는 문장은 다루지 않는다 — extract_pdf_text()가
    페이지 텍스트를 페이지 단위로 반환하므로 여기 도달할 때는 이미 하나의
    이어붙인 문자열이라 원래 페이지 경계 정보가 없다(Phase 2 후보).
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

        if _BULLET_MARKER_RE.match(line):
            flush()
            current.append(line)
        elif current and _HYPHEN_BREAK_RE.search(current[-1]) and line[:1].islower():
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
    pages = strip_repeated_lines(extract_pdf_text(pdf_path))
    body = clean_paragraphs("\n\n".join(pages))

    stem = title or pdf_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{stem}.md"
    md_path.write_text(f"# {stem}\n\n{body}\n", encoding="utf-8")

    write_minimal_book_yaml(out_dir / "book.yaml", language="en")
    return md_path
