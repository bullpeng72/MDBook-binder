"""pdf_import.py의 텍스트 정리(순수 함수) + PDF→코퍼스 추출 회귀 테스트."""

from pathlib import Path

from mdbook_binder.manifest import TIER_NATURAL_SORT, resolve_verbose
from mdbook_binder.pdf_import import clean_paragraphs, import_pdf


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
