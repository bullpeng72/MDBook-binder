"""마크다운 코퍼스 → 검색 가능한 단일 HTML 도서 빌더.

manifest.resolve()로 얻은 챕터 순서에 render.md_to_html()을 적용하고, 이미지를
base64 data URI로 인라인 임베드해 이미지 폴더 없이도 완전히 독립적으로 열리는
단일 HTML 파일을 만든다.

이 모듈이 출력하는 `<section class="chapter-section" id="{slug}">` 구조는
editor/ 가 의존하는 유일한 불변 계약이다 — 다른 무엇을 바꾸더라도 이 마크업
계약은 유지해야 편집기가 섹션을 인식한다.
"""

from __future__ import annotations

import re
import unicodedata
from html import escape as _html_escape
from pathlib import Path

from mdbook_binder.chapter_split import extract_raw_h1, split_chapter_markdown
from mdbook_binder.imgembed import image_to_data_uri
from mdbook_binder.manifest import (
    LOCALE_STRINGS,
    BookConfig,
    ChapterFile,
    resolve,
    resolve_split_targets,
)
from mdbook_binder.mermaid_prerender import (
    mermaid_font_face_css,
    mermaid_label_css,
    prerender_mermaid,
)
from mdbook_binder.render import demote_headings, extract_h1_text, md_to_html, tip_start_pattern
from mdbook_binder.theme import theme_css

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _romanize_char(ch: str) -> str:
    """분해 가능한 문자(악센트 등)만 ASCII로 치환하고, 나머지는 원문 그대로 둔다.

    문자열 전체를 한 번에 ASCII로 치환하려 하면(예전 구현) 제목에 영문 단어가
    하나라도 섞여 있을 때 그 영문 잔여물만 남기고 한글 전체를 버리는 문제가
    있었다(예: "실전 AI 에이전트..." → "ai"). 문자 단위로 판단하면 "é"처럼
    분해되면 ASCII 잔여물이 남는 문자만 로마자로 바뀌고, 한글처럼 NFKD 분해가
    ASCII로 안 떨어지는 문자는 원본 그대로 보존된다.
    """
    ascii_ch = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode("ascii")
    return ascii_ch if ascii_ch else ch


def _slugify(text: str) -> str:
    base = "".join(_romanize_char(ch) for ch in text)
    base = re.sub(r"[^\w\s-]", "", base, flags=re.UNICODE).strip().lower()
    slug = re.sub(r"[\s_]+", "-", base)
    return slug or "section"


def _section_id(fpath: Path, html_body: str, config: BookConfig | None) -> str:
    if config and (
        override := config.section_id_overrides.get(unicodedata.normalize("NFC", fpath.stem))
    ):
        return override
    title = extract_h1_text(html_body)
    return _slugify(title or fpath.stem)


def _dedupe_slug(slug: str, seen: dict[str, int]) -> str:
    """서로 다른 챕터가 같은 제목(따라서 같은 slug)을 쓸 때 id 충돌을 막는다.

    예: 서로 다른 Part에 "개요"라는 제목의 챕터가 둘 있으면 둘 다 slug가
    "개요"가 되어 <section id="개요">가 중복 — 앵커 이동/TOC가 첫 번째
    섹션으로만 깨져서 이동하게 된다. 편집기(editor/html_editor.py)의
    _deduplicate_section_ids()는 편집 시점에만 이를 고치므로, 빌드 시점부터
    애초에 중복 id를 만들지 않도록 여기서 막는다.
    """
    count = seen.get(slug, 0)
    seen[slug] = count + 1
    return slug if count == 0 else f"{slug}-{count + 1}"


def _rewrite_internal_links(html_body: str, chap_dir: Path, path_to_sid: dict[str, str]) -> str:
    """챕터 간 상대경로 .md 링크를 같은 HTML 안의 #앵커로 재작성한다.

    여러 마크다운 파일을 한 HTML로 이어붙이면 `[§5](../Part_I/Chapter_05_*.md)` 같은
    상호참조가 그대로 살아남아, 브라우저에서 열었을 때 존재하지 않는 로컬 .md 파일로
    연결되는 죽은 링크가 된다 — 정작 그 챕터의 실제 내용은 같은 HTML 안 `#{sid}`
    섹션에 이미 들어있는데도 그렇다. 이 함수는 빌드에 포함된 다른 챕터를 가리키는
    링크만 골라 해당 섹션의 앵커로 바꾼다.

    Skills/ 처럼 book.yaml이 의도적으로 코퍼스에서 제외한 파일이나 외부 URL은
    `path_to_sid`에 없으므로 원본 href를 그대로 둔다 — imgembed의 누락 이미지
    처리와 같은 관대한 파싱 원칙이다. 서브섹션 fragment(`#절`)는 파일 단위로만
    id를 추적하므로 매핑 대상에서 제외하고 버린다 — 챕터 최상단으로라도 연결되는
    것이 죽은 링크보다는 낫다.

    `path_to_sid`의 키는 NFC로 정규화돼 있다고 가정한다(manifest._robust_children와
    동일한 계약) — macOS(APFS)는 파일명을 NFD(분해형)로 저장하지만 마크다운 본문의
    링크 텍스트는 보통 에디터가 저장한 NFC(조합형)라, 두 정규화형을 맞추지 않으면
    `.resolve()`를 거쳐도 바이트가 달라 dict 조회가 항상 실패한다.
    """

    def _replace(m: re.Match) -> str:
        href = m.group(1)
        if href.startswith(("http://", "https://", "data:", "#", "mailto:")):
            return m.group(0)
        path_part, _, _fragment = href.partition("#")
        if not path_part.endswith(".md"):
            return m.group(0)
        target = unicodedata.normalize("NFC", str((chap_dir / path_part).resolve()))
        sid = path_to_sid.get(target)
        return f'href="#{sid}"' if sid else m.group(0)

    return re.sub(r'href="([^"]+)"', _replace, html_body)


def _embed_images_as_data_uri(html_str: str, md_dir: Path, missing: list[tuple[Path, str]]) -> str:
    """img src를 base64 data URI로 인라인 임베드한다.

    http(s)://, data:, file://, # 로 시작하는 src는 이미 절대/인라인이므로 그대로 둔다.
    참조된 이미지가 실제로 없으면(오타 등) `missing`에 기록만 하고 원본 src를
    유지한다 — 관대한 파싱 원칙: 이미지 하나가 빠졌다고 전체 빌드를 실패시키지
    않는다. 대신 build_html()이 빌드 끝에 missing 전체를 한 번에 요약 출력한다
    — 50개가 넘는 챕터의 진행 로그 사이에 경고가 한 줄씩 섞여 나오면 놓치기
    쉽다는 걸 실사용 중 확인했다.
    """

    def _to_data_uri(m: re.Match) -> str:
        src = m.group(1)
        if src.startswith(("http://", "https://", "data:", "file://", "#")):
            return m.group(0)
        abs_path = (md_dir / src).resolve()
        if not abs_path.is_file():
            missing.append((abs_path, src))
            return m.group(0)
        return f'src="{image_to_data_uri(abs_path)}"'

    return re.sub(r'src="([^"]+)"', _to_data_uri, html_str)


def build_html(
    root: Path,
    config: BookConfig | None = None,
    *,
    out_path: Path | None = None,
    title_override: str | None = None,
    language_override: str | None = None,
    color_override: str | None = None,
) -> Path:
    if config is None:
        config = BookConfig.load(root)

    chapters = resolve(root, config)
    if not chapters:
        raise ValueError(f"변환할 마크다운 파일을 찾지 못했습니다: {root}")

    tip_pattern = tip_start_pattern(config.tip_markers if config else [])
    language = language_override or (config.language if config else "ko")
    locale = LOCALE_STRINGS.get(language, LOCALE_STRINGS["ko"])

    split_targets = resolve_split_targets(root, config)
    split_level = config.split.heading_level if config and config.split else 2

    sections: list[str] = []
    toc_entries: list[str] = []
    last_part: str | None = None
    seen_slugs: dict[str, int] = {}
    missing_images: list[tuple[Path, str]] = []

    # 1패스 — 렌더링 + section id 확정. 링크 재작성은 아직 하지 않는다: 앞쪽
    # 챕터가 뒤쪽 챕터를 가리키는 상호참조는 이 시점엔 대상 sid를 아직 모른다.
    rendered: list[tuple[ChapterFile, str, str, str]] = []
    path_to_sid: dict[str, str] = {}
    for chap in chapters:
        print(f"  \U0001f4c4 {chap.path.relative_to(root)}")
        raw = chap.path.read_text(encoding="utf-8")

        is_split_target = chap.path.resolve() in split_targets
        pieces = split_chapter_markdown(raw, split_level) if is_split_target else [raw]
        # split.files에 등록됐어도 실제로 지정 레벨 헤딩이 없으면 pieces는
        # [raw] 그대로라 group_label도 None — 기존(파일 1개=섹션 1개) 동작과
        # 완전히 동일하게 폴백된다.
        group_label = extract_raw_h1(raw) if is_split_target and len(pieces) > 1 else None

        chap_key = unicodedata.normalize("NFC", str(chap.path.resolve()))
        for piece in pieces:
            html_body = md_to_html(piece, tip_pattern)
            html_body = _embed_images_as_data_uri(html_body, chap.path.parent, missing_images)

            sid = _dedupe_slug(_section_id(chap.path, html_body, config), seen_slugs)
            title_text = extract_h1_text(html_body) or chap.path.stem
            # 한 파일이 여러 섹션으로 쪼개져도, 그 파일을 가리키는 상호참조
            # 앵커는 첫 조각(진입점)으로만 연결한다 — _rewrite_internal_links()
            # 계약대로 파일 단위 매핑만 지원하므로.
            if chap_key not in path_to_sid:
                path_to_sid[chap_key] = sid

            piece_chap = ChapterFile(path=chap.path, part_label=group_label or chap.part_label)
            rendered.append((piece_chap, sid, title_text, html_body))

    # 2패스 — 이제 코퍼스 전체의 path→sid가 확정됐으므로 챕터 간 상호참조
    # 링크를 #앵커로 재작성한다.
    for chap, sid, title_text, html_body in rendered:
        html_body = _rewrite_internal_links(html_body, chap.path.parent, path_to_sid)
        html_body = demote_headings(html_body)

        if chap.part_label and chap.part_label != last_part:
            toc_entries.append(f'<a class="part-heading">{_html_escape(chap.part_label)}</a>')
            last_part = chap.part_label

        toc_entries.append(
            f'<a class="chapter toc-link" href="#{sid}">{_html_escape(title_text)}</a>'
        )
        sections.append(
            f'<section class="chapter-section" id="{sid}" data-section-title="{_html_escape(title_text)}">'
            f"\n{html_body}\n</section>"
        )

    title = title_override or (config.title if config else None) or root.name

    color = color_override or (config.color if config else None)
    css = (_TEMPLATES_DIR / "html_book.css").read_text(encoding="utf-8")
    if color:
        css += f"\n\n{theme_css(color)}"
    if config and (custom_css := config.load_custom_css(root)):
        css += f"\n\n/* ── custom_css (book.yaml) ── */\n{custom_css}"
    js = (_TEMPLATES_DIR / "html_book.js").read_text(encoding="utf-8")
    js = (
        js.replace("__PLACEHOLDER_HITS_FOUND__", locale["hits_found"])
        .replace("__PLACEHOLDER_NO_MATCH__", locale["no_match"])
        .replace("__PLACEHOLDER_OF__", locale["of"])
    )

    body_html, needs_mermaid_cdn = prerender_mermaid("\n".join(sections))
    # 사전 렌더링된 다이어그램이 있을 때만 폰트를 심는다 — 다이어그램이 없는
    # 책까지 base64 폰트(약 2MB)로 무겁게 만들 이유가 없다.
    has_prerendered_diagrams = 'data-prerendered="true"' in body_html
    mermaid_font_css = (
        f"{mermaid_font_face_css()}\n{mermaid_label_css()}" if has_prerendered_diagrams else ""
    )

    html = _render_shell(
        title=title,
        language=language,
        css=css,
        js=js,
        toc_html="\n".join(toc_entries),
        body_html=body_html,
        search_placeholder=locale["search_placeholder"],
        prev_title=locale["prev_title"],
        next_title=locale["next_title"],
        needs_mermaid_cdn=needs_mermaid_cdn,
        mermaid_font_css=mermaid_font_css,
    )

    out = out_path or (root / f"{_slugify(title)}.html")
    out.write_text(html, encoding="utf-8")
    size_kb = out.stat().st_size // 1024
    file_note = (
        f"{len(chapters)}개 파일"
        if len(sections) == len(chapters)
        else f"{len(chapters)}개 파일 → {len(sections)}개 섹션"
    )
    print(f"\n✅ Done: {out}  ({size_kb} KB, {file_note})")

    if missing_images:
        print(f"\n⚠️  누락된 이미지 {len(missing_images)}건 (원본 src 그대로 유지됨):")
        for abs_path, src in missing_images:
            try:
                rel = abs_path.relative_to(root)
            except ValueError:
                rel = abs_path
            print(f'   - {rel}  (참조: "{src}")')

    return out


def _render_shell(
    *,
    title: str,
    language: str,
    css: str,
    js: str,
    toc_html: str,
    body_html: str,
    search_placeholder: str,
    prev_title: str,
    next_title: str,
    needs_mermaid_cdn: bool,
    mermaid_font_css: str = "",
) -> str:
    # 다이어그램이 전부(또는 애초에 하나도 없어) 사전 렌더링됐다면 CDN mermaid.js
    # 자체가 필요 없다 — 열람 시 불필요한 외부 요청을 만들지 않도록 태그를 생략한다.
    mermaid_script_tag = (
        '<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>\n'
        if needs_mermaid_cdn
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="{language}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_html_escape(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@300;400;500;700&family=Noto+Serif+KR:wght@400;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
{mermaid_script_tag}<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/atom-one-dark.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<style>
{mermaid_font_css}
{css}
</style>
</head>
<body>
<nav id="sidebar">
  <div id="sidebar-header">{_html_escape(title)}</div>
  <div id="search-wrap">
    <input id="search-box" type="search" placeholder="{_html_escape(search_placeholder)}" autocomplete="off" spellcheck="false">
    <div id="search-meta">
      <span id="search-count"></span>
      <div id="search-nav">
        <button id="search-prev" disabled title="{_html_escape(prev_title)}">▲</button>
        <button id="search-next" disabled title="{_html_escape(next_title)}">▼</button>
      </div>
    </div>
  </div>
  <div id="toc">
{toc_html}
  </div>
</nav>
<main id="main">
{body_html}
</main>
<script>
{js}
</script>
</body>
</html>
"""
