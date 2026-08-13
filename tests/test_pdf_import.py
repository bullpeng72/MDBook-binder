"""pdf_import.py의 텍스트 정리(순수 함수) + PDF→코퍼스 추출 회귀 테스트."""

from pathlib import Path

from mdbook_binder.manifest import TIER_NATURAL_SORT, resolve_verbose
from mdbook_binder.pdf_import import (
    _detect_document_bullet_chars,
    _group_into_rows,
    _row_groups,
    calibrate_word_x_tolerance,
    clean_paragraphs,
    detect_columns,
    detect_table,
    filter_garbled_words,
    filter_page_number_words,
    import_pdf,
    strip_repeated_lines,
)


def _word(text: str, x0: float, top: float, *, char_width: float = 6.0, height: float = 12.0) -> dict:
    """pdfplumber의 extract_words() 반환 형식(x0/x1/top/bottom)을 흉내낸 단어 dict를 만든다."""
    return {"text": text, "x0": x0, "x1": x0 + len(text) * char_width, "top": top, "bottom": top + height}


def _prose_words(lines: list[str], *, x_start: float = 72.0, y_start: float = 100.0, y_step: float = 20.0) -> list[dict]:
    """실제 산문처럼 줄마다 단어 시작 위치가 들쭉날쭉한 가짜 단어 목록을 만든다."""
    words: list[dict] = []
    for i, line in enumerate(lines):
        x = x_start
        for w in line.split():
            words.append(_word(w, x, y_start + i * y_step))
            x += len(w) * 6 + 4
    return words


def _make_minimal_pdf(lines: list[str]) -> bytes:
    """텍스트만 추출 가능하면 되는 최소 유효 PDF를 바이트 단위로 직접 구성한다.

    pypdf.PdfWriter는 페이지 병합/조작용이라 새 텍스트 콘텐츠 스트림을 쓰는
    공개 API가 없고, reportlab 같은 저작 라이브러리를 테스트 전용으로 새로
    들이는 것도 과하다 — PDF 포맷 자체가 단순 텍스트 객체만 쓰면 손으로도
    충분히 구성 가능해 별도 의존성/바이너리 픽스처 없이 이 방식을 쓴다.
    """
    text_ops = "BT /F1 12 Tf 72 720 Td\n" + "\n".join(f"({line}) Tj 0 -14 Td" for line in lines) + "\nET"
    stream = text_ops.encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
            b"/MediaBox [0 0 612 792] /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode()
    return bytes(out)


def _make_positioned_pdf(placements: list[tuple[str, float, float]]) -> bytes:
    """(텍스트, x, y) 절대좌표 배치를 받아 최소 유효 PDF를 만든다 — 2단
    레이아웃·표처럼 특정 x좌표에 텍스트를 놓아야 하는 픽스처용. _make_minimal_pdf()는
    순차적인 줄내림(Td)만 지원해 이런 배치를 표현할 수 없다."""
    ops = ["BT /F1 12 Tf"]
    for text, x, y in placements:
        ops.append(f"1 0 0 1 {x} {y} Tm ({text}) Tj")
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
            b"/MediaBox [0 0 612 792] /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode()
    return bytes(out)


class TestCleanParagraphs:
    def test_hard_wrapped_lines_joined_into_one_paragraph(self):
        raw = "This is a sentence that got\nhard wrapped across two lines."
        assert clean_paragraphs(raw) == "This is a sentence that got hard wrapped across two lines."

    def test_sentence_ending_punctuation_starts_new_paragraph(self):
        raw = "First sentence.\nSecond paragraph starts here, no blank line."
        # 문장부호로 끝난 줄 다음은 새 단락으로 취급된다(빈 줄이 없어도).
        assert clean_paragraphs(raw) == "First sentence.\n\nSecond paragraph starts here, no blank line."

    def test_hyphenated_word_break_dehyphenated(self):
        raw = "This is an exam-\nple of hyphenation."
        assert clean_paragraphs(raw) == "This is an example of hyphenation."

    def test_hyphen_before_capitalized_word_left_untouched(self):
        """다음 줄이 대문자로 시작하면(고유명사 등) 의도적 하이픈일 수 있어 건드리지 않는다."""
        raw = "We visited South-\nKorea last year."
        assert clean_paragraphs(raw) == "We visited South- Korea last year."

    def test_multiple_blank_lines_collapse_to_single_separator(self):
        raw = "Para one.\n\n\n\n\nPara two."
        assert clean_paragraphs(raw) == "Para one.\n\nPara two."

    def test_empty_input_returns_empty_string(self):
        assert clean_paragraphs("") == ""

    def test_bullet_marker_starts_new_paragraph_without_terminal_punctuation(self):
        """슬라이드형 PDF는 불릿 항목이 문장부호로 안 끝나는 경우가 많다 —
        새 불릿 마커를 만나면 이전 줄이 문장부호로 안 끝났어도 단락을 끊는다."""
        raw = "Heading with no period\n- First point\n- Second point"
        assert clean_paragraphs(raw) == "Heading with no period\n\n- First point\n\n- Second point"

    def test_wrapped_continuation_of_bullet_stays_in_same_paragraph(self):
        raw = "- A bullet that wraps\n  across two lines."
        assert clean_paragraphs(raw) == "- A bullet that wraps across two lines."

    def test_numbered_and_lettered_markers_also_split(self):
        raw = "1. First item\na. Sub item\n* Star item"
        assert clean_paragraphs(raw) == "1. First item\n\na. Sub item\n\n* Star item"

    def test_dash_without_following_space_is_not_treated_as_bullet(self):
        """행 앞에 나온 하이픈이라도 뒤에 공백이 없으면(예: 음수, 복합어) 불릿으로
        오인해 단락을 끊지 않는다."""
        raw = "The value is\n-5 degrees today."
        assert clean_paragraphs(raw) == "The value is -5 degrees today."


class TestGroupIntoRows:
    def test_words_at_same_y_grouped_into_one_row(self):
        words = [_word("Hello", 72, 100), _word("world", 110, 100)]
        rows = _group_into_rows(words)
        assert len(rows) == 1
        assert [w["text"] for w in rows[0]] == ["Hello", "world"]

    def test_words_at_different_y_form_separate_rows(self):
        words = [_word("Line", 72, 100), _word("one", 72, 130)]
        rows = _group_into_rows(words)
        assert len(rows) == 2

    def test_row_words_sorted_by_x_regardless_of_input_order(self):
        words = [_word("world", 110, 100), _word("Hello", 72, 100)]
        rows = _group_into_rows(words)
        assert [w["text"] for w in rows[0]] == ["Hello", "world"]

    def test_empty_input_returns_no_rows(self):
        assert _group_into_rows([]) == []


class TestDetectColumns:
    def test_single_column_returns_one_band(self):
        words = _prose_words(["Just a single column of normal text here"])
        columns = detect_columns(words)
        assert len(columns) == 1

    def test_two_column_layout_detected_via_gutter(self):
        """실제 2단 레이아웃(같은 높이에 좌/우 컬럼)을 흉내낸 좌표로 거터를 감지한다."""
        words = [
            _word("Left", 72, 100), _word("column", 100, 100),
            _word("Right", 320, 100), _word("column", 348, 100),
            _word("Left", 72, 120), _word("column", 100, 120),
            _word("Right", 320, 120), _word("column", 348, 120),
        ]
        columns = detect_columns(words)
        assert len(columns) == 2
        assert columns[0][1] < 320  # 왼쪽 컬럼 대역은 오른쪽 컬럼 시작 전에 끝남
        assert columns[1][0] > 200  # 오른쪽 컬럼 대역은 왼쪽 컬럼 다음에서 시작

    def test_gap_near_page_edge_not_treated_as_gutter(self):
        """가장자리 여백은 컬럼 사이 거터가 아니라 그냥 페이지 여백이다."""
        words = _prose_words(["A single paragraph with normal left-aligned margins only"])
        columns = detect_columns(words)
        assert len(columns) == 1

    def test_empty_words_returns_no_columns(self):
        assert detect_columns([]) == []


class TestFilterGarbledWords:
    def test_word_with_null_byte_is_removed(self):
        """회귀 대상: 특정 PDF의 폰트 인코딩이 깨져(ToUnicode CMap 불량 등)
        추출된 단어에 NUL 바이트가 글자 사이에 섞여 나온 사례 — 실사용 PDF로 확인."""
        words = [_word("Hello", 72, 100), _word("e\x00n\x00g", 110, 100), _word("world", 150, 100)]
        result = filter_garbled_words(words)
        assert [w["text"] for w in result] == ["Hello", "world"]

    def test_clean_words_all_kept(self):
        words = [_word("Hello", 72, 100), _word("world", 110, 100)]
        assert filter_garbled_words(words) == words

    def test_empty_input_returns_empty_list(self):
        assert filter_garbled_words([]) == []


class TestFilterPageNumberWords:
    _PAGE_HEIGHT = 792.0  # US Letter, points

    def test_standalone_number_in_top_margin_removed(self):
        """회귀 대상: 쪽번호는 페이지마다 값이 달라 strip_repeated_lines()로는
        못 잡는다 — 위치(상/하단 여백)와 형태(그 줄에 홀로)로 판단해야 한다."""
        words = [_word("7", 300, 20)]  # 상단 여백(10% = 79.2pt) 안
        assert filter_page_number_words(words, self._PAGE_HEIGHT) == []

    def test_standalone_number_in_bottom_margin_removed(self):
        words = [_word("42", 300, 770)]  # 하단 여백(90% = 712.8pt) 안
        assert filter_page_number_words(words, self._PAGE_HEIGHT) == []

    def test_number_in_body_area_kept(self):
        """여백 밖(본문 영역)에 있는 숫자는 쪽번호가 아니라 진짜 본문 내용일 수 있어 건드리지 않는다."""
        words = [_word("42", 300, 400)]
        assert filter_page_number_words(words, self._PAGE_HEIGHT) == words

    def test_number_sharing_row_with_other_words_kept(self):
        """같은 줄에 다른 단어가 있으면(예: "Page 7") 쪽번호 단독 줄이 아니므로 보존한다."""
        words = [_word("Page", 300, 20), _word("7", 340, 20)]
        assert filter_page_number_words(words, self._PAGE_HEIGHT) == words

    def test_non_numeric_standalone_word_in_margin_kept(self):
        words = [_word("Chapter", 300, 20)]
        assert filter_page_number_words(words, self._PAGE_HEIGHT) == words

    def test_empty_input_returns_empty_list(self):
        assert filter_page_number_words([], self._PAGE_HEIGHT) == []


class TestCalibrateWordXTolerance:
    def test_too_few_samples_returns_default(self):
        assert calibrate_word_x_tolerance([0.0, 0.1, 3.0]) == 3.0

    def test_tight_word_spacing_lowers_threshold(self):
        """회귀 대상: 실사용 PDF(2009년 발행 서적)에서 단어 사이 간격이 기본값
        3pt보다 좁아(약 2.8pt) 문장 전체가 한 단어로 붙어버린 사례."""
        within_word = [0.0] * 40 + [0.1] * 20
        between_word = [2.8] * 30
        gaps = within_word + between_word
        threshold = calibrate_word_x_tolerance(gaps)
        assert 0.1 < threshold < 2.8

    def test_wide_word_spacing_still_below_default(self):
        """단어 사이 간격이 기본값(3pt)보다 넓은 문서라도, 보정값은 여전히
        그 문서의 실제 간격보다 확실히 낮게 잡혀 정상 단어 분리를 해치지
        않는다 — default를 넘어서는 일은 없다."""
        within_word = [0.0] * 40
        between_word = [3.5] * 30 + [4.0] * 20
        gaps = within_word + between_word
        threshold = calibrate_word_x_tolerance(gaps)
        assert threshold <= 3.0
        assert threshold < min(between_word)

    def test_never_exceeds_default_even_with_wide_gaps(self):
        gaps = [0.0] * 30 + [10.0] * 30
        assert calibrate_word_x_tolerance(gaps) <= 3.0

    def test_first_jump_used_not_largest_jump(self):
        """전체에서 가장 큰 도약이 아니라, 0 근처 군집을 벗어나는 첫 도약을
        경계로 써야 한다 — 뒤쪽의 드문 이상치 구간에 더 큰 도약이 있어도
        속으면 안 된다."""
        gaps = [0.0] * 30 + [1.5] * 30 + [16.0] * 30  # 1.5 근처가 진짜 경계
        threshold = calibrate_word_x_tolerance(gaps)
        assert threshold < 2.0


class TestDetectDocumentBulletChars:
    def test_frequently_repeated_leading_letter_detected(self):
        """회귀 대상: 특정 PDF의 커스텀 불릿 폰트가 글리프를 알파벳 한 글자
        (구두점 없음)로 잘못 매핑해 내보내는 경우 — 실사용 PDF로 확인된 문제."""
        raw = "\n".join([f"q Item number {i} in the list" for i in range(6)])
        assert _detect_document_bullet_chars(raw) == {"q"}

    def test_below_threshold_not_detected(self):
        raw = "\n".join([f"q Item number {i}" for i in range(3)])
        assert _detect_document_bullet_chars(raw, min_occurrences=5) == set()

    def test_common_single_letter_words_excluded_even_if_frequent(self):
        """"a"/"i"는 실제 영어 단어라 아무리 반복돼도 불릿으로 오인하면 안 된다."""
        raw = "\n".join([f"a book about topic {i}" for i in range(10)])
        assert _detect_document_bullet_chars(raw) == set()

    def test_no_candidates_returns_empty_set(self):
        raw = "Just a normal paragraph with no repeated single-letter line starts."
        assert _detect_document_bullet_chars(raw) == set()


class TestCleanParagraphsDocumentBullets:
    def test_document_specific_bullet_char_splits_paragraphs(self):
        """clean_paragraphs()가 _detect_document_bullet_chars()로 추론한 불릿도
        _BULLET_MARKER_RE와 동일하게 단락 경계로 취급해야 한다."""
        raw = "\n".join([f"q Item number {i} in the list" for i in range(6)])
        result = clean_paragraphs(raw)
        assert result.count("\n\n") == 5  # 6개 항목 → 단락 5번 구분

    def test_infrequent_single_letter_line_not_treated_as_bullet(self):
        """빈도가 낮으면(우연) 불릿으로 취급하지 않아 하드랩 해제 로직이 그대로 적용된다."""
        raw = "q\nis a rare standalone letter here"
        result = clean_paragraphs(raw)
        assert "\n\n" not in result


class TestDetectTable:
    def test_prose_is_not_misidentified_as_table(self):
        """회귀 대상: 단어 x0를 문서 전체에서 무작정 클러스터링하면 왼쪽 정렬된
        산문도 표로 오인된다 — 실제로 검증된 오탐. within-row 간격 분석으로
        고쳐진 뒤에는 산문이 표로 감지되지 않아야 한다."""
        rows = _group_into_rows(
            _prose_words(
                [
                    "This is the first line of a normal paragraph that wraps naturally",
                    "across several lines without any consistent word alignment at all",
                    "because real prose text reflows differently on every single line",
                ]
            )
        )
        assert detect_table(rows) is None

    def test_genuine_table_detected(self):
        data = [["Name", "Age", "City"], ["Alice", "30", "Seoul"], ["Bob", "25", "Busan"], ["Carol", "40", "Incheon"]]
        words = [
            w
            for i, row in enumerate(data)
            for w in (_word(row[0], 72, 100 + i * 20), _word(row[1], 250, 100 + i * 20), _word(row[2], 400, 100 + i * 20))
        ]
        rows = _group_into_rows(words)
        region = detect_table(rows)
        assert region == (0, 4)

    def test_too_few_rows_not_detected_as_table(self):
        data = [["Name", "Age"], ["Alice", "30"]]
        words = [w for i, row in enumerate(data) for w in (_word(row[0], 72, 100 + i * 20), _word(row[1], 250, 100 + i * 20))]
        rows = _group_into_rows(words)
        assert detect_table(rows, min_rows=3) is None


class TestRowGroups:
    def test_close_words_form_one_group(self):
        row = [_word("Hello", 72, 100), _word("world", 110, 100)]
        assert len(_row_groups(row)) == 1

    def test_widely_spaced_words_form_separate_groups(self):
        row = [_word("Name", 72, 100), _word("Alice", 250, 100)]
        groups = _row_groups(row)
        assert len(groups) == 2

    def test_empty_row_returns_no_groups(self):
        assert _row_groups([]) == []


class TestStripRepeatedLines:
    def test_line_repeated_on_every_page_is_removed(self):
        pages = ["Title A\nFooter\ncontent 1", "Title B\nFooter\ncontent 2", "Title C\nFooter\ncontent 3"]
        result = strip_repeated_lines(pages)
        assert all("Footer" not in p for p in result)
        assert "Title A" in result[0] and "content 1" in result[0]

    def test_single_page_returned_unchanged(self):
        """반복 여부를 판단할 표본(2페이지 이상)이 없으면 그대로 반환한다."""
        pages = ["Title\nFooter\ncontent"]
        assert strip_repeated_lines(pages) == pages

    def test_line_below_threshold_is_kept(self):
        """min_fraction 미만으로만 등장하는 줄은 진짜 본문일 수 있어 지우지 않는다."""
        pages = ["Footer\npage one", "page two", "page three", "page four"]
        result = strip_repeated_lines(pages, min_fraction=0.5)
        assert "Footer" in result[0]

    def test_repeated_line_within_same_page_counts_once(self):
        """같은 페이지 안에서 같은 줄이 여러 번 나와도 그 페이지는 1회로만 센다."""
        pages = ["Footer\nFooter\ncontent 1", "unique page two"]
        result = strip_repeated_lines(pages, min_fraction=0.5)
        # 2페이지 중 1페이지에만 등장(중복 카운트 안 함) → threshold(max(2, 1)=2) 미달로 유지
        assert "Footer" in result[0]

    def test_no_repeated_lines_returns_pages_unchanged(self):
        pages = ["unique one", "unique two", "unique three"]
        assert strip_repeated_lines(pages) == pages


def test_import_pdf_produces_buildable_corpus(tmp_path: Path):
    """import_pdf()의 산출물이 manifest.resolve()로 실제 빌드 가능한 단일
    챕터 코퍼스(3순위 자연정렬)로 해석되는지 확인한다 — 파일 존재만으로는
    "빌드 가능한 코퍼스"라는 실제 계약을 증명하지 못한다."""
    pdf_path = tmp_path / "book.pdf"
    pdf_path.write_bytes(_make_minimal_pdf(["Hello world.", "This is a test PDF."]))
    out_dir = tmp_path / "corpus"

    md_path = import_pdf(pdf_path, out_dir)

    assert md_path.exists()
    content = md_path.read_text(encoding="utf-8")
    assert content.startswith("# book\n\n")
    assert "Hello world." in content

    from mdbook_binder.manifest import BookConfig

    config = BookConfig.load(out_dir)
    chapters, tier = resolve_verbose(out_dir, config)
    assert tier == TIER_NATURAL_SORT
    assert len(chapters) == 1
    assert chapters[0].path == md_path


def test_import_pdf_writes_minimal_book_yaml_with_english_language(tmp_path: Path):
    pdf_path = tmp_path / "book.pdf"
    pdf_path.write_bytes(_make_minimal_pdf(["Hello world."]))
    out_dir = tmp_path / "corpus"

    import_pdf(pdf_path, out_dir)

    import yaml

    data = yaml.safe_load((out_dir / "book.yaml").read_text(encoding="utf-8"))
    assert data == {"language": "en"}


def test_import_pdf_title_override_sets_filename_and_heading(tmp_path: Path):
    pdf_path = tmp_path / "book.pdf"
    pdf_path.write_bytes(_make_minimal_pdf(["Hello world."]))
    out_dir = tmp_path / "corpus"

    md_path = import_pdf(pdf_path, out_dir, title="My Custom Title")

    assert md_path.name == "My Custom Title.md"
    assert md_path.read_text(encoding="utf-8").startswith("# My Custom Title\n\n")


def test_import_pdf_reads_two_column_layout_in_correct_order(tmp_path: Path):
    """회귀 대상: y좌표 대역만으로 줄을 재구성하면(pypdf layout 모드) 2단
    레이아웃에서 같은 높이의 좌/우 컬럼 텍스트가 한 줄에 섞여버렸다 — 왼쪽
    컬럼을 위→아래로 다 읽은 뒤 오른쪽 컬럼으로 넘어가는 순서가 맞아야 한다."""
    placements = [
        ("Left column line one.", 72, 700), ("Right column line one.", 320, 700),
        ("Left column line two.", 72, 680), ("Right column line two.", 320, 680),
        ("Left column line three.", 72, 660), ("Right column line three.", 320, 660),
    ]
    pdf_path = tmp_path / "two_col.pdf"
    pdf_path.write_bytes(_make_positioned_pdf(placements))
    out_dir = tmp_path / "corpus"

    md_path = import_pdf(pdf_path, out_dir)
    body = md_path.read_text(encoding="utf-8")

    left_end = body.index("Left column line three.")
    right_start = body.index("Right column line one.")
    assert left_end < right_start, "왼쪽 컬럼을 다 읽기 전에 오른쪽 컬럼 내용이 먼저 나옴"


def test_import_pdf_renders_genuine_table_as_markdown(tmp_path: Path):
    data = [["Name", "Age", "City"], ["Alice", "30", "Seoul"], ["Bob", "25", "Busan"], ["Carol", "40", "Incheon"]]
    placements = [(cell, x, 700 - i * 20) for i, row in enumerate(data) for cell, x in zip(row, (72, 250, 400))]
    pdf_path = tmp_path / "table.pdf"
    pdf_path.write_bytes(_make_positioned_pdf(placements))
    out_dir = tmp_path / "corpus"

    md_path = import_pdf(pdf_path, out_dir)
    body = md_path.read_text(encoding="utf-8")

    assert "| Name" in body
    assert "| Alice" in body
    assert "-" * 3 in body  # 헤더 구분선(| --- | --- |)이 있어야 유효한 마크다운 표
