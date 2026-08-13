"""check.check_corpus()/check_environment()의 빌드 전 사전 점검 검증."""

import sys
from pathlib import Path

import pytest

from mdbook_binder.check import (
    EnvCheckItem,
    _check_module,
    check_corpus,
    check_environment,
    check_ollama,
    format_env_report,
)
from mdbook_binder.manifest import (
    TIER_NATURAL_SORT,
    TIER_PART_CONVENTION,
    BookConfig,
    TranslationConfig,
)


def _write(root: Path, rel: str, content: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_reports_used_tier(tmp_path: Path):
    _write(tmp_path, "a.md", "# A\n\n본문\n")
    result = check_corpus(tmp_path)
    assert result.tier == TIER_NATURAL_SORT


def test_detects_duplicate_titles_without_building(tmp_path: Path):
    _write(tmp_path, "Part_I_A/Chapter_01_x.md", "# 개요\n\nPart I\n")
    _write(tmp_path, "Part_II_B/Chapter_01_y.md", "# 개요\n\nPart II\n")

    result = check_corpus(tmp_path)

    assert result.tier == TIER_PART_CONVENTION
    assert "개요" in result.duplicate_titles
    assert len(result.duplicate_titles["개요"]) == 2


def test_detects_missing_image_reference(tmp_path: Path):
    _write(tmp_path, "chapter.md", "# 챕터\n\n![그림](./images/missing.png)\n")

    result = check_corpus(tmp_path)

    assert len(result.missing_images) == 1
    assert result.missing_images[0][1] == "./images/missing.png"


def test_no_false_positive_for_remote_or_data_uri_images(tmp_path: Path):
    _write(
        tmp_path,
        "chapter.md",
        "# 챕터\n\n![원격](https://example.com/x.png)\n\n![인라인](data:image/png;base64,AAA)\n",
    )

    result = check_corpus(tmp_path)

    assert result.missing_images == []


def test_check_module_reports_installed():
    item = _check_module("테스트 기능", "os", "pip install os-는-필요-없음")
    assert item.installed
    assert item.install_hint == ""


def test_check_module_reports_missing_with_hint():
    item = _check_module("테스트 기능", "이런_모듈은_없다", 'pip install "mdbook-binder[pdf]"')
    assert not item.installed
    assert item.install_hint == 'pip install "mdbook-binder[pdf]"'


def test_check_environment_returns_six_items(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    items = check_environment()
    assert len(items) == 6
    assert all(isinstance(item, EnvCheckItem) for item in items)


def test_check_environment_flags_missing_playwright(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    items = check_environment()
    playwright_item = next(i for i in items if "Playwright" in i.feature)
    assert not playwright_item.installed
    assert "mdbook-binder[pdf]" in playwright_item.install_hint


def test_format_env_report_all_installed():
    items = [EnvCheckItem("기능 A", True), EnvCheckItem("기능 B", True)]
    report = format_env_report(items)
    assert "✅ 기능 A" in report
    assert "✅ 기능 B" in report
    assert "모든 선택 기능을 사용할 수 있습니다." in report


def test_format_env_report_missing_shows_hint():
    items = [EnvCheckItem("기능 A", False, 'pip install "mdbook-binder[pdf]"')]
    report = format_env_report(items)
    assert "⚠️" in report
    assert "기능 A" in report
    assert 'pip install "mdbook-binder[pdf]"' in report
    assert "모든 선택 기능을 사용할 수 있습니다." not in report


def test_check_ollama_flags_missing_package(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "ollama", None)
    item = check_ollama(None)
    assert not item.installed
    assert 'mdbook-binder[translate]' in item.install_hint


def test_check_ollama_uses_config_host_on_connection_failure():
    """실제 Ollama 서버가 없는 CI 환경에서도, 연결 실패 메시지에 설정한
    host가 그대로 반영되는지로 config가 실제로 전달됨을 검증한다."""
    config = BookConfig(translation=TranslationConfig(host="http://192.0.2.1:11434", timeout=1))

    item = check_ollama(config)

    assert not item.installed
    assert "192.0.2.1:11434" in item.install_hint


def test_check_ollama_defaults_when_config_absent_or_translation_unset():
    """config가 None이거나 translation이 비어 있으면 TranslationConfig() 기본값과
    동일하게 동작해야 한다 — 로컬 Ollama 서버 유무에 좌우되지 않도록 두 결과를
    직접 비교한다(실제 설치 여부를 단정하지 않음)."""
    default_result = check_ollama(BookConfig(translation=TranslationConfig()))

    assert check_ollama(None) == default_result
    assert check_ollama(BookConfig()) == default_result
