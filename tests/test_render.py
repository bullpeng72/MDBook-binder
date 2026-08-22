"""render.py의 마크다운→HTML 변환 회귀 테스트.

html_book.py/pdf_book.py/chapter_split.py가 전부 이 모듈의 블록 보호 규칙
(HTML_BLOCK_RE/MERMAID_FENCE_RE/BQ_CODE_FENCE_RE)을 "이미 검증된 규칙"으로
전제하고 재사용하는데, 정작 그 규칙 자체(세 정규식의 상호작용, 콜아웃 분리
로직)를 직접 검증하는 테스트가 지금까지 없었다 — blockquote 안 Mermaid
다이어그램이 깨지는 회귀(실사용 재현·확인됨)가 이 공백 때문에 발견되지
않았던 사례를 계기로 신설한다.
"""

from mdbook_binder.render import demote_headings, extract_h1_text, md_to_html, tip_start_pattern

_NO_TIP = tip_start_pattern([])


class TestMermaidFence:
    def test_top_level_mermaid_renders_as_diagram_div(self):
        html = md_to_html("```mermaid\ngraph LR\nA-->B\n```\n", _NO_TIP)
        assert '<div class="mermaid"' in html
        assert "graph LR" in html

    def test_mermaid_inside_blockquote_does_not_corrupt_diagram(self):
        """회귀 테스트: 줄 시작(^) 앵커링 없이 "```mermaid"만 찾던 예전
        정규식은 blockquote 안의 "> ```mermaid"에서도 매칭돼, "> " 접두어가
        섞인 채로 다이어그램 소스가 깨지고 BQ_CODE_FENCE_RE보다 먼저
        낚아채가 콜아웃 코드펜스 처리 자체를 무력화했다(실사용 재현·확인됨).
        지금은 blockquote 안 코드펜스로 처리돼(BQ_CODE_FENCE_RE), 최소한
        "> " 접두어가 다이어그램 소스에 섞이는 파손은 없어야 한다."""
        text = "> quote\n> ```mermaid\n> graph TD\n> A-->B\n> ```\n> more\n"
        html = md_to_html(text, _NO_TIP)
        assert "> graph TD" not in html
        assert "> A" not in html
        assert "graph TD" in html
        assert "A-->B" in html or "A--&gt;B" in html

    def test_mermaid_followed_by_paragraph_stays_separate(self):
        """닫는 펜스 뒤 공백을 [ \\t]*(줄 안 공백만)로 끊어야 다음 문단과의
        빈 줄 구분선이 살아남는다 — \\s*였다면 개행까지 먹어치워 둘이
        <br>로 이어붙은 한 문단이 돼버리는 회귀가 있었다."""
        text = "```mermaid\ngraph TD\nA-->B\n```\n\n다음 문단.\n"
        html = md_to_html(text, _NO_TIP)
        assert "<p>다음 문단.</p>" in html


class TestBlockquoteCodeFence:
    def test_code_fence_inside_blockquote_becomes_pre_code(self):
        text = "> ```python\n> def f():\n>     pass\n> ```\n"
        html = md_to_html(text, _NO_TIP)
        assert '<pre><code class="language-python">' in html
        assert "def f():" in html

    def test_fence_language_defaults_to_no_class_when_absent(self):
        text = "> ```\n> plain text\n> ```\n"
        html = md_to_html(text, _NO_TIP)
        assert "<pre><code>" in html


class TestTipBoxSplitting:
    def test_marked_blockquote_becomes_tip_box(self):
        pattern = tip_start_pattern(["\U0001f4a1"])
        html = md_to_html("> \U0001f4a1 콜아웃입니다.\n", pattern)
        assert '<div class="tip-box">' in html
        assert "콜아웃입니다" in html

    def test_unmarked_blockquote_stays_plain(self):
        pattern = tip_start_pattern(["\U0001f4a1"])
        html = md_to_html("> 그냥 인용문입니다.\n", pattern)
        assert "<blockquote>" in html
        assert "tip-box" not in html


class TestTableWrap:
    def test_table_wrapped_in_div(self):
        text = "| a | b |\n|---|---|\n| 1 | 2 |\n"
        html = md_to_html(text, _NO_TIP)
        assert '<div class="table-wrap">' in html
        assert "<table>" in html


class TestDemoteHeadings:
    def test_h1_becomes_h2(self):
        assert demote_headings("<h1>T</h1>") == "<h2>T</h2>"

    def test_closing_tag_also_demoted(self):
        assert demote_headings("<h2>A</h2>") == "<h3>A</h3>"

    def test_h4_becomes_h5_and_stops(self):
        assert demote_headings("<h4>A</h4>") == "<h5>A</h5>"


class TestExtractH1Text:
    def test_extracts_plain_text_stripping_tags(self):
        assert extract_h1_text('<h1 id="x">Hello <em>World</em></h1>') == "Hello World"

    def test_returns_none_when_absent(self):
        assert extract_h1_text("<p>no heading here</p>") is None
