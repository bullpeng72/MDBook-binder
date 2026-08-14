"""단일 마크다운 파일을 헤딩 경계로 여러 "가상 챕터"로 쪼갠다.

import가 뽑아낸 단일 파일이나, 손으로 아직 안 쪼갠 코퍼스도 book.yaml의
`split.files`에 등록하면 물리적으로 파일을 나누지 않고도 사이드바에 챕터별
항목이 생기게 한다(html_book.py가 소비). 소스 마크다운은 전혀 건드리지
않으므로 언제든 book.yaml 설정만 지우면 원래 동작(파일 하나 = 섹션 하나)으로
되돌아간다.

경계 탐지는 render.py의 md_to_html()이 이미 알고 있는 "헤딩처럼 보여도
헤딩이 아닌" 블록들(펜스 코드, `@@HTML_START@@` raw HTML, blockquote 안
코드펜스)을 동일하게 피해야 한다 — 그렇지 않으면 코드 예제 안 주석
("## 예시" 등)까지 분할 경계로 오인한다.
"""

from __future__ import annotations

import re

_FENCE_LINE_RE = re.compile(r"^```")
_BQ_FENCE_LINE_RE = re.compile(r"^> ```")
_HTML_BLOCK_START = "@@HTML_START@@"
_HTML_BLOCK_END = "@@HTML_END@@"
_RAW_H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def extract_raw_h1(text: str) -> str | None:
    """마크다운 원문(렌더링 전)에서 첫 H1 텍스트를 뽑는다(순수 함수).

    html_book.py/pdf_book.py 둘 다 split 대상 파일이 여러 가상 챕터로
    쪼개질 때, 쪼개지기 전 원본 파일의 H1을 그 조각들의 공통 Part 라벨로
    쓰기 위해 이 함수를 공유한다.
    """
    m = _RAW_H1_RE.search(text)
    return m.group(1).strip() if m else None


def find_heading_boundaries(text: str, level: int) -> list[int]:
    """지정 레벨(예: 2 → "## ")의 헤딩 줄 인덱스를 순서대로 반환한다(순수 함수).

    펜스 코드 블록(``` ~ ```, blockquote 안의 `> ``` ` ~ `> ``` ` 포함)과
    `@@HTML_START@@` ~ `@@HTML_END@@` raw HTML 블록 안의 "## "는 후보에서
    제외한다 — 그 안 내용은 렌더링 시 md_to_html()도 마크다운으로 해석하지
    않는 보호 영역이라, 분할 경계로 오인하면 코드/HTML 예제 한가운데가
    잘려나간다.
    """
    heading_re = re.compile(rf"^#{{{level}}}(?!#)\s+\S")
    lines = text.split("\n")
    in_fence = False
    in_bq_fence = False
    in_html_block = False
    boundaries: list[int] = []

    for i, line in enumerate(lines):
        if in_html_block:
            if line.strip() == _HTML_BLOCK_END:
                in_html_block = False
            continue
        if in_bq_fence:
            if _BQ_FENCE_LINE_RE.match(line):
                in_bq_fence = False
            continue
        if in_fence:
            if _FENCE_LINE_RE.match(line):
                in_fence = False
            continue

        if line.strip() == _HTML_BLOCK_START:
            in_html_block = True
            continue
        if _BQ_FENCE_LINE_RE.match(line):
            in_bq_fence = True
            continue
        if _FENCE_LINE_RE.match(line):
            in_fence = True
            continue

        if heading_re.match(line):
            boundaries.append(i)

    return boundaries


def split_chapter_markdown(text: str, level: int = 2) -> list[str]:
    """text를 지정 레벨 헤딩 경계로 조각낸다. 경계가 없으면 [text] 그대로 반환.

    첫 조각(맨 앞 헤딩 앞부분, 보통 원본 H1 + 도입부)은 H1 다음에 실제
    본문이 있을 때만 별도 조각으로 남긴다 — H1 바로 다음에 첫 헤딩이 오면
    (도입부가 사실상 빈 경우) 의미 없는 첫 섹션을 만들지 않는다.

    경계 헤딩 줄은 `#`을 하나 떼어 한 레벨 승격한다("## 제목" → "# 제목")
    — 마크다운 저작 규칙상 각 조각은 H1 하나로 시작해야 하므로, 손으로
    쪼갤 때와 동일한 규칙을 기계적으로 적용한다.
    """
    boundaries = find_heading_boundaries(text, level)
    if not boundaries:
        return [text]

    lines = text.split("\n")
    segments: list[str] = []

    front_lines = lines[: boundaries[0]]
    h1_idx = next((i for i, ln in enumerate(front_lines) if ln.startswith("# ")), None)
    body_after_h1 = front_lines[h1_idx + 1 :] if h1_idx is not None else front_lines
    if any(ln.strip() for ln in body_after_h1):
        segments.append("\n".join(front_lines))

    bounds = [*boundaries, len(lines)]
    for k in range(len(boundaries)):
        seg_lines = list(lines[bounds[k] : bounds[k + 1]])
        if seg_lines and seg_lines[0].startswith("#"):
            seg_lines[0] = seg_lines[0][1:]
        segments.append("\n".join(seg_lines))

    return segments
