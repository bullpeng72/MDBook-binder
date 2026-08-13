"""cli.py의 CLI 배선(옵션 → 내부 함수 인자 매핑) 회귀 테스트.

각 명령의 무거운 의존성(Playwright/Ollama/Flask)은 실제로 띄우지 않고,
해당 함수를 모킹해 어떤 인자로 호출되는지만 검증한다 — cli.py는 195개
테스트 중 어느 것에서도 CliRunner로 두드려진 적이 없어(옵션 파싱 실수,
에러 메시지 포맷 붕괴, 새로 추가한 분기 로직이 회귀해도) 테스트 스위트가
잡아내지 못했다. cli.py의 각 명령은 무거운 모듈을 함수 본문 안에서
지연 임포트하므로, `mdbook_binder.<module>.<func>`를 monkeypatch하면
호출 시점에 패치된 버전을 그대로 집어간다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from mdbook_binder.cli import main


def _write(root: Path, rel: str, content: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ── check ──────────────────────────────────────────────────────────────


def test_check_runs_against_minimal_corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    monkeypatch.setitem(sys.modules, "ollama", None)
    _write(tmp_path, "chapter.md", "# 챕터\n\n본문\n")

    result = CliRunner().invoke(main, ["check", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "챕터 수: 1개" in result.output


def test_check_rejects_missing_root():
    result = CliRunner().invoke(main, ["check", "/no/such/dir"])
    assert result.exit_code != 0


# ── build html ─────────────────────────────────────────────────────────


def test_build_html_passes_cli_options_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    captured = {}

    def fake_build_html(root, config, *, out_path, title_override, language_override, color_override):
        captured.update(
            root=root,
            out_path=out_path,
            title_override=title_override,
            language_override=language_override,
            color_override=color_override,
        )

    monkeypatch.setattr("mdbook_binder.html_book.build_html", fake_build_html)
    _write(tmp_path, "chapter.md", "# 챕터\n")

    result = CliRunner().invoke(
        main,
        [
            "build", "html", str(tmp_path),
            "--out", str(tmp_path / "out.html"),
            "--title", "제목", "--language", "en", "--color", "teal",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["out_path"] == tmp_path / "out.html"
    assert captured["title_override"] == "제목"
    assert captured["language_override"] == "en"
    assert captured["color_override"] == "teal"


def test_build_html_rejects_unknown_color(tmp_path: Path):
    _write(tmp_path, "chapter.md", "# 챕터\n")

    result = CliRunner().invoke(main, ["build", "html", str(tmp_path), "--color", "neon-pink"])

    assert result.exit_code != 0


# ── build pdf ──────────────────────────────────────────────────────────


def test_build_pdf_passes_merge_and_out_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    captured = {}

    def fake_build_pdf(root, *, merge_name, out_dir, color_override):
        captured.update(root=root, merge_name=merge_name, out_dir=out_dir, color_override=color_override)

    monkeypatch.setattr("mdbook_binder.pdf_book.build_pdf", fake_build_pdf)
    _write(tmp_path, "chapter.md", "# 챕터\n")

    result = CliRunner().invoke(
        main,
        ["build", "pdf", str(tmp_path), "--merge", "my_book", "--out-dir", str(tmp_path / "dist")],
    )

    assert result.exit_code == 0, result.output
    assert captured["merge_name"] == "my_book"
    assert captured["out_dir"] == tmp_path / "dist"


def test_build_pdf_wraps_runtime_error_as_click_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def fake_build_pdf(root, *, merge_name, out_dir, color_override):
        raise RuntimeError("Playwright Chromium 없음")

    monkeypatch.setattr("mdbook_binder.pdf_book.build_pdf", fake_build_pdf)
    _write(tmp_path, "chapter.md", "# 챕터\n")

    result = CliRunner().invoke(main, ["build", "pdf", str(tmp_path)])

    assert result.exit_code != 0
    assert "Playwright Chromium 없음" in result.output


# ── edit ───────────────────────────────────────────────────────────────


def test_edit_passes_options_to_run_editor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    captured = {}

    def fake_run_editor(html_path, *, output_path, port, open_browser):
        captured.update(html_path=html_path, output_path=output_path, port=port, open_browser=open_browser)

    monkeypatch.setattr("mdbook_binder.editor.server.run_editor", fake_run_editor)
    html_file = _write(tmp_path, "book.html", "<html></html>")

    result = CliRunner().invoke(
        main,
        ["edit", str(html_file), "--port", "9999", "--out", str(tmp_path / "final.html"), "--no-browser"],
    )

    assert result.exit_code == 0, result.output
    assert captured["port"] == 9999
    assert captured["open_browser"] is False
    assert captured["output_path"] == str(tmp_path / "final.html")


# ── import ─────────────────────────────────────────────────────────────


def test_import_rejects_non_pdf_extension(tmp_path: Path):
    fake_docx = _write(tmp_path, "book.docx", "not a pdf")

    result = CliRunner().invoke(main, ["import", str(fake_docx), str(tmp_path / "out")])

    assert result.exit_code != 0
    assert "PDF 파일만 지원합니다" in result.output
    assert "book.docx" in result.output


def test_import_extension_check_is_case_insensitive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    captured = {}

    def fake_import_pdf(pdf_path, out_dir, *, title, extract_images):
        captured.update(pdf_path=pdf_path, out_dir=out_dir, title=title, extract_images=extract_images)
        return out_dir / "book.md"

    monkeypatch.setattr("mdbook_binder.pdf_import.import_pdf", fake_import_pdf)
    fake_pdf = _write(tmp_path, "book.PDF", "uppercase extension")

    result = CliRunner().invoke(main, ["import", str(fake_pdf), str(tmp_path / "out")])

    assert result.exit_code == 0, result.output
    assert captured["pdf_path"] == fake_pdf


def test_import_passes_title_and_no_images(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    captured = {}

    def fake_import_pdf(pdf_path, out_dir, *, title, extract_images):
        captured.update(title=title, extract_images=extract_images)
        return out_dir / f"{title}.md"

    monkeypatch.setattr("mdbook_binder.pdf_import.import_pdf", fake_import_pdf)
    fake_pdf = _write(tmp_path, "book.pdf", "")

    result = CliRunner().invoke(
        main,
        ["import", str(fake_pdf), str(tmp_path / "out"), "--title", "My Book", "--no-images"],
    )

    assert result.exit_code == 0, result.output
    assert captured["title"] == "My Book"
    assert captured["extract_images"] is False


# ── translate ──────────────────────────────────────────────────────────


def test_translate_check_only_does_not_invoke_translate_corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from mdbook_binder.check import EnvCheckItem

    monkeypatch.setattr("mdbook_binder.check.check_ollama", lambda config: EnvCheckItem("번역 (Ollama)", True))
    called = []
    monkeypatch.setattr("mdbook_binder.translation.translate_corpus", lambda *a, **k: called.append((a, k)))
    _write(tmp_path, "chapter.md", "# 챕터\n")

    result = CliRunner().invoke(
        main,
        ["translate", str(tmp_path), str(tmp_path / "out"), "--direction", "k2e", "--check-only"],
    )

    assert result.exit_code == 0, result.output
    assert "사용 가능" in result.output
    assert called == []


def test_translate_reports_missing_ollama_as_click_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from mdbook_binder.check import EnvCheckItem

    monkeypatch.setattr(
        "mdbook_binder.check.check_ollama",
        lambda config: EnvCheckItem("번역 (Ollama)", False, 'pip install "mdbook-binder[translate]"'),
    )
    _write(tmp_path, "chapter.md", "# 챕터\n")

    result = CliRunner().invoke(main, ["translate", str(tmp_path), str(tmp_path / "out"), "--direction", "k2e"])

    assert result.exit_code != 0
    assert "번역 (Ollama)" in result.output


def test_translate_passes_direction_and_chunk_chars_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from mdbook_binder.check import EnvCheckItem

    monkeypatch.setattr("mdbook_binder.check.check_ollama", lambda config: EnvCheckItem("번역 (Ollama)", True))
    monkeypatch.setattr("mdbook_binder.translation.make_ollama_translate_fn", lambda cfg, target: (lambda text: text))
    captured = {}

    def fake_translate_corpus(root, out_dir, config, target_language, translate_fn, *, chunk_chars):
        captured.update(root=root, out_dir=out_dir, target_language=target_language, chunk_chars=chunk_chars)

    monkeypatch.setattr("mdbook_binder.translation.translate_corpus", fake_translate_corpus)
    _write(tmp_path, "chapter.md", "# 챕터\n")

    result = CliRunner().invoke(
        main,
        ["translate", str(tmp_path), str(tmp_path / "out"), "--direction", "k2e", "--chunk-chars", "500"],
    )

    assert result.exit_code == 0, result.output
    assert captured["target_language"] == "en"
    assert captured["chunk_chars"] == 500
