"""HTML 도서 내 이미지/다이어그램 편집 도구.

Lecture_forge의 `tools/image_editor.py`(ImageEditor)를 lecture-forge 비의존으로
포크한 것이다. 벡터스토어 기반 유사 이미지 추천(`_search_vector_db` 등)은
책 편집에 불필요한 임베딩 의존성이라 제외했다 — 대안 이미지는 파일시스템
검색(`_search_filesystem`)만으로 찾는다.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import NotRequired, TypedDict

from bs4 import BeautifulSoup, Tag

from mdbook_binder.imgembed import image_to_data_uri

logger = logging.getLogger("mdbook_binder.editor")

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


class _Replacement(TypedDict):
    new_path: str


class _Changes(TypedDict):
    delete: set[int]
    replace: dict[int, _Replacement]
    add: list[dict]
    diagram_delete: set[int]


class _ImageAlternative(TypedDict):
    path: str
    page: int | None
    source: str
    filename: str
    score: int
    index: NotRequired[int]
    description: NotRequired[str]


class ImageEditor:
    """HTML 도서 안의 이미지를 편집하는 도구."""

    def __init__(self, html_path: str):
        self.html_path = Path(html_path)
        if not self.html_path.exists():
            raise FileNotFoundError(f"HTML file not found: {html_path}")

        with open(self.html_path, "r", encoding="utf-8") as f:
            self.html_content = f.read()

        self.soup = BeautifulSoup(self.html_content, "html.parser")

        self.images = self._extract_images()
        self.diagrams = self._extract_diagrams()

        self.changes: _Changes = {
            "delete": set(),
            "replace": {},
            "add": [],
            "diagram_delete": set(),
        }

        logger.info(
            f"Image Editor initialized: {len(self.images)} images, {len(self.diagrams)} diagrams found"
        )

    def _extract_images(self) -> list[dict]:
        images = []
        for idx, img_tag in enumerate(self.soup.find_all("img"), 1):
            images.append(
                {
                    "index": idx,
                    "tag": img_tag,
                    "src": img_tag.get("src", ""),
                    "alt": img_tag.get("alt", ""),
                    "caption": self._extract_caption(img_tag),
                    "section": self._find_section(img_tag),
                    "page": self._extract_page_number(img_tag),
                }
            )
        return images

    def _extract_diagrams(self) -> list[dict]:
        diagrams = []
        mermaid_divs = self.soup.find_all("div", class_="mermaid")
        for idx, mermaid_div in enumerate(mermaid_divs, 1):
            container = mermaid_div.find_parent("div", class_="my-8") or mermaid_div
            h4 = container.find("h4") if container is not mermaid_div else None
            title = h4.get_text(strip=True) if h4 else f"다이어그램 {idx}"

            code = mermaid_div.get_text(strip=True)
            words = code.split()
            diagram_type = words[0].lower() if words else "diagram"

            diagrams.append(
                {
                    "index": idx,
                    "mermaid_div": mermaid_div,
                    "container": container,
                    "mermaid_code": code,
                    "title": title,
                    "diagram_type": diagram_type,
                    "section": self._find_section(mermaid_div),
                }
            )
        return diagrams

    def _extract_caption(self, img_tag) -> str:
        figure = img_tag.find_parent("figure")
        if figure:
            figcaption = figure.find("figcaption")
            if figcaption:
                return figcaption.get_text(strip=True)
        return ""

    def _find_section(self, img_tag) -> str:
        for parent in img_tag.parents:
            if parent.name in ["section", "div"]:
                heading = parent.find(["h1", "h2", "h3"])
                if heading:
                    return heading.get_text(strip=True)
        return "Unknown section"

    def _extract_page_number(self, img_tag) -> int | None:
        alt_text = img_tag.get("alt", "")
        caption = self._extract_caption(img_tag)
        for text in [alt_text, caption]:
            match = re.search(r"page\s+(\d+)", text, re.IGNORECASE)
            if match:
                return int(match.group(1))
        return None

    def list_elements(self) -> list[dict]:
        """이미지·다이어그램을 문서 순서대로 통합해 반환한다."""
        img_by_id = {id(img["tag"]): img for img in self.images}
        dgm_by_id = {id(dgm["mermaid_div"]): dgm for dgm in self.diagrams}

        elements = []
        seen_dgm_ids: set = set()

        for tag in self.soup.descendants:
            if not isinstance(tag, Tag):
                continue

            if tag.name == "img" and id(tag) in img_by_id:
                img = img_by_id[id(tag)]
                status = (
                    "delete"
                    if img["index"] in self.changes["delete"]
                    else "replace"
                    if img["index"] in self.changes["replace"]
                    else "keep"
                )
                elements.append(
                    {
                        "kind": "image",
                        "img_index": img["index"],
                        "dgm_index": None,
                        "title": (img["alt"] or img["caption"] or "설명 없음")[:50],
                        "section": img["section"],
                        "extra": f"p.{img['page']}" if img["page"] else "-",
                        "status": status,
                    }
                )
            elif tag.name == "div" and "mermaid" in (tag.get("class") or []):
                tag_id = id(tag)
                if tag_id in dgm_by_id and tag_id not in seen_dgm_ids:
                    seen_dgm_ids.add(tag_id)
                    dgm = dgm_by_id[tag_id]
                    status = "delete" if dgm["index"] in self.changes["diagram_delete"] else "keep"
                    elements.append(
                        {
                            "kind": "diagram",
                            "img_index": None,
                            "dgm_index": dgm["index"],
                            "title": dgm["title"][:50],
                            "section": dgm["section"],
                            "extra": dgm["diagram_type"],
                            "mermaid_code": dgm["mermaid_code"],
                            "status": status,
                        }
                    )

        for i, el in enumerate(elements, 1):
            el["display_index"] = i
        return elements

    def mark_delete(self, image_index: int) -> bool:
        if not (1 <= image_index <= len(self.images)):
            return False
        self.changes["delete"].add(image_index)
        return True

    def unmark_delete(self, image_index: int) -> bool:
        if image_index in self.changes["delete"]:
            self.changes["delete"].remove(image_index)
            return True
        return False

    def mark_delete_diagram(self, dgm_index: int) -> bool:
        if not (1 <= dgm_index <= len(self.diagrams)):
            return False
        self.changes["diagram_delete"].add(dgm_index)
        return True

    def unmark_delete_diagram(self, dgm_index: int) -> bool:
        if dgm_index in self.changes["diagram_delete"]:
            self.changes["diagram_delete"].discard(dgm_index)
            return True
        return False

    def find_alternative_images(
        self, image_index: int, max_results: int = 5
    ) -> list[_ImageAlternative]:
        if not (1 <= image_index <= len(self.images)):
            return []
        current_img = self.images[image_index - 1]
        return self._search_filesystem(current_img, max_results)

    def _search_filesystem(
        self, current_img: dict, max_results: int
    ) -> list[_ImageAlternative]:
        """HTML 파일 인근 디렉토리에서 대체 이미지를 찾는다.

        Lecture_forge 원본은 전역 Config.DATA_DIR(모든 강의 세션 공용 이미지
        저장소)까지 뒤졌지만, mdbook-binder에는 그런 전역 데이터 디렉토리 개념이
        없다 — 편집 중인 HTML 파일 인근(같은 디렉토리, images/ 하위)만 검색한다.
        """
        possible_bases = [
            self.html_path.parent / "images",
            self.html_path.parent,
        ]

        images_base = next((b for b in possible_bases if b.exists()), None)
        if not images_base:
            return []

        alternatives: list[_ImageAlternative] = []
        current_page = current_img.get("page")
        current_src = current_img.get("src", "")

        for img_file in images_base.rglob("*"):
            if not img_file.is_file() or img_file.suffix.lower() not in _IMAGE_EXTS:
                continue
            if str(img_file) == current_src or img_file.name in current_src:
                continue

            page_match = None
            page_pattern = re.search(r"page_?(\d+)", img_file.stem, re.IGNORECASE)
            if page_pattern:
                page_match = int(page_pattern.group(1))

            score = 5
            if page_match and current_page:
                page_diff = abs(page_match - current_page)
                score = (
                    100
                    if page_diff == 0
                    else 50
                    if page_diff <= 2
                    else 25
                    if page_diff <= 5
                    else 10
                )

            alternatives.append(
                {
                    "path": str(img_file),
                    "page": page_match,
                    "source": img_file.parent.name,
                    "filename": img_file.name,
                    "score": score,
                }
            )

        alternatives.sort(key=lambda x: x["score"], reverse=True)
        alternatives = alternatives[:max_results]
        for idx, alt in enumerate(alternatives, 1):
            alt["index"] = idx
            alt["description"] = f"Image from {alt['source']}"
            if alt["page"]:
                alt["description"] += f" (page {alt['page']})"
        return alternatives

    def replace_image(self, image_index: int, new_image_path: str) -> bool:
        if not (1 <= image_index <= len(self.images)):
            return False
        if not Path(new_image_path).exists():
            return False
        self.changes["replace"][image_index] = {"new_path": new_image_path}
        return True

    def save_changes(self, output_path: str | None = None) -> str:
        """스테이징된 이미지/다이어그램 변경을 적용하고 HTML을 저장한다."""
        if output_path is None:
            output_path = str(
                self.html_path.parent / f"{self.html_path.stem}_edited{self.html_path.suffix}"
            )

        for img_index in sorted(self.changes["delete"], reverse=True):
            img_tag = self.images[img_index - 1]["tag"]
            figure = img_tag.find_parent("figure")
            (figure or img_tag).decompose()

        for img_index, replacement in self.changes["replace"].items():
            img_tag = self.images[img_index - 1]["tag"]
            img_tag["src"] = image_to_data_uri(Path(replacement["new_path"]))

        for dgm_index in sorted(self.changes["diagram_delete"], reverse=True):
            self.diagrams[dgm_index - 1]["container"].decompose()

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(str(self.soup))

        return output_path

    def get_summary(self) -> dict:
        return {
            "total_images": len(self.images),
            "to_delete": len(self.changes["delete"]),
            "to_replace": len(self.changes["replace"]),
            "to_add": len(self.changes["add"]),
            "total_diagrams": len(self.diagrams),
            "diagrams_to_delete": len(self.changes["diagram_delete"]),
        }
