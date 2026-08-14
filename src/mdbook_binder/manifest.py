"""코퍼스별 설정(book.yaml) 로딩과 챕터 순서 해석.

새 마크다운 파일이 코드 수정 없이 자동으로 빌드에 반영되도록, 순서는 항상
아래 3단계 우선순위로 결정된다:

    1. 명시적 config(book.yaml의 order.manifest 또는 order.files)
    1.5. config가 없어도 루트에 ```toc 펜스 매니페스트(Book-forge 목차 포맷)가
         있으면 자동 채택
    2. Part_<로마숫자>_.../Chapter_<NN>_... 명명 규칙 감지
    3. 위 전부 실패 시 디렉토리 트리 전체를 자연정렬(natural sort)해 전부 포함
       — "새 파일이 조용히 누락되는 일"을 없애는 최종 폴백

이 우선순위는 mdbook_binder 전체(HTML/PDF 빌드 공통)가 공유하는 계약이다.
"""

from __future__ import annotations

import fnmatch
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_ROMAN = {
    "I": 1,
    "II": 2,
    "III": 3,
    "IV": 4,
    "V": 5,
    "VI": 6,
    "VII": 7,
    "VIII": 8,
    "IX": 9,
    "X": 10,
}

# 관대한 기본값 — 코퍼스가 명시하지 않아도 흔한 관리용 문서는 자동 제외.
# 그래도 걸러지지 않는 파일이 있으면 book.yaml의 exclude로 보완한다.
DEFAULT_EXCLUDE: list[str] = [
    "book.yaml",
    "README.md",
    "readme.md",
]

# 콜아웃(TIP박스) 마커 — 기본값은 빈 리스트(전부 일반 blockquote로 렌더).
# 코퍼스가 book.yaml의 callouts.tip_markers로 명시해야 활성화된다.
DEFAULT_TIP_MARKERS: list[str] = []

LOCALE_STRINGS: dict[str, dict[str, str]] = {
    "ko": {
        "search_placeholder": "\U0001f50d 본문 검색…",
        "hits_found": "{n}건 발견",
        "no_match": "일치 없음",
        "of": "{cur} / {total}건",
        "prev_title": "이전 결과",
        "next_title": "다음 결과",
    },
    "en": {
        "search_placeholder": "\U0001f50d Search…",
        "hits_found": "{n} results",
        "no_match": "No matches",
        "of": "{cur} of {total}",
        "prev_title": "Previous match",
        "next_title": "Next match",
    },
}


@dataclass
class OrderConfig:
    manifest: str | None = None
    files: list[str] | None = None


@dataclass
class TranslationConfig:
    model: str = "exaone3.5:7.8b"
    host: str = "http://localhost:11434"
    timeout: int = 300
    chunk_chars: int = 2000


@dataclass
class SplitConfig:
    files: list[str] = field(default_factory=list)
    heading_level: int = 2


@dataclass
class BookConfig:
    title: str | None = None
    author: str | None = None
    language: str = "ko"
    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE))
    order: OrderConfig | None = None
    tip_markers: list[str] = field(default_factory=lambda: list(DEFAULT_TIP_MARKERS))
    section_id_overrides: dict[str, str] = field(default_factory=dict)
    custom_css: str | None = None
    color: str | None = None
    translation: TranslationConfig | None = None
    split: SplitConfig | None = None

    @classmethod
    def load(cls, root: Path) -> BookConfig | None:
        """루트의 book.yaml을 읽는다. 파일이 없으면 None(전부 기본값/자동 감지로 동작)."""
        path = root / "book.yaml"
        if not path.exists():
            return None
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            # 관대한 파싱 원칙 — 오타 하나가 빌드 전체를 죽이면 안 된다.
            # YAMLError는 줄/컬럼 포인터가 딸린 여러 줄짜리 메시지라, 뒤에
            # 이어질 안내 문구를 exc 안에 그대로 끼워 넣으면 마지막 줄에
            # 붙어버린다 — 안내 문구를 먼저 보여주고 상세는 들여써서 뒤에 둔다.
            detail = "\n".join(f"      {line}" for line in str(exc).splitlines())
            print(f"  ⚠️  book.yaml 파싱 실패 — 설정 없이 자동 감지로 진행\n{detail}")
            return None

        order_data = data.get("order")
        order = OrderConfig(**order_data) if order_data else None
        callouts = data.get("callouts") or {}
        translation_data = data.get("translation")
        translation = TranslationConfig(**translation_data) if translation_data else None
        split_data = data.get("split")
        split = SplitConfig(**split_data) if split_data else None

        return cls(
            title=data.get("title"),
            author=data.get("author"),
            language=data.get("language", "ko"),
            exclude=list(data.get("exclude", DEFAULT_EXCLUDE)),
            order=order,
            tip_markers=list(callouts.get("tip_markers", DEFAULT_TIP_MARKERS)),
            # 조회 시 fpath.stem도 NFC로 맞춰 비교한다(_section_id 참고) —
            # 여기서 키를 NFC로 정규화해둬야 그 비교가 성립한다.
            section_id_overrides={
                unicodedata.normalize("NFC", k): v
                for k, v in (data.get("section_id_overrides", {}) or {}).items()
            },
            custom_css=data.get("custom_css"),
            color=data.get("color"),
            translation=translation,
            split=split,
        )

    def locale(self) -> dict[str, str]:
        return LOCALE_STRINGS.get(self.language, LOCALE_STRINGS["ko"])

    def load_custom_css(self, root: Path) -> str:
        """book.yaml의 custom_css가 가리키는 파일을 읽는다.

        코퍼스별 raw-HTML 다이어그램(@@HTML_START@@ 블록)이 쓰는 커스텀
        클래스는 mdbook_binder의 범용 템플릿에 넣을 수 없다 — 코퍼스마다
        다이어그램 종류가 다르기 때문이다. 대신 각 코퍼스가 자기 CSS를
        선언하면 HTML/PDF 빌드 모두에 그대로 얹는다.
        """
        if not self.custom_css:
            return ""
        path = root / self.custom_css
        if not path.is_file():
            print(f"  ⚠️  custom_css 지정 파일 없음: {path}")
            return ""
        return path.read_text(encoding="utf-8")


def write_minimal_book_yaml(path: Path, *, language: str, **extra: object) -> None:
    """language(+선택 필드)만 있는 최소 book.yaml을 쓴다.

    pdf_import.py(PDF→코퍼스 추출)와 translation.py(번역된 코퍼스 출력) 둘 다
    자기 산출물이 바로 빌드 가능한 코퍼스이길 요구하는데, 원본에 book.yaml이
    없던 경우(PDF 임포트) 또는 원본 book.yaml을 그대로 옮기지 않기로 한 경우
    공통으로 필요한 최소 스캐폴딩이라 한 곳에 둔다.
    """
    data = {"language": language, **extra}
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


@dataclass
class ChapterFile:
    path: Path
    part_label: str | None = None


def _is_excluded(path: Path, patterns: list[str]) -> bool:
    # macOS(APFS/HFS+)는 파일명을 NFD(분해형)로 저장하지만 book.yaml은 보통
    # 에디터가 저장한 NFC(조합형)로 작성된다. 둘 다 화면엔 똑같이 보이지만
    # fnmatch는 바이트 비교라 정규화형이 다르면 절대 매치되지 않는다 —
    # 두 쪽 다 NFC로 맞춰야 exclude 패턴이 실제로 걸린다.
    name = unicodedata.normalize("NFC", path.name)
    return any(fnmatch.fnmatch(name, unicodedata.normalize("NFC", pat)) for pat in patterns)


def _natural_sort_key(path: Path) -> list:
    parts = re.split(r"(\d+)", str(path))
    return [int(p) if p.isdigit() else p.lower() for p in parts]


def _robust_children(d: Path) -> list[Path]:
    """NFC/NFD 정규화 차이로 인한 macOS 한글 파일명 미인식 문제를 피해 디렉토리 항목을 나열한다."""
    if not d.exists():
        return []
    return sorted(d.iterdir(), key=lambda p: unicodedata.normalize("NFC", p.name))


# ── 1순위: 명시적 config ──────────────────────────────────────────────────────


def _from_explicit_order(root: Path, order: OrderConfig) -> list[ChapterFile] | None:
    if order.manifest:
        manifest_path = root / order.manifest
        if not manifest_path.exists():
            print(f"  ⚠️  order.manifest 지정 파일 없음: {manifest_path} — 다음 순위로 폴백")
            return None
        return _from_toc_manifest(root, manifest_path)

    if order.files:
        result: list[ChapterFile] = []
        for pattern in order.files:
            matches = (
                sorted(root.glob(pattern), key=_natural_sort_key)
                if any(ch in pattern for ch in "*?[")
                else [root / pattern]
            )
            for m in matches:
                if m.exists():
                    result.append(ChapterFile(path=m))
                else:
                    print(f"  ⚠️  order.files 지정 파일 없음: {m}")
        return result or None

    return None


# ── 1.5순위: ```toc 펜스 매니페스트 자동 감지 (Book-forge 목차 포맷) ──────────

_TOC_FENCE_RE = re.compile(r"```toc\s*\n(.*?)```", re.DOTALL)


def _find_toc_manifest(root: Path) -> Path | None:
    for candidate in sorted(root.glob("*.md")):
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        if _TOC_FENCE_RE.search(text):
            return candidate
    return None


def _from_toc_manifest(root: Path, manifest_path: Path) -> list[ChapterFile] | None:
    text = manifest_path.read_text(encoding="utf-8")
    m = _TOC_FENCE_RE.search(text)
    if not m:
        return None

    result: list[ChapterFile] = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line:
            continue
        fields = line.split("|")
        if len(fields) < 4:
            continue
        part_no, part_title, chapter_no = fields[0], fields[1], fields[2]

        found = _find_chapter_file(root, part_no, chapter_no)
        if found is None:
            print(
                f"  ⚠️  매니페스트 항목에 대응하는 파일 없음: Part {part_no}/Chapter {chapter_no} — 건너뜀"
            )
            continue
        result.append(ChapterFile(path=found, part_label=f"Part {part_no}. {part_title}"))

    return result or None


_INT_TO_ROMAN = {v: k for k, v in _ROMAN.items()}


def _find_chapter_file(root: Path, part_no: str, chapter_no: str) -> Path | None:
    # 매니페스트의 part_no는 순수 숫자("1|기초|...")지만, 실제 디렉토리는
    # Book-forge 스캐폴딩 관례상 로마숫자(Part_I_...)로 명명된다 — 숫자/로마숫자
    # 둘 다 매칭 대상에 넣어야 한다.
    numeral_alts = [re.escape(part_no)]
    if part_no.isdigit() and (roman := _INT_TO_ROMAN.get(int(part_no))):
        numeral_alts.append(roman)
    part_re = re.compile(rf"Part_?0*(?:{'|'.join(numeral_alts)})\D")

    part_dirs = [d for d in root.iterdir() if d.is_dir() and part_re.match(d.name + "_")]
    search_dirs = part_dirs or [root]
    chapter_pat = re.compile(rf"Chapter_?0*{re.escape(chapter_no)}\D")
    for d in search_dirs:
        for f in sorted(d.glob("*.md")):
            if chapter_pat.match(f.stem + "_"):
                return f
    return None


# ── 2순위: Part_<로마숫자>_.../Chapter_<NN>_... 명명 규칙 감지 ────────────────

_PART_DIR_RE = re.compile(r"^Part_([IVX]+)_")
_CHAPTER_FILE_RE = re.compile(r"Chapter_(\d+)_")
_LEADING_NUM_RE = re.compile(r"^(\d+)")

# 최상위 파일의 앞머리 번호가 이 값 이상이면 "후주"(맺음말 등)로 보고 Part
# 챕터들 뒤에 배치한다 — 세 코퍼스 모두 00_ 접두사는 서문류, 9x_ 접두사는
# 맺음말류로 쓰는 실제 관례를 반영한 임계값이다.
_BACK_MATTER_THRESHOLD = 50


def _split_front_back(files: list[Path]) -> tuple[list[Path], list[Path]]:
    front: list[Path] = []
    back: list[Path] = []
    for f in files:
        m = _LEADING_NUM_RE.match(f.stem)
        if m and int(m.group(1)) >= _BACK_MATTER_THRESHOLD:
            back.append(f)
        else:
            front.append(f)
    return front, back


def _detect_part_chapter_convention(
    root: Path, config: BookConfig | None
) -> list[ChapterFile] | None:
    exclude = config.exclude if config else DEFAULT_EXCLUDE
    part_dirs = [d for d in root.iterdir() if d.is_dir() and _PART_DIR_RE.match(d.name)]
    if not part_dirs:
        return None

    def part_sort_key(d: Path) -> int:
        # part_dirs 필터링 단계에서 이미 _PART_DIR_RE에 매치된 이름만 걸러졌으므로
        # 여기서는 항상 매치된다 — mypy는 그 불변 조건을 함수 경계 너머로
        # 못 보므로 명시적으로 단언한다.
        m = _PART_DIR_RE.match(d.name)
        assert m is not None
        return _ROMAN.get(m.group(1), 99)

    top_level = [
        f for f in sorted(root.glob("*.md"), key=_natural_sort_key) if not _is_excluded(f, exclude)
    ]
    front, back = _split_front_back(top_level)

    result: list[ChapterFile] = [ChapterFile(path=f) for f in front]

    for part_dir in sorted(part_dirs, key=part_sort_key):
        part_label = re.sub(r"^Part_[IVX]+_", "", part_dir.name).replace("_", " ")
        chapters = sorted(
            (f for f in part_dir.glob("*.md") if not _is_excluded(f, exclude)),
            key=_natural_sort_key,
        )
        for f in chapters:
            result.append(
                ChapterFile(path=f, part_label=f"{part_dir.name.split('_')[1]}부. {part_label}")
            )

    result.extend(ChapterFile(path=f) for f in back)

    # Appendix/ 디렉토리는 Part_로마숫자 규칙을 안 따르므로 별도 처리 —
    # 항상 맨 뒤(후주 다음)에 붙인다.
    appendix_dir = next(
        (d for d in root.iterdir() if d.is_dir() and d.name.lower() == "appendix"), None
    )
    if appendix_dir:
        appendix_files = sorted(
            (f for f in appendix_dir.glob("*.md") if not _is_excluded(f, exclude)),
            key=_natural_sort_key,
        )
        result.extend(ChapterFile(path=f, part_label="Appendix") for f in appendix_files)

    return result or None


# ── 3순위: 자연정렬 전체 포함 (최종 폴백) ─────────────────────────────────────


def _natural_sort_all(root: Path, config: BookConfig | None) -> list[ChapterFile]:
    exclude = config.exclude if config else DEFAULT_EXCLUDE
    all_md = [f for f in root.rglob("*.md") if not _is_excluded(f, exclude)]
    all_md.sort(key=_natural_sort_key)
    return [ChapterFile(path=f) for f in all_md]


# ── 가상 분할(split) 대상 파일 해석 ──────────────────────────────────────────


def resolve_split_targets(root: Path, config: BookConfig | None) -> set[Path]:
    """book.yaml의 split.files 패턴에 매칭되는 파일들의 절대경로 집합을 반환한다.

    html_book.py가 이 집합에 속한 챕터만 chapter_split.split_chapter_markdown()으로
    빌드 시점에 여러 섹션으로 쪼갠다 — order.files와 동일한 glob 문법(리터럴
    경로는 그대로, 와일드카드가 있으면 root.glob())을 써서 일관성을 유지한다.
    """
    if not config or not config.split or not config.split.files:
        return set()

    result: set[Path] = set()
    for pattern in config.split.files:
        matches = root.glob(pattern) if any(ch in pattern for ch in "*?[") else [root / pattern]
        for m in matches:
            if m.exists():
                result.add(m.resolve())
    return result


# ── 진입점 ────────────────────────────────────────────────────────────────────


TIER_EXPLICIT_CONFIG = "1순위: 명시적 config(book.yaml order)"
TIER_TOC_MANIFEST = "1.5순위: ```toc 매니페스트 자동 감지"
TIER_PART_CONVENTION = "2순위: Part/Chapter 명명 규칙 감지"
TIER_NATURAL_SORT = "3순위: 자연정렬 전체 포함(폴백)"


def resolve_verbose(root: Path, config: BookConfig | None) -> tuple[list[ChapterFile], str]:
    """resolve()와 동일한 3단계 우선순위를 따르되, 어느 단계가 순서를 확정했는지도
    함께 반환한다 — `mdbook-binder check`가 빌드 전에 "왜 이 순서로 잡혔는지"를
    설명하는 데 쓴다.
    """
    if config and config.order:
        result = _from_explicit_order(root, config.order)
        if result:
            return result, TIER_EXPLICIT_CONFIG

    manifest_path = _find_toc_manifest(root)
    if manifest_path:
        result = _from_toc_manifest(root, manifest_path)
        if result:
            return result, TIER_TOC_MANIFEST

    result = _detect_part_chapter_convention(root, config)
    if result:
        return result, TIER_PART_CONVENTION

    return _natural_sort_all(root, config), TIER_NATURAL_SORT


def resolve(root: Path, config: BookConfig | None) -> list[ChapterFile]:
    chapters, _tier = resolve_verbose(root, config)
    return chapters
