"""Minimal multipart/form-data parser (Task 377 #4).

The stdlib HTTP server has no multipart parser and `cgi` is deprecated/removed, so
this is a small, dependency-free parser for the authed "Create new case" upload:
it takes the request body bytes + the boundary and returns (fields, files).

  fields : {name: str_value}          (non-file form fields, utf-8 decoded)
  files  : [{name, filename, content_type, content(bytes)}]

Robustness notes: boundaries are chosen by the client to not occur in the content,
so a bytes-split on the delimiter is safe for this trusted, authed, single-user
form. A malformed part is skipped rather than raising. Size enforcement is the
caller's job (Content-Length cap before reading; per-file cap after parsing).
"""
import re


def boundary_of(content_type):
    """Extract the boundary token from a Content-Type header, or None."""
    if not content_type:
        return None
    m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type, re.I)
    if not m:
        return None
    return (m.group(1) or m.group(2) or "").strip()


def _header_map(head_bytes):
    headers = {}
    for line in head_bytes.split(b"\r\n"):
        if b":" in line:
            k, v = line.split(b":", 1)
            headers[k.strip().lower().decode("latin-1")] = v.strip().decode("latin-1")
    return headers


def parse(body, boundary):
    """Parse a multipart/form-data ``body`` (bytes) with ``boundary`` (str).
    Returns (fields:dict, files:list). Never raises on a malformed part."""
    fields, files = {}, []
    if not body or not boundary:
        return fields, files
    delim = b"--" + boundary.encode("latin-1")
    segments = body.split(delim)
    # segments[0] is the preamble (usually empty); the last is the closing "--\r\n".
    for seg in segments[1:-1]:
        if seg.startswith(b"\r\n"):
            seg = seg[2:]
        if seg.endswith(b"\r\n"):
            seg = seg[:-2]
        head, sep, content = seg.partition(b"\r\n\r\n")
        if not sep:
            continue
        headers = _header_map(head)
        disp = headers.get("content-disposition", "")
        mname = re.search(r'name="([^"]*)"', disp)
        if not mname:
            continue
        name = mname.group(1)
        mfile = re.search(r'filename="([^"]*)"', disp)
        if mfile and mfile.group(1):
            files.append({
                "name": name,
                "filename": mfile.group(1),
                "content_type": headers.get("content-type", ""),
                "content": content,
            })
        else:
            fields[name] = content.decode("utf-8", "replace")
    return fields, files
