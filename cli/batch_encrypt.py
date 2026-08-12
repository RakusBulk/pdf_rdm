"""Encrypt every PDF in a folder in one run and write a manifest.

Each input file is encrypted exactly once. Run this once per set of PDFs --
never re-run it on files you've already granted licenses for, since it
creates a brand new doc_id (and therefore a new key) each time, orphaning
any licenses already granted against the old doc_id.

Usage:
    python -m cli.batch_encrypt ./pdfs_to_share --out-dir ./encrypted \\
        --manifest ./manifest.csv \\
        --server http://localhost:8443 --admin-token $PDF_DRM_ADMIN_TOKEN
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import crypto  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input_dir", type=Path, help="Folder containing .pdf files")
    parser.add_argument("--out-dir", type=Path, required=True, help="Where to write .cpdf files")
    parser.add_argument("--manifest", type=Path, required=True, help="CSV manifest to write (or append to)")
    parser.add_argument("--server", required=True)
    parser.add_argument("--admin-token", required=True)
    parser.add_argument("--recursive", action="store_true")
    args = parser.parse_args()

    pdfs = sorted(
        args.input_dir.rglob("*.pdf") if args.recursive else args.input_dir.glob("*.pdf")
    )
    if not pdfs:
        print(f"No .pdf files found in {args.input_dir}")
        return

    args.out_dir.mkdir(parents=True, exist_ok=True)
    manifest_exists = args.manifest.exists()

    with args.manifest.open("a", newline="") as f:
        writer = csv.writer(f)
        if not manifest_exists:
            writer.writerow(["source_pdf", "title", "doc_id", "cpdf_path"])

        for i, pdf_path in enumerate(pdfs, 1):
            title = pdf_path.stem
            pdf_bytes = pdf_path.read_bytes()
            key = crypto.generate_key()
            doc_id, blob = crypto.encrypt_pdf(pdf_bytes, key)

            out_path = args.out_dir / f"{pdf_path.stem}.cpdf"
            out_path.write_bytes(blob)

            resp = requests.post(
                f"{args.server.rstrip('/')}/admin/documents",
                json={"doc_id": doc_id, "title": title, "key_hex": key.hex()},
                headers={"X-Admin-Token": args.admin_token},
                timeout=15,
            )
            resp.raise_for_status()

            writer.writerow([str(pdf_path), title, doc_id, str(out_path)])
            f.flush()
            print(f"[{i}/{len(pdfs)}] {pdf_path.name} -> {out_path.name}  doc_id={doc_id}")

    print(f"\nDone. Manifest: {args.manifest}")
    print("Distribute the files in", args.out_dir, "to all 100 people as-is (same files for everyone).")
    print("Next: collect access requests and run cli.approve_pending, or use cli.batch_grant with known fingerprints.")


if __name__ == "__main__":
    main()
