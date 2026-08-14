"""Mermaid 다이어그램을 빌드 시점에 정적 SVG로 사전 렌더링.

HTML 도서를 열 때마다 CDN의 mermaid.js에 의존하지 않도록, 가능하면 빌드
시점에 Playwright/Chromium으로 각 다이어그램을 SVG로 렌더링해 그 결과를
그대로 삽입한다. 로컬에 번들된 templates/vendor/mermaid.min.js만 쓰므로
빌드 자체도 네트워크 접속 없이 동작한다.

Playwright가 설치돼 있지 않거나 Chromium 실행에 실패하면(선택 설치인
`[pdf]` extra 미설치 등) 조용히 원본을 유지해, 열람 시 CDN mermaid.js로
렌더링하는 기존 방식으로 폴백한다 — 관대한 파싱 원칙: 사전 렌더링이
안 된다고 HTML 빌드 자체가 실패해서는 안 된다.

Mermaid 기본 테마 폰트("trebuchet ms", verdana, arial, sans-serif)에는 한글
글리프가 없다. SVG 텍스트는 (경로가 아니라) 살아있는 <text>/<span>이라
열람 시 각자의 OS가 임의로 대체 폰트를 골라 렌더링하게 되는데, 빌드 머신에
한글 폰트가 아예 없으면(헤드리스 Linux CI 등) 두부(tofu) 박스로 영구히
박제될 수 있다. 이를 막기 위해 Noto Sans KR을 로컬에 번들해 빌드용 페이지와
최종 HTML 양쪽에 동일한 @font-face로 심어, 빌드/열람 환경과 무관하게 항상
같은 폰트로 렌더링되게 한다.
"""

from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path

from bs4 import BeautifulSoup

_VENDOR_DIR = Path(__file__).parent / "templates" / "vendor"
_FONT_PATH = _VENDOR_DIR / "NotoSansKR-Regular.woff2"

MERMAID_FONT_FAMILY = "Noto Sans KR"

_RENDER_JS = """async (codes) => {
    try { await document.fonts.ready; } catch (e) { /* ignore */ }
    mermaid.initialize({
        startOnLoad: false,
        theme: 'default',
        securityLevel: 'loose',
        themeVariables: { fontFamily: '"__FONT__", sans-serif' },
    });
    const out = [];
    for (let i = 0; i < codes.length; i++) {
        try {
            const { svg } = await mermaid.render('mmd_' + i, codes[i]);
            out.push(svg);
        } catch (e) {
            out.push(null);
        }
    }
    return out;
}""".replace("__FONT__", MERMAID_FONT_FAMILY)


@lru_cache(maxsize=1)
def mermaid_font_face_css() -> str:
    """번들된 Noto Sans KR을 base64로 심은 `@font-face` 규칙 하나를 반환한다.

    빌드용 렌더 페이지와 최종 HTML 도서 `<head>` 양쪽에 그대로 주입해, 다이어그램
    텍스트가 어느 환경에서 빌드/열람되든 동일한 폰트로 그려지게 만드는 데 쓴다.
    """
    b64 = base64.b64encode(_FONT_PATH.read_bytes()).decode("ascii")
    return (
        f"@font-face {{ font-family: '{MERMAID_FONT_FAMILY}'; font-style: normal; "
        f"font-weight: 400; font-display: block; "
        f"src: url(data:font/woff2;base64,{b64}) format('woff2'); }}"
    )


# Mermaid는 노드/엣지 라벨(<foreignObject> 안의 <div>/<span>)의 높이를 자체
# 측정해 도형(rect/polygon) 크기를 정하는데, 이 내부 측정치가 실제 브라우저의
# line-height: normal 계산값과 어긋난다 — 특히 Noto Sans KR처럼 세로 메트릭이
# 큰 한글 폰트에서는 실측치가 mermaid의 측정치보다 20~30% 더 크다. 그 결과
# 라벨이 두 줄 이상으로 줄바꿈되면 마지막 줄이 도형/foreignObject 경계 밖으로
# 밀려나 잘려 보인다(HTML 열람 시, 그리고 청크를 스크린샷하는 PDF 변환 시 모두
# 동일하게 발생). line-height를 고정된 숫자값으로 못박아 빌드 시점 측정과
# 실제 표시 시점 렌더링을 일치시키고, foreignObject에는 overflow: visible을
# 둬 그래도 남는 서브픽셀 오차가 텍스트를 자르지 않고 살짝 넘치는 선에서
# 그치게 한다.
MERMAID_LABEL_LINE_HEIGHT = 1.2


def mermaid_label_css() -> str:
    """라벨 line-height 고정 + foreignObject 클리핑 해제 규칙을 반환한다.

    mermaid_font_face_css()와 마찬가지로 빌드용 렌더 페이지, 최종 HTML 도서,
    PDF 변환용 렌더 페이지 세 곳 모두에 동일하게 주입해야 빌드 시점 측정과
    표시 시점 렌더링이 어긋나지 않는다.
    """
    return (
        "foreignObject { overflow: visible; } "
        f"foreignObject div, foreignObject span {{ line-height: {MERMAID_LABEL_LINE_HEIGHT}; }}"
    )


def prerender_mermaid(sections_html: str) -> tuple[str, bool]:
    """`.mermaid` div의 원본 소스를 렌더링된 `<svg>`로 치환한다.

    반환값의 두 번째 요소는 "열람 시 CDN mermaid.js가 여전히 필요한가"이다
    — 사전 렌더링이 전부 성공하면 False(CDN 스크립트 태그 자체를 생략해도
    됨), 다이어그램이 아예 없어도 False, 일부라도 실패/폴백하면 True.
    """
    soup = BeautifulSoup(sections_html, "html.parser")
    mermaid_divs = soup.find_all("div", class_="mermaid")
    if not mermaid_divs:
        return sections_html, False

    codes = [d.get_text() for d in mermaid_divs]
    try:
        svgs = _render_svgs(codes)
    except Exception as exc:
        print(
            f"  ⚠️  Mermaid 사전 렌더링 불가({_summarize_exc(exc)}) — 열람 시 CDN mermaid.js로 렌더링됩니다"
        )
        return sections_html, True

    needs_cdn = False
    for div, svg in zip(mermaid_divs, svgs):
        if not svg:
            needs_cdn = True
            continue
        div.clear()
        div["data-prerendered"] = "true"
        div.append(BeautifulSoup(svg, "html.parser"))

    return str(soup), needs_cdn


def _summarize_exc(exc: Exception) -> str:
    """예외 메시지를 경고 한 줄에 끼워 넣기 안전한 한 줄로 요약한다.

    Playwright의 "Executable doesn't exist" 예외는 안내용 ASCII 박스가 딸린
    여러 줄짜리 메시지라, 그대로 `print(f"...({exc})...")`에 넣으면 뒤따르는
    문장이 박스 마지막 줄에 그대로 이어붙어 버린다. 첫 줄만 취해 원인은
    유지하되, 설치 미완료 케이스는 아예 설치 안내 문구로 바꿔치기한다.
    """
    text = str(exc).strip()
    first_line = text.splitlines()[0] if text else type(exc).__name__
    if "Executable doesn't exist" in first_line:
        return "Playwright 브라우저 엔진 미설치 — python -m playwright install chromium"
    return first_line


def _render_svgs(codes: list[str]) -> list[str | None]:
    from playwright.sync_api import sync_playwright

    mermaid_js = (_VENDOR_DIR / "mermaid.min.js").read_text(encoding="utf-8")
    page_html = (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        f"<style>{mermaid_font_face_css()}\n{mermaid_label_css()}</style>"
        f"<script>{mermaid_js}</script></head><body></body></html>"
    )

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(page_html, wait_until="load")
            return page.evaluate(_RENDER_JS, codes)
        finally:
            browser.close()
