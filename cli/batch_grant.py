"""Grant many people access to many documents in one shot.

Use this when you already know everyone's machine_fingerprint ahead of time
(e.g. IT collected them from managed laptops). If you don't have
fingerprints yet, prefer the self-service flow: users click "Request
access" in the viewer, then use cli.approve_pending instead.

users.csv columns: machine_fingerprint,label   (label is free text, e.g. an email)
manifest.csv (from cli.batch_encrypt) columns: source_pdf,title,doc_id,cpdf_path

Usage:
    python -m cli.batch_grant --manifest ./manifest.csv --users ./users.csv \\
        --expires-at 2026-12-31T23:59:59+00:00 \\
        --server http://localhost:8443 --admin-token $PDF_DRM_ADMIN_TOKEN

    # Grant only specific documents instead of the whole manifest:
    python -m cli.batch_grant --doc-id <id1> --doc-id <id2> --users ./users.csv \\
        --expires-at ... --server ... --admin-token ...

    # Grant every document in a group (see cli.manage_groups) instead of
    # picking documents one by one:
    python -m cli.batch_grant --group-id <id> --users ./users.csv \\
        --expires-at ... --server ... --admin-token ...
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import requests

CHUNK_SIZE = 500


def read_doc_ids(
    manifest: Path | None, explicit: list[str], group_id: str | None,
    server: str, admin_token: str,
) -> list[str]:
    doc_ids = list(explicit)
    if manifest:
        with manifest.open(newline="") as f:
            for row in csv.DictReader(f):
                doc_ids.append(row["doc_id"])
    if group_id:
        resp = requests.get(
            f"{server.rstrip('/')}/admin/groups/{group_id}",
            headers={"X-Admin-Token": admin_token},
            timeout=15,
        )
        resp.raise_for_status()
        doc_ids.extend(d["doc_id"] for d in resp.json()["documents"])
    doc_ids = list(dict.fromkeys(doc_ids))  # dedupe, keep order
    if not doc_ids:
        raise SystemExit("No documents specified: pass --manifest, --doc-id, and/or --group-id")
    return doc_ids


def read_users(users_csv: Path) -> list[dict]:
    with users_csv.open(newline="") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        if not row.get("machine_fingerprint"):
            raise SystemExit(f"Missing machine_fingerprint in row: {row}")
    return rows


def chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--doc-id", action="append", default=[], dest="doc_ids")
    parser.add_argument("--group-id", default=None)
    parser.add_argument("--users", type=Path, required=True)
    parser.add_argument("--expires-at", required=True)
    parser.add_argument("--server", required=True)
    parser.add_argument("--admin-token", required=True)
    args = parser.parse_args()

    doc_ids = read_doc_ids(args.manifest, args.doc_ids, args.group_id, args.server, args.admin_token)
    users = read_users(args.users)

    grants = [
        {
            "doc_id": doc_id,
            "machine_fingerprint": user["machine_fingerprint"],
            "expires_at": args.expires_at,
            "label": user.get("label"),
        }
        for doc_id in doc_ids
        for user in users
    ]

    print(f"Granting {len(users)} user(s) x {len(doc_ids)} document(s) = {len(grants)} licenses...")

    total_granted = 0
    total_skipped: list[dict] = []
    for chunk in chunked(grants, CHUNK_SIZE):
        resp = requests.post(
            f"{args.server.rstrip('/')}/admin/licenses/bulk",
            json={"grants": chunk},
            headers={"X-Admin-Token": args.admin_token},
            timeout=60,
        )
        resp.raise_for_status()
        result = resp.json()
        total_granted += result["granted"]
        total_skipped.extend(result["skipped"])

    print(f"Granted: {total_granted}")
    if total_skipped:
        print(f"Skipped (unknown doc_id): {len(total_skipped)}")
        for s in total_skipped[:10]:
            print(f"  {s}")


if __name__ == "__main__":
    main()
