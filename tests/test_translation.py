"""translation.py의 순수 로직(청킹·블록 보호·재조립·진행 출력) 회귀 테스트.

실제 Ollama 클라이언트(make_ollama_translate_fn)는 서버/모델이 필요해 CI에서
검증할 수 없으므로 여기서는 다루지 않는다 — translate_fn을 주입받는 나머지
함수는 전부 순수/의사-순수라 가짜 translate_fn만으로 충분히 테스트된다.
"""

from pathlib import Path

import yaml

from mdbook_binder.manifest import BookConfig
from mdbook_binder.translation import (
    _build_prompt,
    chunk_paragraphs,
    protect_blocks,
    reassemble_chunks,
    resolve_model_name,
    restore_blocks,
    translate_chapter,
    translate_corpus,
)


def _write(root: Path, rel: str, content: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


class TestChunkParagraphs:
    def test_empty_text_returns_no_chunks(self):
        assert chunk_paragraphs("", 100) == []

    def test_single_short_paragraph_is_one_chunk(self):
        assert chunk_paragraphs("hello", 100) == ["hello"]

    def test_paragraphs_under_limit_are_packed_together(self):
        text = "a" * 10 + "\n\n" + "b" * 10
        assert chunk_paragraphs(text, 100) == [text]

    def test_paragraphs_over_limit_split_into_separate_chunks(self):
        text = "a" * 60 + "\n\n" + "b" * 60
        chunks = chunk_paragraphs(text, 100)
        assert chunks == ["a" * 60, "b" * 60]

    def test_oversized_single_paragraph_is_not_split(self):
        """단락 하나가 chunk_chars를 넘어도 문장 중간에서 자르지 않는다."""
        text = "x" * 300
        assert chunk_paragraphs(text, 100) == [text]

    def test_greedily_packs_multiple_paragraphs_up_to_limit(self):
        paras = ["a" * 30] * 4
        chunks = chunk_paragraphs("\n\n".join(paras), 100)
        assert len(chunks) == 2
        assert chunks[0] == "\n\n".join(paras[:3])
        assert chunks[1] == paras[3]


class TestReassembleChunks:
    def test_joins_with_blank_line(self):
        assert reassemble_chunks(["A", "B"]) == "A\n\nB"


class TestProtectBlocks:
    def test_mermaid_block_round_trips_verbatim(self):
        text = "prose\n\n```mermaid\ngraph TD\nA-->B\n```\n\nmore prose"
        masked, blocks = protect_blocks(text)
        assert "```mermaid" not in masked
        assert restore_blocks(masked, blocks) == text

    def test_raw_html_block_round_trips_verbatim(self):
        text = 'before\n\n@@HTML_START@@\n<div class="x">raw</div>\n@@HTML_END@@\n\nafter'
        masked, blocks = protect_blocks(text)
        assert "@@HTML_START@@" not in masked
        assert restore_blocks(masked, blocks) == text

    def test_blockquoted_code_round_trips_verbatim(self):
        text = "before\n\n> ```python\n> def f():\n>     return 1\n> ```\n\nafter"
        masked, blocks = protect_blocks(text)
        assert "> ```" not in masked
        assert restore_blocks(masked, blocks) == text

    def test_generic_fence_round_trips_verbatim(self):
        text = "before\n\n```python\ndef f():\n    return 1\n```\n\nafter"
        masked, blocks = protect_blocks(text)
        assert "```python" not in masked
        assert restore_blocks(masked, blocks) == text

    def test_plain_text_without_blocks_is_unchanged(self):
        text = "just prose, no blocks here."
        masked, blocks = protect_blocks(text)
        assert masked == text
        assert blocks == []


class TestTranslateChapter:
    def test_translate_fn_never_receives_protected_block_content(self):
        """코드/mermaid 원문이 청크로 나뉘기 전에 이미 플레이스홀더로 치환되므로,
        translate_fn에는 블록 내용이 절대 전달되지 않아야 한다."""
        text = "prose before\n\n```python\nSECRET_CODE\n```\n\nprose after"

        def fake(chunk: str) -> str:
            assert "SECRET_CODE" not in chunk
            return chunk.upper()

        out = translate_chapter(text, chunk_chars=1000, translate_fn=fake)
        assert "SECRET_CODE" in out  # 복원 후에는 원문 그대로 남아있어야 함
        assert "```python" in out

    def test_prose_is_translated_and_blocks_preserved(self):
        text = "hello world.\n\n```python\ncode_here()\n```\n\ngoodbye world."
        out = translate_chapter(text, chunk_chars=1000, translate_fn=lambda s: s.upper())
        assert "HELLO WORLD." in out
        assert "GOODBYE WORLD." in out
        assert "code_here()" in out  # 코드 블록은 대문자로 안 바뀜(보호됨)

    def test_on_chunk_start_fires_with_one_indexed_progress(self):
        text = "\n\n".join(["a" * 60, "b" * 60, "c" * 60])
        progress: list[tuple[int, int]] = []

        translate_chapter(
            text, chunk_chars=100, translate_fn=lambda s: s, on_chunk_start=lambda j, n: progress.append((j, n))
        )

        assert progress == [(1, 3), (2, 3), (3, 3)]

    def test_no_progress_callback_needed(self):
        """on_chunk_start를 안 주면 그냥 조용히 동작해야 한다(선택 인자)."""
        out = translate_chapter("hello", chunk_chars=100, translate_fn=lambda s: s)
        assert out == "hello"


class TestTranslateCorpus:
    def test_respects_manifest_exclude(self, tmp_path: Path):
        """디렉토리를 직접 훑지 않고 manifest.resolve()를 재사용하므로, exclude된
        파일은 번역 대상에서도 빠져야 한다."""
        root = tmp_path / "src"
        out_dir = tmp_path / "out"
        _write(root, "chapter.md", "# 챕터\n\n본문.\n")
        _write(root, "draft.md", "# 초안\n\n제외되어야 함.\n")
        config = BookConfig(exclude=["book.yaml", "README.md", "draft.md"])

        translate_corpus(root, out_dir, config, "en", translate_fn=lambda s: s)

        assert (out_dir / "chapter.md").exists()
        assert not (out_dir / "draft.md").exists()

    def test_prints_chapter_and_chunk_progress(self, tmp_path: Path, capsys):
        root = tmp_path / "src"
        out_dir = tmp_path / "out"
        _write(root, "chapter.md", "# 챕터\n\n본문.\n")

        translate_corpus(root, out_dir, config=None, target_language="en", translate_fn=lambda s: s)

        out = capsys.readouterr().out
        assert "[1/1]" in out
        assert "chapter.md" in out
        assert "청크 1/1 번역 중..." in out

    def test_rewrites_book_yaml_language_and_keeps_other_fields(self, tmp_path: Path):
        root = tmp_path / "src"
        out_dir = tmp_path / "out"
        _write(root, "chapter.md", "# 챕터\n\n본문.\n")
        _write(root, "book.yaml", "title: 원본 제목\nlanguage: ko\n")
        config = BookConfig.load(root)

        translate_corpus(root, out_dir, config, "en", translate_fn=lambda s: s)

        data = yaml.safe_load((out_dir / "book.yaml").read_text(encoding="utf-8"))
        assert data["language"] == "en"
        assert data["title"] == "원본 제목"

    def test_creates_minimal_book_yaml_when_source_has_none(self, tmp_path: Path):
        root = tmp_path / "src"
        out_dir = tmp_path / "out"
        _write(root, "chapter.md", "# 챕터\n\n본문.\n")

        translate_corpus(root, out_dir, config=None, target_language="ko", translate_fn=lambda s: s)

        data = yaml.safe_load((out_dir / "book.yaml").read_text(encoding="utf-8"))
        assert data == {"language": "ko"}

    def test_uses_chunk_chars_override_over_config(self, tmp_path: Path):
        root = tmp_path / "src"
        out_dir = tmp_path / "out"
        _write(root, "chapter.md", "\n\n".join(["a" * 60, "b" * 60]))
        calls: list[str] = []

        translate_corpus(
            root, out_dir, config=None, target_language="en",
            translate_fn=lambda s: calls.append(s) or s, chunk_chars=1000,
        )

        assert len(calls) == 1  # chunk_chars=1000이면 두 단락이 한 청크로 합쳐짐


def test_build_prompt_instructs_markdown_preservation_and_includes_chunk():
    prompt = _build_prompt("hello world", "ko")
    assert "hello world" in prompt
    assert "Markdown" in prompt


class TestResolveModelName:
    def test_exact_match_preferred(self):
        assert resolve_model_name("qwen3.6:35b", ["qwen3.6:35b", "qwen3.6:35b-mlx"]) == "qwen3.6:35b"

    def test_falls_back_to_base_name_match(self):
        """회귀 대상: check_ollama()는 베이스 이름이 같으면 "설치됨"으로
        판정하는데, 실제 generate() 호출은 요청한 이름을 그대로 써서 서버에
        없는 태그라 404로 실패했다 — 실사용 중 재현된 버그. 두 곳이 반드시
        같은 이름을 골라야 한다."""
        assert resolve_model_name("qwen3.6:35b", ["qwen3.6:35b-mlx"]) == "qwen3.6:35b-mlx"

    def test_no_match_returns_none(self):
        assert resolve_model_name("qwen3.6:35b", ["llama3:8b"]) is None

    def test_empty_available_list_returns_none(self):
        assert resolve_model_name("qwen3.6:35b", []) is None

    def test_first_base_name_match_used_when_multiple_candidates(self):
        assert resolve_model_name("qwen3.6:35b", ["qwen3.6:7b", "qwen3.6:35b-mlx"]) == "qwen3.6:7b"
