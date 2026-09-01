"""长期事件与研报仓库命令行入口。所有业务写入都需要 ``--confirm-ref``。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .event_store import InvestmentEventStore
from .research_report_store import ResearchReportStore


def _json(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FoundF 长期事件与研报资产库")
    parser.add_argument("--data-root", default=None, help="NAS data 根目录")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化幂等数据库结构")

    ingest = sub.add_parser("ingest-event", help="导入重大事件 JSON")
    ingest.add_argument("path")
    ingest.add_argument("--confirm-ref", required=True)

    review = sub.add_parser("review-event", help="记录事件事后结果")
    review.add_argument("event_id")
    review.add_argument("path")
    review.add_argument("--confirm-ref", required=True)

    lesson = sub.add_parser("create-lesson", help="创建长期经验候选")
    lesson.add_argument("path")
    lesson.add_argument("--confirm-ref", required=True)

    institution = sub.add_parser("register-institution", help="登记研报机构")
    institution.add_argument("name")
    institution.add_argument("--alias", action="append", default=[])
    institution.add_argument("--jurisdiction")
    institution.add_argument("--confirm-ref", required=True)

    report = sub.add_parser("ingest-report", help="导入授权研报元数据和主张")
    report.add_argument("path")
    report.add_argument("--confirm-ref", required=True)

    evaluate = sub.add_parser("evaluate-claim", help="评估到期研报主张")
    evaluate.add_argument("claim_id")
    evaluate.add_argument("path")
    evaluate.add_argument("--confirm-ref", required=True)

    status = sub.add_parser("set-institution-status", help="人工调整机构状态")
    status.add_argument("institution_id")
    status.add_argument("status", choices=["ACTIVE", "WATCH", "REDUCED", "BLOCKED"])
    status.add_argument("--reason", required=True)
    status.add_argument("--confirm-ref", required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    events = InvestmentEventStore(data_root=args.data_root)
    reports = ResearchReportStore(data_root=args.data_root)
    if args.command == "init":
        events.initialize()
        print(json.dumps({"status": "READY", "db_path": str(events.db_path)}))
        return
    if args.command == "ingest-event":
        result = events.ingest_event(
            _json(args.path), confirmation_reference=args.confirm_ref
        )
    elif args.command == "review-event":
        result = events.record_outcome(
            args.event_id,
            _json(args.path),
            confirmation_reference=args.confirm_ref,
        )
    elif args.command == "create-lesson":
        result = events.create_lesson(
            _json(args.path), confirmation_reference=args.confirm_ref
        )
    elif args.command == "register-institution":
        result = reports.register_institution(
            args.name,
            aliases=args.alias,
            jurisdiction=args.jurisdiction,
            confirmation_reference=args.confirm_ref,
        )
    elif args.command == "ingest-report":
        result = reports.ingest_report(
            _json(args.path), confirmation_reference=args.confirm_ref
        )
    elif args.command == "evaluate-claim":
        result = reports.evaluate_claim(
            args.claim_id,
            _json(args.path),
            confirmation_reference=args.confirm_ref,
        )
    elif args.command == "set-institution-status":
        result = reports.set_institution_status(
            args.institution_id,
            args.status,
            reason=args.reason,
            confirmation_reference=args.confirm_ref,
        )
    else:
        raise AssertionError(args.command)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
