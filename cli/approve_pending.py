"""Review and approve self-service access requests.

Users click "Request access" in the viewer (enters their email); that POSTs
their machine_fingerprint to the server without needing you to manually copy
it over email/chat. This tool lists those requests and approves/rejects them.

Usage:
    python -m cli.approve_pending list --server ... --admin-token ...

    python -m cli.approve_pending approve --request-id 7 \\
        --manifest ./manifest.csv --expires-at 2026-12-31T23:59:59+00:00 \\
        --server ... --admin-token ...

    # Approve every pending request for a fixed set of documents at once
    # (e.g. run this on a schedule for a self-serve "anyone can request" flow):
    python -m cli.approve_pending approve-all \\
        --manifest ./manifest.csv --expires-at 2026-12-31T23:59:59+00:00 \\
        --only-domain example.com \\
        --server ... --admin-token ...

    python -m cli.approve_pending reject --request-id 8 --server ... --admin-token ...
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import requests


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


def cmd_list(args: argparse.Namespace) -> None:
    resp = requests.get(
        f"{args.server.rstrip('/')}/admin/pending",
        params={"status": args.status},
        headers={"X-Admin-Token": args.admin_token},
        timeout=15,
    )
    resp.raise_for_status()
    print(json.dumps(resp.json(), indent=2))


def cmd_approve(args: argparse.Namespace) -> None:
    doc_ids = read_doc_ids(args.manifest, args.doc_ids, args.group_id, args.server, args.admin_token)
    resp = requests.post(
        f"{args.server.rstrip('/')}/admin/pending/approve",
        json={
            "request_id": args.request_id,
            "doc_ids": doc_ids,
            "expires_at": args.expires_at,
            "label": args.label,
        },
        headers={"X-Admin-Token": args.admin_token},
        timeout=30,
    )
    resp.raise_for_status()
    print(json.dumps(resp.json(), indent=2))


def cmd_approve_all(args: argparse.Namespace) -> None:
    doc_ids = read_doc_ids(args.manifest, args.doc_ids, args.group_id, args.server, args.admin_token)

    resp = requests.get(
        f"{args.server.rstrip('/')}/admin/pending",
        params={"status": "pending"},
        headers={"X-Admin-Token": args.admin_token},
        timeout=15,
    )
    resp.raise_for_status()
    pending = resp.json()

    if args.only_domain:
        pending = [p for p in pending if p["email"].lower().endswith("@" + args.only_domain.lower())]

    if not pending:
        print("No matching pending requests.")
        return

    print(f"Approving {len(pending)} request(s) for {len(doc_ids)} document(s) each...")
    for req in pending:
        resp = requests.post(
            f"{args.server.rstrip('/')}/admin/pending/approve",
            json={
                "request_id": req["id"],
                "doc_ids": doc_ids,
                "expires_at": args.expires_at,
            },
            headers={"X-Admin-Token": args.admin_token},
            timeout=30,
        )
        resp.raise_for_status()
        who = req.get("username") or req["email"]
        print(f"  #{req['id']} ({who}) -> granted {len(resp.json()['granted'])} doc(s)")


def cmd_reject(args: argparse.Namespace) -> None:
    resp = requests.post(
        f"{args.server.rstrip('/')}/admin/pending/reject",
        json={"request_id": args.request_id},
        headers={"X-Admin-Token": args.admin_token},
        timeout=15,
    )
    resp.raise_for_status()
    print("Rejected.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(required=True)

    p_list = sub.add_parser("list")
    p_list.add_argument("--status", default="pending", choices=["pending", "approved", "rejected"])
    p_list.add_argument("--server", required=True)
    p_list.add_argument("--admin-token", required=True)
    p_list.set_defaults(func=cmd_list)

    p_approve = sub.add_parser("approve")
    p_approve.add_argument("--request-id", type=int, required=True)
    p_approve.add_argument("--manifest", type=Path, default=None)
    p_approve.add_argument("--doc-id", action="append", default=[], dest="doc_ids")
    p_approve.add_argument("--group-id", default=None)
    p_approve.add_argument("--expires-at", required=True)
    p_approve.add_argument("--label", default=None)
    p_approve.add_argument("--server", required=True)
    p_approve.add_argument("--admin-token", required=True)
    p_approve.set_defaults(func=cmd_approve)

    p_approve_all = sub.add_parser("approve-all")
    p_approve_all.add_argument("--manifest", type=Path, default=None)
    p_approve_all.add_argument("--doc-id", action="append", default=[], dest="doc_ids")
    p_approve_all.add_argument("--group-id", default=None)
    p_approve_all.add_argument("--expires-at", required=True)
    p_approve_all.add_argument("--only-domain", default=None, help="Only approve requests whose email ends with @<domain>")
    p_approve_all.add_argument("--server", required=True)
    p_approve_all.add_argument("--admin-token", required=True)
    p_approve_all.set_defaults(func=cmd_approve_all)

    p_reject = sub.add_parser("reject")
    p_reject.add_argument("--request-id", type=int, required=True)
    p_reject.add_argument("--server", required=True)
    p_reject.add_argument("--admin-token", required=True)
    p_reject.set_defaults(func=cmd_reject)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
