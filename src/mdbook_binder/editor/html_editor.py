"""HTML 도서 편집 로직 — 섹션 단위 파싱/수정/삭제.

Lecture_forge의 `editor/html_editor.py`(LectureHTMLEditor)를 lecture-forge
비의존으로 포크한 것이다. html_book.py가 출력하는
`<section class="chapter-section" id="{slug}">` 구조에만 의존한다 — 그 구조를
만든 코퍼스가 어떤 관례를 썼는지는 전혀 모른다.
"""

from __future__ import annotations

import base64
import binascii
import logging
import mimetypes
import re
from pathlib import Path
from typing import Literal, TypedDict

import markdown
from bs4 import BeautifulSoup, Tag
from markdownify import markdownify

from mdbook_binder.imgembed import image_to_data_uri
from mdbook_binder.manifest import write_minimal_book_yaml

logger = logging.getLogger("mdbook_binder.editor")

_DATA_URI_RE = re.compile(r"^data:([^;,]*)?(;base64)?,(.*)$", re.DOTALL)


class _StagedEdit(TypedDict):
    title: str
    markdown: str


_StagedChange = _StagedEdit | Literal["deleted"]


def _preserved_kind(tag: Tag) -> str | None:
    """섹션 저장 시 편집 대상 마크다운에서 제외하고 원본 그대로 보존해야
    하는 요소(figure/다이어그램/콜아웃)인지 판정한다(순수 함수).

    _html_section_to_markdown()(편집용 마크다운 생성)과
    _replace_section_content()(저장 시 재조립) 양쪽이 "무엇을 보존
    대상으로 볼지" 판정을 공유해야 한다 — 두 곳에서 각자 다른 조건을
    쓰면 한쪽만 인식하는 요소가 생겨 조용히 사라지거나 중복될 수 있다.
    """
    if tag.name == "figure":
        return "figure"
    if tag.name != "div":
        return None
    classes: list[str] = tag.get("class") or []  # type: ignore[assignment]
    if "tip-box" in classes:
        return "tip-box"
    if "mermaid" in classes:
        return "diagram"
    if "my-8" in classes and tag.find("div", class_="mermaid"):
        return "diagram"
    return None


def _protect_code_blocks(clone: BeautifulSoup) -> dict[str, str]:
    """`<pre><code class="language-X">` 블록을 안전한 토큰으로 바꾸고, 완성된
    마크다운에서 되돌릴 치환表(토큰 → ` ```X\\n...\\n``` `)을 반환한다.

    markdownify는 fenced code를 되돌릴 때 `class="language-X"`의 언어를
    펜스 info string으로 안 옮기고 빈 ` ``` `만 남긴다(실사용 재현·확인됨)
    — 코드 자체는 살아있어도 재빌드하면 문법 하이라이트가 빠진다. mermaid
    플레이스홀더(export_markdown_corpus 참고)와 같은 방식으로 markdownify
    변환 전에 코드 블록 전체를 토큰으로 빼뒀다가, 변환 후 언어를 보존한
    펜스로 직접 되돌린다.
    """
    placeholders: dict[str, str] = {}
    for i, pre in enumerate(clone.find_all("pre")):
        code = pre.find("code")
        if code is None:
            continue
        lang = ""
        for cls in code.get("class") or []:
            if cls.startswith("language-"):
                lang = cls[len("language-") :]
                break
        token = f"MDBOOKBINDERCODEBLOCKPLACEHOLDER{i}END"
        text = code.get_text()
        placeholders[token] = f"\n\n```{lang}\n{text}\n```\n\n"
        pre.replace_with(token)
    return placeholders


def _write_data_uri_image(data_uri: str, images_dir: Path, base_name: str) -> Path | None:
    """`data:<mime>;base64,<data>` URI를 디코딩해 images_dir에 파일로 저장한다.

    base64 인코딩이 아니거나(퍼센트 인코딩 data URI 등) 디코딩에 실패하면
    None을 반환한다 — html_book.py/editor가 만드는 이미지는 항상 base64라
    실전 경로는 아니지만, 방어적으로 처리해 export 전체를 멈추지 않는다.
    """
    m = _DATA_URI_RE.match(data_uri)
    if not m or not m.group(2):
        return None
    mime = m.group(1) or "application/octet-stream"
    ext = mimetypes.guess_extension(mime) or ".png"
    if ext == ".jpe":
        ext = ".jpg"
    images_dir.mkdir(parents=True, exist_ok=True)
    out_path = images_dir / f"{base_name}{ext}"
    try:
        out_path.write_bytes(base64.b64decode(m.group(3)))
    except (binascii.Error, ValueError):
        return None
    return out_path


def export_markdown_corpus(
    soup: BeautifulSoup, out_dir: Path, *, language: str | None = None
) -> list[Path]:
    """편집된 HTML 도서를 여러 .md 파일 + images/ + book.yaml로 되돌린다(코퍼스 역반영).

    language를 안 주면 html_book.py가 써 둔 `<html lang="...">`에서 읽는다
    (html_book.py:300 `_render_shell()` 참고) — 못 찾으면 "ko"로 폴백한다.

    html_book.py의 build_html()이 여러 마크다운 파일을 `<section id="...">`로
    이어붙인 것을 역으로 챕터별 파일로 되돌린다 — 파일명은 문서 순서 접두
    ("001_", "002_"...) + section id. 각 파일은 섹션의 h2(빌드 시 원본 h1이
    demote_headings()로 강등된 것)를 다시 "# "로 승격해서 시작한다.

    알려진 한계(빌드 시점에 원본 정보가 이미 사라져 완전 복원 불가):
    - Mermaid 다이어그램: mermaid_prerender.py가 빌드 시 성공적으로 렌더링된
      다이어그램은 원본 소스를 SVG로 대체하며 지운다(prerender_mermaid()의
      `div.clear()` 참고). 그런 다이어그램은 SVG를 이미지로만 보존하고
      마크다운에 "원본 소스 복원 불가" 코멘트를 남긴다 — 다시 편집 가능한
      Mermaid로 쓰려면 수작업 재작성이 필요하다. 렌더링에 실패해 원본 텍스트가
      그대로 남은 다이어그램(`data-prerendered` 속성 없음)은 ` ```mermaid `
      펜스로 완전히 복원된다.
    - 콜아웃(tip-box): 원본에 어떤 마커 문자열을 썼는지(book.yaml의
      tip_markers)는 HTML에 남지 않아, 일반 blockquote로 내보낸다.
    - 챕터 간 상호참조 링크: build_html()이 `#앵커`로 재작성한 뒤라 원래
      가리키던 상대경로 .md 파일을 복원할 수 없다 — 앵커 링크 그대로 남는다.
    """
    if language is None:
        html_tag = soup.find("html")
        lang_attr = html_tag.get("lang") if html_tag else None
        language = lang_attr if isinstance(lang_attr, str) and lang_attr else "ko"

    out_dir.mkdir(parents=True, exist_ok=True)
    images_dir = out_dir / "images"

    written: list[Path] = []
    img_counter = 0
    diagram_counter = 0

    for idx, sec in enumerate(soup.find_all("section", id=True), start=1):
        h2 = sec.find("h2")
        if not h2:
            continue
        title = h2.get_text(strip=True)
        sec_id = sec.get("id") or f"section-{idx}"

        clone = BeautifulSoup(str(sec), "html.parser")
        clone_h2 = clone.find("h2")
        if clone_h2:
            clone_h2.decompose()
        # html_book.py의 demote_headings()가 원본 h1→h2뿐 아니라 섹션 본문의
        # 하위 헤딩도 통째로 한 단계씩 낮춘다(h2→h3, h3→h4, h4→h5) — 위에서
        # 챕터 제목(h2)만 "# "로 되돌리고 본문 하위 헤딩은 그대로 두면 원래
        # "## 절"이었던 게 "### 절"로 어긋난다. 같은 규칙을 역순으로 적용해
        # 본문 헤딩도 원래 레벨로 승격한다.
        for level, new_level in ((3, 2), (4, 3), (5, 4)):
            for tag in clone.find_all(f"h{level}"):
                tag.name = f"h{new_level}"
        for script in clone.find_all("script"):
            script.decompose()
        # 콜아웃(tip-box)이 어떤 마커 문자열을 썼는지는 여기서 알 길이 없다
        # (book.yaml 없이 완성된 HTML만 봄) — 태그만 blockquote로 바꿔
        # markdownify가 "> "로 렌더링하게 한다. 마커 이모지 자체는 내부
        # 텍스트에 그대로 남아 있어 결과 blockquote 첫 줄에서 보인다.
        for div in clone.find_all("div", class_="tip-box"):
            div.name = "blockquote"

        for img in clone.find_all("img"):
            src = img.get("src", "")
            if isinstance(src, str) and src.startswith("data:"):
                img_counter += 1
                saved = _write_data_uri_image(src, images_dir, f"{sec_id}-{img_counter}")
                if saved:
                    img["src"] = f"images/{saved.name}"

        # markdownify는 텍스트 노드를 그대로 지나치지 않고 마크다운 특수문자를
        # 이스케이프할 수 있어, 다이어그램 대체 텍스트(펜스·이미지 링크)를 직접
        # 넣으면 깨질 수 있다 — render.py의 @@BLOCK_n@@ 패턴과 동일하게, 우선
        # 안전한 토큰으로 바꿔 변환을 통과시킨 뒤 완성된 마크다운에서 치환한다.
        placeholders: dict[str, str] = _protect_code_blocks(clone)
        for div in clone.find_all("div", class_="mermaid"):
            diagram_counter += 1
            # markdownify는 "_"/"*" 같은 마크다운 특수문자가 든 텍스트 노드를
            # 이스케이프한다("_" → "\_") — 토큰에 그런 문자를 쓰면 치환 시점에
            # 문자열이 안 맞아 그대로 남는다(회귀 재현됨). 영숫자만 쓴다.
            token = f"MDBOOKBINDERMERMAIDPLACEHOLDER{diagram_counter}END"
            if div.get("data-prerendered") == "true":
                svg = div.find("svg")
                if svg:
                    images_dir.mkdir(parents=True, exist_ok=True)
                    svg_path = images_dir / f"{sec_id}-diagram-{diagram_counter}.svg"
                    svg_path.write_text(str(svg), encoding="utf-8")
                    placeholders[token] = (
                        "\n\n<!-- Mermaid 원본 소스는 빌드 시 사전 렌더링되며 사라졌습니다 "
                        "— 아래는 렌더링된 결과 이미지이며, 다시 편집하려면 Mermaid 코드를 "
                        "손으로 재작성해야 합니다. -->\n\n"
                        f"![다이어그램](images/{svg_path.name})\n\n"
                    )
                else:
                    placeholders[token] = "\n\n<!-- Mermaid 다이어그램(내용 복원 불가) -->\n\n"
            else:
                code = div.get_text().strip()
                placeholders[token] = f"\n\n```mermaid\n{code}\n```\n\n"
            div.replace_with(token)

        section_tag = clone.find("section")
        inner_html = section_tag.decode_contents() if section_tag else str(clone)

        md = markdownify(inner_html, heading_style="ATX")
        for token, replacement in placeholders.items():
            md = md.replace(token, replacement)
        md = re.sub(r"\n{3,}", "\n\n", md).strip()

        out_path = out_dir / f"{idx:03d}_{sec_id}.md"
        out_path.write_text(f"# {title}\n\n{md}\n", encoding="utf-8")
        written.append(out_path)

    write_minimal_book_yaml(out_dir / "book.yaml", language=language)
    return written


class BookHTMLEditor:
    """HTML 도서 파일의 섹션 단위 편집을 관리한다."""

    def __init__(self, html_path: str, soup: BeautifulSoup | None = None):
        self.html_path = Path(html_path)
        if not self.html_path.exists():
            raise FileNotFoundError(f"HTML file not found: {html_path}")

        if soup is not None:
            self.soup = soup
        else:
            with open(self.html_path, "r", encoding="utf-8") as f:
                self.soup = BeautifulSoup(f.read(), "html.parser")

        self._deduplicate_section_ids()

        # Staging: {section_id: {"title": ..., "markdown": ...}} or "deleted"
        self._staged: dict[str, _StagedChange] = {}
        # Image additions: {section_id: [{"path": ..., "caption": ...}]}
        self._added_images: dict[str, list[dict]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_book_meta(self) -> dict:
        """도서 메타데이터와 섹션 목록을 반환한다."""
        # html_book.py는 문서 레벨 <h1>을 렌더링하지 않으므로(각 챕터의 h1은
        # 섹션 안에서 h2로 강등됨) <title> 태그를 기본 폴백으로 쓴다.
        h1_tag = self.soup.find("h1")
        title_tag = self.soup.find("title")
        if h1_tag:
            title = h1_tag.get_text(strip=True)
        elif title_tag:
            title = title_tag.get_text(strip=True)
        else:
            title = "Untitled"

        sections = []
        for sec in self.soup.find_all("section", id=True):
            sec_id = str(sec.get("id", ""))
            h2 = sec.find("h2")
            if not h2:
                continue
            sec_title = h2.get_text(strip=True)

            text = sec.get_text(separator=" ")
            word_count = len(text.split())

            img_count = len(sec.find_all("img"))
            dgm_count = len(sec.find_all("div", class_="mermaid"))

            staged = self._staged.get(sec_id)
            if staged == "deleted":
                status = "deleted"
            elif staged is not None:
                status = "modified"
            else:
                status = "original"

            sections.append(
                {
                    "id": sec_id,
                    "title": sec_title,
                    "word_count": word_count,
                    "image_count": img_count,
                    "diagram_count": dgm_count,
                    "status": status,
                }
            )

        return {"title": title, "sections": sections}

    def get_section_content(self, section_id: str) -> dict:
        """섹션 내용을 Markdown으로 반환한다(markdownify 경유)."""
        sec = self.soup.find("section", id=section_id)
        if not sec:
            return {"error": f"Section not found: {section_id}"}

        h2 = sec.find("h2")
        title = h2.get_text(strip=True) if h2 else ""

        staged = self._staged.get(section_id)
        if staged and staged != "deleted":
            return {
                "id": section_id,
                "title": staged.get("title", title),
                "markdown": staged.get("markdown", ""),
                "word_count": len(staged.get("markdown", "").split()),
                "status": "modified",
            }

        md = self._html_section_to_markdown(sec)
        word_count = len(md.split())

        return {
            "id": section_id,
            "title": title,
            "markdown": md,
            "word_count": word_count,
            "status": "original",
        }

    def update_section_content(
        self, section_id: str, markdown_text: str, title: str | None = None
    ) -> dict:
        """섹션 내용 수정을 스테이징한다(디스크에 쓰지 않음)."""
        sec = self.soup.find("section", id=section_id)
        if not sec:
            return {"success": False, "error": f"Section not found: {section_id}"}

        h2 = sec.find("h2")
        current_title = h2.get_text(strip=True) if h2 else ""

        self._staged[section_id] = {
            "title": title if title is not None else current_title,
            "markdown": markdown_text,
        }
        return {"success": True, "section_id": section_id}

    def delete_section(self, section_id: str) -> bool:
        """섹션 삭제를 스테이징한다."""
        sec = self.soup.find("section", id=section_id)
        if not sec:
            return False
        self._staged[section_id] = "deleted"
        return True

    def stage_add_image(self, section_id: str, image_path: str, caption: str = "") -> bool:
        sec = self.soup.find("section", id=section_id)
        if not sec:
            return False
        self._added_images.setdefault(section_id, []).append(
            {"path": image_path, "caption": caption}
        )
        return True

    def unstage_add_image(self, section_id: str, index: int) -> bool:
        images = self._added_images.get(section_id, [])
        if 0 <= index < len(images):
            images.pop(index)
            return True
        return False

    def get_pending_additions(self, section_id: str) -> list[dict]:
        return list(self._added_images.get(section_id, []))

    def deleted_section_ids(self) -> set[str]:
        """이번 세션에서 삭제 스테이징된 섹션 id 목록을 반환한다.

        server.py의 이미지 재매핑(apply_all_changes()가 만든 새 soup에서
        image_editor가 참조하던 <img> 태그를 src 문자열로 다시 찾는 로직)이
        이미 삭제된 섹션에 속했던 이미지 항목까지 매칭 대상으로 끼워 넣지
        않도록 걸러내는 데 쓰인다 — 그러지 않으면 동일 src를 가진 이미지가
        문서에 두 개 이상 있을 때, 삭제된 섹션의 이미지 항목이 먼저
        선점(pop)해 살아있는 다른 이미지의 자리를 가로채고, 정작 그 살아있는
        이미지에 대한 삭제/교체 요청은 매칭 실패로 조용히 무시되는 회귀가
        있었다(실사용 재현·확인됨).
        """
        return {sid for sid, change in self._staged.items() if change == "deleted"}

    def apply_all_changes(self) -> BeautifulSoup:
        """스테이징된 모든 변경을 soup 사본에 적용한다. 디스크 저장은 호출부 책임."""
        for section_id, change in self._staged.items():
            sec = self.soup.find("section", id=section_id)
            if not sec:
                logger.warning(f"Section {section_id} not found during apply; skipping")
                continue

            if change == "deleted":
                sec.decompose()
                self._remove_toc_entry(section_id)
                self._unlink_dead_references(section_id)
            else:
                new_title = change.get("title", "")
                h2 = sec.find("h2")
                if h2 and new_title:
                    old_text = h2.get_text(strip=True)
                    prefix_match = re.match(r"^(\d+\.\s+)", old_text)
                    prefix = prefix_match.group(1) if prefix_match else ""
                    clean_title = re.sub(r"^\d+\.\s+", "", new_title)
                    h2.string = prefix + clean_title

                md_text = change.get("markdown", "")
                new_html = self._markdown_to_section_html(md_text)
                self._replace_section_content(sec, new_html)

                self._update_toc_entry(section_id, new_title)

        for section_id, images in self._added_images.items():
            sec = self.soup.find("section", id=section_id)
            if not sec:
                logger.warning(f"Section {section_id} not found for image addition; skipping")
                continue
            for img_info in images:
                self._append_figure(sec, img_info["path"], img_info.get("caption", ""))

        return self.soup

    def export_markdown(self, out_dir: Path, *, language: str | None = None) -> list[Path]:
        """스테이징된 변경을 전부 반영한 뒤 마크다운 코퍼스로 내보낸다(역방향 저장).

        apply_all_changes()로 이 세션의 편집(텍스트 수정·삭제·이미지 추가)을
        먼저 self.soup에 반영한 뒤 export_markdown_corpus()에 넘긴다 — 그
        함수의 docstring에 알려진 한계(Mermaid 원본 소스·콜아웃 마커·챕터 간
        상대경로 링크 복원 불가)가 정리돼 있다.
        """
        soup = self.apply_all_changes()
        return export_markdown_corpus(soup, out_dir, language=language)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _deduplicate_section_ids(self) -> None:
        """중복된 <section id="..."> 를 제자리에서 고유하게 만든다."""
        seen: dict[str, int] = {}
        all_ids: set[str] = {str(s.get("id", "")) for s in self.soup.find_all("section", id=True)}

        for sec in self.soup.find_all("section", id=True):
            sec_id = str(sec.get("id", ""))
            occurrence = seen.get(sec_id, 0)
            seen[sec_id] = occurrence + 1

            if occurrence == 0:
                continue

            counter = occurrence + 1
            new_id = f"{sec_id}_{counter}"
            while new_id in all_ids:
                counter += 1
                new_id = f"{sec_id}_{counter}"
            all_ids.add(new_id)

            sec["id"] = new_id

            dup_links = self.soup.find_all("a", href=f"#{sec_id}", class_="toc-link")
            if occurrence < len(dup_links):
                dup_links[occurrence]["href"] = f"#{new_id}"

    def _html_section_to_markdown(self, sec: Tag) -> str:
        """섹션의 내부 HTML을 Markdown으로 변환한다(figure/다이어그램/콜아웃 제외).

        링크는(strip=["a"]로 예전엔 지웠었다) 그대로 살려서 markdownify에
        넘긴다 — 지워버리면 저장 시 아무것도 안 건드린 문장에서도 링크(외부
        URL 포함)가 통째로 사라지는 회귀가 있었다(실사용 재현·확인됨). 이미지도
        마찬가지다 — `<figure>`로 감싸진 이미지(에디터의 "이미지 추가" 기능이
        붙인 것, _append_figure() 참고)만 편집 대상에서 제외하고, 저작
        마크다운의 일반 `![alt](src)`(빌드되면 `<figure>` 없이 `<p><img></p>`가
        됨, render.py 참고)는 링크와 똑같이 markdownify에 그대로 넘겨야 한다.
        예전에는 `<figure>` 제거 뒤 남은 `<img>`까지 전부 지우는 별도 루프가
        있어서, 아무것도 안 건드린 섹션을 열었다가 저장만 해도(no-op) 저작
        이미지가 마크다운에서 통째로 사라지는 회귀가 있었다(실사용 재현·확인됨).
        콜아웃(tip-box)은 markdownify가 `<div class="tip-box">`의 의미를 모르고
        일반 `<p>`로 뭉개버려(등록된 마커 이모지만 남고 상자 스타일이
        사라짐), figure/다이어그램과 같은 방식으로 편집 대상에서 아예
        제외하고 원본을 그대로 보존한다(_replace_section_content() 참고).
        코드 블록은 _protect_code_blocks()로 언어(class="language-X")를
        보존한다 — markdownify 기본 변환은 언어 정보를 버리고 빈 펜스만
        남긴다(실사용 재현·확인됨).
        """
        clone = BeautifulSoup(str(sec), "html.parser")

        h2 = clone.find("h2")
        if h2:
            h2.decompose()

        # 아래 제거 대상은 _preserved_kind()가 판정하는 것과 같은 집합이다
        # (figure/다이어그램/tip-box) — 이쪽만 고치고 저쪽을 안 고치면
        # 한쪽만 인식하는 요소가 생겨 조용히 사라지거나 중복될 수 있으니
        # 두 곳을 함께 유지해야 한다(_preserved_kind() docstring 참고).
        # _preserved_kind()를 여기서 그대로 재사용하지 않는 이유는, my-8
        # 래퍼를 먼저 통째로 지운 뒤에야 순서대로 최신 상태를 다시
        # find_all()해야 안전하기 때문이다 — 미리 한 번에 스냅샷을 떠서
        # 반복하면(예: find_all(["figure","div"]) 한 번) my-8 부모가 이미
        # decompose()된 뒤 그 자식(중첩된 mermaid)에 다시 접근하려다
        # AttributeError가 난다(bs4 decompose()는 속성을 지워버림 — 직접
        # 확인함).
        for fig in clone.find_all("figure"):
            fig.decompose()
        for div in clone.find_all("div", class_="my-8"):
            if div.find("div", class_="mermaid"):
                div.decompose()
        for div in clone.find_all("div", class_="mermaid"):
            div.decompose()
        for div in clone.find_all("div", class_="tip-box"):
            div.decompose()
        for script in clone.find_all("script"):
            script.decompose()

        placeholders = _protect_code_blocks(clone)

        section_tag = clone.find("section")
        inner_html = section_tag.decode_contents() if section_tag else str(clone)

        md = markdownify(inner_html, heading_style="ATX")
        for token, replacement in placeholders.items():
            md = md.replace(token, replacement)
        md = re.sub(r"\n{3,}", "\n\n", md).strip()
        return md

    def _markdown_to_section_html(self, md_text: str) -> str:
        # sane_lists 없이는 python-markdown이 마커만 다른(*, 1.) 인접 목록을
        # 빈 줄로 떼어놔도 하나로 합쳐버린다(순서 목록이 통째로 글머리 기호
        # 목록에 흡수되는 회귀 재현됨) — render.py의 md_to_html()과 같은
        # 확장 목록을 써서 두 변환 경로의 결과가 어긋나지 않게 한다.
        return markdown.markdown(
            md_text, extensions=["fenced_code", "tables", "nl2br", "sane_lists"]
        )

    def _replace_section_content(self, sec: Tag, new_html: str) -> None:
        """섹션의 텍스트/콘텐츠 자식을 교체하되 figure/다이어그램/콜아웃은
        원래 있던 자리 근처에 다시 끼워 넣는다.

        편집 가능한 마크다운에는 이 요소들 자체가 아예 안 보이므로
        (_html_section_to_markdown 참고) 정확한 삽입 지점을 알 방법은 없다
        — 대신 "일반 콘텐츠 태그 몇 개 뒤에 있었는지"를 기억해뒀다가 새
        콘텐츠에서 같은 지점에 되돌려 놓는다. 예전에는 무조건 새 콘텐츠를
        다 채운 뒤 맨 끝에 이어붙여서, 편집 여부와 무관하게 저장할 때마다
        다이어그램·콜아웃이 섹션 맨 뒤로 밀려나는 회귀가 있었다(실사용
        재현·확인됨) — "문단A → 다이어그램 → 문단B" 구조가 저장 후
        "문단A → 문단B → 다이어그램"이 됐다.
        """
        heading: Tag | None = None
        preserved: list[tuple[Tag, int]] = []
        content_count = 0
        for child in list(sec.children):
            if not isinstance(child, Tag):
                continue
            if child.name == "h2":
                heading = child
            elif _preserved_kind(child) is not None:
                preserved.append((child, content_count))
            else:
                content_count += 1

        new_soup = BeautifulSoup(new_html, "html.parser")
        # 위치 계산의 고정 기준 좌표계 — 아래 루프에서 절대 변경하지 않는다.
        new_children: list = list(new_soup.children)

        def _target_index(position: int) -> int:
            counted = 0
            for i, c in enumerate(new_children):
                if isinstance(c, Tag):
                    if counted == position:
                        return i
                    counted += 1
            return len(new_children)

        final_children: list = list(new_children)
        # 원본 문서 순서의 역순으로 삽입한다. target_index는 항상 고정된
        # new_children 기준으로만 계산해야 한다 — 계속 자라는 final_children을
        # 기준으로 다시 스캔하면, 이미 삽입해둔 다른 보존 요소도 Tag이므로
        # "일반 콘텐츠 태그"로 잘못 세어져, 문단 없이 바로 인접한 보존 요소
        # 둘 이상(다이어그램 두 개가 연달아 오는 등)의 상대 순서가 뒤집히는
        # 회귀가 있었다(실사용 재현·확인됨 — "다이어그램A→다이어그램B"가
        # 저장 후 "다이어그램B→다이어그램A"가 됐다). 역순으로 처리하면 같은
        # position을 공유하는 요소끼리도 원본상 뒤에 있던 것부터 같은 자리에
        # 꽂히고, 그다음 처리되는(원본상 앞선) 요소가 매번 그 앞으로 끼어들어
        # 최종적으로 원본 순서가 그대로 보존된다.
        for tag, position in reversed(preserved):
            final_children.insert(_target_index(position), tag)

        sec.clear()
        if heading is not None:
            sec.append(heading)
        for child in final_children:
            sec.append(child)

    def _unlink_dead_references(self, section_id: str) -> None:
        """삭제된 섹션을 가리키던 다른 섹션의 상호참조 링크를 정리한다.

        html_book.py의 _rewrite_internal_links()가 챕터 간 상대경로 링크를
        `href="#{section_id}"` 앵커로 재작성해두는데(build_html() 참고),
        delete_section()은 사이드바 TOC 항목만 지우고 본문에 남은 이
        앵커 링크는 그대로 뒀다 — 삭제 후에도 다른 챕터에 아무 데도 안
        가는 죽은 링크가 남는 회귀였다(실사용 재현·확인됨). 이 시점엔
        _remove_toc_entry()가 이미 사이드바 쪽 링크는 지운 뒤라, 여기서
        찾는 건 본문 안의 상호참조뿐이다. 링크를 통째로 지우면 문장이
        부자연스러워지므로 href만 없애고 텍스트는 그대로 남긴다(unwrap).
        """
        for link in self.soup.find_all("a", href=f"#{section_id}"):
            link.unwrap()

    def _remove_toc_entry(self, section_id: str) -> None:
        link = self.soup.find("a", href=f"#{section_id}")
        if not link:
            return
        li = link.find_parent("li")
        if li:
            li.decompose()
        else:
            link.decompose()

    def _update_toc_entry(self, section_id: str, new_title: str) -> None:
        if not new_title:
            return
        link = self.soup.find("a", href=f"#{section_id}")
        if link:
            clean_title = re.sub(r"^\d+\.\s+", "", new_title)
            old_text = link.get_text(strip=True)
            prefix_match = re.match(r"^(\d+\.\s+)", old_text)
            prefix = prefix_match.group(1) if prefix_match else ""
            link.string = prefix + clean_title

    def _append_figure(self, sec: Tag, image_path: str, caption: str = "") -> None:
        fig = self.soup.new_tag("figure", attrs={"class": "my-6 text-center"})
        src = image_path if image_path.startswith("data:") else image_to_data_uri(Path(image_path))
        img = self.soup.new_tag(
            "img",
            src=src,
            alt=caption or "추가된 이미지",
            attrs={"class": "max-w-full rounded shadow mx-auto"},
        )
        fig.append(img)
        if caption:
            figcap = self.soup.new_tag("figcaption")
            figcap.string = caption
            fig.append(figcap)
        sec.append(fig)
