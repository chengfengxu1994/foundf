"""CLI for fail-closed archival of broker simulation exports.

The CLI deliberately has no parser, mapper, normalizer, or trading action.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .broker_simulation_archive import (
    ACCOUNT_PROOF_SOURCES,
    EXPORT_KINDS,
    BrokerSimulationArchive,
)
from .warehouse import Warehouse


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Archive and verify raw Guosen simulation exports."
    )
    parser.add_argument("--data-root", default="data")
    subparsers = parser.add_subparsers(dest="command", required=True)

    archive = subparsers.add_parser("archive")
    archive.add_argument("source_path")
    archive.add_argument("--kind", required=True, choices=sorted(EXPORT_KINDS))
    archive.add_argument("--exported-at", required=True)
    archive.add_argument(
        "--proof-source",
        required=True,
        choices=sorted(ACCOUNT_PROOF_SOURCES),
    )
    archive.add_argument("--proof-reference", required=True)
    archive.add_argument("--confirmation-reference", required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("artifact_id")

    subparsers.add_parser("list")
    return parser


def _list_archives(store: BrokerSimulationArchive) -> dict:
    if not store.db_path.exists():
        return {"status": "DATABASE_MISSING", "total": 0, "artifacts": []}
    with Warehouse(store.db_path) as warehouse:
        warehouse.init()
        rows = warehouse.query(
            "SELECT artifact_id, export_kind, broker, client_name, exported_at, "
            "received_at, source_sha256, source_size_bytes, account_mode, "
            "archive_relpath, mapping_status, normalization_status "
            "FROM broker_sim_export_archive ORDER BY received_at, artifact_id"
        )
    return {"status": "OK", "total": len(rows), "artifacts": rows}


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    store = BrokerSimulationArchive(data_root=Path(args.data_root))
    if args.command == "archive":
        result = store.archive_export(
            args.source_path,
            export_kind=args.kind,
            exported_at=args.exported_at,
            account_mode="SIMULATION",
            account_proof_source=args.proof_source,
            account_proof_reference=args.proof_reference,
            confirmation_reference=args.confirmation_reference,
        )
    elif args.command == "verify":
        result = store.verify_archive(args.artifact_id)
    else:
        result = _list_archives(store)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
