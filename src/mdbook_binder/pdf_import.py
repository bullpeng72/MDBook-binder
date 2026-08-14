"""영문 PDF → 단일 평면 마크다운 코퍼스 추출 — `mdbook-binder import`.

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
- 이미지 추출: pypdf(디코딩된 바이트)와 pdfplumber(정확한 좌표)를 XObject
  이름으로 대응시켜 이미지를 파일로 저장하고, 위치에 맞춰 본문 흐름에
  마크다운 이미지 참조로 끼워 넣는다. 장식용 스페이서(너무 작은 이미지)와
  여러 페이지에 반복되는 로고/아이콘은 제외한다.
- 챕터 제목 후보 감지(휴리스틱): 문서 앞부분 표본에서 본문 폰트 크기의
  최빈값을 구하고, 그보다 눈에 띄게 큰(기본 1.15배 이상) 폰트로 된 짧은
  단독 줄을 챕터/절 제목으로 추정해 `## ` 마커를 붙인다. 폰트 크기 신호가
  아예 없는 문서(표본 부족)나 본문과 헤딩 폰트 크기가 거의 같은 문서에서는
  아무것도 감지하지 않고 조용히 넘어간다 — 다른 휴리스틱들과 마찬가지로
  "확신 없으면 건드리지 않는다"는 원칙을 따른다. 하나라도 감지되면
  `book.yaml`에 `split: {files: [<파일명>], heading_level: 2}`를 자동으로
  써서, `manifest.resolve_split_targets()`/`html_book.build_html()`이
  빌드 시점에 그 경계로 사이드바 섹션을 나누게 한다(사람이 손으로
  `book.yaml`을 편집하지 않아도 `check`/`build html`만으로 챕터별 사이드바가
  생긴다). `--no-headings`로 끌 수 있다.

챕터 분리는 여전히 파일 하나 안에서(위 감지된 `## ` 경계 + `split` 설정으로)
이뤄진다 — Part_/Chapter_ 명명 규칙에 맞춘 실제 파일 자동 분할은 아직
없다. manifest.py의 3순위 자연정렬 폴백이 단일 파일 코퍼스를 그대로
받아들이므로, 이 모듈의 산출물은 manifest.py를 전혀 건드리지 않고도 그
자체로 유효한 mdbook-binder 코퍼스다(`build html`/`build pdf`/`translate`
모두 바로 이어받을 수 있다).
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
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
_BULLET_MARKER_RE = re.compile(r"^(?:[•●\-*◦‣]|\d+[.)]|[a-zA-Z][.)])\s")
# 슬라이드형 PDF는 여러 불릿 항목을 한 물리적 줄(같은 y좌표 row)에 가로로
# 나란히 배치하는 경우가 있다("● The model ● LoRA low-rank (r) ● Modules
# ..." 전부 한 줄, 실사용 PDF로 확인됨) — _rows_to_text()는 같은 row의
# 단어를 공백으로만 이어붙이므로 그대로 두면 항목 여러 개가 한 문단으로
# 뭉친다. 줄 시작이 아니라 줄 중간에서도 불릿을 인식해야 하므로 별도
# 정규식으로 분리한다. •/◦/‣/●처럼 산문에 절대 등장하지 않는 기호만
# 포함한다 — "-"/"*"는 하이픈 복합어("well-known")·강조(**bold**) 등과
# 흔히 섞여 줄 중간에서 쪼개면 오히려 문장을 망가뜨릴 위험이 크다.
_MIDLINE_BULLET_SPLIT_RE = re.compile(r"(?<=\S) (?=[•●◦‣]\s)")
# "#1)"/"#3.1)" 같은 순번 표기(원문이 "Step #1)" 식으로 씀)도 앞의 불릿
# 목록·문장과 같은 물리적 줄에 뭉쳐 나오는 경우가 있다("● The answer in
# the required format #4) Define reward functions In GRPO, ..." 전부 한
# 줄, 실사용 PDF로 확인됨) — 새 단계가 시작되는 지점이므로 불릿과 동일하게
# 줄 중간이라도 그 앞에서 쪼개고, 쪼갠 줄은 paragraph 경계로도 취급한다
# (_STEP_MARKER_RE). 렌더링 시 H1로 오인식되지 않게 하는 처리는 별도로
# 이미 하고 있다("#"으로 시작하는 줄은 전부 이스케이프).
_MIDLINE_STEP_SPLIT_RE = re.compile(r"(?<=\S) (?=#\d+(?:\.\d+)*\)\s)")
_STEP_MARKER_RE = re.compile(r"^#\d+(?:\.\d+)*\)\s")
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
# _collect_page_images()가 본문 흐름에 끼워 넣는 이미지 참조 — 표 행과
# 마찬가지로 하드랩 해제 대상이 아니다(옆의 캡션 문장과 한 줄로 합쳐지면
# 마크다운 이미지 문법이 깨진다).
_IMAGE_REF_RE = re.compile(r"^!\[[^\]]*\]\([^)]*\)$")
# pdfplumber.extract_words()의 기본 단어 구분 임계값 — calibrate_word_x_tolerance()가
# 문서별로 이보다 낮춰야 할 근거를 못 찾으면 이 값을 그대로 쓴다. 낮추는
# 방향으로만 보정하는 이유는 반대로 올리면(단어 사이 간격을 넓게 잡으면)
# 정상적으로 붙어 있던 단어까지 잘못 합쳐질 위험이 더 크기 때문이다.
_DEFAULT_X_TOLERANCE = 3.0
# 이 값(페이지 표시 크기 기준, pt)보다 작은 이미지는 장식용 스페이서/구분선/
# 트래킹 픽셀로 보고 제외한다.
_MIN_IMAGE_DIMENSION = 20.0
# 쪽번호로 볼 페이지 상/하단 여백 비율 — 표준적인 책 레이아웃의 위/아래
# 여백 비중과 비슷한 값이다.
_PAGE_NUMBER_MARGIN_FRAC = 0.1
# _mark_heading_words()가 감지된 챕터 제목 줄 앞에 붙이는 마커 — 리터럴
# "##"을 쓰면 원문 PDF 텍스트에 우연히 등장하는 "##"(예: 해시태그, 코드
# 스니펫 인용)까지 진짜 헤딩과 구분할 수 없어진다(escape 대상인지 판단할
# 방법이 없음). 실제 추출 텍스트에는 절대 등장하지 않는 제어문자 조합을
# 써서, clean_paragraphs()가 "우리가 직접 붙인 마커"와 "원문에 우연히
# 있던 #"을 확실히 구분하게 한다 — filter_garbled_words()가 이미 앞
# 단계에서 원문 단어의 제어문자를 걸러내므로 이 시퀀스가 원문과 충돌할
# 일은 없다.
_HEADING_SENTINEL = "\x01HEADING\x01"
_HEADING_LINE_RE = re.compile(rf"^{re.escape(_HEADING_SENTINEL)}\s+\S", re.MULTILINE)
# clean_paragraphs()가 _HEADING_SENTINEL을 실제 "## " 마크다운으로 바꾼
# 뒤, import_pdf()가 "헤딩이 하나라도 감지됐는가"를 최종 결과물에서
# 확인할 때 쓴다(그 시점엔 센티널이 이미 사라지고 진짜 "## "만 남는다).
_MD_H2_RE = re.compile(r"^##\s+\S", re.MULTILINE)
# calibrate_body_font_size()가 표본으로 쓰는 문자 크기 표본 최소 개수 —
# 이보다 적으면(표본 부족) 최빈값이 우연에 좌우되기 쉬워 헤딩 감지 자체를
# 건너뛴다(휴리스틱 비활성화가 오탐보다 안전하다는 원칙, calibrate_word_x_tolerance()와
# 동일한 태도).
_MIN_FONT_SIZE_SAMPLES = 20


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


def detect_heading_rows(
    rows: list[list[dict]],
    body_size: float,
    *,
    min_ratio: float = 1.15,
    max_words: int = 15,
    max_size_spread: float = 0.5,
) -> set[int]:
    """본문 폰트보다 눈에 띄게 큰(min_ratio 이상) 짧은 단독 줄을 챕터 제목
    후보로 본다(순수 함수).

    - 줄 안의 폰트 크기 편차가 max_size_spread를 넘으면(예: 본문 문장에
      큰 글자 하나가 섞인 경우 — 첫 글자 장식체 등) 제외한다. 헤딩은
      보통 줄 전체가 같은 크기이므로, 편차가 크면 헤딩이 아니라 본문
      문장에 우연히 큰 글자가 섞인 경우로 본다.
    - max_words를 넘는 긴 줄은 제외한다 — 진짜 헤딩은 짧고, 본문 문단이
      우연히 본문보다 살짝 큰 폰트로 통째로 조판된 경우(강조 인용문 등)와
      구분하기 위함이다.
    - `size` 키가 없는 단어(예: _save_images_and_build_elements()가 만든
      이미지 참조 요소)만 있는 줄은 판단 근거가 없으므로 후보에서 제외한다.
    """
    result: set[int] = set()
    for i, row in enumerate(rows):
        if not row or len(row) > max_words:
            continue
        sizes = [w["size"] for w in row if "size" in w]
        if not sizes:
            continue
        if max(sizes) - min(sizes) > max_size_spread:
            continue
        if (sum(sizes) / len(sizes)) >= body_size * min_ratio:
            result.add(i)
    return result


def _mark_heading_words(words: list[dict], rows: list[list[dict]], heading_indices: set[int]) -> list[dict]:
    """감지된 헤딩 후보 줄마다 "##" 마커 요소를 그 줄 맨 앞에 끼워 넣는다.

    _save_images_and_build_elements()가 이미지를 "단어처럼 생긴" 요소로
    본문 흐름에 끼워 넣는 것과 같은 방식 — 좌표(x0/x1/top/bottom)만 실제
    단어와 같은 계로 맞추면, 이후 컬럼/행 재구성 파이프라인이 이 마커를
    일반 단어처럼 다뤄 줄 맨 앞(가장 작은 x0)에 자연히 배치한다. 그 줄이
    "_rows_to_text()"를 거치면 "## 실제 제목 텍스트"가 된다.
    """
    if not heading_indices:
        return words

    markers: list[dict] = []
    for i in heading_indices:
        row = rows[i]
        if not row:
            continue
        first = row[0]
        markers.append({
            "text": _HEADING_SENTINEL, "x0": first["x0"] - 1.0, "x1": first["x0"] - 0.5,
            "top": first["top"], "bottom": first["bottom"],
        })
    return words + markers


def _process_page(words: list[dict], body_size: float | None = None) -> str:
    """한 페이지의 단어들을 표 인식 + 컬럼 인식(읽기 순서 보정)을 거쳐 텍스트로 만든다.

    표 인식을 컬럼 분리보다 먼저, 페이지 전체 단어를 대상으로 수행한다 —
    순서가 반대면(컬럼부터 나누면) 열이 3개 이상인 진짜 표가 컬럼 3개짜리
    텍스트 레이아웃으로 쪼개져 표로 인식될 기회 자체가 사라진다(실제로
    검증된 회귀). 표 영역을 찾으면 그 앞/뒤 나머지만 컬럼 인식으로 넘긴다 —
    표 영역이 우연히 2단 레이아웃처럼 보이는 것까지 막을 필요는 없다
    (detect_table()의 min_cols=3 기본값이 2단 텍스트와의 오탐을 이미 막는다).

    body_size가 주어지면(문서 전체에서 calibrate_body_font_size()로 구한
    본문 폰트 크기) detect_heading_rows()로 챕터 제목 후보를 찾아 "## "
    마커를 붙인다 — 표 인식보다 먼저 해야 한다(마커 삽입으로 word 목록이
    바뀌면 rows를 다시 구해야 표/컬럼 인식이 최신 상태를 본다).
    """
    if not words:
        return ""

    rows = _group_into_rows(words)
    if body_size is not None:
        heading_indices = detect_heading_rows(rows, body_size)
        if heading_indices:
            words = _mark_heading_words(words, rows, heading_indices)
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


def _sample_font_sizes(pdf, *, max_pages: int = 10) -> list[float]:
    """문서 앞부분 max_pages 페이지에서 문자별 폰트 크기를 표본 수집한다.

    _sample_char_gaps()와 같은 이유로 앞부분만 본다 — 본문 폰트 크기는
    문서 전체에서 대체로 일관되므로 표본으로 충분하고, 매번 문서 전체를
    훑는 비용을 피한다. 공백 문자는 크기가 0이거나 신뢰할 수 없는 경우가
    있어 표본에서 제외한다.
    """
    sizes: list[float] = []
    for page in pdf.pages[:max_pages]:
        for ch in page.chars:
            if ch.get("text", "").strip():
                sizes.append(ch["size"])
    return sizes


def calibrate_body_font_size(sizes: list[float], *, min_samples: int = _MIN_FONT_SIZE_SAMPLES) -> float | None:
    """문자 크기 표본에서 본문 폰트 크기(최빈값)를 추론한다(순수 함수).

    책 한 권 전체에서 가장 많은 글자 수를 차지하는 크기는 거의 항상 본문
    텍스트다(헤딩·캡션 등은 상대적으로 드물다) — 그래서 평균이 아니라
    최빈값을 쓴다(평균은 헤딩이 유난히 크면 한쪽으로 쏠릴 수 있다). 0.5pt
    단위로 반올림해 묶는다 — 폰트 렌더링/부동소수 오차로 미세하게 다른
    크기가 같은 본문 폰트인데도 서로 다른 값으로 흩어지는 것을 막는다.

    표본이 min_samples 미만이면(문서가 너무 짧거나 폰트 크기 정보가 없는
    경우) None을 반환해 헤딩 감지 자체를 건너뛰게 한다 — calibrate_word_x_tolerance()와
    같은 태도로, 확신 없는 추론보다 휴리스틱 비활성화가 안전하다.
    """
    if len(sizes) < min_samples:
        return None
    rounded = [round(s * 2) / 2 for s in sizes]
    return Counter(rounded).most_common(1)[0][0]


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


def _collect_page_images(pdf, pdf_path: Path) -> list[list[dict]]:
    """페이지별로 이미지 메타데이터(정확한 위치 + 디코딩된 원본 바이트 + 해시)를 수집한다.

    pdfplumber의 page.images는 정확한 좌표(x0/top 등)를 주지만 디코딩된
    이미지 바이트를 깔끔하게 꺼내는 API가 약하고, pypdf의 page.images는
    반대로 디코딩은 잘 하지만(내부적으로 Pillow 사용) 좌표가 없다 — 둘 다
    필요해서 XObject 이름으로 서로 대응시킨다(pypdf는 이름에 확장자를
    붙인다: "Im8" → "Im8.png").

    실패해도(Pillow 미설치, 손상된 이미지 스트림 등) 전체 임포트를 막지
    않는다 — 이미지 없이 텍스트만이라도 뽑는 게 낫다(관대한 파싱 원칙,
    이미 write_minimal_book_yaml 등에서 쓰는 것과 동일한 원칙).
    """
    try:
        import pypdf

        reader = pypdf.PdfReader(str(pdf_path))
    except Exception:
        return [[] for _ in pdf.pages]

    result: list[list[dict]] = []
    for plumber_page, pypdf_page in zip(pdf.pages, reader.pages):
        plumber_images = {img["name"]: img for img in plumber_page.images}
        page_result: list[dict] = []
        try:
            pypdf_images = list(pypdf_page.images)
        except Exception:
            pypdf_images = []
        for pypdf_img in pypdf_images:
            name = pypdf_img.name
            base_name = name.rsplit(".", 1)[0] if "." in name else name
            ext = name.rsplit(".", 1)[-1] if "." in name else "png"
            pos = plumber_images.get(base_name)
            if pos is None or not pypdf_img.data:
                continue
            if pos["width"] < _MIN_IMAGE_DIMENSION or pos["height"] < _MIN_IMAGE_DIMENSION:
                continue
            page_result.append({
                "x0": pos["x0"], "x1": pos["x1"], "top": pos["top"], "bottom": pos["bottom"],
                "data": pypdf_img.data, "ext": ext,
                "hash": hashlib.md5(pypdf_img.data).hexdigest(),
            })
        result.append(page_result)
    return result


def _filter_repeated_images(pages_images: list[list[dict]], *, min_fraction: float = 0.5) -> list[list[dict]]:
    """여러 페이지에 걸쳐 동일한 이미지(같은 바이트 해시)가 반복되면 제외한다(순수 함수).

    strip_repeated_lines()가 텍스트 러닝 헤더/푸터를 잡아내는 것과 같은
    원리 — 매 페이지 반복되는 로고/아이콘은 본문 내용이 아니라 장식용
    페이지 furniture로 본다.
    """
    if len(pages_images) < 2:
        return pages_images

    counts: dict[str, int] = {}
    for page_imgs in pages_images:
        for h in {img["hash"] for img in page_imgs}:
            counts[h] = counts.get(h, 0) + 1

    threshold = max(2, round(len(pages_images) * min_fraction))
    repeated = {h for h, c in counts.items() if c >= threshold}
    if not repeated:
        return pages_images

    return [[img for img in page_imgs if img["hash"] not in repeated] for page_imgs in pages_images]


def _save_images_and_build_elements(pages_images: list[list[dict]], images_dir: Path) -> list[list[dict]]:
    """이미지를 images_dir에 저장하고, 본문 흐름에 끼워 넣을 수 있는 "단어처럼
    생긴" 요소 목록으로 바꾼다.

    text 필드에 마크다운 이미지 참조 문자열을 담아 반환한다 — 위치(x0/x1/
    top/bottom)만 실제 단어와 같은 좌표계로 맞춰두면, extract_pdf_text()가
    이 요소를 일반 단어처럼 words 리스트에 섞어 넣는 것만으로
    _group_into_rows()/detect_columns() 등 기존 파이프라인이 알아서 본문
    읽기 순서에 맞춰 배치한다 — 이미지 전용 처리 경로를 새로 만들 필요가
    없다.
    """
    result: list[list[dict]] = []
    for page_idx, page_imgs in enumerate(pages_images):
        elements: list[dict] = []
        for i, img in enumerate(page_imgs):
            filename = f"page{page_idx + 1:04d}_img{i + 1}.{img['ext']}"
            images_dir.mkdir(parents=True, exist_ok=True)
            (images_dir / filename).write_bytes(img["data"])
            elements.append({
                "text": f"![](images/{filename})",
                "x0": img["x0"], "x1": img["x1"], "top": img["top"], "bottom": img["bottom"],
            })
        result.append(elements)
    return result


def extract_pdf_text(
    pdf_path: Path, *, images_dir: Path | None = None, detect_headings: bool = True
) -> list[str]:
    """pdfplumber로 페이지별 단어를 추출해 컬럼/표를 인식한 텍스트로 변환한다(페이지 1개당 문자열 1개).

    pypdf의 layout 모드는 슬라이드/프레젠테이션류 PDF의 줄바꿈은 잘 복원하지만
    2단 이상 레이아웃에서는 같은 높이의 서로 다른 컬럼 텍스트를 한 줄에
    섞어버린다(y좌표 대역만으로 줄을 재구성해 컬럼 개념이 없음 — 실사용
    PDF로 재현·확인됨). pdfplumber로 단어 하나하나의 (x, y) 좌표를 직접 받아
    detect_columns()/detect_table()로 재구성하면 이 문제를 피할 수 있다.

    images_dir가 주어지면 이미지도 추출해 그 아래(images/ 하위 아님 —
    images_dir 자체가 images/ 폴더)에 저장하고, 위치에 맞춰 본문 흐름에
    마크다운 이미지 참조로 끼워 넣는다. None이면(기본값) 텍스트만 뽑는다.

    detect_headings=True(기본)면 문서 앞부분 표본으로 본문 폰트 크기를
    추정해(calibrate_body_font_size()) 그보다 눈에 띄게 큰 짧은 줄을
    챕터 제목 후보로 보고 "## " 마커를 붙인다(detect_heading_rows() 참고).
    표본 부족 등으로 본문 크기를 못 정하면 전체가 조용히 건너뛴다.
    """
    import pdfplumber

    with pdfplumber.open(str(pdf_path)) as pdf:
        x_tolerance = calibrate_word_x_tolerance(_sample_char_gaps(pdf))
        body_size = calibrate_body_font_size(_sample_font_sizes(pdf)) if detect_headings else None

        image_elements_per_page: list[list[dict]] = [[] for _ in pdf.pages]
        if images_dir is not None:
            pages_images = _filter_repeated_images(_collect_page_images(pdf, pdf_path))
            image_elements_per_page = _save_images_and_build_elements(pages_images, images_dir)

        pages = []
        for page, img_elements in zip(pdf.pages, image_elements_per_page):
            words = page.extract_words(x_tolerance=x_tolerance, extra_attrs=["size"])
            words = filter_garbled_words(words)
            words = filter_page_number_words(words, page.height)
            words = words + img_elements
            pages.append(_process_page(words, body_size))
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
    - 새 불릿 마커(•/●/-/*/숫자./영문. 및 _detect_document_bullet_chars()가
      이 문서에서 추론한 구두점 없는 한 글자 불릿)로 시작하는 줄은 그
      앞에서 단락을 끊는다(이전 줄이 문장부호로 안 끝났어도)
    - 여러 불릿(•/●/◦/‣)이 한 물리적 줄에 나란히 배치된 경우 각 불릿
      앞에서 줄을 먼저 쪼갠 뒤 위 규칙을 적용한다
    - "#1)"/"#3.1)" 같은 순번 표기도 앞의 불릿·문장과 한 줄에 뭉쳐 나오면
      그 앞에서 쪼개고, 불릿과 동일하게 새 단락 경계로 취급한다
    - 단어 중간 하이픈 개행은 하이픈 없이 그대로 합친다
    - 빈 줄(몇 개가 연속이든)은 단락 구분 하나로 정규화된다

    페이지 경계를 넘어 이어지는 문장은 다루지 않는다 — extract_pdf_text()가
    페이지 텍스트를 페이지 단위로 반환하므로 여기 도달할 때는 이미 하나의
    이어붙인 문자열이라 원래 페이지 경계 정보가 없다(Phase 2 후보).
    """
    raw_text = _MIDLINE_BULLET_SPLIT_RE.sub("\n", raw_text)
    raw_text = _MIDLINE_STEP_SPLIT_RE.sub("\n", raw_text)
    doc_bullet_chars = _detect_document_bullet_chars(raw_text)
    paragraphs: list[str] = []
    current: list[str] = []
    block_lines: list[str] = []

    def flush_prose() -> None:
        if current:
            paragraphs.append(" ".join(current))
            current.clear()

    def flush_block() -> None:
        if block_lines:
            paragraphs.append("\n".join(block_lines))
            block_lines.clear()

    for raw_line in raw_text.split("\n"):
        line = raw_line.strip()
        if not line:
            flush_prose()
            flush_block()
            continue

        # 순번 표기("#1)" 등)는 escape보다 먼저 판정해야 한다 — escape 후에는
        # "\#1)"이 돼 _STEP_MARKER_RE("^#\d")에 더 이상 매치되지 않는다.
        is_step_marker = _STEP_MARKER_RE.match(line) is not None

        # _mark_heading_words()가 감지된 챕터 제목 줄 앞에 붙인 센티널
        # 마커도 escape보다 먼저 판정한다 — 하드랩 해제·이스케이프 없이
        # 독립된 헤딩 단락으로 그대로 통과시키되, 사람이 읽는 마크다운에는
        # 센티널이 아니라 실제 "## "를 남긴다.
        if _HEADING_LINE_RE.match(line):
            flush_prose()
            flush_block()
            heading_text = line[len(_HEADING_SENTINEL):].strip()
            paragraphs.append(f"## {heading_text}")
            continue

        # 원문이 "#1) Load the model"처럼 "#"을 순번 표기로 쓰거나, 그냥
        # "##"이 우연히 등장하는 경우(해시태그·코드 인용 등)가 흔한데,
        # python-markdown은 "#" 뒤에 공백이 없어도 ATX 헤딩으로 인식해
        # (예: "#1) ..." → <h1>) 챕터당 H1 하나 규칙이 깨지고 본문 곳곳이
        # 큰 제목으로 렌더된다(실사용 PDF로 재현됨). 위에서 이미 센티널
        # 마커(우리가 직접 붙인 진짜 헤딩)는 걸러냈으므로, 여기 남은 "#"로
        # 시작하는 줄은 전부 원문에서 그대로 온 것이라 이스케이프해도
        # 안전하다.
        if line.startswith("#"):
            line = "\\" + line

        if _TABLE_ROW_RE.match(line) or _IMAGE_REF_RE.match(line):
            flush_prose()
            block_lines.append(line)
            continue
        flush_block()

        is_doc_bullet = len(line) > 1 and line[0].lower() in doc_bullet_chars and line[1] == " "
        if _BULLET_MARKER_RE.match(line) or is_doc_bullet or is_step_marker:
            flush_prose()
            current.append(line)
        elif current and _HYPHEN_BREAK_RE.search(current[-1]) and line[:1].islower():
            current[-1] = current[-1][:-1] + line
        else:
            current.append(line)

        if _SENTENCE_END_RE.search(line):
            flush_prose()

    flush_prose()
    flush_block()
    return "\n\n".join(paragraphs)


def import_pdf(
    pdf_path: Path,
    out_dir: Path,
    *,
    title: str | None = None,
    extract_images: bool = True,
    detect_chapter_headings: bool = True,
) -> Path:
    """PDF_PATH를 단일 평면 마크다운 + 최소 book.yaml(language: en)로 out_dir에 쓴다.

    extract_images=True(기본)면 out_dir/images/에 이미지를 저장하고 본문
    흐름에 위치에 맞춰 끼워 넣는다 — html_book.py의 base64 이미지 임베드가
    상대경로 `images/...` 참조를 그대로 읽으므로, 이 코퍼스를 build html로
    바로 이어붙여도 이미지가 그대로 살아있다.

    detect_chapter_headings=True(기본)면 폰트 크기 기반으로 챕터 제목
    후보를 찾아 "## " 마커를 붙인다(extract_pdf_text() 참고). 하나라도
    감지되면 book.yaml에 `split: {files: [<파일명>], heading_level: 2}`도
    함께 써서, 곧바로 `build html`만 돌려도 챕터별 사이드바 섹션이 생기게
    한다(manifest.resolve_split_targets()/html_book.build_html() 소비) —
    감지된 게 없으면(표본 부족·본문과 헤딩 폰트 크기가 거의 같은 문서 등)
    기존과 동일하게 language만 있는 book.yaml을 쓴다. 감지된 경계가
    부정확하면 .md 파일의 "## " 마커를 손으로 고친 뒤 다시 빌드하면 된다
    (파일을 물리적으로 쪼갤 필요 없음).

    Part_/Chapter_ 명명 규칙에 맞춘 실제 파일 자동 분할은 여전히 스코프
    밖이다. 반환값은 생성된 .md 파일 경로.
    """
    images_dir = out_dir / "images" if extract_images else None
    pages = strip_repeated_lines(
        extract_pdf_text(pdf_path, images_dir=images_dir, detect_headings=detect_chapter_headings)
    )
    body = clean_paragraphs("\n\n".join(pages))

    stem = title or pdf_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{stem}.md"
    md_path.write_text(f"# {stem}\n\n{body}\n", encoding="utf-8")

    if _MD_H2_RE.search(body):
        write_minimal_book_yaml(
            out_dir / "book.yaml",
            language="en",
            split={"files": [md_path.name], "heading_level": 2},
        )
    else:
        write_minimal_book_yaml(out_dir / "book.yaml", language="en")
    return md_path
