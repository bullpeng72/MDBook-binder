"""manifest.resolve()의 3단계 우선순위 검증."""

import unicodedata
from pathlib import Path

import yaml

from mdbook_binder.manifest import (
    BookConfig,
    OrderConfig,
    TranslationConfig,
    resolve,
    write_minimal_book_yaml,
)


def _write(root: Path, rel: str, content: str = "# Title\n\nbody\n") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_broken_book_yaml_falls_back_without_crashing(tmp_path: Path, capsys):
    """book.yaml이 깨진 YAML이어도 빌드는 죽지 않고 자동 감지로 폴백해야 한다.

    파싱 실패 경고에 YAMLError의 줄/컬럼 상세를 그대로 문자열 보간하면, 뒤에
    이어지는 안내 문구가 여러 줄짜리 에러 메시지의 마지막 줄에 그대로
    이어붙어 버린다 — 안내 문구가 항상 첫 줄에 온전히 나오는지 확인한다.
    """
    _write(tmp_path, "book.yaml", "a: [1, 2\nb: broken\n")

    config = BookConfig.load(tmp_path)

    assert config is None
    out = capsys.readouterr().out
    first_line = out.splitlines()[0]
    assert first_line.endswith("book.yaml 파싱 실패 — 설정 없이 자동 감지로 진행")


def test_natural_sort_fallback_includes_all_loose_files(tmp_path: Path):
    """1/1.5/2순위가 전부 실패하면(명명 규칙 없음) 트리 전체를 자연정렬로 포함한다."""
    _write(tmp_path, "b.md")
    _write(tmp_path, "a.md")
    _write(tmp_path, "sub/c.md")

    chapters = resolve(tmp_path, config=None)

    assert [c.path.name for c in chapters] == ["a.md", "b.md", "c.md"]


def test_explicit_config_order_wins_over_alphabetical(tmp_path: Path):
    """book.yaml의 order.files가 자연정렬보다 우선한다."""
    _write(tmp_path, "a.md")
    _write(tmp_path, "b.md")
    config = BookConfig(order=OrderConfig(files=["b.md", "a.md"]))

    chapters = resolve(tmp_path, config)

    assert [c.path.name for c in chapters] == ["b.md", "a.md"]


def test_part_chapter_convention_orders_by_roman_numeral(tmp_path: Path):
    """Part_로마숫자 규칙이 있으면 II보다 III이 뒤에, 알파벳순이 아니라 로마숫자 순으로 온다."""
    _write(tmp_path, "Part_III_third/Chapter_01_x.md")
    _write(tmp_path, "Part_I_first/Chapter_01_x.md")
    _write(tmp_path, "Part_II_second/Chapter_01_x.md")

    chapters = resolve(tmp_path, config=None)

    parts = [c.path.parent.name for c in chapters]
    assert parts == ["Part_I_first", "Part_II_second", "Part_III_third"]


def test_back_matter_and_appendix_ordered_after_parts(tmp_path: Path):
    """9x_ 접두사 후주와 Appendix/는 Part 챕터들 뒤에 온다(정면 회귀 대상 버그)."""
    _write(tmp_path, "00_intro.md")
    _write(tmp_path, "99_afterword.md")
    _write(tmp_path, "Part_I_only/Chapter_01_x.md")
    _write(tmp_path, "Appendix/A_glossary.md")

    chapters = resolve(tmp_path, config=None)

    names = [c.path.name for c in chapters]
    assert names == ["00_intro.md", "Chapter_01_x.md", "99_afterword.md", "A_glossary.md"]


def test_exclude_matches_despite_nfc_nfd_mismatch(tmp_path: Path):
    """macOS(APFS)는 파일명을 NFD로 저장하지만 book.yaml은 보통 NFC로 작성된다 —
    exclude 패턴이 시각적으로 동일한 파일명을 정규화형 차이로 놓치면 안 된다."""
    nfc_name = unicodedata.normalize("NFC", "00_기획안.md")
    nfd_name = unicodedata.normalize("NFD", "00_기획안.md")
    assert nfc_name != nfd_name  # 바이트열은 실제로 다름을 전제로 검증

    _write(tmp_path, nfd_name)  # 디스크엔 NFD로 존재(macOS 기본 동작 재현)
    _write(tmp_path, "Part_I_only/Chapter_01_x.md")
    config = BookConfig(exclude=[nfc_name])  # book.yaml엔 NFC로 작성

    chapters = resolve(tmp_path, config)

    assert [c.path.name for c in chapters] == ["Chapter_01_x.md"]


def test_toc_manifest_auto_detected_without_config(tmp_path: Path):
    """book.yaml 없이도 ```toc 펜스가 있는 파일을 자동으로 매니페스트로 채택한다."""
    _write(tmp_path, "Part_I_intro/Chapter_01_hello.md")
    _write(
        tmp_path,
        "00_toc.md",
        "# 목차\n\n```toc\n1|서론|1|hello|narrative\n```\n",
    )

    chapters = resolve(tmp_path, config=None)

    assert len(chapters) == 1
    assert chapters[0].path.name == "Chapter_01_hello.md"
    assert chapters[0].part_label == "Part 1. 서론"


class TestTranslationConfig:
    def test_defaults_to_none_when_absent(self, tmp_path: Path):
        """book.yaml에 translation: 섹션이 없으면 order와 마찬가지로 None이어야 한다."""
        _write(tmp_path, "book.yaml", "title: 예시\n")

        config = BookConfig.load(tmp_path)

        assert config.translation is None

    def test_parses_full_section_from_book_yaml(self, tmp_path: Path):
        _write(
            tmp_path,
            "book.yaml",
            "translation:\n"
            "  model: llama3:8b\n"
            "  host: http://remote:11434\n"
            "  timeout: 120\n"
            "  chunk_chars: 500\n",
        )

        config = BookConfig.load(tmp_path)

        assert config.translation == TranslationConfig(
            model="llama3:8b", host="http://remote:11434", timeout=120, chunk_chars=500
        )

    def test_partial_section_falls_back_to_dataclass_defaults(self, tmp_path: Path):
        """일부 필드만 지정하면 나머지는 TranslationConfig 기본값을 그대로 쓴다."""
        _write(tmp_path, "book.yaml", "translation:\n  model: llama3:8b\n")

        config = BookConfig.load(tmp_path)

        assert config.translation.model == "llama3:8b"
        assert config.translation.host == TranslationConfig().host
        assert config.translation.timeout == TranslationConfig().timeout
        assert config.translation.chunk_chars == TranslationConfig().chunk_chars


class TestWriteMinimalBookYaml:
    def test_writes_language_only(self, tmp_path: Path):
        path = tmp_path / "book.yaml"

        write_minimal_book_yaml(path, language="en")

        assert yaml.safe_load(path.read_text(encoding="utf-8")) == {"language": "en"}

    def test_writes_extra_fields(self, tmp_path: Path):
        path = tmp_path / "book.yaml"

        write_minimal_book_yaml(path, language="ko", title="번역된 책")

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data == {"language": "ko", "title": "번역된 책"}
