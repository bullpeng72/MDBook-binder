"""영문 PDF → 단일 평면 마크다운 코퍼스 추출 — `mdbook-binder import pdf`.

pdfplumber로 단어별 (x, y) 좌표를 직접 얻어 두 가지를 처리한다:
- 컬럼 인식: 페이지를 가로지르는 세로 여백(거터)을 찾아 다단(2단 이상)
  레이아웃을 감지하고, 각 컬럼을 위→아래로 다 읽은 뒤 다음 컬럼으로
  넘어가는 올바른 순서로 재조립한다. pypdf의 layout 모드처럼 y좌표
  대역만으로 줄을 합치면 같은 높이의 서로 다른 컬럼 텍스트가 한 줄에
  섞여버린다(실사용 PDF로 재현된 문제).
- 표 인식: 연속된 여러 줄이 동일한 x좌표 격자에 정렬돼 있으면 표로 보고
  마크다운 파이프 표로 재구성한다. 산문은 줄마다 단어 시작 위치가 들쭉날쭉해
  이 조건을 만족하지 않으므로 오탐 위험이 낮다.
- 문서별 불릿 문자 추론: 특정 PDF의 커스텀 불릿 폰트가 글리프를 구두점
  없는 알파벳 한 글자(예: "q")로 잘못 매핑해 내보내는 경우, 그 글자로
  시작하는 줄이 문서 전체에서 비정상적으로 반복되면 이 문서만의 불릿
  마커로 추론한다(실사용 PDF로 재현·확인된 문제).
- 문서별 단어 간격 보정: pdfplumber의 기본 단어 구분 임계값(3pt)은 문서마다
  실제 자간이 달라 고정값 하나로는 안 맞는 경우가 있다 — 어떤 PDF는 단어
  사이 간격이 3pt보다 좁아(예: 2.8pt) 문장 전체가 한 단어로 붙어버린다
  (실사용 PDF로 재현·확인). 문서 앞부분 몇 페이지에서 문자 간격을 표본
  조사해 "같은 단어 안 간격"과 "단어 사이 간격"의 경계를 문서별로 추론한다.
- 쪽번호 제거: 페이지 상/하단 여백에 홀로 있는 순수 숫자 단어(쪽번호로
  추정)를 위치 기반으로 제거한다. 매 페이지 값이 바뀌어
  strip_repeated_lines()(동일 문자열 반복 감지)로는 못 잡는 것을 보완한다
  (실사용 PDF로 확인 — 본문 문장 사이사이에 쪽번호가 단락으로 끼어듦).

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
# PDF의 커스텀 불릿 폰트가 글리프를 구두점 없는 알파벳 한 글자(예: "q")로
# 잘못 매핑해 내보내는 경우는 이 정규식만으로 못 잡는다 — 그런 매핑은
# 문서마다 다른 글자를 쓰므로 하드코딩할 수 없고, _detect_document_bullet_chars()가
# 문서별로 통계적으로 추론해 보완한다.
_BULLET_MARKER_RE = re.compile(r"^(?:[•\-*◦‣]|\d+[.)]|[a-zA-Z][.)])\s")
# _detect_document_bullet_chars() 후보 패턴 — 구두점 없이 알파벳 한 글자 +
# 공백으로 시작하는 줄. 유니코드 사용자영역(U+E000~U+F8FF)도 후보에 넣는다 —
# Word 등에서 만든 오래된 PDF는 Wingdings류 심볼 폰트의 불릿 글리프를
# ToUnicode CMap으로 이 영역에 매핑하는 경우가 흔하다(실사용 PDF로 확인 —
# 한 문서에서 3천 번 넘게 등장하는 불릿이 전혀 인식되지 않던 사례). 이
# 영역의 문자는 애초에 실제 언어 문자가 아니므로 "a"/"i"처럼 정상 문장과
# 오탐할 걱정 없이 그대로 후보로 써도 안전하다.
_CANDIDATE_BULLET_RE = re.compile(r"^([A-Za-z-])\s+\S")
# "a"/"i"는 실제 영어 단어("a book", "I think")라 문장 첫머리에 흔히 나온다 —
# 아무리 자주 반복돼도 불릿 후보에서 제외해야 오탐(멀쩡한 문장을 불릿으로
# 오인)을 피한다.
_COMMON_SINGLE_LETTER_WORDS = {"a", "i"}
# 특정 PDF의 폰트 인코딩이 깨져 있으면(ToUnicode CMap 불량 등 — 2바이트
# 문자를 1바이트씩 잘못 읽는 경우가 대표적) 추출된 단어에 NUL(\x00) 같은
# 제어 문자가 글자 사이사이에 섞여 나온다(실사용 PDF로 확인됨). 원래 글자가
# 무엇이었는지 PDF 자체에 정보가 없어 복원이 불가능하므로(OCR 없이는 불가),
# filter_garbled_words()가 이런 단어를 조용히 걸러낸다.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
# _process_page()가 만든 마크다운 표의 행 — 그대로 보존해야 하므로 하드랩
# 해제/불릿 분리 로직을 타지 않게 clean_paragraphs()에서 별도 취급한다.
_TABLE_ROW_RE = re.compile(r"^\|.*\|$")
# pdfplumber.extract_words()의 기본 단어 구분 임계값 — calibrate_word_x_tolerance()가
# 문서별로 이보다 낮춰야 할 근거를 못 찾으면 이 값을 그대로 쓴다. 낮추는
# 방향으로만 보정하는 이유는 반대로 올리면(단어 사이 간격을 넓게 잡으면)
# 정상적으로 붙어 있던 단어까지 잘못 합쳐질 위험이 더 크기 때문이다.
_DEFAULT_X_TOLERANCE = 3.0
# 쪽번호로 볼 페이지 상/하단 여백 비율 — 표준적인 책 레이아웃의 위/아래
# 여백 비중과 비슷한 값이다.
_PAGE_NUMBER_MARGIN_FRAC = 0.1


def _detect_document_bullet_chars(raw_text: str, *, min_occurrences: int = 5) -> set[str]:
    """줄 맨 앞에 구두점 없이 비정상적으로 자주 등장하는 알파벳 한 글자를
    이 문서만의 불릿 마커로 추론한다(순수 함수).

    특정 PDF의 커스텀 불릿 폰트가 글리프를 알파벳 한 글자(예: "q")로 잘못
    매핑해 내보내는 경우, "q "처럼 구두점 없이 공백만 붙는 형태는
    _BULLET_MARKER_RE(".", ")" 뒤따르는 마커만 인식)로 못 잡는다. 어떤
    글자가 쓰일지는 문서마다 달라 하드코딩할 수 없으므로, 문서 전체에서
    "그 글자로 시작하는 줄"이 min_occurrences회 이상 반복되면 우연이
    아니라 불릿 마커로 보고 채택한다. 실제 영어 단어인 "a"/"i"는 아무리
    반복돼도 제외한다(정상 문장을 불릿으로 오인하는 것을 막기 위함).
    """
    counts: dict[str, int] = {}
    for raw_line in raw_text.split("\n"):
        line = raw_line.strip()
        m = _CANDIDATE_BULLET_RE.match(line)
        if not m:
            continue
        ch = m.group(1).lower()
        if ch in _COMMON_SINGLE_LETTER_WORDS:
            continue
        counts[ch] = counts.get(ch, 0) + 1
    return {ch for ch, n in counts.items() if n >= min_occurrences}


def _group_into_rows(words: list[dict], y_tolerance: float = 3.0) -> list[list[dict]]:
    """단어를 y좌표(top) 기준으로 같은 줄끼리 묶는다(줄 안에서는 x좌표 순 정렬).

    폰트 렌더링 오차로 같은 시각적 줄의 단어들이 완전히 같은 top값을 갖지
    않을 수 있어 y_tolerance 안에 있으면 같은 줄로 취급한다.
    """
    if not words:
        return []
    ordered = sorted(words, key=lambda w: w["top"])
    rows: list[list[dict]] = [[ordered[0]]]
    row_top = ordered[0]["top"]
    for w in ordered[1:]:
        if abs(w["top"] - row_top) <= y_tolerance:
            rows[-1].append(w)
        else:
            rows.append([w])
            row_top = w["top"]
    return [sorted(row, key=lambda w: w["x0"]) for row in rows]


def detect_columns(
    words: list[dict], *, min_gutter_frac: float = 0.02, margin_frac: float = 0.1
) -> list[tuple[float, float]]:
    """페이지 단어들의 x분포에서 컬럼 사이 거터(공백대)를 찾아 컬럼별 x범위를 반환한다(순수 함수).

    페이지 폭을 잘게 나눈 x-구간마다 텍스트가 걸치는지 표시하고, 텍스트가
    전혀 없는 구간이 min_gutter_frac 이상 폭으로 이어지면 컬럼 경계로 삼는다.
    가장자리 여백(좌우 margin_frac 안쪽)에 생기는 공백은 진짜 컬럼 거터가
    아니라 페이지 여백일 뿐이므로 거터 후보에서 제외한다. 거터를 하나도 못
    찾으면 전체 단어 범위를 컬럼 하나로 반환한다(단일 컬럼 문서의 정상 동작 —
    기존 pypdf 기반 추출과 동등하게 처리됨).
    """
    if not words:
        return []

    min_x = min(w["x0"] for w in words)
    max_x = max(w["x1"] for w in words)
    span = max_x - min_x
    if span <= 0:
        return [(min_x, max_x)]

    bin_width = 2.0
    n_bins = max(1, int(span / bin_width) + 1)
    covered = [False] * n_bins
    for w in words:
        start_bin = max(0, int((w["x0"] - min_x) / bin_width))
        end_bin = min(n_bins - 1, int((w["x1"] - min_x) / bin_width))
        for b in range(start_bin, end_bin + 1):
            covered[b] = True

    # span 비례 폭(min_gutter_frac)만 쓰면, 페이지 폭 대부분을 못 채우는 좁은
    # 컬럼(예: 짧은 문구 두 개짜리 슬라이드)에서 상대 임계값이 너무 작아져
    # 평범한 단어 사이 공백(보통 6pt 미만)까지 거터로 오인할 수 있다 — 절대
    # 최소 폭(15pt, 일반 단어 간격보다 뚜렷이 넓은 값)을 함께 강제한다.
    absolute_min_pt = 15.0
    gutter_min_bins = max(2, int(span * min_gutter_frac / bin_width), int(absolute_min_pt / bin_width))

    gutters: list[float] = []
    run_start: int | None = None
    for i, is_covered in enumerate([*covered, True]):  # 끝에 sentinel을 붙여 마지막 run도 닫는다
        if not is_covered:
            if run_start is None:
                run_start = i
        elif run_start is not None:
            gx0 = min_x + run_start * bin_width
            gx1 = min_x + i * bin_width
            frac = ((gx0 + gx1) / 2 - min_x) / span
            if (i - run_start) >= gutter_min_bins and margin_frac < frac < (1 - margin_frac):
                gutters.append((gx0 + gx1) / 2)
            run_start = None

    if not gutters:
        return [(min_x, max_x)]

    bounds = [min_x, *gutters, max_x]
    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]


def _assign_to_column(word: dict, columns: list[tuple[float, float]]) -> int:
    center = (word["x0"] + word["x1"]) / 2
    for i, (cs, ce) in enumerate(columns):
        if cs <= center <= ce:
            return i
    return min(range(len(columns)), key=lambda i: min(abs(center - columns[i][0]), abs(center - columns[i][1])))


def _cluster_x_positions(xs: list[float], tolerance: float = 10.0) -> list[float]:
    """비슷한 x좌표들을 하나의 대표값(평균)으로 묶는다(표의 열 위치 후보)."""
    if not xs:
        return []
    ordered = sorted(xs)
    clusters: list[list[float]] = [[ordered[0]]]
    for x in ordered[1:]:
        if x - clusters[-1][-1] <= tolerance:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    return [sum(c) / len(c) for c in clusters]


def _row_groups(row: list[dict], gap_threshold: float = 20.0) -> list[dict]:
    """한 줄 안에서 단어들을 큰 간격(gap_threshold 이상) 기준으로 묶는다.

    표는 셀 사이에 뚜렷한 여백이 있어 한 줄에 그룹이 여러 개(각 그룹이 표의
    한 칸) 생기고, 산문은 단어 사이 간격이 일정하고 좁아 문장 전체가 그룹
    하나로 뭉친다 — 표/산문을 구분하는 핵심 신호다(단어 x0를 문서 전체에서
    무작정 클러스터링하면, 왼쪽 정렬된 산문도 매 줄이 같은 여백에서
    시작한다는 이유만으로 표처럼 오인되기 쉽다 — 실제로 검증된 오탐).
    """
    if not row:
        return []
    groups: list[dict] = [{"x0": row[0]["x0"], "x1": row[0]["x1"]}]
    for w in row[1:]:
        if w["x0"] - groups[-1]["x1"] > gap_threshold:
            groups.append({"x0": w["x0"], "x1": w["x1"]})
        else:
            groups[-1]["x1"] = w["x1"]
    return groups


def detect_table(
    rows: list[list[dict]], *, min_rows: int = 3, min_cols: int = 3, gap_threshold: float = 20.0
) -> tuple[int, int] | None:
    """연속된 여러 줄이 동일한 칸(그룹) 위치에 정렬돼 있으면 표 영역을 찾는다(순수 함수).

    각 줄을 _row_groups()로 나눈 뒤, 자체적으로 min_cols개 이상 칸을 가진
    "표 행 후보"들의 칸 시작 위치만 모아 전역 열 위치를 구한다(산문 줄은
    애초에 후보에서 제외되므로 전역 열 위치 계산 자체를 오염시키지 않는다).
    그 열 위치와 min_cols개 이상 정렬되는 줄이 min_rows줄 이상 연속되는
    가장 긴 구간을 (시작, 끝) 인덱스로 반환한다. 못 찾으면 None.

    min_cols 기본값을 3으로 둔 이유: 진짜 2단 텍스트 레이아웃(본문이 좌/우
    두 컬럼으로 흐르는 문서)도 매 줄이 정확히 2개 칸(좌/우 컬럼)으로 정렬돼
    보여, min_cols=2였다면 2단 레이아웃과 열이 2개뿐인 표를 기하학적으로
    구별할 수 없었다(실제로 검증된 오탐 — 2단 레이아웃 테스트 픽스처가 표로
    오인됨). 대부분의 실제 표는 열이 3개 이상이라 이 값으로 2단 텍스트
    오탐은 피하면서 흔한 표는 그대로 잡아낸다 — 다만 열이 정확히 2개인
    표는 이 휴리스틱으로 감지하지 못하는 대가가 있다(알려진 한계).
    """
    if len(rows) < min_rows:
        return None

    row_groups_list = [_row_groups(row, gap_threshold) for row in rows]
    candidate_starts = [g["x0"] for groups in row_groups_list if len(groups) >= min_cols for g in groups]
    if not candidate_starts:
        return None

    global_columns = _cluster_x_positions(candidate_starts, tolerance=gap_threshold / 2)
    if len(global_columns) < min_cols:
        return None

    def aligned_count(groups: list[dict]) -> int:
        aligned: set[int] = set()
        for g in groups:
            for i, gc in enumerate(global_columns):
                if abs(g["x0"] - gc) <= gap_threshold / 2:
                    aligned.add(i)
                    break
        return len(aligned)

    is_table_row = [aligned_count(groups) >= min_cols for groups in row_groups_list]

    best: tuple[int, int] | None = None
    start: int | None = None
    for i, flag in enumerate([*is_table_row, False]):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            if i - start >= min_rows and (best is None or (i - start) > (best[1] - best[0])):
                best = (start, i)
            start = None
    return best


def _rows_to_table_markdown(rows: list[list[dict]], gap_threshold: float = 20.0) -> str:
    """detect_table()이 찾은 표 영역(rows)을 마크다운 파이프 표로 렌더링한다."""
    row_groups_list = [_row_groups(row, gap_threshold) for row in rows]
    columns = _cluster_x_positions([g["x0"] for groups in row_groups_list for g in groups], tolerance=gap_threshold / 2)
    n_cols = len(columns)

    def cells_for(row: list[dict]) -> list[str]:
        cells = [""] * n_cols
        for w in row:
            idx = min(range(n_cols), key=lambda i: abs(w["x0"] - columns[i]))
            cells[idx] = f"{cells[idx]} {w['text']}".strip() if cells[idx] else w["text"]
        return cells

    table = [cells_for(row) for row in rows]
    col_widths = [max(1, max(len(r[c]) for r in table)) for c in range(n_cols)]

    def fmt(row: list[str]) -> str:
        return "| " + " | ".join(cell.ljust(w) for cell, w in zip(row, col_widths)) + " |"

    lines = [fmt(table[0]), "| " + " | ".join("-" * w for w in col_widths) + " |"]
    lines.extend(fmt(r) for r in table[1:])
    return "\n".join(lines)


def _rows_to_text(rows: list[list[dict]]) -> str:
    return "\n".join(" ".join(w["text"] for w in row) for row in rows)


def _process_columns(words: list[dict]) -> str:
    """표가 아닌 것으로 이미 확인된 단어들을 컬럼 인식해(읽기 순서 보정) 텍스트로 만든다."""
    if not words:
        return ""
    columns = detect_columns(words)
    buckets: list[list[dict]] = [[] for _ in columns]
    for w in words:
        buckets[_assign_to_column(w, columns)].append(w)
    parts = [_rows_to_text(_group_into_rows(col_words)) for col_words in buckets]
    return "\n\n".join(p for p in parts if p)


def _process_page(words: list[dict]) -> str:
    """한 페이지의 단어들을 표 인식 + 컬럼 인식(읽기 순서 보정)을 거쳐 텍스트로 만든다.

    표 인식을 컬럼 분리보다 먼저, 페이지 전체 단어를 대상으로 수행한다 —
    순서가 반대면(컬럼부터 나누면) 열이 3개 이상인 진짜 표가 컬럼 3개짜리
    텍스트 레이아웃으로 쪼개져 표로 인식될 기회 자체가 사라진다(실제로
    검증된 회귀). 표 영역을 찾으면 그 앞/뒤 나머지만 컬럼 인식으로 넘긴다 —
    표 영역이 우연히 2단 레이아웃처럼 보이는 것까지 막을 필요는 없다
    (detect_table()의 min_cols=3 기본값이 2단 텍스트와의 오탐을 이미 막는다).
    """
    if not words:
        return ""

    rows = _group_into_rows(words)
    table_region = detect_table(rows)
    if not table_region:
        return _process_columns(words)

    start, end = table_region
    before_words = [w for row in rows[:start] for w in row]
    after_words = [w for row in rows[end:] for w in row]
    before = _process_columns(before_words)
    table_md = _rows_to_table_markdown(rows[start:end])
    after = _process_columns(after_words)
    return "\n\n".join(p for p in (before, table_md, after) if p)


def filter_garbled_words(words: list[dict]) -> list[dict]:
    """폰트 인코딩이 깨져 추출된 단어(제어 문자가 섞인 것)를 걸러낸다(순수 함수).

    이런 단어는 원래 글자를 복원할 방법이 없으므로(_CONTROL_CHAR_RE 주석
    참고), 남겨두면 본문 문장 앞뒤에 알아볼 수 없는 잡음으로만 남는다 —
    조용히 제거하는 편이 낫다.
    """
    return [w for w in words if not _CONTROL_CHAR_RE.search(w["text"])]


def filter_page_number_words(words: list[dict], page_height: float, *, margin_frac: float = _PAGE_NUMBER_MARGIN_FRAC) -> list[dict]:
    """페이지 상/하단 여백에 홀로 있는 순수 숫자 단어(쪽번호로 추정)를 제거한다(순수 함수).

    쪽번호는 페이지마다 값이 바뀌어(1, 2, 3...) strip_repeated_lines()(동일
    문자열 반복 감지)로는 못 잡는다 — 대신 위치(페이지 상/하단 여백 안)와
    형태(그 줄에 다른 단어 없이 숫자만 홀로)로 판단한다. 본문 중간에 있는
    숫자(여백 밖이거나, 다른 단어와 같은 줄)는 건드리지 않는다.
    """
    if not words:
        return words

    top_margin = page_height * margin_frac
    bottom_margin = page_height * (1 - margin_frac)

    result: list[dict] = []
    for row in _group_into_rows(words):
        if len(row) == 1:
            w = row[0]
            text = w["text"].strip()
            if text.isdigit() and len(text) <= 4 and (w["top"] < top_margin or w["bottom"] > bottom_margin):
                continue
        result.extend(row)
    return result


def _sample_char_gaps(pdf, *, max_pages: int = 10) -> list[float]:
    """문서 앞부분 max_pages 페이지에서 같은 줄 안 인접 문자 간 x간격을 표본 수집한다.

    문서 전체를 다 훑지 않는다 — 자간 스타일은 보통 문서 전체에서
    일관되므로 앞부분 표본만으로도 충분히 대표성을 갖고, 수백 페이지짜리
    문서에서 매번 전체를 훑는 비용을 피한다. 20pt 넘는 간격(다른 줄로
    잘못 묶인 경우 등 이상치)은 표본에서 제외한다.
    """
    gaps: list[float] = []
    for page in pdf.pages[:max_pages]:
        chars = sorted(page.chars, key=lambda c: (round(c["top"], 1), c["x0"]))
        for i in range(1, len(chars)):
            prev, cur = chars[i - 1], chars[i]
            if abs(cur["top"] - prev["top"]) < 1.0:
                gap = cur["x0"] - prev["x1"]
                if 0 <= gap < 20:
                    gaps.append(gap)
    return gaps


def calibrate_word_x_tolerance(gaps: list[float], *, default: float = _DEFAULT_X_TOLERANCE) -> float:
    """문자 간격 표본에서 "같은 단어 안"과 "단어 사이" 간격의 경계를 추론한다(순수 함수).

    pdfplumber의 기본 단어 구분 임계값(3pt)은 문서마다 실제 자간이 달라
    맞지 않는 경우가 있다 — 어떤 문서는 단어 사이 간격이 3pt보다 좁아
    (예: 2.8pt) 문장 전체가 한 단어로 붙어버린다(실사용 PDF로 확인).

    간격 표본을 오름차순 정렬한 뒤, 0에 가까운 값(같은 단어 안 간격)들이
    모인 군집을 지나 처음으로 뚜렷하게 벌어지는 지점(jump >= 0.5)을 그
    문서의 "단어 사이 간격" 시작점으로 본다 — 대부분의 폰트는 글자 안
    커닝이 0에 가깝고 단어 사이 공백만 그보다 확연히 크므로, 첫 번째
    큰 도약이 곧 진짜 경계다(전체 표본에서 "가장 큰" 도약을 찾으면 드문
    이상치 구간에서 더 큰 도약에 낚일 수 있어, 반드시 "처음" 나오는
    도약을 써야 한다).

    보정값은 항상 default 이하로만 낮춘다 — 반대로 올리면(간격 허용
    범위를 넓히면) 정상적으로 떨어져 있던 단어까지 잘못 합칠 위험이 더
    크다. 표본이 부족하거나(20건 미만) 뚜렷한 경계를 못 찾으면 default를
    그대로 쓴다.
    """
    if len(gaps) < 20:
        return default

    ordered = sorted(gaps)
    for i in range(1, len(ordered)):
        jump = ordered[i] - ordered[i - 1]
        if jump >= 0.5:
            threshold = (ordered[i] + ordered[i - 1]) / 2
            return max(1.0, min(threshold, default))
    return default


def extract_pdf_text(pdf_path: Path) -> list[str]:
    """pdfplumber로 페이지별 단어를 추출해 컬럼/표를 인식한 텍스트로 변환한다(페이지 1개당 문자열 1개).

    pypdf의 layout 모드는 슬라이드/프레젠테이션류 PDF의 줄바꿈은 잘 복원하지만
    2단 이상 레이아웃에서는 같은 높이의 서로 다른 컬럼 텍스트를 한 줄에
    섞어버린다(y좌표 대역만으로 줄을 재구성해 컬럼 개념이 없음 — 실사용
    PDF로 재현·확인됨). pdfplumber로 단어 하나하나의 (x, y) 좌표를 직접 받아
    detect_columns()/detect_table()로 재구성하면 이 문제를 피할 수 있다.
    """
    import pdfplumber

    with pdfplumber.open(str(pdf_path)) as pdf:
        x_tolerance = calibrate_word_x_tolerance(_sample_char_gaps(pdf))
        pages = []
        for page in pdf.pages:
            words = page.extract_words(x_tolerance=x_tolerance)
            words = filter_garbled_words(words)
            words = filter_page_number_words(words, page.height)
            pages.append(_process_page(words))
        return pages


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

    - 마크다운 표 행("| ... |")은 그대로 보존한다(연속된 행끼리는 빈 줄 없이
      묶어 표 구조가 깨지지 않게 한다) — 하드랩 해제·불릿 분리 대상이 아니다.
    - 문장부호(.!?:)로 안 끝나는 줄은 다음 줄과 공백으로 합친다(하드랩 해제)
    - 새 불릿 마커(•/-/*/숫자./영문. 및 _detect_document_bullet_chars()가
      이 문서에서 추론한 구두점 없는 한 글자 불릿)로 시작하는 줄은 그
      앞에서 단락을 끊는다(이전 줄이 문장부호로 안 끝났어도)
    - 단어 중간 하이픈 개행은 하이픈 없이 그대로 합친다
    - 빈 줄(몇 개가 연속이든)은 단락 구분 하나로 정규화된다

    페이지 경계를 넘어 이어지는 문장은 다루지 않는다 — extract_pdf_text()가
    페이지 텍스트를 페이지 단위로 반환하므로 여기 도달할 때는 이미 하나의
    이어붙인 문자열이라 원래 페이지 경계 정보가 없다(Phase 2 후보).
    """
    doc_bullet_chars = _detect_document_bullet_chars(raw_text)
    paragraphs: list[str] = []
    current: list[str] = []
    table_rows: list[str] = []

    def flush_prose() -> None:
        if current:
            paragraphs.append(" ".join(current))
            current.clear()

    def flush_table() -> None:
        if table_rows:
            paragraphs.append("\n".join(table_rows))
            table_rows.clear()

    for raw_line in raw_text.split("\n"):
        line = raw_line.strip()
        if not line:
            flush_prose()
            flush_table()
            continue

        if _TABLE_ROW_RE.match(line):
            flush_prose()
            table_rows.append(line)
            continue
        flush_table()

        is_doc_bullet = len(line) > 1 and line[0].lower() in doc_bullet_chars and line[1] == " "
        if _BULLET_MARKER_RE.match(line) or is_doc_bullet:
            flush_prose()
            current.append(line)
        elif current and _HYPHEN_BREAK_RE.search(current[-1]) and line[:1].islower():
            current[-1] = current[-1][:-1] + line
        else:
            current.append(line)

        if _SENTENCE_END_RE.search(line):
            flush_prose()

    flush_prose()
    flush_table()
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
