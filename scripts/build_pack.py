#!/usr/bin/env python3
"""Assemble a combined application document pack (stdlib + poppler/ghostscript).

Some postings (CSIT on Lever, and similar) mandate **one** PDF combining the
tailored resume, all education certificates, the latest university transcript,
and the student status letter. This script merges exactly those documents, in a
fixed order, into a single PDF.

Fixed order:

  1. the merged variant ``resume.pdf``  (path passed in via ``--resume``)
  2. ``alevel-cert.pdf``                (GCE A-Level certificate)
  3. ``transcript.pdf``                 (unofficial university transcript)
  4. ``student-status-letter.pdf``      (student status letter)

Missing files are skipped and reported; the run still succeeds as long as at
least one document is present. The document list is fixed in ``INBOX_DOCUMENTS``: add or
remove entry names there to match your own pack. ``nric.jpg`` is never part of a
pack -- it is not in the list above, and the script refuses image/NRIC inputs
defensively.

The script prints **only a manifest** of what it merged -- file names, per-file
and total page counts, and the output path. It never prints document content.

Usage:
    python3 build_pack.py --resume repo/variants/<slug>/resume.pdf \
        [--slug <slug>] [--inbox DIR] [--out PATH] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_INBOX = os.path.join(BASE, "inbox")
DEFAULT_OUTBOX = os.path.join(BASE, "outbox")

# (filename in the inbox, human label) in the mandated merge order, after the
# resume. Kept in code so a pack is always assembled the same way.
INBOX_DOCUMENTS = [
    ("alevel-cert.pdf", "A-Level certificate"),
    ("transcript.pdf", "unofficial university transcript"),
    ("student-status-letter.pdf", "Student status letter"),
]

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff")


class PackError(Exception):
    """A pack could not be assembled (nothing merged, or a merge tool failed)."""


def _run(cmd):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _is_pdf(path):
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == b"%PDF"
    except OSError:
        return False


def pdf_pages(path):
    """Page count from ``pdfinfo`` (int), or None when it cannot be read."""
    res = _run(["pdfinfo", path])
    if res.returncode != 0:
        return None
    for line in res.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("Pages:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def _merge_pdfunite(files, out):
    return _run(["pdfunite", *files, out])


def _merge_gs(files, out):
    cmd = [
        "gs", "-q", "-dNOPAUSE", "-dBATCH", "-sDEVICE=pdfwrite",
        "-sOutputFile=" + out, *files,
    ]
    return _run(cmd)


def merge(files, out):
    """Merge ``files`` (in order) into ``out``.

    Tries ``pdfunite`` first, then falls back to ``gs`` -- the inbox PDFs are
    owner-encrypted (AES, print-only) and pdfunite refuses encrypted inputs,
    while gs decrypts and rewrites them.
    """
    if len(files) == 1:
        shutil.copyfile(files[0], out)
        return "copy"
    errors = []
    if shutil.which("pdfunite"):
        res = _merge_pdfunite(files, out)
        if res.returncode == 0 and os.path.exists(out):
            return "pdfunite"
        errors.append("pdfunite: " + res.stderr.decode("utf-8", "replace").strip())
    if shutil.which("gs"):
        res = _merge_gs(files, out)
        if res.returncode == 0 and os.path.exists(out):
            return "gs"
        errors.append("gs: " + res.stderr.decode("utf-8", "replace").strip())
    if errors:
        raise PackError("no merge tool succeeded -- " + " | ".join(errors))
    raise PackError("neither pdfunite nor gs is available")


def _refused(path):
    """True when a path must never enter a pack (image / NRIC)."""
    name = os.path.basename(path).lower()
    return name.endswith(IMAGE_EXTS) or "nric" in name


def collect(resume, inbox):
    """Return (included, skipped).

    ``included`` items are (path, label, role); ``skipped`` items are
    (name, reason).
    """
    included = []
    skipped = []

    if not resume:
        skipped.append(("resume.pdf", "no --resume path given"))
    elif _refused(resume):
        skipped.append((os.path.basename(resume), "refused (image/NRIC)"))
    elif not os.path.exists(resume):
        skipped.append((os.path.basename(resume), "missing"))
    elif not _is_pdf(resume):
        skipped.append((os.path.basename(resume), "not a PDF"))
    else:
        included.append((resume, "tailored resume (merged variant)", "resume"))

    for name, label in INBOX_DOCUMENTS:
        path = os.path.join(inbox, name)
        if _refused(path):
            skipped.append((name, "refused (image/NRIC)"))
        elif not os.path.exists(path):
            skipped.append((name, "missing"))
        elif not _is_pdf(path):
            skipped.append((name, "not a PDF"))
        else:
            included.append((path, label, "attachment"))

    return included, skipped


def build(resume, inbox, out, slug):
    included, skipped = collect(resume, inbox)
    if not any(role == "resume" for _, _, role in included):
        why = next((r for n, r in skipped if os.path.basename(resume or "") == n),
                   "missing")
        raise PackError("resume not usable (%s); refusing to build a pack "
                        "without the tailored resume" % why)
    if not included:
        raise PackError("nothing to merge: no usable documents found")

    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    tool = merge([p for p, _, _ in included], out)

    if not os.path.exists(out) or not _is_pdf(out):
        raise PackError("merge produced no usable PDF at " + out)

    files = [
        {"path": p, "label": label, "role": role, "pages": pdf_pages(p)}
        for p, label, role in included
    ]
    manifest = {
        "slug": slug,
        "out": os.path.abspath(out),
        "tool": tool,
        "pages": pdf_pages(out),
        "included": files,
        "skipped": [{"name": n, "reason": r} for n, r in skipped],
    }
    return manifest


def print_manifest(manifest):
    lines = ["pack: " + manifest["out"], "pages: %s" % manifest["pages"]]
    lines.append("included:")
    for f in manifest["included"]:
        pages = f["pages"] if f["pages"] is not None else "?"
        lines.append("  - %s (%s pp) [%s] -- %s" % (f["path"], pages, f["role"], f["label"]))
    if manifest["skipped"]:
        lines.append("skipped:")
        for s in manifest["skipped"]:
            lines.append("  - %s (%s)" % (s["name"], s["reason"]))
    else:
        lines.append("skipped: none")
    print("\n".join(lines))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Merge resume + education certificates + transcript + "
                    "student status letter into one application PDF.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--resume", required=True,
                    help="path to the merged variant resume.pdf")
    ap.add_argument("--slug", help="posting slug (default: the resume's parent dir)")
    ap.add_argument("--inbox", default=DEFAULT_INBOX,
                    help="directory holding the certificates/transcript/letter "
                         "(default: ./inbox)")
    ap.add_argument("--out", help="output PDF path "
                    "(default: outbox/<slug>-pack.pdf)")
    ap.add_argument("--json", action="store_true", help="emit the manifest as JSON")
    args = ap.parse_args(argv)

    slug = args.slug or os.path.basename(
        os.path.dirname(os.path.abspath(args.resume))) or "pack"
    out = args.out or os.path.join(DEFAULT_OUTBOX, slug + "-pack.pdf")

    try:
        manifest = build(args.resume, args.inbox, out, slug)
    except PackError as exc:
        print("build_pack: error: %s" % exc, file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(manifest, indent=2, sort_keys=True))
    else:
        print_manifest(manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
