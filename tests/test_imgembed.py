"""imgembed.py의 base64 data URI 인코딩 회귀 테스트.

html_book.py(최초 빌드)와 editor/(이미지 추가·교체)가 공유하는 유틸이라,
MIME 판정이 어긋나면 두 서브시스템 모두에 조용히 영향을 준다.
"""

import base64
from pathlib import Path

from mdbook_binder.imgembed import image_to_data_uri

_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415408d763f8cfc0c0c00000030101006bec38ba0000000049454e44ae426082"
)


def test_png_gets_correct_mime_and_roundtrips(tmp_path: Path):
    path = tmp_path / "pic.png"
    path.write_bytes(_PNG_BYTES)

    uri = image_to_data_uri(path)

    assert uri.startswith("data:image/png;base64,")
    encoded = uri.split(",", 1)[1]
    assert base64.b64decode(encoded) == _PNG_BYTES


def test_svg_uses_mime_override(tmp_path: Path):
    """mimetypes.guess_type()은 플랫폼/버전에 따라 .svg를 못 알아볼 수 있어
    _MIME_OVERRIDES로 명시적으로 고정한다 — 이 오버라이드가 실제로
    적용되는지는 직접 검증해야 한다."""
    path = tmp_path / "diagram.svg"
    path.write_text("<svg></svg>", encoding="utf-8")

    uri = image_to_data_uri(path)

    assert uri.startswith("data:image/svg+xml;base64,")


def test_unknown_extension_falls_back_to_octet_stream(tmp_path: Path):
    path = tmp_path / "mystery.bin"
    path.write_bytes(b"\x00\x01\x02")

    uri = image_to_data_uri(path)

    assert uri.startswith("data:application/octet-stream;base64,")
