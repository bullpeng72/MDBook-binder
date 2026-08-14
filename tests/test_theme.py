"""theme.theme_css()와 build_html()/build_pdf()의 --color 연동 회귀 테스트."""

from pathlib import Path

import pytest

from mdbook_binder.html_book import build_html
from mdbook_binder.manifest import BookConfig
from mdbook_binder.theme import THEMES, theme_css


def test_theme_css_contains_all_three_accent_vars():
    css = theme_css("green")
    assert "--primary:" in css
    assert "--primary-light:" in css
    assert "--accent:" in css


def test_theme_css_case_insensitive():
    assert theme_css("GREEN") == theme_css("green")


def test_theme_css_unknown_name_raises_with_valid_list():
    with pytest.raises(ValueError, match="green"):
        theme_css("neon-pink")


def test_all_theme_names_produce_valid_css():
    for name in THEMES:
        css = theme_css(name)
        assert css.startswith("/* ── color theme:")


def _write(root: Path, rel: str, content: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_build_html_color_override_wins_over_default(tmp_path: Path):
    _write(tmp_path, "a.md", "# A\n\n본문.\n")
    out = build_html(tmp_path, config=None, out_path=tmp_path / "out.html", color_override="green")
    html = out.read_text(encoding="utf-8")
    primary, primary_light, accent = THEMES["green"]
    assert f"--primary: {primary};" in html
    assert f"--primary-light: {primary_light};" in html
    assert f"--accent: {accent};" in html


def test_build_html_without_color_keeps_default_purple(tmp_path: Path):
    _write(tmp_path, "a.md", "# A\n\n본문.\n")
    out = build_html(tmp_path, config=None, out_path=tmp_path / "out.html")
    html = out.read_text(encoding="utf-8")
    assert "color theme" not in html


def test_book_yaml_color_used_when_no_cli_override(tmp_path: Path):
    _write(tmp_path, "a.md", "# A\n\n본문.\n")
    _write(tmp_path, "book.yaml", "color: teal\n")
    config = BookConfig.load(tmp_path)
    out = build_html(tmp_path, config=config, out_path=tmp_path / "out.html")
    html = out.read_text(encoding="utf-8")
    primary, _, _ = THEMES["teal"]
    assert f"--primary: {primary};" in html


def test_cli_color_override_beats_book_yaml(tmp_path: Path):
    _write(tmp_path, "a.md", "# A\n\n본문.\n")
    _write(tmp_path, "book.yaml", "color: teal\n")
    config = BookConfig.load(tmp_path)
    out = build_html(
        tmp_path, config=config, out_path=tmp_path / "out.html", color_override="orange"
    )
    html = out.read_text(encoding="utf-8")
    primary, _, _ = THEMES["orange"]
    assert f"--primary: {primary};" in html
