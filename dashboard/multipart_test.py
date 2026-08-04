#!/usr/bin/env python3
"""Task 377 #4: tests for the multipart/form-data parser (multipart.py).
Self-contained (no pytest).  Run:  python multipart_test.py
"""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import multipart  # noqa: E402

B = "----WebKitFormBoundaryABC123"


def _wire(parts):
    """Assemble a multipart body (bytes) from (headers, content-bytes) parts."""
    out = b""
    for head, content in parts:
        out += b"--" + B.encode() + b"\r\n" + head + b"\r\n\r\n" + content + b"\r\n"
    out += b"--" + B.encode() + b"--\r\n"
    return out


def test_boundary_of():
    assert multipart.boundary_of("multipart/form-data; boundary=" + B) == B
    assert multipart.boundary_of('multipart/form-data; boundary="' + B + '"') == B
    assert multipart.boundary_of("application/json") is None
    assert multipart.boundary_of(None) is None


def test_fields_and_files():
    body = _wire([
        (b'Content-Disposition: form-data; name="precinct"', b"infra"),
        (b'Content-Disposition: form-data; name="model"', b"sonnet"),
        (b'Content-Disposition: form-data; name="description"', b"multi\nline\ndesc"),
        (b'Content-Disposition: form-data; name="upload"; filename="a.png"\r\n'
         b'Content-Type: image/png', b"\x89PNG\r\n\x1a\nbinary\x00bytes"),
    ])
    fields, files = multipart.parse(body, B)
    assert fields["precinct"] == "infra", fields
    assert fields["model"] == "sonnet"
    assert fields["description"] == "multi\nline\ndesc"
    assert len(files) == 1, files
    assert files[0]["filename"] == "a.png"
    assert files[0]["content_type"] == "image/png"
    assert files[0]["content"] == b"\x89PNG\r\n\x1a\nbinary\x00bytes", "binary content corrupted"


def test_multiple_files():
    body = _wire([
        (b'Content-Disposition: form-data; name="description"', b"two files"),
        (b'Content-Disposition: form-data; name="f"; filename="a.txt"', b"AAA"),
        (b'Content-Disposition: form-data; name="f"; filename="b.txt"', b"BBB"),
    ])
    fields, files = multipart.parse(body, B)
    assert fields["description"] == "two files"
    assert [f["filename"] for f in files] == ["a.txt", "b.txt"], files
    assert [f["content"] for f in files] == [b"AAA", b"BBB"]


def test_empty_filename_is_a_field_not_a_file():
    # an unfilled <input type=file> sends filename="" -> must NOT count as a file
    body = _wire([
        (b'Content-Disposition: form-data; name="upload"; filename=""\r\n'
         b'Content-Type: application/octet-stream', b""),
        (b'Content-Disposition: form-data; name="description"', b"no upload"),
    ])
    fields, files = multipart.parse(body, B)
    assert files == [], files
    assert fields["description"] == "no upload"


def test_malformed_body_is_safe():
    assert multipart.parse(b"", B) == ({}, [])
    assert multipart.parse(b"garbage-no-boundary", B) == ({}, [])
    assert multipart.parse(b"something", "") == ({}, [])


def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    print(f"=== multipart parser test suite  ({len(tests)} tests) ===")
    passed, failed = 0, []
    for t in tests:
        try:
            t()
        except Exception:
            failed.append(t.__name__)
            print(f"[FAIL] {t.__name__}")
            traceback.print_exc()
        else:
            passed += 1
            print(f"[PASS] {t.__name__}")
    print(f"=== SUMMARY: {passed} passed, {len(failed)} failed ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
