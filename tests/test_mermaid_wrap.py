"""mermaid_wrap.auto_wrap_long_labels()의 회귀 테스트."""

from mdbook_binder.mermaid_wrap import _wrap_label, auto_wrap_long_labels
from mdbook_binder.render import md_to_html, tip_start_pattern


def test_short_label_untouched():
    code = 'graph TD\nA{"짧은 질문"} --> B["foo"]'
    assert auto_wrap_long_labels(code) == code


def test_long_korean_label_gets_br():
    code = 'graph TD\nA{"이 함수가 부작용을일으키는가? (D.1 질문 5)"} --> B["foo"]'
    out = auto_wrap_long_labels(code)
    assert "\\n" in out
    assert 'B["foo"]' in out  # 짧은 라벨은 그대로


def test_no_space_run_hard_splits():
    code = 'graph TD\nA{"' + "가" * 40 + '"}'
    out = auto_wrap_long_labels(code)
    assert "\\n" in out
    # 줄바꿈 태그를 빼면 원래 글자 수가 보존돼야 한다
    assert out.replace("\\n", "").count("가") == 40


def test_existing_br_tag_normalized_to_backslash_n():
    # 저자가 직접 써넣은 <br/>은 그대로 두면 안 된다 — div.get_text()/textContent가
    # 실제 <br> 엘리먼트를 통째로 삼켜버려 양옆 글자가 공백 없이 들러붙는다.
    code = 'graph TD\nA{"이미<br/>줄바꿈됨 아주 아주 아주 긴 라벨입니다"} --> B'
    out = auto_wrap_long_labels(code)
    assert "<br" not in out.lower()
    assert "\\n" in out


def test_already_backslash_n_wrapped_label_untouched():
    code = 'graph TD\nA{"이미\\n줄바꿈됨 아주 아주 아주 긴 라벨입니다"} --> B'
    assert auto_wrap_long_labels(code) == code


def test_url_label_untouched():
    code = 'graph TD\nA["https://example.com/very/long/path/that/exceeds/threshold"]'
    assert auto_wrap_long_labels(code) == code


def test_subgraph_title_untouched_even_when_long():
    # subgraph 제목은 줄바꿈 대상에서 제외한다 — Mermaid가 제목 위 세로 여백을
    # 한 줄 기준 고정값으로 계산해, 여기서 두 줄로 접으면 둘째 줄이 서브그래프
    # 첫 자식 노드와 겹쳐 보인다(모듈 docstring 참고).
    code = (
        "flowchart TB\n"
        '    subgraph Session1["세션 1 — book-forge draft 아주 아주 긴 제목입니다"]\n'
        '        D["짧은 라벨"]\n'
        "    end\n"
    )
    out = auto_wrap_long_labels(code)
    assert 'subgraph Session1["세션 1 — book-forge draft 아주 아주 긴 제목입니다"]' in out


def test_node_label_inside_subgraph_still_wraps():
    code = (
        "flowchart TB\n"
        '    subgraph S["짧은 제목"]\n'
        '        D{"이 함수가 부작용을일으키는가? (D.1 질문 5)"}\n'
        "    end\n"
    )
    out = auto_wrap_long_labels(code)
    assert "\\n" in out


def test_long_identifier_breaks_at_camel_case_not_mid_word():
    # camelCase 경계("Drafter"/"Agent")에서 접어야지, 폭 한도에 걸린 자리에서
    # 무조건 잘라 "ChapterDrafterAgen"/"t"처럼 한 글자만 남기면 안 된다.
    out = _wrap_label("ChapterDrafterAgent")
    lines = out.split("\\n")
    assert all(len(line) > 1 for line in lines)
    assert out.replace("\\n", "") == "ChapterDrafterAgent"


def test_long_path_breaks_at_separator_not_mid_word():
    out = _wrap_label("knowledge/store.json")
    lines = out.split("\\n")
    assert all(len(line) > 1 for line in lines)
    assert out.replace("\\n", "") == "knowledge/store.json"


def test_long_call_with_parens_keeps_parens_together():
    # "query_with_scores(" / ")"처럼 여는 괄호와 닫는 괄호가 갈라지면 안 된다.
    out = _wrap_label("query_with_scores()")
    assert ")" not in out.split("\\n")[0] or "(" not in out.split("\\n")[0]
    for line in out.split("\\n"):
        assert line.count("(") == line.count(")")


def test_md_to_html_wraps_mermaid_source():
    md = (
        "# Title\n\n"
        "```mermaid\n"
        'graph TD\nA{"이 함수가 부작용을일으키는가? (D.1 질문 5)"} --> B["foo"]\n'
        "```\n"
    )
    out = md_to_html(md, tip_start_pattern([]))
    assert "\\n" in out
    assert 'class="mermaid"' in out
