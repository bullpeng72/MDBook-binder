"""pdf_book.py의 페이지 경계 계산 순수 함수 회귀 테스트.

`_merge_bands`/`_is_occupied`/`_nearest_safe_y`/`_chunk_boundaries`는 Mermaid
다이어그램을 PDF 페이지 경계에 맞춰 청크로 나누는 핵심 로직이다(pdf_book.py
모듈 docstring 참고). Playwright 브라우저 없이도 순수 계산만으로 테스트
가능한데, 최근 커밋 두 개(청크가 페이지 절반도 못 채운 채 다음 페이지로
밀리는 문제, 도형 한가운데가 잘리는 문제)가 정확히 이 로직의 버그를 고친
자리라 회귀 테스트로 고정해둔다.
"""

import io
from pathlib import Path

import pypdf

from mdbook_binder.manifest import ChapterFile
from mdbook_binder.pdf_book import (
    _add_merge_outline,
    _build_pdf_page_html,
    _chunk_boundaries,
    _is_occupied,
    _merge_bands,
    _nearest_safe_y,
    _print_missing_images,
    _rewrite_img_paths,
    _unlink_cross_file_references,
)


def _blank_pdf(n_pages: int) -> io.BytesIO:
    """도형/텍스트 없이 빈 페이지 n장짜리 PDF를 메모리에서 만든다.

    `_add_merge_outline`은 이미 append된 writer의 페이지 수만 보고 북마크의
    페이지 오프셋을 계산하므로, 실제 챕터 렌더링(Playwright) 없이도 검증할 수
    있다.
    """
    w = pypdf.PdfWriter()
    for _ in range(n_pages):
        w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    buf.seek(0)
    return buf


def _flatten_outline(
    items: list, reader: pypdf.PdfReader, depth: int = 0
) -> list[tuple[str, int, int]]:
    """pypdf의 중첩 outline 리스트를 (제목, 중첩깊이, 페이지번호) 튜플로 평탄화한다."""
    flat: list[tuple[str, int, int]] = []
    for item in items:
        if isinstance(item, list):
            flat.extend(_flatten_outline(item, reader, depth + 1))
        else:
            page_number = reader.get_destination_page_number(item)
            assert page_number is not None
            flat.append((item.title, depth, page_number))
    return flat


def _build_and_read_outline(
    entries: list[tuple[ChapterFile, str, int]],
) -> list[tuple[str, int, int]]:
    """entries의 각 챕터를 빈 PDF로 순서대로 append하고 `_add_merge_outline`을
    적용한 뒤, 그 결과를 다시 읽어 평탄화된 outline을 반환한다."""
    writer = pypdf.PdfWriter()
    for _chapter, _title, page_count in entries:
        writer.append(_blank_pdf(page_count))
    _add_merge_outline(writer, entries)

    buf = io.BytesIO()
    writer.write(buf)
    buf.seek(0)
    reader = pypdf.PdfReader(buf)
    return _flatten_outline(reader.outline, reader)


def _chapter(part_label: str | None) -> ChapterFile:
    return ChapterFile(path=Path("/corpus/chapter.md"), part_label=part_label)


class TestMergeBands:
    def test_empty_input(self):
        assert _merge_bands([]) == []

    def test_disjoint_bands_stay_separate(self):
        assert _merge_bands([(0, 5), (10, 15)]) == [(0, 5), (10, 15)]

    def test_overlapping_bands_merge(self):
        assert _merge_bands([(0, 10), (5, 15)]) == [(0, 15)]

    def test_touching_bands_merge(self):
        """끝점이 정확히 맞닿는 경우(s <= 이전 끝)도 하나로 합쳐야 한다."""
        assert _merge_bands([(0, 5), (5, 10)]) == [(0, 10)]

    def test_unsorted_input_is_sorted_before_merging(self):
        assert _merge_bands([(10, 20), (0, 5), (4, 12)]) == [(0, 20)]


class TestIsOccupied:
    def test_interior_point_is_occupied(self):
        assert _is_occupied(5, [(0, 10)]) is True

    def test_exact_boundary_is_not_occupied(self):
        """경계값(s, e) 자체는 점유 구간에 포함하지 않는다(엄격한 부등호) —
        `_nearest_safe_y`가 밴드 가장자리를 안전한 절단 지점으로 쓸 수 있으려면
        이 경계값이 '비어 있음'으로 취급돼야 한다."""
        assert _is_occupied(0, [(0, 10)]) is False
        assert _is_occupied(10, [(0, 10)]) is False

    def test_point_outside_any_band_is_not_occupied(self):
        assert _is_occupied(15, [(0, 10)]) is False


class TestNearestSafeY:
    def test_returns_target_unchanged_when_already_free(self):
        assert _nearest_safe_y(50, [(0, 10)], 0, 100) == 50

    def test_clamps_target_into_lo_hi_range_first(self):
        assert _nearest_safe_y(500, [], 0, 100) == 100
        assert _nearest_safe_y(-50, [], 0, 100) == 0

    def test_shifts_away_from_occupied_band_toward_lo(self):
        """target이 도형 한가운데면, 검색 범위(lo~target) 안에서 가장 가까운
        빈 지점으로 물러나야 한다 — 위쪽(작은 y)으로만 물러나고 hi(페이지
        경계) 너머로는 절대 넘어가지 않는다."""
        safe = _nearest_safe_y(1000, [(950, 1050)], 400, 1000)
        assert safe == 950.0
        assert not _is_occupied(safe, [(950, 1050)])

    def test_falls_back_to_target_when_entire_range_occupied(self):
        """lo~hi 전체가 점유돼 안전한 지점을 못 찾으면 target을 그대로 반환한다
        (호출부가 이 경우를 별도로 감내함)."""
        assert _nearest_safe_y(5, [(0, 10)], 0, 10) == 5


class TestChunkBoundaries:
    def test_diagram_fitting_in_remaining_space_is_single_chunk(self):
        assert _chunk_boundaries(th=100, bands_raw=[], page_h=1000, remaining_first=150) == [
            0.0,
            100.0,
        ]

    def test_tiny_remaining_space_is_skipped_in_favor_of_next_page(self):
        """페이지 하단에 15% 미만만 남았으면 그 자투리를 억지로 쓰지 않고
        페이지 한 장 분량(page_h) 전체를 기준으로 다시 판단해야 한다."""
        # remaining_first=100은 page_h(1000)의 15% 미만이므로 무시되고
        # page_h 전체(1000) 기준으로 판단 → th(50)가 한 청크에 들어간다.
        assert _chunk_boundaries(th=50, bands_raw=[], page_h=1000, remaining_first=100) == [
            0.0,
            50.0,
        ]

    def test_multi_page_split_without_bands_lands_exactly_on_page_boundaries(self):
        boundaries = _chunk_boundaries(th=2500, bands_raw=[], page_h=1000, remaining_first=1000)
        assert boundaries == [0.0, 1000.0, 2000.0, 2500.0]

    def test_boundary_never_cuts_through_the_middle_of_a_shape(self):
        """실측으로 확인됐던 회귀: 청크 경계가 도형/라벨 밴드 한가운데를 지나면
        박스나 텍스트가 반토막나 보인다. 밴드가 페이지 경계에 걸쳐 있을 때
        경계가 밴드 밖으로 밀려나는지 확인한다."""
        boundaries = _chunk_boundaries(
            th=2000, bands_raw=[(950, 1050)], page_h=1000, remaining_first=1000
        )
        interior_boundaries = boundaries[1:-1]
        assert interior_boundaries, "테스트 시나리오가 실제로 중간 경계를 만들어내야 한다"
        for y in interior_boundaries:
            assert not _is_occupied(y, [(950, 1050)])

    def test_chunk_never_overshoots_its_page_budget(self):
        """청크 하나가 페이지 경계를 넘으면 break-inside:avoid가 있어도 이미지
        자체가 잘려버린다(모듈 docstring 참고) — 각 경계는 목표 지점(budget 누적)
        이전에서만 잡혀야 한다."""
        boundaries = _chunk_boundaries(
            th=3000, bands_raw=[(1900, 2100)], page_h=1000, remaining_first=1000
        )
        budget_ceiling = 0.0
        remaining = [1000.0] + [1000.0] * 10  # 첫 청크 1000, 이후 매 페이지 1000
        for i in range(1, len(boundaries)):
            budget_ceiling += remaining[i - 1]
            assert boundaries[i] <= budget_ceiling + 1e-9


class TestAddMergeOutline:
    """병합 PDF(`--merge`) 북마크 — 챕터별 북마크를 달고, Part가 있으면 그
    Part 제목 아래 중첩한다(README '알려진 한계' 개선 항목)."""

    def test_chapters_without_part_get_flat_top_level_bookmarks(self):
        entries = [
            (_chapter(None), "서문", 2),
            (_chapter(None), "맺음말", 1),
        ]
        flat = _build_and_read_outline(entries)

        assert flat == [("서문", 0, 0), ("맺음말", 0, 2)]

    def test_chapters_sharing_a_part_nest_under_one_part_bookmark(self):
        entries = [
            (_chapter("I부. 방법론"), "ch1", 2),
            (_chapter("I부. 방법론"), "ch2", 3),
            (_chapter("II부. 아키텍처"), "ch3", 1),
        ]
        flat = _build_and_read_outline(entries)

        assert flat == [
            ("I부. 방법론", 0, 0),
            ("ch1", 1, 0),
            ("ch2", 1, 2),
            ("II부. 아키텍처", 0, 5),
            ("ch3", 1, 5),
        ]

    def test_front_and_back_matter_stay_top_level_around_a_part(self):
        """Part가 없는 서문/후주는 Part 챕터들 앞뒤에서 각자 최상위 북마크를
        얻어야 한다 — 앞선 Part의 자식으로 잘못 중첩되면 안 된다."""
        entries = [
            (_chapter(None), "서문", 1),
            (_chapter("I부. 방법론"), "ch1", 2),
            (_chapter(None), "맺음말", 1),
        ]
        flat = _build_and_read_outline(entries)

        assert flat == [
            ("서문", 0, 0),
            ("I부. 방법론", 0, 1),
            ("ch1", 1, 1),
            ("맺음말", 0, 3),
        ]


class TestBuildPdfPageHtml:
    def test_language_reflected_in_html_lang_attribute(self):
        """--language(book.yaml language)가 각 페이지의 <html lang>에
        반영돼야 한다 — 예전엔 "ko"로 하드코딩돼 있었다(회귀 재현)."""
        html = _build_pdf_page_html("<p>body</p>", "Title", language="en")
        assert '<html lang="en">' in html

    def test_default_language_is_korean(self):
        html = _build_pdf_page_html("<p>body</p>", "Title")
        assert '<html lang="ko">' in html

    def test_title_is_html_escaped(self):
        html = _build_pdf_page_html("<p>body</p>", "A & B", language="en")
        assert "<title>A &amp; B</title>" in html


class TestUnlinkCrossFileReferences:
    """회귀 테스트: build pdf는 html_book.py의 build_html()과 달리 챕터 간
    상호참조 링크를 앵커로 재작성하지 않는다 — 각 챕터가 별개 Playwright
    페이지로 독립 렌더링된 뒤 pypdf로 병합되는 구조라, 한 챕터의 HTML 안에
    다른 챕터의 앵커 대상 자체가 존재하지 않기 때문이다(pdf_book.py 모듈
    docstring 참고). 원본 .md 상대경로를 클릭 가능한 것처럼 보이는 죽은
    링크로 남기는 대신, 링크 껍데기만 벗기고 텍스트는 보존해야 한다."""

    def test_internal_md_link_is_unwrapped_to_plain_text(self):
        html = '<p>자세한 내용은 <a href="../Part_I/Chapter_05.md">5장</a>을 참고.</p>'
        out = _unlink_cross_file_references(html)
        assert out == "<p>자세한 내용은 5장을 참고.</p>"

    def test_md_link_with_fragment_is_also_unwrapped(self):
        html = '<a href="./Chapter_02.md#절">2장 §절</a>'
        assert _unlink_cross_file_references(html) == "2장 §절"

    def test_external_url_is_untouched(self):
        html = '<a href="https://example.com">외부 링크</a>'
        assert _unlink_cross_file_references(html) == html

    def test_same_page_fragment_is_untouched(self):
        html = '<a href="#섹션">같은 페이지 내부</a>'
        assert _unlink_cross_file_references(html) == html


class TestRewriteImgPaths:
    def test_missing_image_recorded_without_raising(self, tmp_path: Path):
        """회귀 테스트: html_book.py는 누락된 이미지를 빌드 끝에 요약
        경고하는데, pdf_book.py는 존재 여부 확인 자체가 없어 경로 오타가
        있어도 아무 경고 없이 조용히 깨진 이미지로 나왔다."""
        missing: list[tuple[Path, str]] = []
        html = _rewrite_img_paths('<img src="no-such-file.png">', tmp_path, missing)

        assert len(missing) == 1
        assert missing[0][1] == "no-such-file.png"
        assert "file://" in html  # 경고만 기록하고 렌더링 자체는 계속 진행

    def test_existing_image_not_recorded_as_missing(self, tmp_path: Path):
        (tmp_path / "pic.png").write_bytes(b"\x89PNG")
        missing: list[tuple[Path, str]] = []
        _rewrite_img_paths('<img src="pic.png">', tmp_path, missing)
        assert missing == []

    def test_missing_param_omitted_does_not_raise(self, tmp_path: Path):
        """missing을 안 넘기는 기존 호출부와의 하위 호환 — 존재 여부 확인을
        건너뛰고 예전처럼 경로만 치환한다."""
        html = _rewrite_img_paths('<img src="no-such-file.png">', tmp_path)
        assert "file://" in html


class TestPrintMissingImages:
    def test_empty_list_prints_nothing(self, tmp_path: Path, capsys):
        _print_missing_images([], tmp_path)
        assert capsys.readouterr().out == ""

    def test_prints_relative_path_and_original_src(self, tmp_path: Path, capsys):
        abs_path = tmp_path / "images" / "missing.png"
        _print_missing_images([(abs_path, "images/missing.png")], tmp_path)
        out = capsys.readouterr().out
        assert "누락된 이미지 1건" in out
        assert "images/missing.png" in out
