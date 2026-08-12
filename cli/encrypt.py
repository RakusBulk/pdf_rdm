"""Encrypt a PDF into a .cpdf file and register its key with the license server.

Usage:
    python -m cli.encrypt report.pdf --title "Q3 Report" \\
        --server http://localhost:8443 --admin-token $PDF_DRM_ADMIN_TOKEN \\
        --out report.cpdf
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import crypto  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_pdf", type=Path)
    parser.add_argument("--title", required=True, help="Human-readable document title")
    parser.add_argument("--server", required=True, help="License server base URL")
    parser.add_argument("--admin-token", required=True)
    parser.add_argument("--out", type=Path, default=None, help="Output .cpdf path")
    args = parser.parse_args()

    pdf_bytes = args.input_pdf.read_bytes()

    key = crypto.generate_key()
    doc_id, blob = crypto.encrypt_pdf(pdf_bytes, key)

    out_path = args.out or args.input_pdf.with_suffix(".cpdf")
    out_path.write_bytes(blob)

    resp = requests.post(
        f"{args.server.rstrip('/')}/admin/documents",
        json={"doc_id": doc_id, "title": args.title, "key_hex": key.hex()},
        headers={"X-Admin-Token": args.admin_token},
        timeout=15,
    )
    resp.raise_for_status()

    print(f"Encrypted -> {out_path}")
    print(f"doc_id    = {doc_id}")
    print("Grant machines access with:")
    print(
        f"  python -m cli.manage_license grant --doc-id {doc_id} "
        f"--machine-fingerprint <fp> --expires-at 2026-12-31T23:59:59+00:00 "
        f"--server {args.server} --admin-token ***"
    )


if __name__ == "__main__":
    main()
