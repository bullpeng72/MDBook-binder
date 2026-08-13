"""pdf_import.py의 텍스트 정리(순수 함수) + PDF→코퍼스 추출 회귀 테스트."""

from pathlib import Path

from mdbook_binder.manifest import TIER_NATURAL_SORT, resolve_verbose
from mdbook_binder.pdf_import import clean_paragraphs, import_pdf, strip_repeated_lines


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
