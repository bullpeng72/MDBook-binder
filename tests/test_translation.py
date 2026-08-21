"""translation.py의 순수 로직(청킹·블록 보호·재조립·진행 출력) 회귀 테스트.

실제 Ollama 클라이언트(make_ollama_translate_fn)는 서버/모델이 필요해 CI에서
검증할 수 없으므로 여기서는 다루지 않는다 — translate_fn을 주입받는 나머지
함수는 전부 순수/의사-순수라 가짜 translate_fn만으로 충분히 테스트된다.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import yaml

from mdbook_binder.manifest import BookConfig, TranslationConfig
from mdbook_binder.translation import (
    _build_prompt,
    _looks_like_cell_hallucination,
    _translate_cell,
    chunk_paragraphs,
    has_residual_korean,
    is_table_chunk,
    make_ollama_translate_fn,
    protect_blocks,
    reassemble_chunks,
    residual_korean_ratio,
    resolve_model_name,
    restore_blocks,
    translate_chapter,
    translate_chunk,
    translate_corpus,
    translate_table_chunk,
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
            text,
            chunk_chars=100,
            translate_fn=lambda s: s,
            on_chunk_start=lambda j, n: progress.append((j, n)),
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

        out = translate_chapter(
            "한글 원문입니다.", chunk_chars=100, translate_fn=fake, target_language="en"
        )

        assert len(calls) == 2  # 첫 시도 실패 → 1회 재시도로 성공
        assert out == "Translated result."

    def test_k2e_gives_up_after_max_retries_and_reports_incomplete(self):
        """max_retries를 다 써도 한글이 남으면 on_incomplete(j, n)이 호출된다."""
        calls: list[str] = []
        incomplete: list[tuple[int, int]] = []

        out = translate_chapter(
            "한글 원문입니다.",
            chunk_chars=100,
            translate_fn=lambda s: calls.append(s) or s,  # 항상 원문 그대로(번역 실패 시뮬레이션)
            target_language="en",
            max_retries=2,
            on_incomplete=lambda j, n: incomplete.append((j, n)),
        )

        assert len(calls) == 3  # 최초 시도 1 + 재시도 2
        assert incomplete == [(1, 1)]
        assert (
            out == "한글 원문입니다."
        )  # 실패해도 마지막 결과는 그대로 반환(빈 문자열로 날리지 않음)

    def test_e2k_does_not_verify_or_retry(self):
        """target_language='ko'(e2k)는 영문 잔존을 검증하지 않는다 — 정상적인
        영문 고유명사와 번역 실패를 구분할 신호가 없으므로 검증 자체를 건너뛴다."""
        calls: list[str] = []
        out = translate_chapter(
            "some english text",
            chunk_chars=100,
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
            "원문",
            chunk_chars=1000,
            translate_fn=lambda s: calls.append(s) or translated,
            target_language="en",
        )

        assert len(calls) == 1  # 재시도 없이 한 번만 호출됨
        assert out == translated

    def test_table_chunk_is_translated_cell_by_cell(self):
        """표만 있는 청크는 translate_fn에 청크 전체가 아니라 셀 텍스트가
        하나씩 전달돼야 한다 — 표 전체를 한 번에 넘기면 로컬 소형 모델이
        뒷부분 행을 통째로 빠뜨리는 실패가 실사용에서 반복 확인됐다."""
        text = "| # | 문제 |\n|---|---|\n| A1 | 모호한 지시 |"
        calls: list[str] = []

        out = translate_chapter(
            text, chunk_chars=1000, translate_fn=lambda s: calls.append(s) or s.upper()
        )

        assert "문제" in calls  # 청크 전체가 아니라 셀 하나씩 전달됨
        assert text not in calls
        assert "|---|---|" in out  # 구분선은 그대로 보존
        assert "모호한 지시".upper() in out

    def test_k2e_retry_applies_to_table_chunks_too(self):
        """표 청크도 일반 청크와 동일하게 한글이 남으면 재시도·on_incomplete
        대상이 돼야 한다."""
        text = "| 헤더 |\n|---|\n| 내용 |"
        incomplete: list[tuple[int, int]] = []

        translate_chapter(
            text,
            chunk_chars=1000,
            translate_fn=lambda s: s,  # 항상 원문 그대로(번역 실패 시뮬레이션)
            target_language="en",
            max_retries=1,
            on_incomplete=lambda j, n: incomplete.append((j, n)),
        )

        assert incomplete == [(1, 1)]

    def test_on_chunk_done_fires_once_per_chunk_in_order(self):
        text = "\n\n".join(["a" * 60, "b" * 60, "c" * 60])
        done: list[tuple[int, str]] = []

        translate_chapter(
            text,
            chunk_chars=100,
            translate_fn=lambda s: s,
            on_chunk_done=lambda j, r: done.append((j, r)),
        )

        assert [j for j, _ in done] == [1, 2, 3]

    def test_chunks_to_retranslate_skips_translate_fn_for_others(self):
        """청크 단위 --resume의 핵심: chunks_to_retranslate에 없는 청크는
        translate_fn을 다시 호출하지 않고 previous_chunks의 캐시를 그대로
        쓴다."""
        text = "\n\n".join(["a" * 60, "b" * 60, "c" * 60])
        calls: list[str] = []

        out = translate_chapter(
            text,
            chunk_chars=100,
            translate_fn=lambda s: calls.append(s) or s.upper(),
            previous_chunks=["A" * 60, "b" * 60, "C" * 60],  # 청크 2만 미완료였다고 가정
            chunks_to_retranslate={2},
        )

        assert calls == ["b" * 60]  # 청크 1·3은 캐시 재사용, 청크 2만 다시 호출
        assert out == "A" * 60 + "\n\n" + "B" * 60 + "\n\n" + "C" * 60

    def test_skipped_chunk_still_fires_on_chunk_done_with_cached_result(self):
        text = "\n\n".join(["a" * 60, "b" * 60])
        done: dict[int, str] = {}

        translate_chapter(
            text,
            chunk_chars=100,
            translate_fn=lambda s: s.upper(),
            previous_chunks=["cached one", "b" * 60],
            chunks_to_retranslate={2},
            on_chunk_done=lambda j, r: done.__setitem__(j, r),
        )

        assert done[1] == "cached one"  # 재번역 대상이 아니라 캐시 그대로
        assert done[2] == "B" * 60

    def test_previous_chunks_length_mismatch_falls_back_to_full_translation(self):
        """previous_chunks가 이번 청킹 결과와 개수가 다르면(소스가 바뀌었거나
        chunk_chars가 달라졌다는 뜻) 재사용을 포기하고 전부 새로 번역한다."""
        text = "\n\n".join(["a" * 60, "b" * 60, "c" * 60])
        calls: list[str] = []

        translate_chapter(
            text,
            chunk_chars=100,
            translate_fn=lambda s: calls.append(s) or s,
            previous_chunks=["stale cache"],  # 청크 1개짜리 — 지금은 3개
            chunks_to_retranslate={2},
        )

        assert len(calls) == 3  # 전부 새로 번역됨


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


class TestIsTableChunk:
    def test_pure_table_is_detected(self):
        chunk = "| # | 문제 | 담당 |\n|---|---|---|\n| A1 | 모호한 지시 | 방법론 |"
        assert is_table_chunk(chunk)

    def test_plain_prose_is_not_a_table(self):
        assert not is_table_chunk("그냥 평범한 문단입니다.")

    def test_table_merged_with_other_paragraph_is_excluded(self):
        """chunk_paragraphs가 표를 다른 단락과 그리디하게 합쳤으면(청크 안에
        빈 줄이 남아 있으면) 셀 단위 재조립이 안전하지 않으므로 제외한다."""
        chunk = "| # | 문제 |\n|---|---|\n| A1 | 모호함 |\n\n뒤이은 산문 단락."
        assert not is_table_chunk(chunk)


class TestTranslateTableChunk:
    def test_translates_only_cells_containing_korean(self):
        chunk = "| # | 문제 | 담당 |\n|---|---|---|\n| A1 | 모호한 지시 | 방법론 |"
        calls: list[str] = []

        def fake(cell: str) -> str:
            calls.append(cell)
            return f"[EN:{cell}]"

        out = translate_table_chunk(chunk, fake)

        # 한글이 없는 셀("#", "A1")은 모델을 거치지 않는다
        assert calls == ["문제", "담당", "모호한 지시", "방법론"]
        assert (
            out == "| # | [EN:문제] | [EN:담당] |\n"
            "|---|---|---|\n"
            "| A1 | [EN:모호한 지시] | [EN:방법론] |"
        )

    def test_separator_row_is_never_translated(self):
        chunk = "| 헤더 |\n|---|\n| 내용 |"
        calls: list[str] = []

        translate_table_chunk(chunk, lambda c: calls.append(c) or c)

        assert "---" not in calls

    def test_preserves_column_count_per_row(self):
        chunk = "| a | b | c |\n|---|---|---|\n| 하나 | 둘 | 셋 |"
        out = translate_table_chunk(chunk, lambda c: c.upper())
        rows = out.split("\n")
        assert all(row.count("|") == 4 for row in rows)  # 3열 = 파이프 4개

    def test_hallucinated_cell_does_not_break_row_structure(self):
        """셀 하나가 환각(개행 섞인 응답)을 내도 최종 표의 각 행은 여전히
        한 줄이어야 한다 — 실사용에서 표 전체가 깨지던 실패 사례의 회귀 테스트."""
        chunk = "| a | 체크리스트 |\n|---|---|\n| 1 | 항목 |"

        def hallucinate_first_cell(cell: str) -> str:
            if cell == "체크리스트":
                return "## Checklist\n\n* item one\n* item two"
            return cell.upper()

        out = translate_table_chunk(chunk, hallucinate_first_cell)
        rows = out.split("\n")
        assert len(rows) == 3  # 헤더/구분선/데이터 3행 그대로 — 환각으로 행이 늘지 않음
        assert "체크리스트" in rows[0]  # 재시도도 실패했으니 원문 셀 그대로 보존

    def test_falls_back_to_translate_fn_when_not_a_table(self):
        """방어적 폴백 — is_table_chunk()가 아닌 입력이 실수로 들어와도
        조용히 깨지지 않고 기존 방식(전체를 translate_fn에 통째로)으로 처리."""
        calls: list[str] = []
        out = translate_table_chunk("그냥 산문.", lambda c: calls.append(c) or "번역됨")
        assert calls == ["그냥 산문."]
        assert out == "번역됨"


class TestLooksLikeCellHallucination:
    def test_normal_short_translation_is_not_flagged(self):
        assert not _looks_like_cell_hallucination("모호한 지시", "Ambiguous instruction")

    def test_multiline_result_is_flagged(self):
        """정상적인 셀 번역은 항상 한 줄이다 — 개행이 생겼다는 것은 모델이
        표 셀이 아니라 제목·리스트 같은 블록 요소를 지어냈다는 신호다."""
        assert _looks_like_cell_hallucination("체크리스트", "## Checklist\n\n* one\n* two")

    def test_grossly_oversized_result_is_flagged(self):
        """3단어짜리 셀이 수백 자로 부풀려지는 실사용 환각 패턴을 잡는다."""
        original = "코드 리뷰 체크리스트"
        hallucinated = "Code Review Checklist: " + ("Functionality check item. " * 10)
        assert _looks_like_cell_hallucination(original, hallucinated)

    def test_proportionally_longer_translation_is_not_flagged(self):
        """한국어보다 영어가 좀 더 길어지는 정상적인 경우까지 오탐하면 안 된다."""
        assert not _looks_like_cell_hallucination("담당", "Responsible party")


class TestTranslateCell:
    def test_clean_result_is_returned_as_is(self):
        assert _translate_cell("문제", lambda c: "Issue") == "Issue"

    def test_retries_once_on_suspected_hallucination(self):
        calls: list[str] = []

        def flaky(cell: str) -> str:
            calls.append(cell)
            return "## Fabricated\n\n* item" if len(calls) == 1 else "Clean translation"

        assert _translate_cell("셀", flaky) == "Clean translation"
        assert len(calls) == 2

    def test_falls_back_to_original_when_retry_also_hallucinates(self):
        """재시도까지 환각이면 표 구조를 깨는 대신 원문 셀을 그대로 남긴다 —
        미번역 셀은 챕터 단위 has_residual_korean 재시도가 잡아준다."""
        out = _translate_cell("셀", lambda c: "## Always fabricated\n\n* item")
        assert out == "셀"


class TestTranslateChunk:
    def test_pure_table_chunk_skips_translate_fn_for_whole_block(self):
        chunk = "| a | 문제 |\n|---|---|\n| 1 | 모호함 |"
        calls: list[str] = []

        translate_chunk(chunk, lambda c: calls.append(c) or c)

        assert chunk not in calls  # 표 전체가 한 덩어리로 넘어가지 않음

    def test_pure_prose_chunk_goes_through_translate_fn_unchanged(self):
        chunk = "그냥 산문 한 단락."
        calls: list[str] = []

        out = translate_chunk(chunk, lambda c: calls.append(c) or c.upper())

        assert calls == [chunk]
        assert out == chunk.upper()

    def test_table_followed_by_heading_in_same_chunk_splits_correctly(self):
        """chunk_paragraphs가 표 뒤에 짧은 소제목을 그리디하게 같이 묶는 경우
        (실제 실패 코퍼스에서 반복 확인된 패턴) — 표는 셀 단위로, 소제목은
        translate_fn으로 각각 번역돼야 한다."""
        chunk = "| a | 문제 |\n|---|---|\n| 1 | 모호함 |\n\n### 다음 절 제목"
        calls: list[str] = []

        out = translate_chunk(chunk, lambda c: calls.append(c) or f"[EN:{c}]")

        assert "모호함" in calls  # 표는 셀 단위로 쪼개짐
        assert "### 다음 절 제목" in calls  # 소제목은 그대로 한 번 넘어감
        assert chunk not in calls  # 청크 전체가 통째로 넘어가는 경우는 없음
        assert "|---|---|" in out
        assert "[EN:### 다음 절 제목]" in out

    def test_prose_paragraphs_around_a_table_stay_grouped(self):
        """표가 아닌 연속된 단락들은 여전히 하나로 묶여 translate_fn 한 번에
        전달돼야 한다 — 표 처리 도입으로 일반 산문의 호출 단위가 잘게
        쪼개지면 안 된다."""
        chunk = "첫 단락.\n\n둘째 단락.\n\n| a |\n|---|\n| 1 |"
        calls: list[str] = []

        translate_chunk(chunk, lambda c: calls.append(c) or c)

        assert "첫 단락.\n\n둘째 단락." in calls  # 표가 아닌 두 단락은 한 번에 호출됨


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

        translate_corpus(
            root,
            out_dir,
            config=None,
            target_language="en",
            translate_fn=lambda s: "Clean translation.",
        )

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
            root,
            out_dir,
            config=None,
            target_language="en",
            translate_fn=lambda s: calls.append(s) or s,
            chunk_chars=1000,
        )

        assert len(calls) == 1  # chunk_chars=1000이면 두 단락이 한 청크로 합쳐짐

    def test_incomplete_chunks_write_marker_file(self, tmp_path: Path):
        """재시도 후에도 한글이 남은 챕터는 dest 옆에 .incomplete.json 마커를
        남겨야 한다 — --resume이 이 챕터를 "이미 번역됨"으로 착각해 건너뛰지
        않도록 하는 신호다."""
        root = tmp_path / "src"
        out_dir = tmp_path / "out"
        _write(root, "chapter.md", "한글 원문입니다.\n")

        translate_corpus(root, out_dir, config=None, target_language="en", translate_fn=lambda s: s)

        marker = out_dir / "chapter.md.incomplete.json"
        assert marker.exists()
        data = json.loads(marker.read_text(encoding="utf-8"))
        assert data["incomplete_chunks"] == [1]
        assert data["total_chunks"] == 1

    def test_clean_translation_leaves_no_marker(self, tmp_path: Path):
        root = tmp_path / "src"
        out_dir = tmp_path / "out"
        _write(root, "chapter.md", "한글 원문입니다.\n")

        translate_corpus(
            root,
            out_dir,
            config=None,
            target_language="en",
            translate_fn=lambda s: "Clean translation.",
        )

        assert not (out_dir / "chapter.md.incomplete.json").exists()

    def test_resume_retranslates_chapter_marked_incomplete(self, tmp_path: Path):
        """--resume은 이전 실행의 미완료 마커가 남은 챕터를 "이미 번역됨"으로
        건너뛰지 않고 자동으로 삭제 후 재번역해야 한다 — 반복 --resume 실행으로
        점차 미완료 챕터가 줄어드는 것이 목표다."""
        root = tmp_path / "src"
        out_dir = tmp_path / "out"
        _write(root, "chapter.md", "한글 원문입니다.\n")
        out_dir.mkdir(parents=True)
        (out_dir / "chapter.md").write_text("한글 원문입니다.\n", encoding="utf-8")  # 이전 실행의 미완료 결과
        (out_dir / "chapter.md.incomplete.json").write_text(
            json.dumps({"incomplete_chunks": [1], "total_chunks": 1}), encoding="utf-8"
        )
        calls: list[str] = []

        translate_corpus(
            root,
            out_dir,
            config=None,
            target_language="en",
            translate_fn=lambda s: calls.append(s) or "Clean translation.",
            resume=True,
        )

        # 마커가 있었으므로 건너뛰지 않고 실제로 translate_fn이 호출되어야 한다
        assert len(calls) == 1
        assert (out_dir / "chapter.md").read_text(encoding="utf-8") == "Clean translation."
        # 이번엔 성공했으므로 마커는 정리된다
        assert not (out_dir / "chapter.md.incomplete.json").exists()

    def test_resume_keeps_marker_when_still_incomplete_after_retry(self, tmp_path: Path):
        root = tmp_path / "src"
        out_dir = tmp_path / "out"
        _write(root, "chapter.md", "한글 원문입니다.\n")
        out_dir.mkdir(parents=True)
        (out_dir / "chapter.md").write_text("한글 원문입니다.\n", encoding="utf-8")
        (out_dir / "chapter.md.incomplete.json").write_text(
            json.dumps({"incomplete_chunks": [1], "total_chunks": 1}), encoding="utf-8"
        )

        translate_corpus(
            root,
            out_dir,
            config=None,
            target_language="en",
            translate_fn=lambda s: s,  # 이번 재시도도 원문을 그대로 반환 — 여전히 실패
            resume=True,
        )

        marker = out_dir / "chapter.md.incomplete.json"
        assert marker.exists()
        assert json.loads(marker.read_text(encoding="utf-8"))["incomplete_chunks"] == [1]

    def test_incomplete_marker_includes_chunk_cache_for_chunk_level_resume(self, tmp_path: Path):
        """새 마커 포맷은 실패한 청크 번호뿐 아니라 청크별 번역 결과 캐시
        (chunks)와 그때 쓰인 chunk_chars도 담아야 다음 --resume이 실패한
        청크만 골라 재번역할 수 있다."""
        root = tmp_path / "src"
        out_dir = tmp_path / "out"
        _write(root, "chapter.md", "한글 원문입니다.\n")

        translate_corpus(
            root, out_dir, config=None, target_language="en", translate_fn=lambda s: s, chunk_chars=500
        )

        data = json.loads((out_dir / "chapter.md.incomplete.json").read_text(encoding="utf-8"))
        assert data["chunk_chars"] == 500
        assert data["chunks"] == ["한글 원문입니다.\n"]

    def test_chunk_level_resume_only_retranslates_failed_chunks(self, tmp_path: Path):
        """청크 단위 --resume의 핵심 시나리오: 마커에 캐시된 청크 중 실패했던
        청크만 translate_fn을 다시 타고, 나머지는 캐시된 번역을 그대로 써서
        최종 파일에 이어붙여야 한다."""
        root = tmp_path / "src"
        out_dir = tmp_path / "out"
        _write(root, "chapter.md", "\n\n".join(["a" * 60, "b" * 60, "c" * 60]))
        out_dir.mkdir(parents=True)
        # dest 파일 자체의 내용은 resume 판단에 쓰이지 않는다(마커의 chunks가
        # 진짜 캐시다) — 존재 여부만 --resume 진입 조건으로 확인된다.
        (out_dir / "chapter.md").write_text("이전 실행의 (일부 미완료) 결과", encoding="utf-8")
        (out_dir / "chapter.md.incomplete.json").write_text(
            json.dumps(
                {
                    "incomplete_chunks": [2],
                    "total_chunks": 3,
                    "chunk_chars": 100,
                    "chunks": ["cached1", "b" * 60, "cached3"],
                }
            ),
            encoding="utf-8",
        )
        calls: list[str] = []

        translate_corpus(
            root,
            out_dir,
            config=None,
            target_language="en",
            translate_fn=lambda s: calls.append(s) or s.upper(),
            chunk_chars=100,
            resume=True,
        )

        assert calls == ["b" * 60]  # 청크 1·3은 재번역하지 않음
        assert (
            out_dir / "chapter.md"
        ).read_text(encoding="utf-8") == "cached1\n\n" + ("b" * 60).upper() + "\n\ncached3"
        # 이번엔 재번역한 청크가 깔끔히 통과했으므로 마커는 정리된다
        assert not (out_dir / "chapter.md.incomplete.json").exists()

    def test_chunk_level_resume_falls_back_when_chunk_chars_changed(self, tmp_path: Path):
        """마커에 캐시된 chunk_chars가 이번 실행과 다르면 청킹 경계 자체가
        달라져 부분 재사용이 안전하지 않으므로 챕터 전체를 다시 번역한다."""
        root = tmp_path / "src"
        out_dir = tmp_path / "out"
        _write(root, "chapter.md", "\n\n".join(["a" * 60, "b" * 60, "c" * 60]))
        out_dir.mkdir(parents=True)
        (out_dir / "chapter.md").write_text("stale", encoding="utf-8")
        (out_dir / "chapter.md.incomplete.json").write_text(
            json.dumps(
                {
                    "incomplete_chunks": [2],
                    "total_chunks": 3,
                    "chunk_chars": 999,  # 이번 실행(100)과 불일치
                    "chunks": ["cached1", "cached2", "cached3"],
                }
            ),
            encoding="utf-8",
        )
        calls: list[str] = []

        translate_corpus(
            root,
            out_dir,
            config=None,
            target_language="en",
            translate_fn=lambda s: calls.append(s) or s,
            chunk_chars=100,
            resume=True,
        )

        assert len(calls) == 3  # chunk_chars 불일치라 전부 새로 번역

    def test_chunk_level_resume_falls_back_for_pre_0_5_4_marker_without_chunks(
        self, tmp_path: Path
    ):
        """chunks 필드가 없는 구버전 마커(0.5.3 이전)를 만나도 깨지지 않고
        전체 재번역으로 안전하게 폴백해야 한다."""
        root = tmp_path / "src"
        out_dir = tmp_path / "out"
        _write(root, "chapter.md", "\n\n".join(["a" * 60, "b" * 60, "c" * 60]))
        out_dir.mkdir(parents=True)
        (out_dir / "chapter.md").write_text("stale", encoding="utf-8")
        (out_dir / "chapter.md.incomplete.json").write_text(
            json.dumps({"incomplete_chunks": [2], "total_chunks": 3}),  # chunks 필드 없음
            encoding="utf-8",
        )
        calls: list[str] = []

        translate_corpus(
            root,
            out_dir,
            config=None,
            target_language="en",
            translate_fn=lambda s: calls.append(s) or s,
            chunk_chars=100,
            resume=True,
        )

        assert len(calls) == 3

    def test_resume_skips_chapters_already_present_in_out_dir(self, tmp_path: Path):
        """--resume은 중단 후 재실행 시 이미 번역된 챕터를 재번역하지 않아야
        한다 — 대용량 코퍼스에서 중간 실패 시 처음부터 다시 돌리지 않게 한다."""
        root = tmp_path / "src"
        out_dir = tmp_path / "out"
        _write(root, "01_chapter.md", "# 하나\n\n본문.\n")
        _write(root, "02_chapter.md", "# 둘\n\n본문.\n")
        out_dir.mkdir(parents=True)
        (out_dir / "01_chapter.md").write_text("이미 번역된 결과", encoding="utf-8")
        calls: list[str] = []

        translate_corpus(
            root,
            out_dir,
            config=None,
            target_language="en",
            translate_fn=lambda s: calls.append(s) or s,
            resume=True,
        )

        # 이미 있던 01은 그대로 보존(재번역으로 덮어써지지 않음), 02만 새로 번역됨
        assert (out_dir / "01_chapter.md").read_text(encoding="utf-8") == "이미 번역된 결과"
        assert (out_dir / "02_chapter.md").exists()
        assert not any("하나" in c for c in calls)

    def test_resume_false_retranslates_existing_files(self, tmp_path: Path):
        """resume 기본값(False)은 기존 동작 그대로 항상 재번역·덮어써야 한다."""
        root = tmp_path / "src"
        out_dir = tmp_path / "out"
        _write(root, "chapter.md", "# 챕터\n\n본문.\n")
        out_dir.mkdir(parents=True)
        (out_dir / "chapter.md").write_text("낡은 결과", encoding="utf-8")

        translate_corpus(
            root, out_dir, config=None, target_language="en", translate_fn=lambda s: "새 번역"
        )

        assert (out_dir / "chapter.md").read_text(encoding="utf-8") == "새 번역"

    def test_resume_prints_skip_message(self, tmp_path: Path, capsys):
        root = tmp_path / "src"
        out_dir = tmp_path / "out"
        _write(root, "chapter.md", "# 챕터\n\n본문.\n")
        out_dir.mkdir(parents=True)
        (out_dir / "chapter.md").write_text("이미 번역됨", encoding="utf-8")

        translate_corpus(
            root, out_dir, config=None, target_language="en", translate_fn=lambda s: s, resume=True
        )

        out = capsys.readouterr().out
        assert "건너뜀" in out
        assert "chapter.md" in out


def test_build_prompt_instructs_markdown_preservation_and_includes_chunk():
    prompt = _build_prompt("hello world", "ko")
    assert "hello world" in prompt
    assert "Markdown" in prompt


class _FakeOllamaClient:
    """실제 서버 없이 make_ollama_translate_fn()을 검증하기 위한 가짜
    ollama.Client — list()/generate() 호출만 흉내낸다."""

    def __init__(self, host: str, timeout: int) -> None:
        self.host = host
        self.timeout = timeout
        self.generate_calls: list[dict] = []

    def list(self):
        return SimpleNamespace(models=[SimpleNamespace(model="exaone3.5:7.8b")])

    def generate(self, *, model: str, prompt: str, options: dict | None = None):
        self.generate_calls.append({"model": model, "prompt": prompt, "options": options})
        return SimpleNamespace(response="번역 결과")


class TestMakeOllamaTranslateFn:
    def test_passes_temperature_and_num_ctx_as_generate_options(self, monkeypatch):
        """generate()에 options로 temperature/num_ctx를 명시 전달해야 한다 —
        안 넘기면 모델 Modelfile 기본값(exaone3.5:7.8b는 temperature 1)과
        서버 기본 컨텍스트 창이 쓰여, 표처럼 밀도 높은 청크에서 뒤쪽이
        잘리거나 모델이 지시를 놓치는 실패로 이어졌다."""
        created: list[_FakeOllamaClient] = []

        def _fake_client(host: str, timeout: int) -> _FakeOllamaClient:
            client = _FakeOllamaClient(host, timeout)
            created.append(client)
            return client

        monkeypatch.setattr("ollama.Client", _fake_client)

        cfg = TranslationConfig(model="exaone3.5:7.8b", temperature=0.1, num_ctx=16384)
        translate_fn = make_ollama_translate_fn(cfg, "en")
        translate_fn("안녕하세요")

        assert created[0].generate_calls[0]["options"] == {"temperature": 0.1, "num_ctx": 16384}


class TestResolveModelName:
    def test_exact_match_preferred(self):
        assert (
            resolve_model_name("qwen3.6:35b", ["qwen3.6:35b", "qwen3.6:35b-mlx"]) == "qwen3.6:35b"
        )

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
