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

    deleted_ids = html_editor.deleted_section_ids()

    def _in_deleted_section(img_info: dict) -> bool:
        sec = img_info["tag"].find_parent("section")
        return bool(sec and sec.get("id") in deleted_ids)

    src_map: dict = defaultdict(list)
    for tag in updated_soup.find_all("img"):
        src_map[tag.get("src", "")].append(tag)
    for img_info in image_editor.images:
        if _in_deleted_section(img_info):
            continue
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

    def test_authored_image_survives_noop_save(self, tmp_path: Path):
        """회귀 테스트: 저작 마크다운의 일반 `![alt](src)` 이미지는 빌드되면
        `<figure>`가 아니라 `<p><img></p>`가 된다(render.py 참고) — `<figure>`
        제거 뒤 남은 모든 `<img>`를 지우던 예전 로직이 이 형태의 이미지까지
        지워버려, 편집 없이 섹션을 열었다 저장만 해도 이미지가 통째로
        사라지는 회귀가 있었다(실사용 재현·확인됨)."""
        (tmp_path / "images").mkdir()
        (tmp_path / "images" / "pic.png").write_bytes(_PNG_BYTES)
        _write(tmp_path, "chapter.md", "# 챕터\n\n문단1.\n\n![그림](images/pic.png)\n\n문단2.\n")
        html_path = build_html(tmp_path, config=None, out_path=tmp_path / "book.html")

        sec_id = BookHTMLEditor(str(html_path)).get_book_meta()["sections"][0]["id"]
        ed = self._round_trip(html_path, sec_id)
        sec = ed.soup.find("section", id=sec_id)
        assert sec is not None

        img = sec.find("img")
        assert img is not None
        src = img.get("src", "")
        assert isinstance(src, str) and src.startswith("data:image/png;base64,")
        # 이미지 앞뒤 문단도 그대로 남아있어야 한다(위치 관계까지 보존).
        assert "문단1" in sec.get_text()
        assert "문단2" in sec.get_text()

    def test_figure_added_image_still_excluded_from_editable_markdown(self, tmp_path: Path):
        """일반 이미지는 이제 편집 가능한 마크다운에 포함되지만, 에디터의
        "이미지 추가" 기능이 붙인 `<figure>` 이미지는 여전히 편집 대상에서
        제외돼 별도로 보존돼야 한다(get_section_content()의 markdown에
        섞여 들어가 중복 저장되면 안 됨)."""
        _write(tmp_path, "chapter.md", "# 챕터\n\n본문.\n")
        html_path = build_html(tmp_path, config=None, out_path=tmp_path / "book.html")

        ed = BookHTMLEditor(str(html_path))
        sec_id = ed.get_book_meta()["sections"][0]["id"]
        (tmp_path / "added.png").write_bytes(_PNG_BYTES)
        assert ed.stage_add_image(sec_id, str(tmp_path / "added.png"), "캡션")
        ed.apply_all_changes()

        content = ed.get_section_content(sec_id)
        assert "캡션" not in content["markdown"]  # figure는 편집 마크다운에 안 섞임

        sec = ed.soup.find("section", id=sec_id)
        assert sec is not None
        assert sec.find("figure") is not None  # 그래도 섹션엔 실제로 붙어 있음

    def test_diagram_and_tip_box_keep_their_original_position_on_noop_save(
        self, tmp_path: Path
    ):
        """회귀 테스트: 다이어그램·콜아웃은 편집용 마크다운에 안 보이므로
        예전엔 저장할 때마다(수정 여부와 무관하게) 무조건 섹션 맨 끝으로
        밀려났다(실사용 재현·확인됨) — "문단1 → 다이어그램 → 문단2 →
        콜아웃 → 문단3" 구조가 저장 후 "문단1 → 문단2 → 문단3 → 다이어그램
        → 콜아웃"이 됐다. 이제는 원래 있던 자리 근처에 그대로 남아야 한다."""
        _write(
            tmp_path,
            "chapter.md",
            "# 챕터\n\n문단1입니다.\n\n"
            "```mermaid\ngraph TD\nA-->B\n```\n\n문단2입니다.\n\n"
            "> \U0001f4a1 콜아웃입니다.\n\n문단3입니다.\n",
        )
        config = BookConfig(tip_markers=["\U0001f4a1"])
        html_path = build_html(tmp_path, config=config, out_path=tmp_path / "book.html")

        sec_id = BookHTMLEditor(str(html_path)).get_book_meta()["sections"][0]["id"]
        ed = self._round_trip(html_path, sec_id)
        sec = ed.soup.find("section", id=sec_id)
        assert sec is not None

        order = [
            c.name if c.name != "div" else next(iter(c.get("class") or []), "div")
            for c in sec.find_all(recursive=False)
        ]
        assert order == ["h2", "p", "mermaid", "p", "tip-box", "p"]

    def test_adjacent_diagrams_keep_relative_order_on_noop_save(self, tmp_path: Path):
        """회귀 테스트: 문단 없이 바로 인접한 보존 요소(다이어그램 두 개
        연달아 등)가 있으면, 위치 복원 로직이 이미 삽입한 보존 요소를 새
        콘텐츠의 일반 태그로 잘못 세어 두 요소의 상대 순서가 뒤바뀌었다
        (실사용 재현·확인됨) — "다이어그램A → 다이어그램B"가 저장 후
        "다이어그램B → 다이어그램A"가 됐다."""
        _write(
            tmp_path,
            "chapter.md",
            "# 챕터\n\n문단1입니다.\n\n"
            "```mermaid\ngraph TD\nA-->B\n```\n\n"
            "```mermaid\ngraph TD\nC-->D\n```\n\n문단2입니다.\n",
        )
        html_path = build_html(tmp_path, config=None, out_path=tmp_path / "book.html")

        sec_id = BookHTMLEditor(str(html_path)).get_book_meta()["sections"][0]["id"]
        ed = self._round_trip(html_path, sec_id)
        sec = ed.soup.find("section", id=sec_id)
        assert sec is not None

        diagrams = [d.get_text(strip=True) for d in sec.find_all("div", class_="mermaid")]
        assert len(diagrams) == 2
        assert "AB" in diagrams[0] and "CD" not in diagrams[0]
        assert "CD" in diagrams[1] and "AB" not in diagrams[1]


class TestDeleteSection:
    def test_cross_reference_from_other_chapter_survives_as_plain_text(self, tmp_path: Path):
        """회귀 테스트: 섹션 삭제는 사이드바 TOC 항목만 지우고, 다른 챕터
        본문에 남은 `#섹션id` 상호참조 링크(html_book.py의
        _rewrite_internal_links()가 만든 것)는 그대로 뒀다 — 삭제된 챕터를
        가리키던 링크가 아무 데도 안 가는 죽은 링크로 남는 회귀였다(실사용
        재현·확인됨). 링크는 사라지되 문장은 그대로 읽혀야 한다."""
        _write(tmp_path, "01_a.md", "# A\n\n[다음 챕터](02_b.md) 참고.\n")
        _write(tmp_path, "02_b.md", "# B\n\n본문입니다.\n")
        html_path = build_html(tmp_path, config=None, out_path=tmp_path / "book.html")

        ed = BookHTMLEditor(str(html_path))
        assert ed.delete_section("b")
        ed.apply_all_changes()

        sec_a = ed.soup.find("section", id="a")
        assert sec_a is not None
        assert ed.soup.find_all("a", href="#b") == []  # 죽은 링크 제거됨
        assert "다음 챕터 참고" in sec_a.get_text()  # 텍스트는 그대로 남음

    def test_delete_of_duplicate_src_image_does_not_hijack_surviving_images_slot(
        self, tmp_path: Path
    ):
        """회귀 테스트: 동일 src(바이트가 같은 이미지 파일)를 가진 두 이미지
        중 하나가 속한 섹션이 삭제되면, server.py의 /api/save가 src 문자열
        만으로 image_editor 항목을 재매핑하는 로직에서 삭제된 섹션의 이미지
        항목이 먼저 자리를 채가 살아있는 다른(같은 src) 이미지의 교체 요청이
        조용히 무시되던 문제(실사용 재현·확인됨)."""
        (tmp_path / "images").mkdir()
        (tmp_path / "images" / "icon.png").write_bytes(_PNG_BYTES)
        _write(tmp_path, "01_a.md", "# A\n\n![아이콘](./images/icon.png)\n")
        _write(tmp_path, "02_b.md", "# B\n\n![아이콘](./images/icon.png)\n")
        html_path = build_html(tmp_path, config=None, out_path=tmp_path / "book.html")

        html_editor = BookHTMLEditor(str(html_path))
        image_editor = ImageEditor(str(html_path))
        assert len(image_editor.images) == 2
        assert image_editor.images[0]["src"] == image_editor.images[1]["src"]

        assert html_editor.delete_section("a")  # 첫 번째(index 1) 이미지가 속한 섹션 삭제

        new_png = bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
            "de0000000c4944415408d763fcffff3f0305fe02fea1399ff10000000049454e44ae426082"
        )
        (tmp_path / "new.png").write_bytes(new_png)
        assert image_editor.replace_image(2, str(tmp_path / "new.png"))  # 남은(index 2) 이미지 교체

        out = tmp_path / "out.html"
        _apply_and_save(html_editor, image_editor, out)

        saved = out.read_text(encoding="utf-8")
        assert saved.count("<img") == 1  # 섹션 A가 삭제됐으니 이미지도 하나만 남음
        import base64

        assert base64.b64encode(new_png).decode("ascii") in saved  # 남은 이미지가 실제로 교체됨
