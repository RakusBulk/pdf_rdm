"""Grant, revoke, and inspect per-machine licenses on the license server.

Examples:
    python -m cli.manage_license fingerprint
    python -m cli.manage_license grant --doc-id <id> --machine-fingerprint <fp> \\
        --expires-at 2026-12-31T23:59:59+00:00 --server http://localhost:8443 \\
        --admin-token $PDF_DRM_ADMIN_TOKEN
    python -m cli.manage_license revoke --doc-id <id> --machine-fingerprint <fp> ...
    python -m cli.manage_license list --doc-id <id> ...
    python -m cli.manage_license log --doc-id <id> ...
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.machine_id import get_machine_fingerprint  # noqa: E402


def cmd_fingerprint(_args: argparse.Namespace) -> None:
    print(get_machine_fingerprint())


def cmd_grant(args: argparse.Namespace) -> None:
    resp = requests.post(
        f"{args.server.rstrip('/')}/admin/licenses",
        json={
            "doc_id": args.doc_id,
            "machine_fingerprint": args.machine_fingerprint,
            "expires_at": args.expires_at,
            "label": args.label,
        },
        headers={"X-Admin-Token": args.admin_token},
        timeout=15,
    )
    resp.raise_for_status()
    print("Granted.")


def cmd_revoke(args: argparse.Namespace) -> None:
    resp = requests.post(
        f"{args.server.rstrip('/')}/admin/licenses/revoke",
        json={"doc_id": args.doc_id, "machine_fingerprint": args.machine_fingerprint},
        headers={"X-Admin-Token": args.admin_token},
        timeout=15,
    )
    resp.raise_for_status()
    print("Revoked.")


def cmd_list(args: argparse.Namespace) -> None:
    resp = requests.get(
        f"{args.server.rstrip('/')}/admin/licenses",
        params={"doc_id": args.doc_id} if args.doc_id else {},
        headers={"X-Admin-Token": args.admin_token},
        timeout=15,
    )
    resp.raise_for_status()
    print(json.dumps(resp.json(), indent=2))


def cmd_log(args: argparse.Namespace) -> None:
    resp = requests.get(
        f"{args.server.rstrip('/')}/admin/access_log",
        params={"doc_id": args.doc_id} if args.doc_id else {},
        headers={"X-Admin-Token": args.admin_token},
        timeout=15,
    )
    resp.raise_for_status()
    print(json.dumps(resp.json(), indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(required=True)

    p_fp = sub.add_parser("fingerprint", help="Print this machine's fingerprint")
    p_fp.set_defaults(func=cmd_fingerprint)

    common_admin = dict(required=True)

    p_grant = sub.add_parser("grant", help="Grant (or renew) a license")
    p_grant.add_argument("--doc-id", required=True)
    p_grant.add_argument("--machine-fingerprint", required=True)
    p_grant.add_argument("--expires-at", required=True, help="ISO-8601 datetime")
    p_grant.add_argument("--label", default=None)
    p_grant.add_argument("--server", **common_admin)
    p_grant.add_argument("--admin-token", **common_admin)
    p_grant.set_defaults(func=cmd_grant)

    p_revoke = sub.add_parser("revoke", help="Revoke a license")
    p_revoke.add_argument("--doc-id", required=True)
    p_revoke.add_argument("--machine-fingerprint", required=True)
    p_revoke.add_argument("--server", **common_admin)
    p_revoke.add_argument("--admin-token", **common_admin)
    p_revoke.set_defaults(func=cmd_revoke)

    p_list = sub.add_parser("list", help="List licenses")
    p_list.add_argument("--doc-id", default=None)
    p_list.add_argument("--server", **common_admin)
    p_list.add_argument("--admin-token", **common_admin)
    p_list.set_defaults(func=cmd_list)

    p_log = sub.add_parser("log", help="Show access log")
    p_log.add_argument("--doc-id", default=None)
    p_log.add_argument("--server", **common_admin)
    p_log.add_argument("--admin-token", **common_admin)
    p_log.set_defaults(func=cmd_log)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
