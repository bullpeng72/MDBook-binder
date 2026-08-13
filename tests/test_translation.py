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
    has_residual_korean,
    protect_blocks,
    reassemble_chunks,
    residual_korean_ratio,
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

    def test_k2e_retries_chunk_that_still_has_korean(self):
        """target_language='en'일 때, 첫 시도가 한글을 그대로 돌려주면 재시도한다."""
        calls: list[str] = []

        def fake(chunk: str) -> str:
            calls.append(chunk)
            return chunk if len(calls) == 1 else "Translated result."

        out = translate_chapter("한글 원문입니다.", chunk_chars=100, translate_fn=fake, target_language="en")

        assert len(calls) == 2  # 첫 시도 실패 → 1회 재시도로 성공
        assert out == "Translated result."

    def test_k2e_gives_up_after_max_retries_and_reports_incomplete(self):
        """max_retries를 다 써도 한글이 남으면 on_incomplete(j, n)이 호출된다."""
        calls: list[str] = []
        incomplete: list[tuple[int, int]] = []

        out = translate_chapter(
            "한글 원문입니다.", chunk_chars=100,
            translate_fn=lambda s: calls.append(s) or s,  # 항상 원문 그대로(번역 실패 시뮬레이션)
            target_language="en", max_retries=2,
            on_incomplete=lambda j, n: incomplete.append((j, n)),
        )

        assert len(calls) == 3  # 최초 시도 1 + 재시도 2
        assert incomplete == [(1, 1)]
        assert out == "한글 원문입니다."  # 실패해도 마지막 결과는 그대로 반환(빈 문자열로 날리지 않음)

    def test_e2k_does_not_verify_or_retry(self):
        """target_language='ko'(e2k)는 영문 잔존을 검증하지 않는다 — 정상적인
        영문 고유명사와 번역 실패를 구분할 신호가 없으므로 검증 자체를 건너뛴다."""
        calls: list[str] = []
        out = translate_chapter(
            "some english text", chunk_chars=100,
            translate_fn=lambda s: calls.append(s) or s,
            target_language="ko",
        )
        assert len(calls) == 1
        assert out == "some english text"

    def test_successful_translation_with_stray_proper_noun_is_not_retried(self):
        """번역 결과에 고유명사 한두 개 정도(임계값 이하)만 한글로 남으면
        재시도하지 않는다 — 완전 미번역과 정상 잔존을 구분해야 한다."""
        calls: list[str] = []
        translated = "This chapter explains the Harness Method in detail. " * 10 + "(하네스)"

        out = translate_chapter(
            "원문", chunk_chars=1000,
            translate_fn=lambda s: calls.append(s) or translated,
            target_language="en",
        )

        assert len(calls) == 1  # 재시도 없이 한 번만 호출됨
        assert out == translated


class TestResidualKorean:
    def test_pure_english_has_zero_ratio(self):
        assert residual_korean_ratio("Hello, world!") == 0.0

    def test_pure_korean_has_high_ratio(self):
        assert residual_korean_ratio("안녕하세요") == 1.0

    def test_empty_string_has_zero_ratio(self):
        assert residual_korean_ratio("") == 0.0

    def test_below_threshold_is_not_flagged(self):
        text = "a" * 100 + "가"  # ~1%
        assert not has_residual_korean(text)

    def test_above_threshold_is_flagged(self):
        text = "한글이 절반 이상인 문장입니다"
        assert has_residual_korean(text)


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

    def test_reports_incomplete_chunks_after_retries_exhausted(self, tmp_path: Path, capsys):
        """k2e 방향에서 재시도 후에도 한글이 남으면 챕터별 경고와 최종
        요약이 함께 출력돼야 한다 — 이전에는 이런 실패를 조용히 그대로
        출력 파일에 써버렸다."""
        root = tmp_path / "src"
        out_dir = tmp_path / "out"
        _write(root, "chapter.md", "한글 원문입니다.\n")

        translate_corpus(root, out_dir, config=None, target_language="en", translate_fn=lambda s: s)

        out = capsys.readouterr().out
        assert "재시도 후에도 한글이 남은 청크" in out
        assert "chapter.md" in out
        assert "총 1개 챕터에 재시도 후에도" in out
        # 실패해도 파일은 (최선의 결과로) 정상적으로 써진다 — 빌드 자체를 막지 않는다
        assert (out_dir / "chapter.md").exists()

    def test_no_incomplete_summary_when_all_chunks_translate_cleanly(self, tmp_path: Path, capsys):
        root = tmp_path / "src"
        out_dir = tmp_path / "out"
        _write(root, "chapter.md", "한글 원문입니다.\n")

        translate_corpus(root, out_dir, config=None, target_language="en", translate_fn=lambda s: "Clean translation.")

        out = capsys.readouterr().out
        assert "재시도" not in out

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
