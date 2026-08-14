"""마크다운 → HTML 변환 엔진.

Book_forge/Book, Agent-Evaluator/Media/Book, Agent-Evaluator/Media/AOO 세 곳에
복붙되어 갈라졌던 md_to_html()/demote_headings() 로직을 한 곳으로 통합한다.
콜아웃 마커(TIP박스 인식 패턴)는 모듈 레벨 상수가 아니라 호출부에서 주입한다 —
코퍼스마다 다른 관례를 코드 수정 없이 book.yaml로 바꿀 수 있어야 하기 때문이다.
"""

from __future__ import annotations

import re
from html import escape as _html_escape

import markdown as md_lib

from mdbook_binder.mermaid_wrap import auto_wrap_long_labels

# translation.py도 재사용한다 — 번역 전 "손대면 안 되는" 블록을 가리는 경계와
# HTML 렌더링 전 마크다운 파서로부터 블록을 보호하는 경계가 동일해야 한다.
HTML_BLOCK_RE = re.compile(r"@@HTML_START@@\s*\n(.*?)@@HTML_END@@", re.DOTALL)
MERMAID_FENCE_RE = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL)
BQ_CODE_FENCE_RE = re.compile(r"^> ```(\w*)\n(.*?)^> ```\s*$", re.MULTILINE | re.DOTALL)


def tip_start_pattern(markers: list[str]) -> re.Pattern:
    """콜아웃(TIP박스) 시작 마커 패턴을 만든다.

    markers가 비어 있으면 항상 불일치하는 패턴을 반환해 모든 blockquote가
    일반 콜아웃으로만 렌더되게 한다 — 코퍼스가 이 관례를 안 쓰면 조용히
    아무 영향도 주지 않아야 한다.
    """
    if not markers:
        return re.compile(r"(?!x)x")
    return re.compile(r"^[\s]*(?:" + "|".join(re.escape(m) for m in markers) + r")")


def md_to_html(text: str, tip_pattern: re.Pattern) -> str:
    """mermaid / raw-HTML 블록을 보존하면서 markdown → HTML 변환."""
    saved_blocks: list[tuple[str, str]] = []

    def extract_html_block(m: re.Match) -> str:
        saved_blocks.append(("custom_html", m.group(1).strip()))
        return f"@@BLOCK_{len(saved_blocks) - 1}@@"

    text = HTML_BLOCK_RE.sub(extract_html_block, text)

    def extract_mermaid(m: re.Match) -> str:
        saved_blocks.append(("mermaid", auto_wrap_long_labels(m.group(1))))
        return f"@@BLOCK_{len(saved_blocks) - 1}@@"

    text = MERMAID_FENCE_RE.sub(extract_mermaid, text)

    def extract_bq_code(m: re.Match) -> str:
        lang = (m.group(1) or "").strip()
        body = m.group(2)
        lines: list[str] = []
        for line in body.split("\n"):
            if line.startswith("> "):
                lines.append(line[2:])
            elif line.rstrip() == ">":
                lines.append("")
        code = "\n".join(lines).rstrip("\n")
        escaped = _html_escape(code)
        cls_attr = f' class="language-{lang}"' if lang else ""
        raw_html = f"<pre><code{cls_attr}>{escaped}</code></pre>"
        saved_blocks.append(("html", raw_html))
        return f"@@BLOCK_{len(saved_blocks) - 1}@@"

    text = BQ_CODE_FENCE_RE.sub(extract_bq_code, text)

    converter = md_lib.Markdown(
        extensions=["fenced_code", "tables", "toc", "attr_list", "def_list", "nl2br", "sane_lists"],
        extension_configs={"toc": {"permalink": False}},
    )
    html = converter.convert(text)

    def restore_block(m: re.Match) -> str:
        idx = int(m.group(1))
        kind, content = saved_blocks[idx]
        if kind == "mermaid":
            return f'<div class="mermaid" data-lf-preserve="mermaid">{content}</div>'
        if kind == "custom_html":
            if content.strip().startswith("<style"):
                return content
            return f'<div class="lf-html-block" data-lf-preserve="html">{content}</div>'
        return content

    html = re.sub(r"<p>\s*@@BLOCK_(\d+)@@\s*</p>", restore_block, html)
    html = re.sub(r"<p>\s*@@BLOCK_(\d+)@@\s*<br\s*/?>\s*</p>", restore_block, html)
    html = re.sub(r"@@BLOCK_(\d+)@@", restore_block, html)

    def _split_blockquote(m: re.Match) -> str:
        inner = m.group(1)
        segs = re.split(r"(?=<p[\s>])", inner)
        bq_buf: list[str] = []
        tip_buf: list[str] = []
        result: list[str] = []

        def _flush_bq() -> None:
            if bq_buf:
                result.append(f"<blockquote>{''.join(bq_buf)}</blockquote>")
                bq_buf.clear()

        def _flush_tip() -> None:
            if tip_buf:
                result.append(f'<div class="tip-box">{"".join(tip_buf)}</div>')
                tip_buf.clear()

        for seg in segs:
            if not seg.strip():
                continue
            plain = re.sub(r"<[^>]+>", "", seg).strip()
            if tip_pattern.match(plain):
                _flush_bq()
                tip_buf.append(seg)
            else:
                _flush_tip()
                bq_buf.append(seg)

        _flush_bq()
        _flush_tip()
        return "\n".join(result) if result else m.group(0)

    html = re.sub(r"<blockquote>(.*?)</blockquote>", _split_blockquote, html, flags=re.DOTALL)

    html = re.sub(r"(<table\b)", r'<div class="table-wrap">\1', html)
    html = re.sub(r"(</table>)", r"\1</div>", html)

    return html


def demote_headings(html: str) -> str:
    """섹션 body 헤딩을 1레벨 강등 (h1→h2, h2→h3, h3→h4, h4→h5).

    여러 마크다운 문서를 하나의 HTML로 이어붙일 때, 각 문서의 h1이 문서 전체의
    h1과 충돌하지 않도록 한 단계씩 낮춘다. 역순 치환으로 이중 치환을 방지한다.
    """
    for level in range(4, 0, -1):

        def _promote(m: re.Match[str], lv: int = level) -> str:
            return f"{m.group(1)}{lv + 1}{m.group(2)}"

        html = re.sub(rf"(</?h){level}(\s|>)", _promote, html)
    return html


def extract_h1_text(html_body: str) -> str | None:
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html_body, re.DOTALL)
    if not m:
        return None
    return re.sub(r"<[^>]+>", "", m.group(1)).strip()
