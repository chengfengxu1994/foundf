"""CLI for auditable strategy candidate signals.

The CLI records research signals only. It has no order sizing, no broker
action, and no "how much to buy" output.

Examples:
    python3 -m foundf_db.strategy_candidate_cli record \
        --generated-at 2026-07-31T09:30:00+08:00 --data-as-of 2026-07-30 \
        --strategy-version 2026-07-30.1 --symbol 510300 --side BUY \
        --conviction 0.6 --evidence-hash <sha256> --source FACTOR_MODEL
    python3 -m foundf_db.strategy_candidate_cli list --status CANDIDATE
    python3 -m foundf_db.strategy_candidate_cli get sc_...
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .strategy_candidate_store import (
    SIDES,
    SOURCES,
    STATUSES,
    StrategyCandidateStore,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record and inspect strategy candidate signals."
    )
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--db-path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    record = subparsers.add_parser("record")
    record.add_argument("--generated-at", required=True)
    record.add_argument("--data-as-of", required=True)
    record.add_argument("--strategy-version", required=True)
    record.add_argument("--symbol", required=True)
    record.add_argument("--side", required=True, choices=sorted(SIDES))
    record.add_argument("--conviction", required=True, type=float)
    record.add_argument("--evidence-hash", required=True)
    record.add_argument("--source", required=True, choices=sorted(SOURCES))

    update = subparsers.add_parser("update-status")
    update.add_argument("candidate_id")
    update.add_argument("--status", required=True, choices=sorted(STATUSES))
    update.add_argument("--confirmation-reference", required=True)

    listing = subparsers.add_parser("list")
    listing.add_argument("--status", choices=sorted(STATUSES))
    listing.add_argument("--symbol")

    get = subparsers.add_parser("get")
    get.add_argument("candidate_id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    store = StrategyCandidateStore(
        data_root=Path(args.data_root),
        db_path=args.db_path,
    )
    if args.command == "record":
        result = store.record_candidate(
            generated_at=args.generated_at,
            data_as_of=args.data_as_of,
            strategy_version=args.strategy_version,
            symbol=args.symbol,
            side=args.side,
            conviction=args.conviction,
            evidence_hash=args.evidence_hash,
            source=args.source,
        )
    elif args.command == "update-status":
        result = store.update_status(
            args.candidate_id,
            args.status,
            confirmation_reference=args.confirmation_reference,
        )
    elif args.command == "list":
        result = store.list_candidates(status=args.status, symbol=args.symbol)
    else:
        result = store.get_candidate(args.candidate_id)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
