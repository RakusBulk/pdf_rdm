"""Create and manage document groups -- named, reusable sets of doc_ids so
you can grant access to "all 100 reports" in one shot instead of picking
each document individually.

Examples:
    python -m cli.manage_groups create --name "Bao cao Q3" \\
        --manifest ./manifest.csv --server ... --admin-token ...

    python -m cli.manage_groups list --server ... --admin-token ...
    python -m cli.manage_groups show --group-id <id> --server ... --admin-token ...
    python -m cli.manage_groups add-docs --group-id <id> --manifest ./manifest.csv --server ... --admin-token ...
    python -m cli.manage_groups remove-docs --group-id <id> --doc-id <id1> --doc-id <id2> --server ... --admin-token ...
    python -m cli.manage_groups delete --group-id <id> --server ... --admin-token ...

    # Grant every document in a group to one machine in a single call:
    python -m cli.manage_groups grant --group-id <id> --machine-fingerprint <fp> \\
        --expires-at 2026-12-31T23:59:59+00:00 --server ... --admin-token ...
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import requests


def read_doc_ids(manifest: Path | None, explicit: list[str]) -> list[str]:
    doc_ids = list(explicit)
    if manifest:
        with manifest.open(newline="") as f:
            for row in csv.DictReader(f):
                doc_ids.append(row["doc_id"])
    return doc_ids


def cmd_create(args: argparse.Namespace) -> None:
    doc_ids = read_doc_ids(args.manifest, args.doc_ids)
    resp = requests.post(
        f"{args.server.rstrip('/')}/admin/groups",
        json={"name": args.name, "doc_ids": doc_ids},
        headers={"X-Admin-Token": args.admin_token},
        timeout=30,
    )
    resp.raise_for_status()
    print(json.dumps(resp.json(), indent=2))


def cmd_list(args: argparse.Namespace) -> None:
    resp = requests.get(
        f"{args.server.rstrip('/')}/admin/groups",
        headers={"X-Admin-Token": args.admin_token},
        timeout=15,
    )
    resp.raise_for_status()
    print(json.dumps(resp.json(), indent=2))


def cmd_show(args: argparse.Namespace) -> None:
    resp = requests.get(
        f"{args.server.rstrip('/')}/admin/groups/{args.group_id}",
        headers={"X-Admin-Token": args.admin_token},
        timeout=15,
    )
    resp.raise_for_status()
    print(json.dumps(resp.json(), indent=2))


def cmd_add_docs(args: argparse.Namespace) -> None:
    doc_ids = read_doc_ids(args.manifest, args.doc_ids)
    resp = requests.post(
        f"{args.server.rstrip('/')}/admin/groups/{args.group_id}/documents",
        json={"doc_ids": doc_ids},
        headers={"X-Admin-Token": args.admin_token},
        timeout=30,
    )
    resp.raise_for_status()
    print(json.dumps(resp.json(), indent=2))


def cmd_remove_docs(args: argparse.Namespace) -> None:
    resp = requests.post(
        f"{args.server.rstrip('/')}/admin/groups/{args.group_id}/documents/remove",
        json={"doc_ids": args.doc_ids},
        headers={"X-Admin-Token": args.admin_token},
        timeout=30,
    )
    resp.raise_for_status()
    print("Removed.")


def cmd_delete(args: argparse.Namespace) -> None:
    resp = requests.delete(
        f"{args.server.rstrip('/')}/admin/groups/{args.group_id}",
        headers={"X-Admin-Token": args.admin_token},
        timeout=15,
    )
    resp.raise_for_status()
    print("Deleted.")


def cmd_grant(args: argparse.Namespace) -> None:
    resp = requests.post(
        f"{args.server.rstrip('/')}/admin/licenses/grant_by_group",
        json={
            "group_id": args.group_id,
            "machine_fingerprint": args.machine_fingerprint,
            "expires_at": args.expires_at,
            "label": args.label,
        },
        headers={"X-Admin-Token": args.admin_token},
        timeout=30,
    )
    resp.raise_for_status()
    result = resp.json()
    print(f"Granted {len(result['granted'])} document(s) to {args.machine_fingerprint[:12]}...")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(required=True)

    p_create = sub.add_parser("create")
    p_create.add_argument("--name", required=True)
    p_create.add_argument("--manifest", type=Path, default=None)
    p_create.add_argument("--doc-id", action="append", default=[], dest="doc_ids")
    p_create.add_argument("--server", required=True)
    p_create.add_argument("--admin-token", required=True)
    p_create.set_defaults(func=cmd_create)

    p_list = sub.add_parser("list")
    p_list.add_argument("--server", required=True)
    p_list.add_argument("--admin-token", required=True)
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show")
    p_show.add_argument("--group-id", required=True)
    p_show.add_argument("--server", required=True)
    p_show.add_argument("--admin-token", required=True)
    p_show.set_defaults(func=cmd_show)

    p_add = sub.add_parser("add-docs")
    p_add.add_argument("--group-id", required=True)
    p_add.add_argument("--manifest", type=Path, default=None)
    p_add.add_argument("--doc-id", action="append", default=[], dest="doc_ids")
    p_add.add_argument("--server", required=True)
    p_add.add_argument("--admin-token", required=True)
    p_add.set_defaults(func=cmd_add_docs)

    p_remove = sub.add_parser("remove-docs")
    p_remove.add_argument("--group-id", required=True)
    p_remove.add_argument("--doc-id", action="append", required=True, dest="doc_ids")
    p_remove.add_argument("--server", required=True)
    p_remove.add_argument("--admin-token", required=True)
    p_remove.set_defaults(func=cmd_remove_docs)

    p_delete = sub.add_parser("delete")
    p_delete.add_argument("--group-id", required=True)
    p_delete.add_argument("--server", required=True)
    p_delete.add_argument("--admin-token", required=True)
    p_delete.set_defaults(func=cmd_delete)

    p_grant = sub.add_parser("grant")
    p_grant.add_argument("--group-id", required=True)
    p_grant.add_argument("--machine-fingerprint", required=True)
    p_grant.add_argument("--expires-at", required=True)
    p_grant.add_argument("--label", default=None)
    p_grant.add_argument("--server", required=True)
    p_grant.add_argument("--admin-token", required=True)
    p_grant.set_defaults(func=cmd_grant)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
