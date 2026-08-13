"""mdbook-binder CLI.

    mdbook-binder check       <root>                                     빌드 전 사전 점검
    mdbook-binder build html  <root> [--out FILE] [--title TITLE] [--language ko|en] [--color NAME]
    mdbook-binder build pdf   <root> [--merge [이름]] [--out-dir ...] [--color NAME]
    mdbook-binder edit        <html>  [--port 5757] [--out ...] [--no-browser]
    mdbook-binder import      <pdf> <out_dir> [--title TITLE]
    mdbook-binder translate   <root> <out_dir> --direction k2e|e2k [--model ...] [--host ...]
"""

from __future__ import annotations

from pathlib import Path

import click

from mdbook_binder import __version__
from mdbook_binder.manifest import BookConfig
from mdbook_binder.theme import THEMES

_COLOR_CHOICE = click.Choice(sorted(THEMES), case_sensitive=False)
_COLOR_HELP = "강조색 (기본: book.yaml color 또는 purple)"


@click.group(
    epilog="""\b
빠른 시작:
  mdbook-binder check ~/my-book               # 1. 사전 점검
  mdbook-binder build html ~/my-book          # 2. HTML 생성
  mdbook-binder build pdf ~/my-book --merge   # 3. (선택) PDF 병합
  mdbook-binder edit ~/my-book/my-book.html   # 4. (선택) 편집

\b
영문 PDF 번역(로컬 LLM, 토큰 비용 없음, 기본 모델 exaone3.5:7.8b):
  mdbook-binder import ~/book.pdf ~/corpus-en
  mdbook-binder translate ~/corpus-en ~/corpus-ko --direction e2k
  mdbook-binder build html ~/corpus-ko

옵션은 `mdbook-binder <명령> --help`로 확인(예: `build pdf --help`).
"""
)
@click.version_option(__version__, prog_name="mdbook-binder")
def main() -> None:
    """mdbook-binder — 마크다운 코퍼스를 검색 가능한 HTML·PDF·웹 편집기로 변환한다.

    \b
    마크다운 파일 모음을 입력받아 사이드바 목차·전문 검색을 갖춘 단일 HTML,
    챕터별/병합 PDF, 브라우저에서 다시 편집할 수 있는 웹 에디터를 만든다.
    챕터 순서는 book.yaml 설정 또는 파일명 규칙·자연정렬로 자동 추론한다.
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

    \b
    인자:
      ROOT  마크다운 코퍼스 루트 디렉토리

    \b
    확인 항목:
    - 챕터 순서: book.yaml order → toc 매니페스트 → Part/Chapter 명명
      규칙 → 자연정렬 중 무엇으로 정해졌는지와 최종 순서
    - 중복 제목: 같은 h1 제목을 쓰는 챕터(HTML id에 -2, -3... 자동 부여됨)
    - 누락 이미지: 마크다운이 참조하지만 실제로 없는 이미지 파일
    - 선택 기능(extras): PDF 빌드·웹 에디터·번역에 필요한 패키지 설치 여부

    \b
    코퍼스 점검은 원본 마크다운만 훑어 즉시 끝나고, 선택 기능 점검은
    Playwright Chromium을 잠깐 띄워봐야 해서 몇 초 더 걸린다.
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


@main.group(
    "build",
    epilog="""\b
예시:
  mdbook-binder build html <ROOT> [--out FILE] [--title ...] [--color ...]
  mdbook-binder build pdf  <ROOT> [--merge [이름]] [--out-dir ...]

옵션은 `build html --help` / `build pdf --help`로 확인.
"""
)
def build() -> None:
    """HTML 또는 PDF 도서를 빌드한다.

    \b
    두 하위 명령 모두 book.yaml과 3단계 순서 해석 규칙을 공유한다 —
    어느 쪽을 먼저 빌드하든 챕터 순서는 항상 같다. 순서가 미심쩍으면
    빌드 전에 `mdbook-binder check`로 먼저 확인한다.
    """


@build.command(
    "html",
    short_help="마크다운 코퍼스를 검색 가능한 단일 HTML 도서로 빌드한다.",
    epilog="""\b
예시:
  mdbook-binder build html ~/my-book
  mdbook-binder build html ~/my-book --title "나의 책" --language en
  mdbook-binder build html ~/my-book --out ./dist/book.html --color teal
"""
)
@click.argument("root", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--out", "out_path", type=click.Path(path_type=Path), default=None,
    help="출력 경로 (기본: ROOT/<제목 슬러그>.html)",
)
@click.option(
    "--title", "title_override", default=None,
    help="도서 제목 (기본: book.yaml title/디렉토리명)",
)
@click.option(
    "--language", "language_override", default=None,
    help="UI 로케일 (기본: book.yaml language 또는 ko)",
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

    \b
    인자:
      ROOT  마크다운 코퍼스 루트 디렉토리

    \b
    사이드바 목차·인페이지 전문 검색을 갖춘 단일 HTML 파일을 만든다
    (이미지는 base64로 인라인 임베드). Mermaid 다이어그램은 가능하면
    Playwright/Chromium으로 정적 SVG를 사전 렌더링해 같이 임베드하고,
    없으면 CDN mermaid.js로 폴백한다. 코드 하이라이트·웹폰트는 아직
    CDN 의존적이다.
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
    short_help="마크다운 코퍼스를 챕터별 PDF(또는 --merge 시 단권)로 빌드한다.",
    epilog="""\b
예시:
  mdbook-binder build pdf ~/my-book                # 챕터별 PDF
  mdbook-binder build pdf ~/my-book --merge         # 한 권으로 병합
  mdbook-binder build pdf ~/my-book --merge my_book --out-dir ./dist
"""
)
@click.argument("root", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--merge", "merge_out", is_flag=False, flag_value="full_book", default=None,
    metavar="[이름]",
    help="한 권으로 병합 (기본 full_book.pdf)",
)
@click.option(
    "--out-dir", "out_dir", type=click.Path(path_type=Path), default=None,
    help="출력 디렉토리 (기본: ROOT/pdf)",
)
@click.option("--color", "color_override", type=_COLOR_CHOICE, default=None, help=_COLOR_HELP)
def build_pdf_cmd(
    root: Path, merge_out: str | None, out_dir: Path | None, color_override: str | None
) -> None:
    """ROOT 아래 마크다운을 챕터별 PDF(또는 --merge 시 단권)로 빌드한다.

    \b
    인자:
      ROOT  마크다운 코퍼스 루트 디렉토리

    \b
    Playwright/Chromium으로 각 챕터를 렌더링한다 — `pip install
    "mdbook-binder[pdf]"` 후 `python -m playwright install chromium`
    설치가 먼저 필요하다. 긴 Mermaid 다이어그램은 페이지 경계에 맞춰
    청크 스크린샷으로 나눠 삽입해 도형이 잘리지 않게 한다.
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
  mdbook-binder edit my-book.html --no-browser   # URL 직접 열기
"""
)
@click.argument("html_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--port", "-p", type=int, default=5757, help="에디터 서버 포트 (기본: 5757)")
@click.option(
    "--out", "out_path", type=click.Path(path_type=Path), default=None,
    help="저장 경로 (기본: <stem>_edited.html, 원본 유지)",
)
@click.option("--no-browser", is_flag=True, default=False, help="브라우저 자동 오픈 생략")
def edit_cmd(html_path: Path, port: int, out_path: Path | None, no_browser: bool) -> None:
    """HTML_PATH를 브라우저 편집 UI로 연다.

    \b
    인자:
      HTML_PATH  build html로 만든 HTML 파일 경로

    \b
    build html이 만든 <section> 구조에만 의존한다. 섹션 단위
    마크다운 편집, 이미지 추가/교체/삭제, Mermaid 삭제, 이미지
    업로드·갤러리를 제공한다. `pip install "mdbook-binder[editor]"`
    (Flask/Pillow)가 필요하다.
    """
    from mdbook_binder.editor.server import run_editor

    run_editor(
        str(html_path),
        output_path=str(out_path) if out_path else None,
        port=port,
        open_browser=not no_browser,
    )


@main.command(
    "import",
    epilog="""\b
예시:
  mdbook-binder import ./book.pdf ./corpus-en
  mdbook-binder import ./book.pdf ./corpus-en --title "My Book"
  mdbook-binder import ./book.pdf ./corpus-en --no-images
"""
)
@click.argument("pdf_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("out_dir", type=click.Path(path_type=Path))
@click.option("--title", "title_override", default=None, help="제목/파일명 오버라이드 (기본: PDF 파일명)")
@click.option("--no-images", "no_images", is_flag=True, default=False, help="이미지 추출 없이 텍스트만 뽑는다")
def import_cmd(pdf_path: Path, out_dir: Path, title_override: str | None, no_images: bool) -> None:
    """PDF를 마크다운 코퍼스로 추출한다(지원 포맷은 PDF뿐 — docx 등은 PDF로 변환 후 사용).

    \b
    인자:
      PDF_PATH  추출할 PDF 경로
      OUT_DIR   마크다운 코퍼스를 생성할 디렉토리

    \b
    전체를 단일 파일로 추출한다(챕터 자동 분리는 아직 지원 안 함).
    이미지는 기본적으로 OUT_DIR/images/에 저장되고 본문 흐름의
    원래 위치에 삽입된다(--no-images로 끌 수 있음) — 너무 작은
    장식용 이미지와 반복되는 로고/아이콘은 자동으로 제외한다.
    결과는 그 자체로 build html/pdf나 translate가 바로 받는
    유효한 코퍼스다.
    """
    if pdf_path.suffix.lower() != ".pdf":
        raise click.ClickException(f"PDF 파일만 지원합니다 (받은 파일: {pdf_path.name}) — docx 등은 PDF로 변환 후 다시 시도하세요")

    from mdbook_binder.pdf_import import import_pdf

    print(f"\U0001f4c4 Extracting {pdf_path} ...")
    md_path = import_pdf(pdf_path, out_dir, title=title_override, extract_images=not no_images)
    print(f"✅ {md_path}")


@main.command(
    "translate",
    epilog="""\b
예시:
  mdbook-binder translate ~/book ~/book-ko --direction e2k
  mdbook-binder translate ~/book ~/book-ko --direction e2k --check-only
  mdbook-binder translate ~/book ~/book-en --direction k2e --model exaone3.5
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
    help="Ollama 모델 (기본: exaone3.5:7.8b 또는 book.yaml)",
)
@click.option(
    "--host", "host_override", default=None,
    help="Ollama 서버 (기본: localhost:11434 또는 book.yaml)",
)
@click.option("--timeout", "timeout_override", type=int, default=None, help="청크당 타임아웃(초) 오버라이드")
@click.option("--chunk-chars", "chunk_chars_override", type=int, default=None, help="청크 최대 글자수 오버라이드")
@click.option(
    "--check-only", is_flag=True, default=False,
    help="번역 없이 Ollama 연결·모델 설치 상태만 확인",
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

    \b
    인자:
      ROOT     번역할 마크다운 코퍼스 루트
      OUT_DIR  번역 결과를 미러링할 디렉토리

    \b
    기본 모델은 exaone3.5:7.8b다(--model로 오버라이드 가능). 코드·
    mermaid·raw-HTML 블록은 번역하지 않고 그대로 보존하고, book.yaml의
    language 필드는 방향에 맞춰(k2e→en, e2k→ko) 다시 쓴다. 청크 하나에도
    수십 초가 걸릴 수 있어 챕터·청크 단위 진행 상황을 출력한다. 모델이
    없어도 자동으로 pull하지 않는다 — `ollama pull <모델>`을 직접
    실행해야 한다.
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
