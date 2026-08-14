"""editor/server.py Flask API 회귀 테스트.

Playwright 없이도(PDF 렌더링 경로만 브라우저가 필요하다) 테스트 가능한 두
영역을 고정한다:

1. `/api/images/serve` 경로 화이트리스트 — 과거엔 `str(path).startswith(...)`
   문자열 접두사 비교를 썼는데, "/tmp/book_project"와 "/tmp/book_project_evil"
   같은 형제 디렉토리를 구분 못 해 우회당할 수 있었다(경로 구성요소 단위가
   아니라 문자열 단위 비교라서). `Path.is_relative_to()`로 바뀐 뒤에도 정상
   경로는 계속 서빙되고, 형제 디렉토리 우회는 차단되는지 확인한다.
2. 섹션 CRUD·이미지 스테이징·요소(이미지/다이어그램) 편집 API — 스테이징만
   하고 디스크에 쓰지 않다가 `/api/save`에서 한 번에 적용되는 흐름 전체를
   실제 HTML 파일로 왕복 검증한다.

`_build_two_chapter_book()`은 `build_html()`을 거치지 않고 `<section
class="chapter-section" id="...">` 계약(html_book.py 모듈 docstring 참고)을
직접 만족하는 HTML을 손으로 써서 만든다 — editor/는 이 마크업 계약에만
의존하고 그걸 누가 만들었는지는 모르므로, 다이어그램이 있는 문서로 이 API를
테스트하는 데 실제 Mermaid 사전 렌더링(Playwright/Chromium 구동)까지 거칠
필요가 없다.
"""

import base64
from pathlib import Path

from mdbook_binder.editor.server import create_app
from mdbook_binder.html_book import build_html

_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415408d763f8cfc0c0c00000030101006bec38ba0000000049454e44ae426082"
)


def _build_book(root: Path) -> Path:
    (root / "chapter.md").write_text("# 챕터\n\n본문.\n", encoding="utf-8")
    return build_html(root, config=None, out_path=root / "book.html")


def _build_two_chapter_book(root: Path) -> Path:
    img_data_uri = f"data:image/png;base64,{base64.b64encode(_PNG_BYTES).decode('ascii')}"
    html_path = root / "book.html"
    html_path.write_text(
        f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="UTF-8"><title>테스트 도서</title></head>
<body>
<section class="chapter-section" id="chapter-1">
<h2>챕터 1</h2>
<p>본문 텍스트입니다.</p>
<p><img src="{img_data_uri}" alt="그림 설명"></p>
<div class="mermaid">graph TD
    A --> B</div>
</section>
<section class="chapter-section" id="chapter-2">
<h2>챕터 2</h2>
<p>두 번째 챕터 본문.</p>
</section>
</body>
</html>""",
        encoding="utf-8",
    )
    return html_path


def test_serve_image_allows_file_inside_allowed_root(tmp_path: Path):
    html_path = _build_book(tmp_path)
    img_path = tmp_path / "inside.png"
    img_path.write_bytes(_PNG_BYTES)

    client = create_app(str(html_path)).test_client()
    resp = client.get("/api/images/serve", query_string={"path": str(img_path)})

    assert resp.status_code == 200


def test_serve_image_rejects_sibling_directory_with_shared_prefix(tmp_path: Path):
    """형제 디렉토리(`book_evil`)가 허용 루트(`book`)와 문자열 접두사를 공유해도
    거부돼야 한다 — startswith() 비교였을 때 우회 가능했던 케이스."""
    root = tmp_path / "book"
    root.mkdir()
    html_path = _build_book(root)

    evil_root = tmp_path / "book_evil"
    evil_root.mkdir()
    secret = evil_root / "secret.png"
    secret.write_bytes(_PNG_BYTES)

    client = create_app(str(html_path)).test_client()
    resp = client.get("/api/images/serve", query_string={"path": str(secret)})

    assert resp.status_code == 403


def test_serve_image_rejects_path_outside_allowed_roots(tmp_path: Path):
    html_path = _build_book(tmp_path)
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(_PNG_BYTES)
    try:
        client = create_app(str(html_path)).test_client()
        resp = client.get("/api/images/serve", query_string={"path": str(outside)})

        assert resp.status_code == 403
    finally:
        outside.unlink()


# ---------------------------------------------------------------------- #
#  섹션 CRUD
# ---------------------------------------------------------------------- #


def test_book_meta_lists_sections_in_order_with_original_status(tmp_path: Path):
    html_path = _build_two_chapter_book(tmp_path)
    client = create_app(str(html_path)).test_client()

    resp = client.get("/api/lecture")
    data = resp.get_json()

    assert resp.status_code == 200
    titles = [s["title"] for s in data["sections"]]
    assert titles == ["챕터 1", "챕터 2"]
    assert all(s["status"] == "original" for s in data["sections"])
    assert data["sections"][0]["image_count"] == 1
    assert data["sections"][0]["diagram_count"] == 1


def test_get_section_returns_markdown_and_404_for_unknown_id(tmp_path: Path):
    html_path = _build_two_chapter_book(tmp_path)
    client = create_app(str(html_path)).test_client()
    sec_id = client.get("/api/lecture").get_json()["sections"][0]["id"]

    ok = client.get(f"/api/sections/{sec_id}")
    assert ok.status_code == 200
    assert "본문 텍스트입니다" in ok.get_json()["markdown"]

    missing = client.get("/api/sections/no-such-id")
    assert missing.status_code == 404


def test_update_section_stages_change_without_touching_disk(tmp_path: Path):
    """섹션 수정은 저장(`/api/save`) 전까지 원본 HTML 파일을 건드리지 않아야 한다."""
    html_path = _build_two_chapter_book(tmp_path)
    original_bytes = html_path.read_bytes()
    client = create_app(str(html_path)).test_client()
    sec_id = client.get("/api/lecture").get_json()["sections"][0]["id"]

    resp = client.post(
        f"/api/sections/{sec_id}", json={"markdown": "수정된 본문", "title": "새 제목"}
    )
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True
    assert html_path.read_bytes() == original_bytes

    meta = client.get("/api/lecture").get_json()
    assert meta["sections"][0]["status"] == "modified"

    content = client.get(f"/api/sections/{sec_id}").get_json()
    assert content["markdown"] == "수정된 본문"
    assert content["status"] == "modified"


def test_delete_section_is_staged_until_save(tmp_path: Path):
    html_path = _build_two_chapter_book(tmp_path)
    client = create_app(str(html_path)).test_client()
    sec_id = client.get("/api/lecture").get_json()["sections"][1]["id"]

    resp = client.delete(f"/api/sections/{sec_id}")
    assert resp.status_code == 200

    meta = client.get("/api/lecture").get_json()
    assert meta["sections"][1]["status"] == "deleted"

    missing = client.delete("/api/sections/no-such-id")
    assert missing.status_code == 404


def test_save_applies_update_and_delete_to_output_html(tmp_path: Path):
    html_path = _build_two_chapter_book(tmp_path)
    out_path = tmp_path / "book_edited.html"
    client = create_app(str(html_path), output_path=str(out_path)).test_client()
    meta = client.get("/api/lecture").get_json()
    keep_id, drop_id = meta["sections"][0]["id"], meta["sections"][1]["id"]

    client.post(
        f"/api/sections/{keep_id}", json={"markdown": "새로운 본문 내용", "title": "챕터 1"}
    )
    client.delete(f"/api/sections/{drop_id}")

    resp = client.post("/api/save")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True

    saved_html = Path(body["path"]).read_text(encoding="utf-8")
    assert "새로운 본문 내용" in saved_html
    assert f'id="{drop_id}"' not in saved_html


# ---------------------------------------------------------------------- #
#  이미지 스테이징 (섹션에 추가할 이미지)
# ---------------------------------------------------------------------- #


def test_pending_image_add_list_and_remove_roundtrip(tmp_path: Path):
    html_path = _build_two_chapter_book(tmp_path)
    new_img = tmp_path / "new.png"
    new_img.write_bytes(_PNG_BYTES)
    client = create_app(str(html_path)).test_client()
    sec_id = client.get("/api/lecture").get_json()["sections"][0]["id"]

    add = client.post(
        f"/api/sections/{sec_id}/images", json={"path": str(new_img), "caption": "캡션"}
    )
    assert add.status_code == 200
    assert add.get_json()["success"] is True

    listed = client.get(f"/api/sections/{sec_id}/images").get_json()
    assert len(listed["additions"]) == 1
    assert listed["additions"][0]["caption"] == "캡션"

    removed = client.delete(f"/api/sections/{sec_id}/images/0")
    assert removed.status_code == 200

    listed_after = client.get(f"/api/sections/{sec_id}/images").get_json()
    assert listed_after["additions"] == []


def test_add_pending_image_rejects_missing_file(tmp_path: Path):
    html_path = _build_two_chapter_book(tmp_path)
    client = create_app(str(html_path)).test_client()
    sec_id = client.get("/api/lecture").get_json()["sections"][0]["id"]

    resp = client.post(f"/api/sections/{sec_id}/images", json={"path": str(tmp_path / "nope.png")})

    assert resp.status_code == 400


# ---------------------------------------------------------------------- #
#  요소(이미지/다이어그램) 편집
# ---------------------------------------------------------------------- #


def test_elements_lists_image_and_diagram_in_document_order(tmp_path: Path):
    html_path = _build_two_chapter_book(tmp_path)
    client = create_app(str(html_path)).test_client()

    elements = client.get("/api/elements").get_json()["elements"]

    kinds = [(e["kind"], e["status"]) for e in elements]
    assert kinds == [("image", "keep"), ("diagram", "keep")]


def test_toggle_element_marks_image_for_deletion_and_save_removes_it(tmp_path: Path):
    html_path = _build_two_chapter_book(tmp_path)
    out_path = tmp_path / "book_edited.html"
    client = create_app(str(html_path), output_path=str(out_path)).test_client()
    image_el = next(
        e for e in client.get("/api/elements").get_json()["elements"] if e["kind"] == "image"
    )

    toggled = client.patch(f"/api/elements/{image_el['display_index']}", json={"action": "delete"})
    assert toggled.status_code == 200
    assert toggled.get_json()["elements"][0]["status"] == "delete"

    saved = client.post("/api/save").get_json()
    saved_html = Path(saved["path"]).read_text(encoding="utf-8")
    assert "<img" not in saved_html
