"""证券注册表（stock_registry）构建 — 真 PIT 宇宙重建 Phase 1。

口径（与立项方案 §5、probe_2026-08-14.md 实测一致）：

- 主源 baostock ``query_stock_basic()`` 空参全量（约 8897 行），过滤
  ``type=1``（股票，约 5545 只；ETF type=5 不入表），单次会话一次调用。
- baostock code 形如 ``sh.600068`` / ``sz.002260``，落库时拆成
  ``symbol``（6 位纯数字，与 daily_price 同口径）+ ``exchange``（SH/SZ）。
- ``status=1`` → LISTED（out_date 恒 NULL）；``status=0`` → DELISTED，
  out_date 取 outDate。ipoDate/outDate 为空串时落 NULL。
- 写入用 INSERT OR REPLACE（UNIQUE(symbol)），幂等，重跑安全。
- 交叉校验：tushare ``stock_basic list_status=D`` 退市清单（裸 HTTP API，
  urllib；连续调用会 502 限频，失败按 20 秒节奏重试）。与 baostock
  status=0 集合比对，**互相缺失只进报告，绝不自动改数**；token 缺失或
  调用失败时降级为「仅 baostock」并在报告中注明。
- 只写 stock_registry 一张表，不碰 daily_price 等任何其它表。

运行（宿主机 .venv，baostock 只装在 .venv）：
    .venv/bin/python deploy/build_stock_registry.py --dry-run   # 只打印统计不写库
    .venv/bin/python deploy/build_stock_registry.py             # 实跑写库

注意：baostock 单账号单会话，**避开 17:15 前后 nightly collect 窗口**
（16:30–18:30 只跑 --dry-run），否则采集会话会被踢。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

try:
    _PROJECT_ROOT = Path(__file__).resolve().parent.parent
except NameError:
    _PROJECT_ROOT = Path("/app")
sys.path.insert(0, str(_PROJECT_ROOT))

from foundf_db import Warehouse  # noqa: E402

TUSHARE_API = "http://api.tushare.pro"


def _now_cn() -> datetime:
    """北京时间（报告文件名与抬头与 probe 报告同口径）。"""
    from datetime import timedelta
    return datetime.now(timezone(timedelta(hours=8)))


TUSHARE_RETRY = 3                 # 限频 502 重试次数
TUSHARE_RETRY_SLEEP_S = 20        # 实测 20 秒节奏可恢复（probe_2026-08-14.md）
DB_LOCK_RETRY = 5
DB_LOCK_SLEEP_S = 10
REPORT_DIR = Path("reports/pit_universe")


# ── 纯函数：口径转换（可单测，不依赖网络）─────────────────────────

def normalize_baostock_code(code: str) -> tuple[str, str]:
    """``sh.600068`` → ``('600068', 'SH')``；格式不符抛 ValueError。"""
    prefix, sep, digits = code.partition(".")
    if not sep or len(digits) != 6 or not digits.isdigit():
        raise ValueError(f"baostock code 格式无法识别: {code!r}")
    return digits, prefix.upper()


def _parse_date_or_none(value: str) -> date | None:
    """baostock 日期字段：'YYYY-MM-DD' → date；空串/None → None。"""
    value = (value or "").strip()
    if not value:
        return None
    return date.fromisoformat(value)


def build_registry_rows(raw_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    """baostock query_stock_basic 原始行 → stock_registry 行。

    只保留 ``type=1``（股票）；security_type 原样落库留痕。
    raw_rows 元素为 ``{code, code_name, ipoDate, outDate, type, status}``。
    """
    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        if str(raw.get("type", "")).strip() != "1":
            continue  # ETF(type=5) 等不入注册表
        symbol, exchange = normalize_baostock_code(str(raw["code"]))
        status = str(raw.get("status", "")).strip()
        rows.append({
            "symbol": symbol,
            "code_name": (raw.get("code_name") or "").strip() or None,
            "exchange": exchange,
            "ipo_date": _parse_date_or_none(raw.get("ipoDate", "")),
            "out_date": _parse_date_or_none(raw.get("outDate", "")),
            "security_type": "1",
            "list_status": "LISTED" if status == "1" else "DELISTED",
            "source": "baostock",
        })
    return rows


def parse_tushare_delisted(items: list[list], fields: list[str]) -> dict[str, dict[str, Any]]:
    """tushare stock_basic list_status=D 响应 → ``{symbol: {exchange, delist_date, name}}``。

    ts_code 形如 ``002260.SZ``；delist_date 形如 ``20220617``（可空）。
    """
    idx = {name: i for i, name in enumerate(fields)}
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        ts_code = str(item[idx["ts_code"]])
        digits, sep, exch = ts_code.partition(".")
        if not sep or not digits.isdigit():
            continue
        raw_date = str(item[idx["delist_date"]] or "").strip()
        delist_date = (
            date.fromisoformat(f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}")
            if len(raw_date) == 8 and raw_date.isdigit() else None
        )
        out[digits] = {
            "exchange": exch.upper(),
            "delist_date": delist_date,
            "name": str(item[idx["name"]] or ""),
        }
    return out


def cross_check_delisted(
    registry_rows: list[dict[str, Any]],
    tushare_delisted: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """baostock 退市集 × tushare 退市集交叉比对（只报差异，不改数）。"""
    bs_delisted = {r["symbol"]: r for r in registry_rows
                   if r["list_status"] == "DELISTED"}
    ts_set = set(tushare_delisted)
    # tushare 有而 baostock 未标记退市：区分「baostock 仍标上市」与「baostock 无此票」
    ts_only_listed = sorted(s for s in ts_set - set(bs_delisted) if s in
                            {r["symbol"] for r in registry_rows})
    ts_only_absent = sorted(s for s in ts_set - set(bs_delisted)
                            if s not in {r["symbol"] for r in registry_rows})
    bs_only = sorted(set(bs_delisted) - ts_set)
    return {
        "baostock_delisted_count": len(bs_delisted),
        "tushare_delisted_count": len(ts_set),
        "tushare_only_bs_listed": ts_only_listed,   # 口径冲突：tushare 退市、baostock 仍上市
        "tushare_only_bs_absent": ts_only_absent,   # baostock 全量里根本没有此票
        "baostock_only": bs_only,                   # baostock 退市、tushare D 清单没有
    }


def reconcile_with_daily_price(
    registry_rows: list[dict[str, Any]],
    daily_price_symbols: list[str],
) -> dict[str, Any]:
    """注册表 × daily_price 现有 symbol 集合对账（现役池应全部 LISTED）。"""
    reg = {r["symbol"]: r for r in registry_rows}
    not_listed = sorted(
        s for s in daily_price_symbols
        if s in reg and reg[s]["list_status"] != "LISTED"
    )
    missing = sorted(s for s in daily_price_symbols if s not in reg)
    return {
        "daily_price_symbol_count": len(daily_price_symbols),
        "not_listed_in_registry": not_listed,   # daily_price 在收、注册表却非 LISTED
        "absent_from_registry": missing,        # daily_price 有、注册表没有
    }


def registry_stats(registry_rows: list[dict[str, Any]]) -> dict[str, int]:
    """总数/上市/退市/2020 后退市 对账计数。"""
    delisted = [r for r in registry_rows if r["list_status"] == "DELISTED"]
    after_2020 = [r for r in delisted
                  if r["out_date"] is not None and r["out_date"] >= date(2020, 1, 1)]
    return {
        "total": len(registry_rows),
        "listed": len(registry_rows) - len(delisted),
        "delisted": len(delisted),
        "delisted_after_2020": len(after_2020),
    }


# ── 网络层（单测用假数据替换，不在测试覆盖范围）────────────────────

def fetch_baostock_stock_basic() -> list[dict[str, str]]:
    """baostock 单会话 query_stock_basic() 空参全量。

    baostock 只装在 .venv，局部导入避免系统 python3（无 baostock）
    导入本模块时炸掉（测试走纯函数，不经过此路径）。
    """
    import baostock as bs

    login = bs.login()
    if str(login.error_code) != "0":
        raise RuntimeError(f"baostock 登录失败: {login.error_msg}")
    try:
        rs = bs.query_stock_basic()
        if str(rs.error_code) != "0":
            raise RuntimeError(f"query_stock_basic 失败: {rs.error_msg}")
        fields: list[str] = list(rs.fields)
        rows: list[dict[str, str]] = []
        while rs.next():
            rows.append(dict(zip(fields, rs.get_row_data())))
        return rows
    finally:
        bs.logout()


def _load_tushare_token() -> str:
    """Token 取自环境变量，缺失时回退解析项目根目录 .env。

    严禁把 token 写进任何文件/日志/输出（与 daily_candidates 同约束）。
    """
    import os
    token = os.getenv("TUSHARE_TOKEN", "")
    if token:
        return token
    env_file = _PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("TUSHARE_TOKEN="):
                return line.split("=", 1)[1].strip()
    return ""


def fetch_tushare_delisted(token: str) -> dict[str, dict[str, Any]]:
    """tushare stock_basic list_status=D 退市清单（urllib 裸 HTTP）。

    连续调用触发 502 限频，实测 20 秒节奏可恢复；重试耗尽仍失败则抛
    RuntimeError，由调用方降级为「仅 baostock」。
    """
    payload = json.dumps({
        "api_name": "stock_basic",
        "token": token,
        "params": {"list_status": "D"},
        "fields": "ts_code,name,list_date,delist_date",
    }).encode("utf-8")
    last_err: Exception | None = None
    # 宿主机 shell 常带 clash 代理环境变量（http_proxy=127.0.0.1:7890），
    # urllib 默认走代理会被网关 502 拦截；空 ProxyHandler 强制直连。
    # cron/容器环境无代理变量，此行为与原先一致。
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for attempt in range(TUSHARE_RETRY + 1):
        try:
            req = urllib.request.Request(
                TUSHARE_API, data=payload,
                headers={"Content-Type": "application/json"},
            )
            with direct.open(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("code") != 0:
                raise RuntimeError(f"tushare stock_basic(D): {data.get('msg')}")
            block = data.get("data") or {}
            return parse_tushare_delisted(
                block.get("items") or [], block.get("fields") or [])
        except Exception as exc:  # 502/超时/接口错误统一按限频节奏重试
            last_err = exc
            if attempt < TUSHARE_RETRY:
                print(f"    ⏳ tushare 调用失败（{type(exc).__name__}），"
                      f"{TUSHARE_RETRY_SLEEP_S}s 后重试 "
                      f"({attempt + 1}/{TUSHARE_RETRY})")
                time.sleep(TUSHARE_RETRY_SLEEP_S)
    raise RuntimeError(f"tushare 退市清单获取失败（已重试 {TUSHARE_RETRY} 次）: "
                       f"{type(last_err).__name__}: {last_err}")


# ── 报告与主流程 ─────────────────────────────────────────────────

def render_report(
    stats: dict[str, int],
    cross: dict[str, Any] | None,
    cross_note: str,
    recon: dict[str, Any],
    *,
    dry_run: bool,
    db_path: str,
) -> str:
    """生成对账报告 Markdown（交叉校验差异只进报告，不自动改数）。"""
    lines = [
        f"# stock_registry 构建对账报告 — {_now_cn():%Y-%m-%d %H:%M} 北京时间",
        "",
        f"> 库：`{db_path}`；模式：{'DRY-RUN（未写库）' if dry_run else '已写库（INSERT OR REPLACE 幂等）'}",
        "",
        "## 1. 注册表统计（baostock type=1 股票）",
        "",
        f"- 总数：{stats['total']}",
        f"- 上市（LISTED）：{stats['listed']}",
        f"- 退市（DELISTED）：{stats['delisted']}",
        f"- 其中 2020 年后退市：{stats['delisted_after_2020']}",
        "",
        "## 2. tushare 退市清单交叉校验（只报差异，未自动改数）",
        "",
        f"- 状态：{cross_note}",
    ]
    if cross is not None:
        lines += [
            f"- baostock 退市数：{cross['baostock_delisted_count']}；"
            f"tushare 退市数：{cross['tushare_delisted_count']}",
            f"- tushare 退市但 baostock 仍标 LISTED（{len(cross['tushare_only_bs_listed'])}）："
            f"{', '.join(cross['tushare_only_bs_listed']) or '无'}",
            f"- tushare 退市但 baostock 全量无此票（{len(cross['tushare_only_bs_absent'])}）："
            f"{', '.join(cross['tushare_only_bs_absent']) or '无'}",
            f"- baostock 退市但 tushare D 清单没有（{len(cross['baostock_only'])}）："
            f"{', '.join(cross['baostock_only']) or '无'}",
        ]
    lines += [
        "",
        "## 3. 与 daily_price 现有 symbol 集合比对",
        "",
        f"- daily_price 6 位数字 symbol 数：{recon['daily_price_symbol_count']}",
        f"- 注册表非 LISTED 但 daily_price 在收（{len(recon['not_listed_in_registry'])}）："
        f"{', '.join(recon['not_listed_in_registry']) or '无'}",
        f"- daily_price 有但注册表缺失（{len(recon['absent_from_registry'])}）："
        f"{', '.join(recon['absent_from_registry']) or '无'}",
        "",
    ]
    return "\n".join(lines)


def _open_warehouse(db_path: str, *, init: bool = True) -> Warehouse:
    """建立连接（DuckDB 单写锁冲突时重试）。

    init=False 时不执行 schema DDL——dry-run 严格只读，连建表也不做。
    """
    for attempt in range(DB_LOCK_RETRY):
        try:
            wh = Warehouse(db_path)
            if init:
                wh.init()
            return wh
        except Exception as exc:
            if "lock" not in str(exc).lower() or attempt == DB_LOCK_RETRY - 1:
                raise
            print(f"    ⏳ DuckDB 连接锁冲突，{DB_LOCK_SLEEP_S}s 后重试 "
                  f"({attempt + 1}/{DB_LOCK_RETRY})")
            time.sleep(DB_LOCK_SLEEP_S)
    raise RuntimeError("unreachable")


def run(
    db_path: str,
    *,
    dry_run: bool = False,
    raw_rows: list[dict[str, str]] | None = None,
    tushare_delisted: dict[str, dict[str, Any]] | None = None,
    report_dir: Path = REPORT_DIR,
) -> dict[str, Any]:
    """主流程。raw_rows/tushare_delisted 可注入（测试用假数据，不碰网络）。

    返回对账结果字典；报告落 reports/pit_universe/registry_<date>.md。
    """
    if raw_rows is None:
        raw_rows = fetch_baostock_stock_basic()
    rows = build_registry_rows(raw_rows)
    stats = registry_stats(rows)
    print(f"[stock_registry] baostock 原始 {len(raw_rows)} 行 → "
          f"type=1 股票 {stats['total']} 只 "
          f"(LISTED {stats['listed']} / DELISTED {stats['delisted']} / "
          f"2020后退市 {stats['delisted_after_2020']})")

    # tushare 退市清单交叉校验：token 缺失/限频失败 → 降级「仅 baostock」
    cross: dict[str, Any] | None = None
    if tushare_delisted is not None:
        cross = cross_check_delisted(rows, tushare_delisted)
        cross_note = "已比对（注入数据）"
    else:
        token = _load_tushare_token()
        if not token:
            cross_note = "降级：TUSHARE_TOKEN 缺失，仅 baostock 单源"
        else:
            try:
                cross = cross_check_delisted(rows, fetch_tushare_delisted(token))
                cross_note = "已比对（tushare stock_basic list_status=D）"
            except RuntimeError as exc:
                cross_note = f"降级：{exc}，仅 baostock 单源"
    print(f"[stock_registry] 交叉校验：{cross_note}")
    if cross is not None:
        print(f"    差异：tushare退市而bs上市 {len(cross['tushare_only_bs_listed'])} / "
              f"bs无此票 {len(cross['tushare_only_bs_absent'])} / "
              f"仅bs退市 {len(cross['baostock_only'])}")

    # daily_price 现役池对账（dry-run 严格只读，连 DDL 也不执行）
    wh = _open_warehouse(db_path, init=not dry_run)
    try:
        dp_symbols = [
            str(r["symbol"]) for r in wh.query(
                "SELECT DISTINCT symbol FROM daily_price "
                "WHERE regexp_matches(symbol, '^[0-9]{6}$') ORDER BY symbol")
        ]
        recon = reconcile_with_daily_price(rows, dp_symbols)
        print(f"[stock_registry] daily_price symbol {recon['daily_price_symbol_count']} 只："
              f"非 LISTED {len(recon['not_listed_in_registry'])} / "
              f"注册表缺失 {len(recon['absent_from_registry'])}")

        if dry_run:
            print("[stock_registry] DRY-RUN：不写库")
            written = 0
        else:
            written = wh.insert("stock_registry", rows, conflict_strategy="replace")
            print(f"[stock_registry] 已写库 {written} 行（INSERT OR REPLACE 幂等）")
    finally:
        wh.close()

    report = render_report(stats, cross, cross_note, recon,
                           dry_run=dry_run, db_path=db_path)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"registry_{_now_cn():%Y-%m-%d}.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"[stock_registry] 报告：{report_path}")

    return {"stats": stats, "cross": cross, "cross_note": cross_note,
            "recon": recon, "written": written, "report_path": str(report_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description="stock_registry 证券注册表构建")
    parser.add_argument("--db", default="data/finance.duckdb")
    parser.add_argument("--dry-run", action="store_true",
                        help="只拉取并打印统计，不写库（仍写报告）")
    args = parser.parse_args()
    run(args.db, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
