"""editor/ 이미지 추가·교체 후 저장 시 base64 임베드 회귀 테스트.

과거엔 img src에 파일 경로를 그대로 써서, 편집기로 저장한 HTML이 최초
빌드본과 달리 이미지 폴더에 의존하는 문제가 있었다.
"""

from collections import defaultdict
from pathlib import Path

from bs4 import BeautifulSoup

from mdbook_binder.editor.html_editor import BookHTMLEditor, export_markdown_corpus
from mdbook_binder.editor.image_editor import ImageEditor
from mdbook_binder.html_book import build_html
from mdbook_binder.manifest import BookConfig

_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415408d763f8cfc0c0c00000030101006bec38ba0000000049454e44ae426082"
)


def _write(root: Path, rel: str, content: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _apply_and_save(html_editor: BookHTMLEditor, image_editor: ImageEditor, out_path: Path) -> str:
    """server.py의 /api/save와 동일한 순서로 변경을 적용·저장한다."""
    updated_soup = html_editor.apply_all_changes()

    src_map: dict = defaultdict(list)
    for tag in updated_soup.find_all("img"):
        src_map[tag.get("src", "")].append(tag)
    for img_info in image_editor.images:
        live = src_map.get(img_info["src"], [])
        if live:
            img_info["tag"] = live.pop(0)

    image_editor.soup = updated_soup
    return image_editor.save_changes(str(out_path))


def test_added_image_embedded_as_base64(tmp_path: Path):
    (tmp_path / "new.png").write_bytes(_PNG_BYTES)
    _write(tmp_path, "chapter.md", "# 챕터\n\n본문.\n")
    html_path = build_html(tmp_path, config=None, out_path=tmp_path / "book.html")

    html_editor = BookHTMLEditor(str(html_path))
    image_editor = ImageEditor(str(html_path))
    sec_id = html_editor.get_book_meta()["sections"][0]["id"]

    assert html_editor.stage_add_image(sec_id, str(tmp_path / "new.png"), "캡션")

    out = tmp_path / "book_edited.html"
    _apply_and_save(html_editor, image_editor, out)
    html = Path(out).read_text(encoding="utf-8")

    assert 'src="data:image/png;base64,' in html
    assert str(tmp_path) not in html


def test_replaced_image_embedded_as_base64(tmp_path: Path):
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "orig.png").write_bytes(_PNG_BYTES)
    (tmp_path / "new.png").write_bytes(_PNG_BYTES)
    _write(tmp_path, "chapter.md", "# 챕터\n\n![그림](./images/orig.png)\n")
    html_path = build_html(tmp_path, config=None, out_path=tmp_path / "book.html")

    html_editor = BookHTMLEditor(str(html_path))
    image_editor = ImageEditor(str(html_path))

    assert image_editor.replace_image(1, str(tmp_path / "new.png"))

    out = tmp_path / "book_edited.html"
    _apply_and_save(html_editor, image_editor, out)
    html = Path(out).read_text(encoding="utf-8")

    assert html.count('src="data:image/png;base64,') == 1
    assert str(tmp_path) not in html


# ── export_markdown_corpus (역방향 저장: HTML → 코퍼스) ──────────────────


def test_export_markdown_writes_one_file_per_section_with_promoted_h1(tmp_path: Path):
    _write(tmp_path, "01_intro.md", "# Introduction\n\nIntro body.\n")
    _write(tmp_path, "02_setup.md", "# Setup\n\nSetup body.\n")
    html_path = build_html(tmp_path, config=None, out_path=tmp_path / "book.html")

    out_dir = tmp_path / "corpus_out"
    written = BookHTMLEditor(str(html_path)).export_markdown(out_dir)

    assert len(written) == 2
    contents = [p.read_text(encoding="utf-8") for p in written]
    assert any(c.startswith("# Introduction\n\n") and "Intro body." in c for c in contents)
    assert any(c.startswith("# Setup\n\n") and "Setup body." in c for c in contents)
    assert (out_dir / "book.yaml").exists()


def test_export_markdown_reflects_staged_edit_and_skips_deleted_section(tmp_path: Path):
    _write(tmp_path, "01_intro.md", "# Introduction\n\nOriginal body.\n")
    _write(tmp_path, "02_setup.md", "# Setup\n\nSetup body.\n")
    html_path = build_html(tmp_path, config=None, out_path=tmp_path / "book.html")

    html_editor = BookHTMLEditor(str(html_path))
    sections = html_editor.get_book_meta()["sections"]
    intro_id = next(s["id"] for s in sections if s["title"] == "Introduction")
    setup_id = next(s["id"] for s in sections if s["title"] == "Setup")

    html_editor.update_section_content(intro_id, "Edited body.")
    html_editor.delete_section(setup_id)

    written = html_editor.export_markdown(tmp_path / "corpus_out")

    assert len(written) == 1
    text = written[0].read_text(encoding="utf-8")
    assert "Edited body." in text
    assert "Setup body." not in text


def test_export_markdown_promotes_subheadings_back_one_level(tmp_path: Path):
    """demote_headings()가 섹션 본문의 h2→h3도 함께 낮춘다 — export는 그걸
    역으로 승격해야 원래 "## 절"이 "### 절"로 어긋나지 않는다(회귀 재현됨)."""
    _write(tmp_path, "chapter.md", "# Chapter\n\nIntro.\n\n## Sub Heading\n\nBody.\n")
    html_path = build_html(tmp_path, config=None, out_path=tmp_path / "book.html")

    written = BookHTMLEditor(str(html_path)).export_markdown(tmp_path / "corpus_out")
    text = written[0].read_text(encoding="utf-8")

    assert "# Chapter\n" in text
    assert "## Sub Heading" in text
    assert "### Sub Heading" not in text


def test_export_markdown_decodes_embedded_image_to_file(tmp_path: Path):
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "pic.png").write_bytes(_PNG_BYTES)
    _write(tmp_path, "chapter.md", "# Chapter\n\n![그림](./images/pic.png)\n")
    html_path = build_html(tmp_path, config=None, out_path=tmp_path / "book.html")

    out_dir = tmp_path / "corpus_out"
    written = BookHTMLEditor(str(html_path)).export_markdown(out_dir)

    saved_images = list((out_dir / "images").glob("*.png"))
    assert len(saved_images) == 1
    text = written[0].read_text(encoding="utf-8")
    assert f"images/{saved_images[0].name}" in text


def test_export_markdown_wraps_unrendered_mermaid_as_fence(tmp_path: Path):
    soup = BeautifulSoup(
        '<section id="s1"><h2>D</h2><div class="mermaid">graph TD;\nA-->B;</div></section>',
        "html.parser",
    )

    written = export_markdown_corpus(soup, tmp_path / "corpus_out")
    text = written[0].read_text(encoding="utf-8")

    assert "```mermaid" in text
    assert "A-->B;" in text


def test_export_markdown_flattens_prerendered_mermaid_to_svg_with_note(tmp_path: Path):
    soup = BeautifulSoup(
        '<section id="s1"><h2>D</h2>'
        '<div class="mermaid" data-prerendered="true"><svg><text>A</text></svg></div>'
        "</section>",
        "html.parser",
    )
    out_dir = tmp_path / "corpus_out"

    written = export_markdown_corpus(soup, out_dir)
    text = written[0].read_text(encoding="utf-8")

    assert "```mermaid" not in text
    assert "원본 소스" in text
    saved_svgs = list((out_dir / "images").glob("*.svg"))
    assert len(saved_svgs) == 1
    assert f"images/{saved_svgs[0].name}" in text


def test_export_markdown_converts_tip_box_to_blockquote(tmp_path: Path):
    """export_markdown_corpus()는 콜아웃(tip-box)을 blockquote로 내보내야
    한다(docstring에 이미 그렇게 문서화돼 있었지만, 실제로는 markdownify가
    div를 일반 <p>로 뭉개 마커 이모지만 남고 인용 표시가 사라지는
    회귀였다)."""
    soup = BeautifulSoup(
        '<section id="s1"><h2>T</h2>'
        '<div class="tip-box"><p>\U0001f4a1 콜아웃 내용입니다.</p></div>'
        "</section>",
        "html.parser",
    )
    written = export_markdown_corpus(soup, tmp_path / "corpus_out")
    text = written[0].read_text(encoding="utf-8")

    assert "> \U0001f4a1 콜아웃 내용입니다." in text


def test_export_markdown_preserves_code_fence_language(tmp_path: Path):
    """export_markdown_corpus()는 코드 블록의 language-X 클래스를 펜스 info
    string으로 보존해야 한다 — markdownify 기본 변환은 언어 정보를 버리고
    빈 ``` 펜스만 남기는 회귀가 있었다(실사용 재현·확인됨)."""
    soup = BeautifulSoup(
        '<section id="s1"><h2>T</h2>'
        '<pre><code class="language-python">def f():\n    pass</code></pre>'
        "</section>",
        "html.parser",
    )
    written = export_markdown_corpus(soup, tmp_path / "corpus_out")
    text = written[0].read_text(encoding="utf-8")

    assert "```python" in text
    assert "def f():" in text


# ── 에디터 저장 왕복 충실도(no-op round-trip fidelity) ──────────────────


class TestEditRoundTripFidelity:
    """섹션을 열어보기만 하고(수정 없이) 그대로 저장해도 내용이 깨지면 안
    된다 — 실사용 재현: 링크·콜아웃이 통째로 사라지고, 인접한 순서/비순서
    목록이 하나로 합쳐지고, 코드 블록 언어 태그가 빠지는 네 가지 회귀가
    있었다."""

    def _round_trip(self, html_path: Path, section_id: str) -> BookHTMLEditor:
        ed = BookHTMLEditor(str(html_path))
        content = ed.get_section_content(section_id)
        ed.update_section_content(section_id, content["markdown"])
        ed.apply_all_changes()
        return ed

    def test_links_survive_noop_save(self, tmp_path: Path):
        _write(tmp_path, "01_a.md", "# A\n\n[다음](02_b.md) 링크입니다.\n")
        _write(tmp_path, "02_b.md", "# B\n\n본문.\n")
        html_path = build_html(tmp_path, config=None, out_path=tmp_path / "book.html")

        ed = self._round_trip(html_path, "a")
        sec = ed.soup.find("section", id="a")
        assert sec is not None

        link = sec.find("a")
        assert link is not None
        assert link["href"] == "#b"

    def test_tip_box_survives_noop_save(self, tmp_path: Path):
        _write(tmp_path, "chapter.md", "# 챕터\n\n> \U0001f4a1 콜아웃입니다.\n")
        config = BookConfig(tip_markers=["\U0001f4a1"])
        html_path = build_html(tmp_path, config=config, out_path=tmp_path / "book.html")

        sec_id = BookHTMLEditor(str(html_path)).get_book_meta()["sections"][0]["id"]
        ed = self._round_trip(html_path, sec_id)
        sec = ed.soup.find("section", id=sec_id)
        assert sec is not None

        tip_box = sec.find("div", class_="tip-box")
        assert tip_box is not None
        assert "콜아웃입니다" in tip_box.get_text()

    def test_adjacent_ordered_and_unordered_lists_stay_separate(self, tmp_path: Path):
        _write(
            tmp_path,
            "chapter.md",
            "# 챕터\n\n- 항목 1\n- 항목 2\n\n1. 첫번째\n2. 두번째\n",
        )
        html_path = build_html(tmp_path, config=None, out_path=tmp_path / "book.html")

        sec_id = BookHTMLEditor(str(html_path)).get_book_meta()["sections"][0]["id"]
        ed = self._round_trip(html_path, sec_id)
        sec = ed.soup.find("section", id=sec_id)
        assert sec is not None

        ul = sec.find("ul")
        ol = sec.find("ol")
        assert ul is not None
        assert ol is not None
        assert len(ul.find_all("li")) == 2
        assert len(ol.find_all("li")) == 2

    def test_code_fence_language_survives_noop_save(self, tmp_path: Path):
        _write(tmp_path, "chapter.md", "# 챕터\n\n```python\ndef f():\n    pass\n```\n")
        html_path = build_html(tmp_path, config=None, out_path=tmp_path / "book.html")

        sec_id = BookHTMLEditor(str(html_path)).get_book_meta()["sections"][0]["id"]
        ed = self._round_trip(html_path, sec_id)
        sec = ed.soup.find("section", id=sec_id)
        assert sec is not None

        code = sec.find("code")
        assert code is not None
        assert "language-python" in (code.get("class") or [])
