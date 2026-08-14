"""마크다운 코퍼스 → 챕터별 PDF(개별 또는 --merge 단권) 빌더.

Playwright/Chromium으로 각 챕터를 독립 렌더링한다. Mermaid 다이어그램은 청크
단위로 스크린샷 캡처해 삽입한다 — Chromium PDF 엔진이 매우 긴 SVG를 페이지
경계에서 잘라버리는 문제를 피하기 위함이다(`build_pdf_chapters.py`에서 검증된
방식을 그대로 이식). 병합(`--merge`)도 각 챕터를 동일한 코드 경로로 개별
렌더링한 뒤 pypdf로 PDF 객체 레벨에서 합친다 — 개별 생성과 병합 생성의 폰트
크기·다이어그램 해상도가 항상 동일하게 유지된다.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
from html import escape as _html_escape
from pathlib import Path

from mdbook_binder.manifest import BookConfig, ChapterFile
from mdbook_binder.mermaid_prerender import mermaid_font_face_css, mermaid_label_css
from mdbook_binder.render import extract_h1_text, md_to_html, tip_start_pattern
from mdbook_binder.theme import theme_css

_TEMPLATES_DIR = Path(__file__).parent / "templates"

_PDF_CONTENT_W = 624
_AVAIL_W = _PDF_CONTENT_W - 32

# PDF 페이지 여백. 아래 page.pdf() 호출과 반드시 같은 값을 써야 한다 — 다이어그램
# 청크 크기를 이 여백 기준 "페이지 콘텐츠 높이"에 맞춰야, break-inside:avoid로
# 청크가 통째로 다음 페이지에 밀렸을 때 이전 페이지 하단에 남는 공백이 최소화된다
# (예전에는 페이지 크기와 무관한 고정값 300px로 잘라, 청크 하나가 페이지의 절반도
# 못 채운 채 다음 페이지로 밀리며 하단에 큰 공백이 남았다).
_PDF_MARGIN_TOP_MM = 22
_PDF_MARGIN_RIGHT_MM = 20
_PDF_MARGIN_BOTTOM_MM = 25
_PDF_MARGIN_LEFT_MM = 25
_A4_HEIGHT_MM = 297
_MM_TO_PX = 96 / 25.4
_PAGE_CONTENT_H = (_A4_HEIGHT_MM - _PDF_MARGIN_TOP_MM - _PDF_MARGIN_BOTTOM_MM) * _MM_TO_PX

# Mermaid 스크린샷 캡처 및 최종 PDF 래스터화 해상도 배율.
# 1이면 CSS px = 물리 px(96dpi 상당)로 캡처되어 인쇄/확대 시 다이어그램과
# 이미지가 흐릿해진다. getBoundingClientRect() 등 레이아웃 계산은 항상 CSS px
# 기준이라 clip 좌표·페이지 나눔 로직에는 영향 없이 캡처 해상도만 올라간다.
_DEVICE_SCALE = 3


def _rewrite_img_paths(html_str: str, base_dir: Path) -> str:
    """img src의 상대 경로를 file:// 절대 경로로 치환한다 (Playwright 로컬 렌더링용)."""

    def _to_abs(m: re.Match) -> str:
        src = m.group(1)
        if src.startswith(("http://", "https://", "data:", "file://")):
            return m.group(0)
        abs_path = (base_dir / src).resolve()
        return f'src="file://{abs_path}"'

    return re.sub(r'src="([^"]+)"', _to_abs, html_str)


def _merge_bands(bands: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """겹치는 (y0, y1) 구간들을 하나로 합친다."""
    if not bands:
        return []
    ordered = sorted(bands)
    merged = [list(ordered[0])]
    for s, e in ordered[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def _is_occupied(y: float, bands: list[tuple[float, float]]) -> bool:
    return any(s < y < e for s, e in bands)


def _nearest_safe_y(target: float, bands: list[tuple[float, float]], lo: float, hi: float) -> float:
    """target 근방에서 도형/텍스트와 겹치지 않는 y좌표를 찾는다. 못 찾으면 target 그대로 반환한다."""
    if lo > hi:
        return target
    target = max(lo, min(hi, target))
    if not _is_occupied(target, bands):
        return target
    offset = 2.0
    while True:
        left, right = target - offset, target + offset
        left_ok, right_ok = left >= lo, right <= hi
        if not left_ok and not right_ok:
            return target
        if left_ok and not _is_occupied(left, bands):
            return left
        if right_ok and not _is_occupied(right, bands):
            return right
        offset += 2.0


def _chunk_boundaries(
    th: float, bands_raw: list[tuple[float, float]], page_h: float, remaining_first: float
) -> list[float]:
    """다이어그램 전체 높이(th)를 실제 PDF 페이지 경계에 맞춰 나누되, 도형/텍스트
    중앙을 피해 자연스러운 여백에서 끊는 경계 목록을 반환한다.

    각 청크의 목표 크기를 고정값이 아니라 "이 페이지에 남은 공간"(첫 청크,
    remaining_first) · "페이지 한 장 분량"(이후 청크)으로 잡는다. remaining_first는
    다이어그램이 시작하는 지점부터 그 페이지가 끝나기까지 실측된 공간이다 — 이걸
    무시하고 항상 다이어그램의 0지점부터 고정 간격으로 자르면, 청크 경계와 실제
    페이지 경계가 어긋나 청크 하나가 페이지 절반도 못 채운 채 break-inside:avoid로
    통째로 다음 페이지에 밀리고, 이전 페이지 하단에는 그만큼 빈 공백이 남는다.
    각 경계는 목표 지점 주변에서 비어 있는(도형이 없는) y좌표로 옮겨 박스/라벨이
    반토막나는 것도 피한다.
    """
    # 페이지 하단에 아주 조금(15% 미만)만 남았으면 그 자투리에 억지로 첫 청크를
    # 욱여넣지 않는다 — 검색 구간이 너무 좁아 안전한 절단 지점을 못 찾고 도형
    # 한가운데를 그대로 자를 위험이 커진다. 이 경우 다음 페이지 처음부터 채운다.
    remaining = remaining_first
    if remaining < page_h * 0.15:
        remaining = page_h
    if th <= remaining:
        return [0.0, float(th)]

    bands = _merge_bands(bands_raw)
    boundaries = [0.0]
    y = 0.0
    budget = remaining
    while y < th:
        target = y + budget
        if target >= th:
            boundaries.append(float(th))
            break
        # 청크(래스터 이미지) 하나가 페이지 경계(target)를 넘으면 안 된다 — 넘으면
        # break-inside:avoid가 있어도 이미지 자체가 다음 페이지로 밀리지 못하고
        # 그 자리에서 그대로 잘려, 도형 한가운데가 끊기고 다음 페이지 대부분이
        # 텅 비어버린다(실측으로 확인된 회귀). 그래서 안전 지점은 target 이전
        # 방향으로만 찾는다 — 위쪽(budget*0.4)으로는 물러날 수 있어도 아래쪽
        # (페이지 경계 너머)으로는 절대 넘어가지 않는다.
        safe = _nearest_safe_y(target, bands, y + budget * 0.4, target)
        boundaries.append(safe)
        y = safe
        budget = page_h
    return boundaries


async def _measure_remaining_space(
    render_page, container, page_h: float, temp_dir: Path, seq: int
) -> float | None:
    """컨테이너가 문서 흐름상 시작하는 지점에서, 실제로 인쇄될 페이지에 남은
    공간을 측정한다.

    document.body.scrollHeight 기준 순수 흐름 높이만으로 y % 페이지높이를
    계산하면, 제목/표/인용구 등에 걸린 break-avoid CSS 규칙이 실제 페이지 경계를
    앞당기는 걸 전혀 반영하지 못한다 — 챕터 앞부분에서는 오차가 작아도, 그런
    요소가 많이 쌓인 챕터 뒤쪽에서는 실측으로 수백 px 어긋나는 경우가 확인됐다.

    처음에는 y좌표 마커를 `position: absolute`로 문서 전체에 흩뿌려 어느
    페이지에 찍히는지 읽는 방식을 시도했으나, 절대 위치 요소는 여러 페이지에
    걸친 컨테이닝 블록(body) 안에서 어느 페이지 조각에 귀속되는지가 명세상
    회색지대라 실측 결과가 실제 본문 페이지 배치와 맞지 않았다(같은 컨테이너의
    시작 지점을 가리키는 마커가, 그 앞에 오는 제목보다 더 이른 페이지에 찍히는
    모순이 실측으로 확인됨). 대신 컨테이너의 내용을 임시로 "자"(고정 높이 줄이
    오프셋 라벨을 담은 일반 흐름 요소들)로 바꿔치기한다 — 일반 흐름 콘텐츠는
    페이지 나눔이 소스 순서를 절대 어기지 않으므로, 이 자가 어느 페이지에서
    잘리는지 읽으면 컨테이너 시작 지점 기준 실제 남은 공간을 모호함 없이 알 수
    있다. 측정 후에는 컨테이너 내용을 원래대로 되돌린다.

    호출부가 이 함수를 부르기 전에 대상 컨테이너의 `break-inside`를 일시
    해제해둬야 한다 — 그러지 않으면 `.mermaid { break-inside: avoid }` 규칙
    때문에 이 자 콘텐츠 전체가 안 들어가는 페이지에서 통째로 다음 페이지로
    밀리며 측정 자체가 무의미해진다.

    측정에 실패하면(렌더링/파싱 오류 등) None을 반환해, 호출부가 순수 흐름
    높이 기준 근사치로 폴백하게 한다.
    """
    try:
        interval = 20
        # 페이지 하나보다 넉넉히 긴 자를 컨테이너 시작 지점에 채워 넣는다 — 남은
        # 공간이 얼마든(최대가 page_h) 이 자의 일부는 반드시 다음 페이지로
        # 넘어가므로, "시작 페이지에서 관측되는 최댓값"이 곧 실제 남은 공간이다.
        max_offset = int(page_h * 1.3)
        ruler_html = "".join(
            f'<div style="height:{interval}px;overflow:hidden;font-size:10px;'
            f'line-height:{interval}px;margin:0;padding:0;">RULER_{off}</div>'
            for off in range(0, max_offset, interval)
        )

        original_html = await container.evaluate("(el) => el.innerHTML")
        await container.evaluate("(el, html) => { el.innerHTML = html; }", ruler_html)
        try:
            measure_path = temp_dir / f"measure_{seq:04d}.pdf"
            await render_page.pdf(
                path=str(measure_path),
                format="A4",
                margin={
                    "top": f"{_PDF_MARGIN_TOP_MM}mm",
                    "right": f"{_PDF_MARGIN_RIGHT_MM}mm",
                    "bottom": f"{_PDF_MARGIN_BOTTOM_MM}mm",
                    "left": f"{_PDF_MARGIN_LEFT_MM}mm",
                },
            )
        finally:
            await container.evaluate("(el, html) => { el.innerHTML = html; }", original_html)

        import pypdf

        reader = pypdf.PdfReader(str(measure_path))
        marker_re = re.compile(r"RULER_(\d+)")
        for page in reader.pages:
            text = page.extract_text() or ""
            found = [int(m) for m in marker_re.findall(text)]
            if 0 in found:
                return float(max(found))
        return None
    except Exception:
        return None


def _mermaid_chunk_html(chunks: list[tuple[str, int, int]]) -> str:
    """mermaid 컨테이너를 대체할 청크 스크린샷 <img> 마크업을 만든다."""
    n = len(chunks)
    parts = []
    for ci, (fu, w, h) in enumerate(chunks):
        mt = "14px" if ci == 0 else "0"
        mb = "14px" if ci == n - 1 else "0"
        parts.append(
            f'<div class="mermaid-chunk" style="margin:{mt} 0 {mb};'
            f'display:table;width:100%;break-inside:avoid;page-break-inside:avoid;">'
            f'<img src="{fu}" width="{w}" height="{h}" '
            f'style="display:block;margin:0 auto;width:{w}px;height:{h}px;max-width:none;border:none;">'
            f"</div>"
        )
    return "".join(parts)


def _build_pdf_page_html(body_html: str, title: str, custom_css: str = "") -> str:
    css = (_TEMPLATES_DIR / "html_book.css").read_text(encoding="utf-8")
    pdf_css = (_TEMPLATES_DIR / "pdf_override.css").read_text(encoding="utf-8")
    js = (_TEMPLATES_DIR / "pdf_book.js").read_text(encoding="utf-8")
    # Google Fonts CDN이 원래 폰트 출처지만, 빌드 머신이 오프라인이면 조용히
    # 실패해 시스템 폰트로 대체된다. 번들된 Noto Sans KR을 같은 이름의
    # @font-face로 같이 심어 두면 CDN이 막혀도 mermaid 다이어그램만은 항상
    # 동일한 폰트로 렌더링된다(mermaid_prerender.py의 HTML 경로와 동일 폰트).
    font_css = f"{mermaid_font_face_css()}\n{mermaid_label_css()}"
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>{_html_escape(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@300;400;500;700&family=Noto+Serif+KR:wght@400;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/atom-one-dark.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<style>
{font_css}
{css}
{pdf_css}
{custom_css}
</style>
</head>
<body>
<main id="main">
{body_html}
</main>
<script>
{js}
</script>
</body>
</html>"""


async def convert_one(
    chapter: ChapterFile, browser, tip_pattern, *, out_path: Path, custom_css: str = ""
) -> tuple[Path, str]:
    """챕터 하나를 PDF로 변환한다. 긴 mermaid 다이어그램은 청크 스크린샷으로 대체 삽입한다.

    반환하는 title은 챕터의 실제 h1 제목(없으면 파일명)이다 — PDF 문서 타이틀과
    병합본 북마크(outline) 라벨에 공용으로 쓴다(html_book.py의 제목 추출 규칙과 동일).
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    raw = chapter.path.read_text(encoding="utf-8")
    body = md_to_html(raw, tip_pattern)
    title = extract_h1_text(body) or chapter.path.stem
    html = _rewrite_img_paths(_build_pdf_page_html(body, title, custom_css), chapter.path.parent)

    temp_dir = Path(tempfile.mkdtemp(prefix="book_binder_pdf_"))
    try:
        # 뷰포트 폭은 처음부터 PDF 목표 폭(_PDF_CONTENT_W)으로 고정한다. 폭을 기본값(1280px)
        # 그대로 두면 pdf_book.js의 mermaid/table 스케일 계산이 잘못된(더 넓은) availW
        # 기준으로 이뤄지고, 이후 뷰포트를 좁히는 순간 텍스트 줄바꿈이 늘어나 문서가
        # 측정한 full_h보다 더 길어진다 — 문서 끝부분 요소가 뷰포트 밖으로 밀려나면
        # screenshot(clip=...)이 좌표를 벗어나 캡처 실패(빈 chunk → 원본 mermaid div
        # 그대로 잔존)로 이어져 빈 페이지가 삽입되고, 가로형 다이어그램은 잘못된
        # availW 기준으로 스케일되어 과대 표시된다.
        ctx1 = await browser.new_context(
            device_scale_factor=_DEVICE_SCALE,
            viewport={"width": _PDF_CONTENT_W, "height": 800},
        )
        render_page = await ctx1.new_page()
        await render_page.set_content(html, wait_until="networkidle")
        try:
            await render_page.wait_for_function(
                "() => window.__mermaidDone === true", timeout=20000
            )
        except Exception:
            pass
        full_h = await render_page.evaluate("() => Math.max(document.body.scrollHeight, 6000)")
        await render_page.set_viewport_size({"width": _PDF_CONTENT_W, "height": full_h})

        containers = await render_page.query_selector_all(".mermaid")

        for idx, container in enumerate(containers):
            try:
                info = await container.evaluate(f"""(container) => {{
                    const svg = container.querySelector('svg');
                    if (!svg) return null;
                    const r = svg.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) return null;
                    const scW = r.width > {_AVAIL_W} ? {_AVAIL_W} / r.width : 1;
                    const tw = Math.ceil(r.width  * scW);
                    const th = Math.ceil(r.height * scW);
                    svg.setAttribute('width',  tw);
                    svg.setAttribute('height', th);
                    svg.style.cssText = 'display:block;margin:0 auto;';
                    const b = svg.getBoundingClientRect();
                    // 도형/텍스트가 차지하는 y구간을 모아, 청크 경계가 그 한가운데를
                    // 가로지르지 않도록 한다 (박스/라벨이 반토막나는 것을 방지).
                    // path(화살표/연결선)는 제외한다 — 연결선은 정의상 노드와 노드
                    // 사이 여백 전체를 잇는 선이라, 이를 포함하면 촘촘히 연결된
                    // 플로우차트에서 "비어 있는" 구간이 사실상 사라져 버려 안전한
                    // 절단 지점을 못 찾고(_nearest_safe_y가 탐색창 전체가 점유된
                    // 것으로 보고 target을 그대로 반환) 결국 다이아몬드/박스 한가운데를
                    // 그대로 잘라버리는 결과로 이어진다. 화살표 선 중간이 페이지
                    // 경계에서 끊기는 건 시각적으로 자연스럽다.
                    const bands = [];
                    svg.querySelectorAll('rect, polygon, circle, ellipse, text, image').forEach(el => {{
                        const er = el.getBoundingClientRect();
                        if (er.width <= 0 || er.height <= 0) return;
                        const y0 = er.top - b.y;
                        const y1 = er.bottom - b.y;
                        if (y1 <= 0 || y0 >= b.height) return;
                        bands.push([Math.max(0, y0), Math.min(b.height, y1)]);
                    }});
                    return {{x: b.x, y: b.y, w: Math.round(b.width), h: Math.round(b.height), bands}};
                }}""")
                if not info:
                    continue

                sx, sy, tw, th = info["x"], info["y"], info["w"], info["h"]
                bands = [(b[0], b[1]) for b in info.get("bands", [])]

                # 이 컨테이너 자신의 break-inside:avoid를 측정 동안만 해제한다 —
                # 그러지 않으면 자(ruler) 콘텐츠 전체가 안 들어가는 페이지에서
                # 통째로 다음 페이지로 밀리며 측정 자체가 무의미해진다
                # (_measure_remaining_space 참고).
                await container.evaluate("(el) => { el.style.breakInside = 'auto'; }")
                remaining_first = await _measure_remaining_space(
                    render_page, container, _PAGE_CONTENT_H, temp_dir, idx
                )
                await container.evaluate("(el) => { el.style.breakInside = ''; }")
                if remaining_first is None:
                    remaining_first = _PAGE_CONTENT_H - (sy % _PAGE_CONTENT_H)

                boundaries = _chunk_boundaries(th, bands, _PAGE_CONTENT_H, remaining_first)
                chunks: list[tuple[str, int, int]] = []

                for ci in range(len(boundaries) - 1):
                    y0, y1 = boundaries[ci], boundaries[ci + 1]
                    ry = round(sy + y0)
                    ch = round(y1 - y0)
                    if ch <= 0:
                        continue
                    png = await render_page.screenshot(
                        type="png",
                        clip={"x": round(sx), "y": ry, "width": tw, "height": ch},
                    )
                    chunk_path = temp_dir / f"mermaid_{idx:04d}_{ci:02d}.png"
                    chunk_path.write_bytes(png)
                    chunks.append((f"file://{chunk_path}", tw, ch))

                if not chunks:
                    continue

                # container 자체를 청크 이미지로 교체한다 — 문자열 정규식이 아니라
                # DOM 노드 단위 치환이라, mermaid SVG 내부에 중첩된 <div>(foreignObject
                # 라벨)가 몇 개든 컨테이너 경계를 잘못 잡을 일이 없다.
                await container.evaluate(
                    "(el, html) => { el.outerHTML = html; }", _mermaid_chunk_html(chunks)
                )
            except Exception:
                continue

        p2_html = await render_page.content()
        await render_page.close()
        await ctx1.close()

        html_file = temp_dir / "chapter.html"
        html_file.write_text(p2_html, encoding="utf-8")

        ctx2 = await browser.new_context(device_scale_factor=_DEVICE_SCALE)
        page = await ctx2.new_page()
        await page.set_viewport_size({"width": _PDF_CONTENT_W, "height": 6000})
        await page.goto(f"file://{html_file}", wait_until="networkidle")
        await page.wait_for_load_state("load")
        try:
            await page.wait_for_function("() => window.__mermaidDone === true", timeout=10000)
        except Exception:
            pass

        real_h = await page.evaluate("document.body.scrollHeight")
        await page.set_viewport_size({"width": _PDF_CONTENT_W, "height": max(real_h, 6000)})

        await page.pdf(
            path=str(out_path),
            format="A4",
            margin={
                "top": f"{_PDF_MARGIN_TOP_MM}mm",
                "right": f"{_PDF_MARGIN_RIGHT_MM}mm",
                "bottom": f"{_PDF_MARGIN_BOTTOM_MM}mm",
                "left": f"{_PDF_MARGIN_LEFT_MM}mm",
            },
            print_background=True,
        )
        await page.close()
        await ctx2.close()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    size_kb = out_path.stat().st_size // 1024
    print(f"  ✅ {chapter.path.name}  ({size_kb} KB)")
    return out_path, title


async def _run_all(
    chapters: list[ChapterFile], root: Path, pdf_dir: Path, tip_pattern, custom_css: str = ""
) -> tuple[int, int]:
    from playwright.async_api import async_playwright

    ok = fail = 0
    async with async_playwright() as pw:
        browser = await _launch_chromium(pw)
        if browser is None:
            return 0, len(chapters)
        for chapter in chapters:
            rel = chapter.path.relative_to(root)
            out_path = pdf_dir / rel.with_suffix(".pdf")
            try:
                await convert_one(
                    chapter, browser, tip_pattern, out_path=out_path, custom_css=custom_css
                )
                ok += 1
            except Exception as exc:
                # 여러 챕터 결과가 한 줄씩 나열되는 목록이라, 예외가 여러 줄이면
                # 목록 형태가 깨진다 — 첫 줄만 보여준다.
                detail = str(exc).strip().splitlines()
                summary = detail[0] if detail else type(exc).__name__
                print(f"  ❌ {rel}: {summary}")
                fail += 1
        await browser.close()
    return ok, fail


def _add_merge_outline(writer, entries: list[tuple[ChapterFile, str, int]]) -> None:
    """병합 PDF에 챕터별 북마크(아웃라인)를 단다 — Part가 있으면 그 Part 제목
    북마크 아래 챕터들을 중첩하고, Part가 없는 챕터(서문/후주 등)는 최상위에 둔다.

    entries는 writer에 이미 순서대로 append된 챕터들과 1:1 대응해야 한다 —
    각 항목의 page_count(그 챕터가 차지한 페이지 수)를 누적해 페이지 오프셋을
    계산하므로, 순서나 개수가 append 순서와 어긋나면 북마크가 엉뚱한 페이지를
    가리키게 된다. Part 전환 감지는 build_html()의 TOC 구성 로직과 동일하게
    "직전 챕터와 part_label이 다른가"만 본다.
    """
    page_offset = 0
    part_bookmark = None
    last_part: str | None = None
    for chapter, title, page_count in entries:
        if chapter.part_label != last_part:
            part_bookmark = (
                writer.add_outline_item(chapter.part_label, page_offset)
                if chapter.part_label
                else None
            )
            last_part = chapter.part_label
        writer.add_outline_item(title, page_offset, parent=part_bookmark)
        page_offset += page_count


async def _run_merged(
    chapters: list[ChapterFile], out_path: Path, tip_pattern, custom_css: str = ""
) -> bool:
    import pypdf
    from playwright.async_api import async_playwright

    out_path.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="book_binder_merge_"))
    try:
        async with async_playwright() as pw:
            browser = await _launch_chromium(pw)
            if browser is None:
                return False
            parts: list[tuple[Path, str]] = []
            try:
                for idx, chapter in enumerate(chapters):
                    part_out = temp_dir / f"{idx:03d}.pdf"
                    _, title = await convert_one(
                        chapter, browser, tip_pattern, out_path=part_out, custom_css=custom_css
                    )
                    parts.append((part_out, title))
            finally:
                await browser.close()

        writer = pypdf.PdfWriter()
        outline_entries: list[tuple[ChapterFile, str, int]] = []
        for chapter, (part_path, title) in zip(chapters, parts):
            start = len(writer.pages)
            writer.append(str(part_path))
            outline_entries.append((chapter, title, len(writer.pages) - start))
        _add_merge_outline(writer, outline_entries)
        with open(out_path, "wb") as f:
            writer.write(f)
        writer.close()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    size_kb = out_path.stat().st_size // 1024
    print(f"  ✅ {out_path.name}  ({len(chapters)}개 파일 병합, {size_kb} KB)")
    return True


async def _launch_chromium(pw):
    try:
        return await pw.chromium.launch()
    except Exception as e:
        if "Executable doesn't exist" in str(e):
            print("\n❌ Playwright 브라우저 엔진이 설치되지 않았습니다.")
            print("   python -m playwright install chromium")
            return None
        raise


def build_pdf(
    root: Path,
    config: BookConfig | None = None,
    *,
    merge_name: str | None = None,
    out_dir: Path | None = None,
    color_override: str | None = None,
) -> Path | list[Path]:
    """ROOT의 마크다운 코퍼스를 PDF로 빌드한다.

    merge_name이 주어지면 단권(out_dir/{merge_name}.pdf), 아니면 챕터별 개별
    PDF(out_dir 아래 디렉토리 구조 그대로)를 만든다. 순서 해석은
    manifest.resolve()의 3단계 우선순위를 html_book.build_html()과 동일하게
    공유한다.
    """
    from mdbook_binder.manifest import resolve

    if config is None:
        config = BookConfig.load(root)

    chapters = resolve(root, config)
    if not chapters:
        raise ValueError(f"변환할 마크다운 파일을 찾지 못했습니다: {root}")

    tip_pattern = tip_start_pattern(config.tip_markers if config else [])
    custom_css = config.load_custom_css(root) if config else ""
    color = color_override or (config.color if config else None)
    if color:
        custom_css = f"{theme_css(color)}\n\n{custom_css}"
    pdf_dir = out_dir or (root / "pdf")

    if merge_name is not None:
        out_path = pdf_dir / f"{merge_name}.pdf"
        print(f"\U0001f4c4 병합 대상: {len(chapters)}개 파일 → {out_path}")
        ok = asyncio.run(_run_merged(chapters, out_path, tip_pattern, custom_css))
        if not ok:
            raise RuntimeError("PDF 병합 실패")
        return out_path

    print(f"\U0001f4c4 변환 대상: {len(chapters)}개 파일 → {pdf_dir}")
    ok_n, fail_n = asyncio.run(_run_all(chapters, root, pdf_dir, tip_pattern, custom_css))
    print(f"완료: {ok_n}개 성공 / {fail_n}개 실패")
    if fail_n:
        raise RuntimeError(f"{fail_n}개 챕터 PDF 변환 실패")
    return [pdf_dir / c.path.relative_to(root).with_suffix(".pdf") for c in chapters]
