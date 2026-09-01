"""CLI for auditable Walk-Forward input assets.

Examples:
    python -m foundf_db.walk_forward_input_cli readiness
    python -m foundf_db.walk_forward_input_cli archive /path/to/manifest.json
    python -m foundf_db.walk_forward_input_cli verify wfi_...

The ``approve-basis`` command records only a human review of total-return price
basis.  It never approves a strategy, production weight change, or order.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .walk_forward_input_store import WalkForwardInputStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage immutable Walk-Forward input evidence"
    )
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--db-path")
    subcommands = parser.add_subparsers(dest="command", required=True)

    archive = subcommands.add_parser("archive")
    archive.add_argument("manifest", type=Path)

    verify = subcommands.add_parser("verify")
    verify.add_argument("bundle_id")

    readiness = subcommands.add_parser("readiness")
    readiness.add_argument("--minimum-universe-size", type=int, default=100)

    approve = subcommands.add_parser("approve-basis")
    approve.add_argument("bundle_id")
    approve.add_argument("--reviewer", required=True)
    approve.add_argument("--confirmation-reference", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    store = WalkForwardInputStore(
        data_root=args.data_root,
        db_path=args.db_path,
    )
    if args.command == "archive":
        result = store.archive_bundle(args.manifest)
    elif args.command == "verify":
        result = store.verify_bundle(args.bundle_id)
    elif args.command == "readiness":
        result = store.readiness(
            minimum_universe_size=args.minimum_universe_size
        )
    else:
        result = store.approve_total_return_basis(
            args.bundle_id,
            reviewer=args.reviewer,
            confirmation_reference=args.confirmation_reference,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
