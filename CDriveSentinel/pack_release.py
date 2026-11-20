#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Pack a built onedir folder into a release ZIP and write its SHA-256.

Used by .github/workflows/release.yml both for the unsigned artifact and for
the artifact returned by SignPath. Keeping this in Python (instead of
Compress-Archive) preserves the UTF-8 filename 使用说明.txt and gives a stable
SHA-256 so the checksum is always generated AFTER signing.

Usage:
    python pack_release.py <source_dir> <output.zip>

Output:
    <output.zip>          the release archive
    <output.zip>.sha256   "<hex>  <filename>" line
"""
from __future__ import annotations

import hashlib
import sys
import zipfile
from pathlib import Path

# Do not ship transient runtime data if a signed/unsigned folder was ever run.
SKIP_DIRS = {"data", "reports", "__pycache__"}


def pack(src: Path, out: Path) -> None:
    if not src.is_dir():
        raise SystemExit("source directory not found: %s" % src)
    out.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(src.rglob("*")):
            rel = path.relative_to(src)
            if any(part in SKIP_DIRS for part in rel.parts[:-1]):
                continue
            if path.is_dir():
                continue
            zf.write(path, rel.as_posix())
            count += 1
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    # newline="" keeps a bare LF: on Windows the default text mode would turn
    # \n into \r\n, and sha256sum -c would then see a filename with a trailing CR.
    with out.with_suffix(out.suffix + ".sha256").open(
        "w", encoding="ascii", newline=""
    ) as fh:
        fh.write("%s  %s\n" % (digest, out.name))
    print("packed %d files -> %s" % (count, out))
    print("sha256 %s  %s" % (digest, out.name))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: pack_release.py <source_dir> <output.zip>")
    pack(Path(sys.argv[1]), Path(sys.argv[2]))
