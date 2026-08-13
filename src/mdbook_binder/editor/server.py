"""MDBook-binder 웹 편집기 — Flask API 서버.

Lecture_forge의 `editor/server.py`를 lecture-forge 비의존으로 포크한 것이다.
전역 `Config.DATA_DIR`/`Config.OUTPUT_DIR`(모든 강의 세션 공용 디렉토리) 대신
편집 중인 HTML 파일 기준 상대 경로만 사용한다 — mdbook-binder에는 그런 전역
데이터 디렉토리 개념이 없기 때문이다.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
import threading
import uuid
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory

from mdbook_binder.editor.html_editor import BookHTMLEditor
from mdbook_binder.editor.image_editor import ImageEditor
from mdbook_binder.mermaid_prerender import mermaid_font_face_css, mermaid_label_css

logger = logging.getLogger("mdbook_binder.editor")

_ALLOWED_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_TEMPLATE_DIR = Path(__file__).parent.parent / "templates" / "editor"
_INDEX_TEMPLATE = _TEMPLATE_DIR / "index.html"
# Tailwind/EasyMDE/marked/mermaid를 CDN 대신 로컬로 번들해 서빙한다 —
# CDN이 막힌 네트워크에서 편집기 초기화가 중간에 멈춰버리는 문제가 있었다.
_VENDOR_DIR = _TEMPLATE_DIR.parent / "vendor"


def create_app(html_path: str, output_path: str | None = None) -> Flask:
    """편집 중인 HTML_PATH에 대한 Flask 앱을 만든다."""
    app = Flask(
        __name__,
        template_folder=str(_TEMPLATE_DIR),
        static_folder=str(_TEMPLATE_DIR),
        static_url_path="/static",
    )
    app.config["SECRET_KEY"] = os.urandom(24)

    html_p = Path(html_path)
    if output_path is None:
        output_path = str(html_p.parent / f"{html_p.stem}_edited{html_p.suffix}")

    upload_dir = html_p.parent / "_uploaded_images"

    html_editor = BookHTMLEditor(html_path)
    image_editor = ImageEditor(html_path)

    # ------------------------------------------------------------------ #
    #  SPA
    # ------------------------------------------------------------------ #

    @app.route("/")
    def index():
        filename = Path(html_path).name
        content = _INDEX_TEMPLATE.read_text(encoding="utf-8")
        font_css = f"{mermaid_font_face_css()}\n{mermaid_label_css()}"
        return content.replace("{{ filename }}", filename).replace(
            "{{ mermaid_font_css }}", font_css
        )

    @app.route("/vendor/<path:filename>")
    def vendor(filename: str):
        return send_from_directory(_VENDOR_DIR, filename)

    # ------------------------------------------------------------------ #
    #  Book meta
    # ------------------------------------------------------------------ #

    @app.route("/api/lecture")
    def api_book_meta():
        try:
            return jsonify(html_editor.get_book_meta())
        except Exception as exc:
            logger.error(f"api_book_meta error: {exc}")
            return jsonify({"error": str(exc)}), 500

    # ------------------------------------------------------------------ #
    #  Section CRUD
    # ------------------------------------------------------------------ #

    @app.route("/api/sections/<section_id>", methods=["GET"])
    def api_get_section(section_id: str):
        try:
            data = html_editor.get_section_content(section_id)
            if "error" in data:
                return jsonify(data), 404
            return jsonify(data)
        except Exception as exc:
            logger.error(f"api_get_section error: {exc}")
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/sections/<section_id>", methods=["POST"])
    def api_update_section(section_id: str):
        try:
            body = request.get_json(force=True)
            result = html_editor.update_section_content(
                section_id, body.get("markdown", ""), body.get("title")
            )
            if not result.get("success"):
                return jsonify(result), 404
            return jsonify(result)
        except Exception as exc:
            logger.error(f"api_update_section error: {exc}")
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/sections/<section_id>/images", methods=["GET"])
    def api_get_pending_images(section_id: str):
        try:
            additions = html_editor.get_pending_additions(section_id)
            enriched = [
                {
                    "index": i,
                    "path": item["path"],
                    "caption": item.get("caption", ""),
                    "thumbnail": _encode_image_thumbnail(item["path"]),
                }
                for i, item in enumerate(additions)
            ]
            return jsonify({"additions": enriched})
        except Exception as exc:
            logger.error(f"api_get_pending_images error: {exc}")
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/sections/<section_id>/images", methods=["POST"])
    def api_add_image_to_section(section_id: str):
        try:
            body = request.get_json(force=True)
            image_path = body.get("path", "")
            caption = body.get("caption", "")
            if not image_path:
                return jsonify({"error": "No image path provided"}), 400
            if not image_path.startswith("data:") and not Path(image_path).exists():
                return jsonify({"error": "Image file not found"}), 400
            ok = html_editor.stage_add_image(section_id, image_path, caption)
            if not ok:
                return jsonify({"error": f"Section not found: {section_id}"}), 404
            thumbnail = _encode_image_thumbnail(image_path)
            additions = html_editor.get_pending_additions(section_id)
            return jsonify({"success": True, "index": len(additions) - 1, "thumbnail": thumbnail})
        except Exception as exc:
            logger.error(f"api_add_image_to_section error: {exc}")
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/sections/<section_id>/images/<int:img_index>", methods=["DELETE"])
    def api_remove_pending_image(section_id: str, img_index: int):
        try:
            ok = html_editor.unstage_add_image(section_id, img_index)
            if not ok:
                return jsonify({"error": "Pending image not found"}), 404
            return jsonify({"success": True})
        except Exception as exc:
            logger.error(f"api_remove_pending_image error: {exc}")
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/sections/<section_id>", methods=["DELETE"])
    def api_delete_section(section_id: str):
        try:
            ok = html_editor.delete_section(section_id)
            if not ok:
                return jsonify({"error": f"Section not found: {section_id}"}), 404
            return jsonify({"success": True, "section_id": section_id})
        except Exception as exc:
            logger.error(f"api_delete_section error: {exc}")
            return jsonify({"error": str(exc)}), 500

    # ------------------------------------------------------------------ #
    #  Visual elements (images + diagrams)
    # ------------------------------------------------------------------ #

    @app.route("/api/elements")
    def api_elements():
        try:
            return jsonify({"elements": image_editor.list_elements()})
        except Exception as exc:
            logger.error(f"api_elements error: {exc}")
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/elements/<int:display_idx>", methods=["PATCH"])
    def api_toggle_element(display_idx: int):
        try:
            elements = image_editor.list_elements()
            el = next((e for e in elements if e["display_index"] == display_idx), None)
            if not el:
                return jsonify({"error": "Element not found"}), 404

            body = request.get_json(force=True) or {}
            action = body.get("action", "toggle")

            if el["kind"] == "image":
                idx = el["img_index"]
                if action == "delete" or (action == "toggle" and el["status"] != "delete"):
                    image_editor.mark_delete(idx)
                else:
                    image_editor.unmark_delete(idx)
            else:
                idx = el["dgm_index"]
                if action == "delete" or (action == "toggle" and el["status"] != "delete"):
                    image_editor.mark_delete_diagram(idx)
                else:
                    image_editor.unmark_delete_diagram(idx)

            return jsonify({"elements": image_editor.list_elements()})
        except Exception as exc:
            logger.error(f"api_toggle_element error: {exc}")
            return jsonify({"error": str(exc)}), 500

    # ------------------------------------------------------------------ #
    #  Image alternatives & replacement
    # ------------------------------------------------------------------ #

    @app.route("/api/images/<int:img_idx>/alternatives")
    def api_image_alternatives(img_idx: int):
        try:
            alts = image_editor.find_alternative_images(img_idx, max_results=5)
            enriched = []
            for alt in alts:
                item = dict(alt)
                item["thumbnail"] = _encode_image_thumbnail(alt.get("path", ""))
                enriched.append(item)
            return jsonify({"alternatives": enriched})
        except Exception as exc:
            logger.error(f"api_image_alternatives error: {exc}")
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/images/<int:img_idx>/replace", methods=["POST"])
    def api_replace_image(img_idx: int):
        try:
            body = request.get_json(force=True)
            new_path = body.get("path", "")
            if not new_path or not Path(new_path).exists():
                return jsonify({"error": "Invalid image path"}), 400
            if not image_editor.replace_image(img_idx, new_path):
                return jsonify({"error": "Replace failed"}), 400
            return jsonify({"success": True})
        except Exception as exc:
            logger.error(f"api_replace_image error: {exc}")
            return jsonify({"error": str(exc)}), 500

    # ------------------------------------------------------------------ #
    #  Gallery — 편집 중인 HTML 파일 인근 이미지만 스캔한다
    # ------------------------------------------------------------------ #

    @app.route("/api/gallery")
    def api_gallery():
        try:
            page = int(request.args.get("page", 1))
            per_page = int(request.args.get("per_page", 24))
            query = request.args.get("q", "").lower()

            base = html_p.parent
            all_images = []
            if base.exists():
                for img_file in sorted(base.rglob("*")):
                    if not img_file.is_file() or img_file.suffix.lower() not in _ALLOWED_EXTS:
                        continue
                    if query and query not in img_file.name.lower():
                        continue
                    all_images.append({
                        "path": str(img_file),
                        "name": img_file.name,
                        "session": img_file.parent.name,
                        "size": img_file.stat().st_size,
                    })

            total = len(all_images)
            start = (page - 1) * per_page
            page_images = all_images[start: start + per_page]
            for item in page_images:
                item["thumbnail"] = _encode_image_thumbnail(item["path"])

            return jsonify({
                "images": page_images,
                "total": total,
                "page": page,
                "per_page": per_page,
                "pages": max(1, (total + per_page - 1) // per_page),
            })
        except Exception as exc:
            logger.error(f"api_gallery error: {exc}")
            return jsonify({"error": str(exc)}), 500

    # ------------------------------------------------------------------ #
    #  Serve image by index
    # ------------------------------------------------------------------ #

    @app.route("/api/images/by-index/<int:img_index>")
    def api_serve_image_by_index(img_index: int):
        if not (1 <= img_index <= len(image_editor.images)):
            return "Not found", 404

        replacement = image_editor.changes["replace"].get(img_index)
        if replacement:
            repl_path = Path(replacement["new_path"]).resolve()
            if repl_path.exists():
                mime = mimetypes.guess_type(str(repl_path))[0] or "image/jpeg"
                return send_file(repl_path, mimetype=mime)

        src = image_editor.images[img_index - 1].get("src", "")
        if not src:
            return "No source", 404

        if src.startswith("data:"):
            try:
                from flask import Response
                header, data = src.split(",", 1)
                mime = header.split(";")[0].replace("data:", "") or "image/jpeg"
                return Response(base64.b64decode(data), mimetype=mime)
            except Exception:
                return "Invalid data URI", 400

        img_path = Path(src)
        if not img_path.is_absolute():
            img_path = (Path(html_path).parent / src).resolve()
        else:
            img_path = img_path.resolve()

        if not img_path.exists():
            return "Not found", 404

        mime = mimetypes.guess_type(str(img_path))[0] or "image/jpeg"
        return send_file(img_path, mimetype=mime)

    # ------------------------------------------------------------------ #
    #  Serve local image file (path whitelist)
    # ------------------------------------------------------------------ #

    @app.route("/api/images/serve")
    def api_serve_image():
        path_str = request.args.get("path", "")
        if not path_str:
            return "Missing path", 400

        img_path = Path(path_str).resolve()
        allowed_roots = [Path(html_path).parent.resolve(), upload_dir.resolve()]
        # 문자열 startswith 비교는 "/foo/book"과 "/foo/book_evil" 같은 형제
        # 디렉토리를 구분 못 해 화이트리스트를 우회당한다 — 경로 구성요소
        # 단위로 비교하는 is_relative_to()를 써야 한다.
        if not any(img_path.is_relative_to(r) for r in allowed_roots):
            return "Forbidden", 403
        if not img_path.exists():
            return "Not found", 404

        mime = mimetypes.guess_type(str(img_path))[0] or "image/jpeg"
        return send_file(img_path, mimetype=mime)

    # ------------------------------------------------------------------ #
    #  Upload
    # ------------------------------------------------------------------ #

    @app.route("/api/images/upload", methods=["POST"])
    def api_upload_image():
        try:
            if "file" not in request.files:
                return jsonify({"error": "No file provided"}), 400
            f = request.files["file"]
            if not f.filename:
                return jsonify({"error": "Empty filename"}), 400
            ext = Path(f.filename).suffix.lower()
            if ext not in _ALLOWED_EXTS:
                return jsonify({"error": f"Unsupported file type: {ext}"}), 400

            upload_dir.mkdir(parents=True, exist_ok=True)
            unique_name = f"{uuid.uuid4().hex}{ext}"
            save_path = upload_dir / unique_name
            f.save(str(save_path))

            return jsonify({
                "success": True,
                "path": str(save_path),
                "name": unique_name,
                "thumbnail": _encode_image_thumbnail(str(save_path)),
            })
        except Exception as exc:
            logger.error(f"api_upload_image error: {exc}")
            return jsonify({"error": str(exc)}), 500

    # ------------------------------------------------------------------ #
    #  Save
    # ------------------------------------------------------------------ #

    @app.route("/api/save", methods=["POST"])
    def api_save():
        try:
            updated_soup = html_editor.apply_all_changes()

            # image_editor는 별도 soup 인스턴스를 갖고 있으므로, 갱신된 트리의
            # 실제 노드로 참조를 다시 매핑해야 삭제/교체가 반영된다.
            from collections import defaultdict
            src_map: dict = defaultdict(list)
            for tag in updated_soup.find_all("img"):
                src_map[tag.get("src", "")].append(tag)
            for img_info in image_editor.images:
                live = src_map.get(img_info["src"], [])
                if live:
                    img_info["tag"] = live.pop(0)

            for dgm in image_editor.diagrams:
                prefix = dgm["mermaid_code"][:40]
                for div in updated_soup.find_all("div", class_="mermaid"):
                    if prefix in div.get_text(strip=True):
                        dgm["mermaid_div"] = div
                        dgm["container"] = div.find_parent("div", class_="my-8") or div
                        break

            image_editor.soup = updated_soup
            saved_path = image_editor.save_changes(output_path)

            return jsonify({"success": True, "path": saved_path})
        except Exception as exc:
            logger.error(f"api_save error: {exc}")
            return jsonify({"error": str(exc)}), 500

    # ------------------------------------------------------------------ #
    #  Shutdown
    # ------------------------------------------------------------------ #

    @app.route("/api/shutdown", methods=["POST"])
    def api_shutdown():
        def _stop():
            import time
            time.sleep(0.3)
            os._exit(0)

        threading.Thread(target=_stop, daemon=True).start()
        return jsonify({"success": True, "message": "Server shutting down"})

    return app


def _encode_image_thumbnail(path: str, max_size: int = 200) -> str:
    """로컬 이미지를 축소한 base64 data URI로 반환한다. 실패 시 빈 문자열."""
    if not path or not Path(path).exists():
        return ""
    try:
        import io

        from PIL import Image

        with Image.open(path) as img:
            img.thumbnail((max_size, max_size))
            buf = io.BytesIO()
            fmt = img.format or "JPEG"
            if fmt not in ("JPEG", "PNG", "WEBP", "GIF"):
                fmt = "JPEG"
            img.save(buf, format=fmt)
            buf.seek(0)
            b64 = base64.b64encode(buf.read()).decode()
            mime = mimetypes.guess_type(path)[0] or "image/jpeg"
            return f"data:{mime};base64,{b64}"
    except Exception as exc:
        logger.debug(f"Thumbnail encode failed for {path}: {exc}")
        return ""


def run_editor(
    html_path: str, output_path: str | None = None, port: int = 5757, open_browser: bool = True
) -> None:
    """편집 서버를 기동하고(옵션) 브라우저를 연다."""
    app = create_app(html_path, output_path)

    if open_browser:
        import webbrowser

        def _open():
            import time
            time.sleep(0.8)
            webbrowser.open(f"http://localhost:{port}")

        threading.Thread(target=_open, daemon=True).start()

    print("\n\U0001f310  Book-binder 편집기 시작됨")
    print(f"   URL   : http://localhost:{port}")
    print(f"   파일  : {Path(html_path).name}")
    print(f"   저장  : {output_path or Path(html_path).stem + '_edited.html'}")
    print("\n   종료  : 브라우저에서 '종료' 버튼 클릭, 또는 Ctrl+C\n")

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
