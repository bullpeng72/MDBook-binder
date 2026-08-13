"""mdbook-binder CLI.

    mdbook-binder check       <root>                                     빌드 전 사전 점검
    mdbook-binder build html  <root> [--out FILE] [--title TITLE] [--language ko|en] [--color NAME]
    mdbook-binder build pdf   <root> [--merge [이름]] [--out-dir ...] [--color NAME]
    mdbook-binder edit        <html>  [--port 5757] [--out ...] [--no-browser]
    mdbook-binder import pdf  <pdf> <out_dir> [--title TITLE]
    mdbook-binder translate   <root> <out_dir> --direction k2e|e2k [--model ...] [--host ...]
"""

from __future__ import annotations

from pathlib import Path

import click

from mdbook_binder import __version__
from mdbook_binder.manifest import BookConfig
from mdbook_binder.theme import THEMES

_COLOR_CHOICE = click.Choice(sorted(THEMES), case_sensitive=False)
_COLOR_HELP = "사이드바/제목 강조색 테마 (기본: book.yaml의 color 또는 purple)"


@click.group(
    epilog="""\b
빠른 시작:
  mdbook-binder check ~/my-book               # 1. 빌드 전 순서·중복·누락 이미지·설치 환경 점검
  mdbook-binder build html ~/my-book          # 2. 검색 가능한 단일 HTML 생성
  mdbook-binder build pdf ~/my-book --merge   # 3. (선택) 한 권으로 병합한 PDF
  mdbook-binder edit ~/my-book/my-book.html   # 4. (선택) 브라우저에서 섹션 단위 편집

영문 PDF를 로컬 LLM으로 번역해 도서로 만들려면(토큰 비용 없음, Ollama 필요):
  mdbook-binder import pdf ~/book.pdf ~/corpus-en           # 5. PDF → 영문 코퍼스
  mdbook-binder translate ~/corpus-en ~/corpus-ko --direction e2k  # 6. 영→한 번역
  mdbook-binder build html ~/corpus-ko                       # 7. 위 2와 동일하게 빌드

각 명령의 옵션은 `mdbook-binder <명령> --help`로 확인한다(예: `mdbook-binder
build pdf --help`).
"""
)
@click.version_option(__version__, prog_name="mdbook-binder")
def main() -> None:
    """mdbook-binder — 마크다운 코퍼스를 검색 가능한 HTML 도서·PDF·웹 편집기로 변환한다.

    임의의 마크다운 파일 모음(코퍼스)을 입력으로 받아 코드 수정 없이 세 가지를
    만든다: 사이드바 목차·전문 검색을 갖춘 단일 HTML 파일, 챕터별 개별 또는
    한 권으로 병합한 PDF, 그리고 생성된 HTML을 브라우저에서 섹션 단위로 다시
    편집하는 웹 편집기. 챕터 순서는 book.yaml 설정이 있으면 그대로 따르고,
    없으면 파일/디렉토리 명명 규칙이나 자연정렬로 자동 추론한다.
    """


@main.command(
    "check",
    epilog="""\b
예시:
  mdbook-binder check ~/my-book
"""
)
@click.argument("root", type=click.Path(exists=True, file_okay=False, path_type=Path))
def check_cmd(root: Path) -> None:
    """ROOT의 마크다운 코퍼스를 실제로 빌드하지 않고 미리 점검한다.

    HTML/PDF를 만들지 않고 원본 마크다운만 훑으므로 큰 코퍼스에서도 즉시
    끝난다(아래 세 가지). 이어서 선택 기능(extras)의 설치 상태를 점검하는데,
    이쪽은 Playwright Chromium을 실제로 잠깐 띄워봐야 해서 몇 초 더 걸린다.

    \b
    - 순서 해석: book.yaml order → ```toc 매니페스트 → Part/Chapter 명명 규칙
      → 자연정렬 폴백 중 어느 단계(1~3순위)가 챕터 순서를 확정했는지와, 그
      결과 확정된 순서를 그대로 나열해 보여준다.
    - 중복 제목: 서로 다른 파일이 같은 h1 제목을 쓰면 HTML 빌드 시 섹션
      id에 "-2", "-3"...이 자동으로 붙는데, 그 대상을 미리 알려준다.
    - 누락 이미지: 마크다운이 참조하는 이미지 파일이 실제로 없는 경우(오타
      등)를 찾아낸다.

    챕터로 잘못 분류된 문서(예: 집필 가이드용 .md)를 빌드 후 HTML을 열어보고
    나서야 발견하는 일을 줄이기 위한 명령이다.

    \b
    마지막으로 PDF 빌드(Playwright Chromium)·웹 에디터(Flask/Pillow) 등
    선택 기능(extras)의 설치 상태도 함께 점검해, 몇 분짜리 빌드를 끝까지
    돌리고 나서야 "Playwright 브라우저 엔진이 설치되지 않았습니다" 같은
    메시지를 보는 대신 미리 설치 명령을 안내받을 수 있다.
    """
    from mdbook_binder.check import (
        check_corpus,
        check_environment,
        format_env_report,
        format_report,
    )

    config = BookConfig.load(root)
    result = check_corpus(root, config)
    print(format_report(root, result))
    print()
    print(format_env_report(check_environment(config)))


@main.group("build")
def build() -> None:
    """HTML 또는 PDF 도서를 빌드한다.

    두 하위 명령 모두 book.yaml(있으면)과 3단계 순서 해석 규칙을 동일하게
    공유한다 — 어느 쪽을 먼저 빌드하든 챕터 순서는 항상 같다. 순서가 의도한
    대로 잡히는지 미심쩍으면 빌드 전에 `mdbook-binder check`로 먼저
    확인한다.
    """


@build.command(
    "html",
    epilog="""\b
예시:
  mdbook-binder build html ~/my-book
  mdbook-binder build html ~/my-book --title "나의 책" --language en --color teal
  mdbook-binder build html ~/my-book --out ./dist/book.html
"""
)
@click.argument("root", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--out", "out_path", type=click.Path(path_type=Path), default=None,
    help="출력 HTML 경로 (기본: ROOT/<제목을 슬러그화한 이름>.html)",
)
@click.option(
    "--title", "title_override", default=None,
    help="도서 제목 오버라이드 (기본: book.yaml의 title, 그것도 없으면 ROOT 디렉토리 이름)",
)
@click.option(
    "--language", "language_override", default=None,
    help=(
        "검색창 문구 등 UI 로케일 오버라이드 (기본: book.yaml의 language, 그것도 없으면 ko). "
        "ko/en 문자열만 준비돼 있어 그 외 값은 UI 문구는 ko로 폴백하지만 <html lang> "
        "속성에는 입력값이 그대로 쓰인다"
    ),
)
@click.option("--color", "color_override", type=_COLOR_CHOICE, default=None, help=_COLOR_HELP)
def build_html_cmd(
    root: Path,
    out_path: Path | None,
    title_override: str | None,
    language_override: str | None,
    color_override: str | None,
) -> None:
    """ROOT 아래 마크다운 코퍼스를 검색 가능한 단일 HTML 도서로 빌드한다.

    사이드바 목차와 인페이지 전문 검색을 갖추고, 이미지는 base64로 인라인
    임베드되어 파일 하나만으로 열린다. Mermaid 다이어그램도 가능하면(Playwright/
    Chromium이 설치돼 있으면) 빌드 시점에 정적 SVG로 사전 렌더링해 같이
    임베드한다 — 없으면 열람 시 CDN mermaid.js로 폴백한다. 코드 하이라이트와
    본문 웹폰트는 아직 CDN 의존적이다(README의 "알려진 한계" 참고).
    """
    from mdbook_binder.html_book import build_html

    print(f"\U0001f4da Building HTML book from {root} ...")
    config = BookConfig.load(root)
    build_html(
        root,
        config,
        out_path=out_path,
        title_override=title_override,
        language_override=language_override,
        color_override=color_override,
    )


@build.command(
    "pdf",
    epilog="""\b
예시:
  mdbook-binder build pdf ~/my-book                  # 챕터별 개별 PDF (ROOT/pdf/ 아래)
  mdbook-binder build pdf ~/my-book --merge           # 한 권으로 병합 → full_book.pdf
  mdbook-binder build pdf ~/my-book --merge my_book   # 한 권으로 병합 → my_book.pdf
  mdbook-binder build pdf ~/my-book --out-dir ./dist --color green
"""
)
@click.argument("root", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--merge", "merge_out", is_flag=False, flag_value="full_book", default=None,
    metavar="[이름]",
    help=(
        "전체 챕터를 순서대로 한 권의 PDF로 병합한다(부분 병합은 미지원). "
        "이름을 생략하면 full_book.pdf, 지정하면 <이름>.pdf로 저장되고 챕터·Part "
        "북마크가 자동으로 붙는다. 생략하면 챕터별 개별 PDF를 만든다"
    ),
)
@click.option(
    "--out-dir", "out_dir", type=click.Path(path_type=Path), default=None,
    help=(
        "PDF 출력 디렉토리 (기본: ROOT/pdf). 개별 빌드는 이 아래에 코퍼스와 동일한 "
        "디렉토리 구조로, 병합 빌드는 이 아래에 단일 파일로 저장된다"
    ),
)
@click.option("--color", "color_override", type=_COLOR_CHOICE, default=None, help=_COLOR_HELP)
def build_pdf_cmd(
    root: Path, merge_out: str | None, out_dir: Path | None, color_override: str | None
) -> None:
    """ROOT 아래 마크다운을 챕터별 PDF(또는 --merge 시 단권)로 빌드한다.

    각 챕터를 Playwright/Chromium으로 독립 렌더링한다 —
    `pip install "mdbook-binder[pdf]"`와 `python -m playwright install
    chromium` 설치가 먼저 필요하다(브라우저가 없으면 안내 메시지와 함께
    실패한다). 긴 Mermaid 다이어그램은 실제 PDF 페이지 경계에 맞춰 청크로
    나눠 스크린샷을 삽입해, 도형이 페이지 사이에서 잘리지 않게 한다.
    """
    from mdbook_binder.pdf_book import build_pdf

    print(f"\U0001f4da Building PDF from {root} ...")
    try:
        build_pdf(root, merge_name=merge_out, out_dir=out_dir, color_override=color_override)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command(
    "edit",
    epilog="""\b
예시:
  mdbook-binder edit my-book.html
  mdbook-binder edit my-book.html --port 8080 --out final.html
  mdbook-binder edit my-book.html --no-browser   # 서버만 띄우고 URL은 직접 열기
"""
)
@click.argument("html_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--port", "-p", type=int, default=5757, help="에디터 서버가 열릴 로컬 포트 (기본: 5757)")
@click.option(
    "--out", "out_path", type=click.Path(path_type=Path), default=None,
    help=(
        "브라우저에서 '저장'을 눌렀을 때 쓸 경로 (기본: <HTML_PATH의 stem>_edited.html). "
        "저장 버튼을 누르기 전까지 원본 HTML_PATH는 전혀 수정되지 않는다"
    ),
)
@click.option("--no-browser", is_flag=True, default=False, help="서버만 띄우고 브라우저 자동 오픈은 생략한다")
def edit_cmd(html_path: Path, port: int, out_path: Path | None, no_browser: bool) -> None:
    """HTML_PATH를 브라우저 편집 UI로 연다.

    `build html`이 만든 `<section id="...">` 구조에만 의존하므로 어떤
    코퍼스로 만든 HTML이든 동일하게 동작한다. 섹션 단위 마크다운 편집,
    이미지 추가/교체/삭제, Mermaid 다이어그램 삭제, 이미지 업로드·갤러리를
    제공한다. `pip install "mdbook-binder[editor]"`(Flask/Pillow)가 필요하다.
    """
    from mdbook_binder.editor.server import run_editor

    run_editor(
        str(html_path),
        output_path=str(out_path) if out_path else None,
        port=port,
        open_browser=not no_browser,
    )


@main.group("import")
def import_group() -> None:
    """외부 포맷(PDF 등)을 mdbook-binder 코퍼스로 변환한다."""


@import_group.command(
    "pdf",
    epilog="""\b
예시:
  mdbook-binder import pdf ./book.pdf ./corpus-en
  mdbook-binder import pdf ./book.pdf ./corpus-en --title "My Book"
"""
)
@click.argument("pdf_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("out_dir", type=click.Path(path_type=Path))
@click.option("--title", "title_override", default=None, help="챕터 제목/파일명 오버라이드 (기본: PDF 파일명)")
def import_pdf_cmd(pdf_path: Path, out_dir: Path, title_override: str | None) -> None:
    """PDF_PATH(영문 PDF)를 단일 평면 마크다운 + book.yaml로 추출해 OUT_DIR에 만든다.

    챕터 분리·이미지 추출은 하지 않는다(전체를 파일 하나로) — Phase 2/3.
    결과는 그 자체로 `mdbook-binder build html/pdf`나 `translate`가 바로
    받는 유효한 코퍼스다(3순위 자연정렬 폴백이 단일 파일도 그대로 받는다).
    """
    from mdbook_binder.pdf_import import import_pdf

    print(f"\U0001f4c4 Extracting {pdf_path} ...")
    md_path = import_pdf(pdf_path, out_dir, title=title_override)
    print(f"✅ {md_path}")


@main.command(
    "translate",
    epilog="""\b
예시:
  mdbook-binder translate ~/my-book ~/my-book-ko --direction e2k
  mdbook-binder translate ~/my-book ~/my-book-en --direction k2e --model qwen3.6:35b
  mdbook-binder translate ~/my-book ~/my-book-ko --direction e2k --check-only
"""
)
@click.argument("root", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.argument("out_dir", type=click.Path(path_type=Path))
@click.option(
    "--direction", type=click.Choice(["k2e", "e2k"]), required=True,
    help="k2e: 한국어→영어, e2k: 영어→한국어",
)
@click.option(
    "--model", "model_override", default=None,
    help="Ollama 모델명 오버라이드 (기본: book.yaml의 translation.model, 그것도 없으면 qwen3.6:35b)",
)
@click.option(
    "--host", "host_override", default=None,
    help="Ollama 서버 host 오버라이드 (기본: book.yaml의 translation.host, 그것도 없으면 http://localhost:11434)",
)
@click.option("--timeout", "timeout_override", type=int, default=None, help="청크 1건당 번역 타임아웃(초) 오버라이드")
@click.option("--chunk-chars", "chunk_chars_override", type=int, default=None, help="청크 최대 글자수 오버라이드")
@click.option(
    "--check-only", is_flag=True, default=False,
    help="실제 번역 없이 Ollama 연결·모델 설치 상태만 확인하고 종료한다",
)
def translate_cmd(
    root: Path,
    out_dir: Path,
    direction: str,
    model_override: str | None,
    host_override: str | None,
    timeout_override: int | None,
    chunk_chars_override: int | None,
    check_only: bool,
) -> None:
    """ROOT의 마크다운 코퍼스를 로컬 Ollama로 번역해 OUT_DIR에 미러링한다.

    manifest.resolve()로 챕터를 걷어(exclude/order 규칙 그대로 적용) 순서대로
    번역하고, 코드/mermaid/raw-HTML 블록은 번역하지 않고 그대로 보존한다.
    book.yaml의 language 필드는 방향에 맞춰(k2e→en, e2k→ko) 재작성된다.
    로컬 LLM 번역은 청크 하나에도 수십 초가 걸릴 수 있어 챕터·청크 단위
    진행 상황을 출력한다. 모델이 없어도 자동으로 pull하지 않는다 —
    `ollama pull <모델>`을 직접 실행해야 한다.
    """
    from mdbook_binder.check import check_ollama
    from mdbook_binder.manifest import TranslationConfig
    from mdbook_binder.translation import make_ollama_translate_fn, translate_corpus

    config = BookConfig.load(root) or BookConfig()
    base = config.translation or TranslationConfig()
    cfg = TranslationConfig(
        model=model_override or base.model,
        host=host_override or base.host,
        timeout=timeout_override or base.timeout,
        chunk_chars=chunk_chars_override or base.chunk_chars,
    )
    target_language = "en" if direction == "k2e" else "ko"

    probe = check_ollama(BookConfig(translation=cfg))
    if not probe.installed:
        raise click.ClickException(f"{probe.feature} 사용 불가 — {probe.install_hint}")
    if check_only:
        print(f"✅ {probe.feature} 사용 가능 (model={cfg.model}, host={cfg.host})")
        return

    translate_fn = make_ollama_translate_fn(cfg, target_language)
    print(f"\U0001f310 Translating {root} → {out_dir} ({direction}) via {cfg.model}@{cfg.host} ...")
    translate_corpus(root, out_dir, config, target_language, translate_fn, chunk_chars=cfg.chunk_chars)
    print(f"✅ {out_dir}")


if __name__ == "__main__":
    main()
