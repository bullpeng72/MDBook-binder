"""chapter_split.split_chapter_markdown()의 헤딩 경계 탐지·분할 검증."""

from mdbook_binder.chapter_split import find_heading_boundaries, split_chapter_markdown


def test_no_boundary_returns_text_unchanged():
    text = "# 제목\n\n본문만 있고 H2는 없음.\n"
    assert split_chapter_markdown(text) == [text]


def test_splits_at_each_h2_and_promotes_to_h1():
    text = "# 책 제목\n\n## 1장\n\n1장 본문.\n\n## 2장\n\n2장 본문.\n"

    pieces = split_chapter_markdown(text)

    assert len(pieces) == 2
    assert pieces[0].startswith("# 1장")
    assert "1장 본문." in pieces[0]
    assert pieces[1].startswith("# 2장")
    assert "2장 본문." in pieces[1]


def test_empty_intro_before_first_heading_is_dropped():
    """H1 바로 다음에 첫 H2가 오면(도입부가 사실상 빈 경우) 첫 조각을 만들지 않는다."""
    text = "# 책 제목\n\n## 1장\n\n본문.\n"

    pieces = split_chapter_markdown(text)

    assert len(pieces) == 1
    assert pieces[0].startswith("# 1장")


def test_meaningful_intro_before_first_heading_kept_as_own_piece():
    text = "# 책 제목\n\n이 책은 이런 내용을 다룬다.\n\n## 1장\n\n본문.\n"

    pieces = split_chapter_markdown(text)

    assert len(pieces) == 2
    assert pieces[0].startswith("# 책 제목")
    assert "이 책은 이런 내용을 다룬다." in pieces[0]
    assert pieces[1].startswith("# 1장")


def test_h2_inside_fenced_code_block_is_not_a_boundary():
    text = "# 제목\n\n## 진짜 1장\n\n```markdown\n## 가짜 헤딩(코드 예제)\n```\n\n본문.\n"

    boundaries = find_heading_boundaries(text, level=2)

    assert len(boundaries) == 1
    lines = text.split("\n")
    assert lines[boundaries[0]] == "## 진짜 1장"


def test_h2_inside_raw_html_block_is_not_a_boundary():
    text = "# 제목\n\n## 진짜 1장\n\n@@HTML_START@@\n## 가짜 헤딩\n@@HTML_END@@\n\n본문.\n"

    boundaries = find_heading_boundaries(text, level=2)

    assert len(boundaries) == 1


def test_h2_inside_blockquote_code_fence_is_not_a_boundary():
    text = "# 제목\n\n## 진짜 1장\n\n> ```text\n> ## 가짜 헤딩\n> ```\n\n본문.\n"

    boundaries = find_heading_boundaries(text, level=2)

    assert len(boundaries) == 1


def test_h3_does_not_match_h2_boundary():
    text = "# 제목\n\n## 1장\n\n### 소제목\n\n본문.\n"

    boundaries = find_heading_boundaries(text, level=2)

    assert len(boundaries) == 1


def test_nested_heading_inside_piece_is_left_untouched():
    """분할 경계 자체(H2→H1)만 승격하고, 조각 안의 하위 헤딩(H3 등)은 그대로 둔다."""
    text = "# 제목\n\n## 1장\n\n### 1.1절\n\n본문.\n"

    pieces = split_chapter_markdown(text)

    assert pieces[0].startswith("# 1장")
    assert "### 1.1절" in pieces[0]
