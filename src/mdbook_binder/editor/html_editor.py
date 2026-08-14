"""HTML 도서 편집 로직 — 섹션 단위 파싱/수정/삭제.

Lecture_forge의 `editor/html_editor.py`(LectureHTMLEditor)를 lecture-forge
비의존으로 포크한 것이다. html_book.py가 출력하는
`<section class="chapter-section" id="{slug}">` 구조에만 의존한다 — 그 구조를
만든 코퍼스가 어떤 관례를 썼는지는 전혀 모른다.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import markdown
from bs4 import BeautifulSoup, Tag
from markdownify import markdownify

from mdbook_binder.imgembed import image_to_data_uri

logger = logging.getLogger("mdbook_binder.editor")


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
        self._staged: dict[str, object] = {}
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
            sec_id = sec.get("id", "")
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

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _deduplicate_section_ids(self) -> None:
        """중복된 <section id="..."> 를 제자리에서 고유하게 만든다."""
        seen: dict[str, int] = {}
        all_ids: set = {s.get("id", "") for s in self.soup.find_all("section", id=True)}

        for sec in self.soup.find_all("section", id=True):
            sec_id = sec.get("id", "")
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
        """섹션의 내부 HTML을 Markdown으로 변환한다(figure/다이어그램 제외)."""
        clone = BeautifulSoup(str(sec), "html.parser")

        h2 = clone.find("h2")
        if h2:
            h2.decompose()

        for fig in clone.find_all("figure"):
            fig.decompose()
        for img in clone.find_all("img"):
            img.decompose()
        for div in clone.find_all("div", class_="my-8"):
            if div.find("div", class_="mermaid"):
                div.decompose()
        for div in clone.find_all("div", class_="mermaid"):
            div.decompose()
        for script in clone.find_all("script"):
            script.decompose()

        section_tag = clone.find("section")
        inner_html = section_tag.decode_contents() if section_tag else str(clone)

        md = markdownify(inner_html, heading_style="ATX", strip=["a"])
        md = re.sub(r"\n{3,}", "\n\n", md).strip()
        return md

    def _markdown_to_section_html(self, md_text: str) -> str:
        return markdown.markdown(md_text, extensions=["fenced_code", "tables", "nl2br"])

    def _replace_section_content(self, sec: Tag, new_html: str) -> None:
        """섹션의 텍스트/콘텐츠 자식을 교체하되 figure/다이어그램은 보존한다."""
        preserved = []
        for child in list(sec.children):
            if not isinstance(child, Tag):
                continue
            if child.name == "h2":
                preserved.append(("heading", child))
            elif child.name == "figure":
                preserved.append(("figure", child))
            elif (
                child.name == "div"
                and ("my-8" in (child.get("class") or []) and child.find("div", class_="mermaid"))
                or child.name == "div"
                and "mermaid" in (child.get("class") or [])
            ):
                preserved.append(("diagram", child))

        sec.clear()

        for kind, tag in preserved:
            if kind == "heading":
                sec.append(tag)
                break

        new_soup = BeautifulSoup(new_html, "html.parser")
        for elem in list(new_soup.children):
            sec.append(elem)

        for kind, tag in preserved:
            if kind in ("figure", "diagram"):
                sec.append(tag)

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
