"""html_book.build_html()의 회귀 테스트 — 특히 섹션 id 충돌 회피."""

import unicodedata
from pathlib import Path

from mdbook_binder.html_book import _slugify, build_html
from mdbook_binder.manifest import BookConfig, SplitConfig


def _write(root: Path, rel: str, content: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


class TestSlugify:
    def test_mixed_korean_and_ascii_title_is_not_reduced_to_ascii_only(self):
        """회귀 대상: 예전 구현은 문자열 전체를 한 번에 ASCII로 치환 시도해,
        제목에 영문 단어가 하나라도 섞여 있으면(예: "AI") 그 잔여물만 남기고
        한글 전체를 버렸다("실전 AI 에이전트 하네스 엔지니어링" → "ai")."""
        slug = _slugify("실전 AI 에이전트 하네스 엔지니어링")
        assert slug == "실전-ai-에이전트-하네스-엔지니어링"

    def test_pure_korean_title_preserved(self):
        assert _slugify("나의 책") == "나의-책"

    def test_pure_ascii_title_lowercased_and_hyphenated(self):
        assert _slugify("My Book Title") == "my-book-title"

    def test_diacritics_romanized(self):
        assert _slugify("Café du matin") == "cafe-du-matin"

    def test_empty_or_symbol_only_falls_back_to_section(self):
        assert _slugify("") == "section"
        assert _slugify("!!!") == "section"


def test_duplicate_chapter_titles_get_distinct_section_ids(tmp_path: Path):
    """서로 다른 Part의 챕터 제목이 우연히 같아도(예: "개요") id가 충돌하면 안 된다.

    회귀 대상: 예전엔 두 섹션 모두 <section id="개요">가 되어 TOC 링크가 항상
    첫 번째 섹션으로만 이동했다.
    """
    _write(tmp_path, "Part_I_A/Chapter_01_x.md", "# 개요\n\nPart I의 개요.\n")
    _write(tmp_path, "Part_II_B/Chapter_01_y.md", "# 개요\n\nPart II의 개요.\n")

    out = build_html(tmp_path, config=None, out_path=tmp_path / "out.html")
    html = out.read_text(encoding="utf-8")

    ids = [seg.split('"')[0] for seg in html.split('<section class="chapter-section" id="')[1:]]
    assert len(ids) == len(set(ids)), f"중복 id 발견: {ids}"


def test_build_html_creates_one_section_per_chapter(tmp_path: Path):
    _write(tmp_path, "a.md", "# A\n\n본문 A.\n")
    _write(tmp_path, "b.md", "# B\n\n본문 B.\n")

    out = build_html(tmp_path, config=None, out_path=tmp_path / "out.html")
    html = out.read_text(encoding="utf-8")

    assert html.count('class="chapter-section"') == 2


def test_image_embedded_as_base64_data_uri(tmp_path: Path):
    img_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
        "53de0000000c4944415408d763f8cfc0c0c00000030101006bec38ba0000000049454e44ae426082"
    )
    _write(tmp_path, "images/pic.png", "")
    (tmp_path / "images" / "pic.png").write_bytes(img_bytes)
    _write(tmp_path, "chapter.md", "# 챕터\n\n![그림](./images/pic.png)\n")

    out = build_html(tmp_path, config=None, out_path=tmp_path / "out.html")
    html = out.read_text(encoding="utf-8")

    assert 'src="data:image/png;base64,' in html
    assert "images/pic.png" not in html.replace('src="data:image/png;base64,', "")


def test_cross_chapter_md_link_rewritten_to_anchor(tmp_path: Path):
    """다른 챕터를 가리키는 상대경로 .md 링크는 같은 HTML 안의 #앵커로 바뀌어야 한다.

    회귀 대상: 예전엔 [본문](../Part_II_B/Chapter_02_y.md) 같은 상호참조가 그대로
    남아, HTML 단독으로 열었을 때 존재하지 않는 로컬 파일을 가리키는 죽은 링크가 됐다.
    앞쪽 챕터가 뒤쪽 챕터를 참조하는 순방향 케이스까지 확인한다(2패스 구조의 핵심).
    """
    _write(
        tmp_path,
        "Part_I_A/Chapter_01_x.md",
        "# X\n\n뒤쪽 챕터를 가리키는 [링크](../Part_II_B/Chapter_02_y.md) 있음.\n",
    )
    _write(
        tmp_path,
        "Part_II_B/Chapter_02_y.md",
        "# Y\n\n앞쪽 챕터로 [역참조](../Part_I_A/Chapter_01_x.md).\n",
    )

    out = build_html(tmp_path, config=None, out_path=tmp_path / "out.html")
    html = out.read_text(encoding="utf-8")
    body = html.split('<main id="main">', 1)[1]

    assert '<a href="#y"' in body, "순방향 참조(X→Y)가 앵커로 바뀌지 않음"
    assert '<a href="#x"' in body, "역방향 참조(Y→X)가 앵커로 바뀌지 않음"
    assert ".md" not in body


def test_link_to_excluded_file_left_unchanged(tmp_path: Path):
    """빌드 코퍼스 밖의 .md(예: Skills/ 같은 의도적 제외 디렉토리)를 가리키는 링크는
    앵커로 바꿀 대상이 없으므로 원본 href를 그대로 유지해야 한다."""
    _write(
        tmp_path, "Part_I_A/Chapter_01_x.md", "# X\n\n[스킬 템플릿](../Skills/foo/SKILL.md) 참고.\n"
    )
    _write(tmp_path, "Skills/foo/SKILL.md", "skill 파일 — 챕터 코퍼스 밖")

    out = build_html(tmp_path, config=None, out_path=tmp_path / "out.html")
    html = out.read_text(encoding="utf-8")

    assert 'href="../Skills/foo/SKILL.md"' in html


def test_external_and_fragment_only_links_untouched(tmp_path: Path):
    _write(
        tmp_path,
        "chapter.md",
        "# 챕터\n\n[외부](https://example.com/x.md) · [내부북마크](#어딘가)\n",
    )

    out = build_html(tmp_path, config=None, out_path=tmp_path / "out.html")
    html = out.read_text(encoding="utf-8")

    assert 'href="https://example.com/x.md"' in html


def test_cross_chapter_link_survives_nfc_nfd_mismatch(tmp_path: Path):
    """한글 파일명이 걸린 상호참조는 NFC/NFD 정규화형이 달라도 앵커로 바뀌어야 한다.

    회귀 대상: macOS(APFS)는 파일명을 디스크에 NFD(분해형, "의" = ㅇ+ㅢ 2코드포인트)로
    저장하지만, 마크다운 본문 안의 링크 텍스트는 보통 에디터가 저장한 NFC(조합형,
    "의" = 1코드포인트)다. 두 정규화형을 맞추지 않고 dict 키로 비교하면 `.resolve()`를
    거쳐도 바이트가 달라 조회가 항상 실패해, 파일이 실제로 존재하는데도 원본 .md
    href가 그대로 남는다.

    Linux/Windows는 파일명을 정규화 없이 받은 바이트 그대로 저장하므로, 그냥 NFC
    문자열로 파일을 쓰면 이 불일치 자체가 재현되지 않는다(macOS에서만 우연히
    재현되는 테스트가 되어버린다) — 그래서 대상 파일명을 `rename()`으로 **명시적으로**
    NFD로 바꿔, 이 프로젝트에 CI가 아직 없어 어느 OS에서 돌든 결정적으로 같은
    조건을 재현하게 만든다. 링크 텍스트는 그대로 NFC로 남겨 실제 버그 조건(파일명
    NFD ↔ 링크 텍스트 NFC)을 정확히 재현한다.
    """
    _write(
        tmp_path,
        "Part_I_A/Chapter_01_x.md",
        "# X\n\n[다음](../Part_II_B/Chapter_02_정의.md) 챕터.\n",
    )
    nfc_target = _write(tmp_path, "Part_II_B/Chapter_02_정의.md", "# Y\n\n본문.\n")
    nfd_name = unicodedata.normalize("NFD", nfc_target.name)
    assert nfd_name != nfc_target.name, "테스트 전제 오류: 이 이름엔 애초에 NFC/NFD 차이가 없음"
    nfd_target = nfc_target.with_name(nfd_name)
    nfc_target.rename(nfd_target)

    out = build_html(tmp_path, config=None, out_path=tmp_path / "out.html")
    html = out.read_text(encoding="utf-8")
    body = html.split('<main id="main">', 1)[1]

    assert '<a href="#' in body, "한글 파일명 상호참조가 앵커로 바뀌지 않음(NFC/NFD 불일치 회귀)"
    assert ".md" not in body


class TestSplitConfigBuildsMultipleSections:
    def test_single_file_with_h2s_produces_one_section_per_chapter(self, tmp_path: Path):
        """import 직후 같은 단일 파일도 book.yaml split.files만 지정하면
        파일을 물리적으로 쪼개지 않고도 사이드바 섹션이 챕터 수만큼 생겨야 한다."""
        _write(
            tmp_path,
            "My_Book.md",
            "# 나의 책\n\n## 1장 서론\n\n1장 본문.\n\n## 2장 본론\n\n2장 본문.\n",
        )
        config = BookConfig(split=SplitConfig(files=["My_Book.md"]))

        out = build_html(tmp_path, config, out_path=tmp_path / "out.html")
        html = out.read_text(encoding="utf-8")

        assert html.count('class="chapter-section"') == 2
        assert "1장 본문." in html
        assert "2장 본문." in html

    def test_split_chapters_grouped_under_original_title_as_part_heading(self, tmp_path: Path):
        _write(
            tmp_path,
            "My_Book.md",
            "# 나의 책\n\n## 1장\n\n본문1.\n\n## 2장\n\n본문2.\n",
        )
        config = BookConfig(split=SplitConfig(files=["My_Book.md"]))

        out = build_html(tmp_path, config, out_path=tmp_path / "out.html")
        html = out.read_text(encoding="utf-8")
        toc = html.split('id="toc">', 1)[1].split("</div>", 1)[0]

        assert '<a class="part-heading">나의 책</a>' in toc
        assert toc.count('class="chapter toc-link"') == 2

    def test_file_without_matching_heading_level_is_not_split(self, tmp_path: Path):
        """split.files에 등록됐어도 H2가 없으면 기존(파일 1개=섹션 1개) 그대로다."""
        _write(tmp_path, "My_Book.md", "# 나의 책\n\n헤딩 없이 본문만 있음.\n")
        config = BookConfig(split=SplitConfig(files=["My_Book.md"]))

        out = build_html(tmp_path, config, out_path=tmp_path / "out.html")
        html = out.read_text(encoding="utf-8")

        assert html.count('class="chapter-section"') == 1

    def test_file_not_listed_in_split_files_is_unaffected(self, tmp_path: Path):
        """다른 코퍼스에 정상적으로 존재하는 파일이 split 대상이 아니면
        내부에 H2가 있어도(정상적인 챕터 내 소제목) 쪼개지지 않는다."""
        _write(
            tmp_path,
            "chapter.md",
            "# 챕터\n\n## 소제목\n\n일반적인 챕터 내부 구조.\n",
        )
        config = BookConfig(split=SplitConfig(files=["other.md"]))

        out = build_html(tmp_path, config, out_path=tmp_path / "out.html")
        html = out.read_text(encoding="utf-8")

        assert html.count('class="chapter-section"') == 1

    def test_no_split_config_leaves_default_behavior_unchanged(self, tmp_path: Path):
        _write(tmp_path, "chapter.md", "# 챕터\n\n## 소제목\n\n본문.\n")

        out = build_html(tmp_path, config=None, out_path=tmp_path / "out.html")
        html = out.read_text(encoding="utf-8")

        assert html.count('class="chapter-section"') == 1

    def test_cross_reference_to_split_file_anchors_to_first_piece(self, tmp_path: Path):
        _write(
            tmp_path,
            "My_Book.md",
            "# 나의 책\n\n## 1장\n\n본문1.\n\n## 2장\n\n본문2.\n",
        )
        _write(tmp_path, "other.md", "# 다른 챕터\n\n[나의 책](./My_Book.md) 참고.\n")
        config = BookConfig(split=SplitConfig(files=["My_Book.md"]))

        out = build_html(tmp_path, config, out_path=tmp_path / "out.html")
        html = out.read_text(encoding="utf-8")
        body = html.split('<main id="main">', 1)[1]

        assert '<a href="#1장"' in body
