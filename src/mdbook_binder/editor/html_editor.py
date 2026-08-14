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

import markdown
from bs4 import BeautifulSoup, Tag
from markdownify import markdownify

from mdbook_binder.imgembed import image_to_data_uri
from mdbook_binder.manifest import write_minimal_book_yaml

logger = logging.getLogger("mdbook_binder.editor")

_DATA_URI_RE = re.compile(r"^data:([^;,]*)?(;base64)?,(.*)$", re.DOTALL)


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
        """섹션의 내부 HTML을 Markdown으로 변환한다(figure/다이어그램/콜아웃 제외).

        링크는(strip=["a"]로 예전엔 지웠었다) 그대로 살려서 markdownify에
        넘긴다 — 지워버리면 저장 시 아무것도 안 건드린 문장에서도 링크(외부
        URL 포함)가 통째로 사라지는 회귀가 있었다(실사용 재현·확인됨). 콜아웃
        (tip-box)은 markdownify가 `<div class="tip-box">`의 의미를 모르고
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

        for fig in clone.find_all("figure"):
            fig.decompose()
        for img in clone.find_all("img"):
            img.decompose()
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
        """섹션의 텍스트/콘텐츠 자식을 교체하되 figure/다이어그램/콜아웃은 보존한다."""
        preserved = []
        for child in list(sec.children):
            if not isinstance(child, Tag):
                continue
            if child.name == "h2":
                preserved.append(("heading", child))
            elif child.name == "figure":
                preserved.append(("figure", child))
            elif child.name == "div" and "tip-box" in (child.get("class") or []):
                preserved.append(("tip-box", child))
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
            if kind in ("figure", "diagram", "tip-box"):
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
