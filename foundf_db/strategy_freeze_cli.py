"""策略预注册冻结 CLI（pre-registration freeze）。

只冻结研究口径（版本/参数/宇宙/评价指标快照 + 哈希），没有下单、
没有交易金额输出。冻结后规则不可变，只能退役或被新冻结取代。

Examples:
    python3 -m foundf_db.strategy_freeze_cli freeze \
        --strategy-id multifactor_sim --version multifactor_v3_sim.5 \
        --params-file freeze_params.json --freeze-date 2026-08-13 \
        --reviewer 张三 --confirmation-reference 治理评审-2026-08-13
    python3 -m foundf_db.strategy_freeze_cli freeze ... --supersede --reason 参数修订
    python3 -m foundf_db.strategy_freeze_cli retire sf_... --outcome FAILED \
        --reviewer 张三 --confirmation-reference 观察期评审-001 --reason 样本外跑输
    python3 -m foundf_db.strategy_freeze_cli list [--status FROZEN]
    python3 -m foundf_db.strategy_freeze_cli get sf_...
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .strategy_freeze_store import (
    STATUSES,
    StrategyFreezeError,
    StrategyFreezeStore,
)


def _load_json_file(path: str, code: str) -> dict[str, Any]:
    """读取 JSON 规格文件，必须是 JSON 对象；失败 fail-closed。"""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StrategyFreezeError(code, f"{path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise StrategyFreezeError(code, f"{path}: 必须是 JSON 对象")
    return payload


def _print_freeze_line(row: dict[str, Any]) -> None:
    print(
        f"{row['freeze_id']}  {row['strategy_id']}  {row['strategy_version']}  "
        f"{row['status']}  freeze_date={row['freeze_date']}  "
        f"params_hash={row['params_hash']}  reviewer={row['reviewer']}"
    )


def _print_detail(result: dict[str, Any]) -> None:
    if result["status"] != "OK":
        print(f"未找到冻结记录: {result['freeze_id']}")
        return
    row = result["freeze"]
    _print_freeze_line(row)
    print(f"  universe_hash={row.get('universe_hash')}  "
          f"code_ref={row.get('code_ref')}")
    print(f"  confirmation_reference={row['confirmation_reference']}")
    print(f"  params_json={row['params_json']}")
    if row.get("universe_spec_json"):
        print(f"  universe_spec_json={row['universe_spec_json']}")
    if row.get("metrics_spec_json"):
        print(f"  metrics_spec_json={row['metrics_spec_json']}")
    print("  审计轨迹:")
    for audit in result["status_audit"]:
        origin = audit["from_status"] or "(创建)"
        print(f"    [{audit['at']}] {origin} -> {audit['to_status']}  "
              f"actor={audit['actor']}  reason={audit['reason']}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="策略预注册冻结：版本/参数/宇宙/评价指标的冻结快照与审计。"
    )
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--db-path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    freeze = subparsers.add_parser("freeze", help="创建预注册冻结（创建即 FROZEN）")
    freeze.add_argument("--strategy-id", required=True)
    freeze.add_argument("--version", required=True, dest="strategy_version")
    freeze.add_argument("--params-file", required=True)
    freeze.add_argument("--universe-file")
    freeze.add_argument("--metrics-file")
    freeze.add_argument("--freeze-date", required=True)
    freeze.add_argument("--reviewer", required=True)
    freeze.add_argument("--confirmation-reference", required=True)
    freeze.add_argument("--supersede", action="store_true",
                        help="显式确认取代同 strategy_id 的现有 FROZEN")
    freeze.add_argument("--reason", help="supersede 原因（写入旧记录审计）")

    retire = subparsers.add_parser("retire", help="退役为终态（记录保留不删除）")
    retire.add_argument("freeze_id")
    retire.add_argument("--outcome", required=True, choices=["FAILED", "SUCCESS"])
    retire.add_argument("--reviewer", required=True)
    retire.add_argument("--confirmation-reference", required=True)
    retire.add_argument("--reason", required=True)

    listing = subparsers.add_parser("list", help="列出冻结记录（含失败终态）")
    listing.add_argument("--status", choices=sorted(STATUSES))
    listing.add_argument("--strategy-id")

    get = subparsers.add_parser("get", help="查看单条冻结与审计轨迹")
    get.add_argument("freeze_id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    store = StrategyFreezeStore(
        data_root=Path(args.data_root),
        db_path=args.db_path,
    )
    try:
        if args.command == "freeze":
            result = store.create_freeze(
                strategy_id=args.strategy_id,
                strategy_version=args.strategy_version,
                params=_load_json_file(args.params_file, "PARAMS_FILE_INVALID"),
                universe_spec=(
                    _load_json_file(args.universe_file, "UNIVERSE_FILE_INVALID")
                    if args.universe_file else None
                ),
                metrics_spec=(
                    _load_json_file(args.metrics_file, "METRICS_FILE_INVALID")
                    if args.metrics_file else None
                ),
                freeze_date=args.freeze_date,
                reviewer=args.reviewer,
                confirmation_reference=args.confirmation_reference,
                supersede=args.supersede,
                reason=args.reason,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        elif args.command == "retire":
            result = store.retire(
                args.freeze_id,
                outcome=args.outcome,
                reviewer=args.reviewer,
                confirmation_reference=args.confirmation_reference,
                reason=args.reason,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        elif args.command == "list":
            result = store.list_freezes(
                status=args.status, strategy_id=args.strategy_id
            )
            print(f"共 {result['total']} 条冻结记录:")
            for row in result["freezes"]:
                _print_freeze_line(row)
        else:
            _print_detail(store.get_freeze(args.freeze_id))
    except StrategyFreezeError as exc:
        print(f"拒绝: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
