"""
FoundF API — portfolio dashboard backend (Phase I4).

Endpoints:
  GET  /api/status          — system health
  GET  /api/summary         — portfolio summary
  GET  /api/holdings        — current holdings
  GET  /api/snapshots       — daily asset history
  GET  /api/returns         — return metrics
  GET  /api/trade-analysis  — per-stock trade analysis
  GET  /api/events          — economic events (paginated)
  GET  /api/investment-events — long-term material event index
  GET  /api/research/institutions — research-source reliability
  GET  /api/research/reports — archived research metadata
  GET  /api/data-assets/health — long-term database and NAS archive gate
  GET  /api/strategy/governance — read-only strategy evidence gates
  GET  /api/chain           — balance chain
  GET  /api/positions       — current positions detail
  GET  /api/dashboard       — customer-facing risk dashboard projection
  GET  /api/simulation      — read-only paper-trading detail projection
  GET  /api/external-sources/status — licensed/discovery source readiness

Run:
    uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import csv
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api.dashboard_service import (
    build_customer_dashboard,
    legacy_untrusted_projection,
)
from api.auth import AuthManager
from api.simulation_service import build_simulation_dashboard
from api.backtest_service import build_backtest_compare
from data_provider.external_intelligence import external_source_status
from foundf_db.event_store import InvestmentEventStore
from foundf_db.research_report_store import ResearchReportStore
from foundf_db.health import inspect_data_assets
from foundf_db.market_watch import MarketWatchStore
from foundf_db.runtime_scheduler import load_runtime_status
from foundf_db.walk_forward_input_store import inspect_walk_forward_inputs
from portfolio_manager.ips import InvestmentPolicyStatement
from strategy_manager.governance_status import load_governance_status

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports/reconciliation"
SIM_REVIEW_REPORTS = ROOT / "reports/sim_review"
CONFIG = ROOT / "config"

app = FastAPI(title="FoundF Portfolio API", version="0.2.0")
cors_origins = [
    item.strip()
    for item in os.getenv("FOUNDF_CORS_ORIGINS", "").split(",")
    if item.strip()
]
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-FoundF-Confirmation"],
    )

AUTH = AuthManager.from_env()
PUBLIC_API_PATHS = {
    "/api/status",
    "/api/auth/status",
    "/api/auth/login",
    "/api/auth/logout",
}


@app.middleware("http")
async def protect_customer_data(request: Request, call_next):
    """Protect every data endpoint when customer authentication is enabled."""
    path = request.url.path
    if (
        AUTH.enabled
        and path.startswith("/api/")
        and path not in PUBLIC_API_PATHS
        and AUTH.verify_session(request.cookies.get(AUTH.cookie_name, "")) is None
    ):
        response = JSONResponse(
            {"detail": "authentication required"},
            status_code=401,
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


class LoginRequest(BaseModel):
    username: str
    password: str


class WatchlistItemRequest(BaseModel):
    symbol: str
    provider_symbol: str
    name: str
    market: str
    exchange: str = ""
    currency: str
    asset_type: str = "STOCK"
    sort_order: int = 1000

# ── Data Loaders ─────────────────────────────────────

def load_csv(name: str) -> list[dict[str, Any]]:
    path = REPORTS / name
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))

engines: dict[str, Any] = {}
def get_engine():
    if "state" not in engines:
        from portfolio_manager.returns import ReturnsCalculator
        from portfolio_manager.state_engine import PortfolioStateEngine
        from portfolio_manager.trade_analysis import TradeAnalyzer

        engines["state"] = PortfolioStateEngine()
        engines["returns"] = ReturnsCalculator()
        engines["trade"] = TradeAnalyzer()
    return engines


def get_market_watch() -> MarketWatchStore:
    if "market_watch" not in engines:
        engines["market_watch"] = MarketWatchStore(
            data_root=Path(os.getenv("FOUNDF_DATA_ROOT", ROOT / "data")),
            config_path=CONFIG / "market_watchlist.json",
        )
    return engines["market_watch"]


def _require_watchlist_write(request: Request) -> str:
    """观察列表写入需同时满足登录、开关和逐次确认。"""
    if not AUTH.enabled:
        raise HTTPException(
            status_code=403,
            detail="启用 Dashboard 登录后才允许修改观察列表",
        )
    if os.getenv("MARKET_WATCH_MUTATIONS_ENABLED", "false").lower() != "true":
        raise HTTPException(status_code=403, detail="观察列表写入开关未启用")
    confirmation = request.headers.get("X-FoundF-Confirmation", "").strip()
    if not confirmation:
        raise HTTPException(status_code=400, detail="缺少显式确认编号")
    return confirmation


async def _market_watch_loop() -> None:
    interval = max(60, int(os.getenv("MARKET_WATCH_POLL_SECONDS", "60")))
    store = get_market_watch()
    last_prune: date | None = None
    failure_count = 0
    while True:
        try:
            result = await asyncio.to_thread(store.collect_quotes)
            failure_count = 0 if result.get("stored", 0) else min(failure_count + 1, 6)
            if last_prune != date.today():
                await asyncio.to_thread(store.prune)
                last_prune = date.today()
        except Exception:
            # 行情失败由快照回退和状态接口显式呈现，不让采集任务拖垮 API。
            failure_count = min(failure_count + 1, 6)
        # 连续失败指数退避，最长一小时，避免限流期间持续冲击上游。
        await asyncio.sleep(min(3600, interval * (2 ** failure_count)))


@app.on_event("startup")
async def start_market_watch_collector() -> None:
    if os.getenv("MARKET_WATCH_ENABLED", "false").lower() == "true":
        engines["market_watch_task"] = asyncio.create_task(_market_watch_loop())


@app.on_event("shutdown")
async def stop_market_watch_collector() -> None:
    task = engines.pop("market_watch_task", None)
    if task is not None:
        task.cancel()

# ── API Endpoints ────────────────────────────────────

@app.get("/api/status")
def status():
    return {"status": "ok", "version": "0.2.0", "date": date.today().isoformat()}


@app.get("/api/auth/status")
def auth_status(request: Request):
    authenticated = (
        not AUTH.enabled
        or AUTH.verify_session(request.cookies.get(AUTH.cookie_name, "")) is not None
    )
    return {"enabled": AUTH.enabled, "authenticated": authenticated}


@app.post("/api/auth/login")
def auth_login(payload: LoginRequest, request: Request):
    if not AUTH.enabled:
        return {"authenticated": True, "mode": "AUTH_DISABLED"}
    client_key = request.client.host if request.client else "unknown"
    valid, reason = AUTH.authenticate(
        payload.username, payload.password, client_key=client_key
    )
    if not valid:
        status_code = 429 if reason == "RATE_LIMITED" else 401
        return JSONResponse({"detail": reason}, status_code=status_code)
    response = JSONResponse({"authenticated": True})
    response.set_cookie(
        key=AUTH.cookie_name,
        value=AUTH.issue_session(),
        max_age=AUTH.session_seconds,
        httponly=True,
        secure=AUTH.secure_cookie,
        samesite="strict",
        path="/",
    )
    return response


@app.post("/api/auth/logout")
def auth_logout():
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(AUTH.cookie_name, path="/")
    return response


@app.get("/api/dashboard")
def customer_dashboard():
    """Return an explainable, read-only projection for end customers."""
    health = inspect_data_assets(data_root=ROOT / "data")
    walk_forward_inputs = inspect_walk_forward_inputs(data_root=ROOT / "data")
    governance = load_governance_status(
        ROOT / "data/governance/strategy_evolution_status.json",
        walk_forward_input_status=walk_forward_inputs,
    )
    runtime = load_runtime_status(data_root=ROOT / "data")
    return build_customer_dashboard(
        reports_dir=REPORTS,
        allocation_policy_path=CONFIG / "asset_allocation.json",
        risk_policy_path=CONFIG / "portfolio_risk_limits.json",
        data_dir=ROOT / "data",
        strategy_policy_path=CONFIG / "long_term_strategy.json",
        investor_profile_path=ROOT / ".secrets" / "investor_profile.json",
        ips_path=CONFIG / "ips.example.json",
        data_assets_health=health,
        strategy_governance=governance,
        runtime_automation=runtime,
    )


@app.get("/api/strategy/governance")
def strategy_governance():
    """返回只读策略证据门禁；读取不会运行研究或改写治理状态。"""
    return load_governance_status(
        ROOT / "data/governance/strategy_evolution_status.json",
        walk_forward_input_status=inspect_walk_forward_inputs(
            data_root=ROOT / "data"
        ),
    )


@app.get("/api/strategy/walk-forward-inputs")
def walk_forward_input_readiness():
    """只读显示历史成分、总回报行情与基准输入缺口。"""
    return inspect_walk_forward_inputs(data_root=ROOT / "data")


@app.get("/api/ips")
def investment_policy_statement():
    """返回只读 IPS；未确认模板不得被解释为已生效策略。"""
    return InvestmentPolicyStatement.load(CONFIG / "ips.example.json").as_api()


@app.get("/api/external-sources/status")
def external_sources_status():
    """只返回配置就绪状态，不暴露 API key、授权凭据或搜索正文。"""
    return external_source_status(
        investing_import_path=ROOT / "data/import/investing_authorized.json"
    )


@app.get("/api/data-assets/health")
def data_assets_health():
    """统一检查数据库、行情时点、事件归档、研报积累和备份状态。"""
    return inspect_data_assets(data_root=ROOT / "data")


@app.get("/api/runtime/automation")
def runtime_automation():
    """只读展示 NAS 调度心跳；不触发任务，也不返回凭据。"""
    return load_runtime_status(data_root=ROOT / "data")


@app.get("/api/simulation")
def simulation_dashboard():
    """只读模拟盘投影；不返回原始券商记录或任何可执行交易能力。"""
    return build_simulation_dashboard(
        Path(os.getenv("DUCKDB_PATH", ROOT / "data/finance.duckdb")),
        review_dir=SIM_REVIEW_REPORTS,
    )


@app.get("/api/backtest-compare")
def backtest_compare():
    """只读回测对比投影：repro bundle 逐年结果 + 逐笔交易账本 + 基准对照。"""
    return build_backtest_compare(
        ROOT / "reports",
        db_path=Path(os.getenv("DUCKDB_PATH", ROOT / "data/finance.duckdb")),
    )


@app.get("/api/backtest-compare/nav-curve")
def backtest_compare_nav_curve():
    """生产口径净值对比图（plot_nav_compare 产物）；缺失时 404 JSON。"""
    path = ROOT / "reports/nav_compare/nav_curve.png"
    if not path.is_file():
        return JSONResponse(
            status_code=404,
            content={"status": "BACKTEST_DATA_MISSING",
                     "detail": "nav_curve.png 不存在（等周末/交易日 cron 生成）"},
        )
    return FileResponse(path)


@app.get("/api/market-watch/lists")
def market_watch_lists():
    """返回配置列表与经确认的用户覆盖项，不触发外部请求。"""
    return get_market_watch().list_watchlists()


@app.get("/api/market-watch/quotes")
def market_watch_quotes(list_id: str | None = Query(None)):
    """读取最新本地行情快照；缺数据不补零。"""
    return get_market_watch().latest_quotes(list_id)


@app.post("/api/market-watch/refresh")
def market_watch_refresh(request: Request, list_id: str | None = Query(None)):
    """人工刷新行情采集；写入的是外部数据快照，不改变组合或交易状态。"""
    if os.getenv("MARKET_WATCH_ENABLED", "false").lower() != "true":
        raise HTTPException(status_code=503, detail="行情采集尚未启用")
    result = get_market_watch().collect_quotes(list_id)
    result["quotes"] = get_market_watch().latest_quotes(list_id)
    return result


@app.post("/api/market-watch/lists/{list_id}/items")
def market_watch_add_item(
    list_id: str,
    payload: WatchlistItemRequest,
    request: Request,
):
    confirmation = _require_watchlist_write(request)
    try:
        return get_market_watch().add_item(
            list_id=list_id,
            payload=(
                payload.model_dump()
                if hasattr(payload, "model_dump")
                else payload.dict()
            ),
            confirmation_reference=confirmation,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/market-watch/lists/{list_id}/items/{symbol}/disable")
def market_watch_disable_item(list_id: str, symbol: str, request: Request):
    confirmation = _require_watchlist_write(request)
    try:
        return get_market_watch().disable_item(
            list_id=list_id,
            symbol=symbol,
            confirmation_reference=confirmation,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/summary")
def summary():
    """Fail closed: the legacy mixed-currency summary is not decision evidence."""
    return legacy_untrusted_projection("summary")

@app.get("/api/holdings")
def holdings():
    eng = get_engine()
    v = eng["state"].verify_against_broker()
    return {"date": v["snapshot_date"], "holdings": v["holdings"], "count": v["stocks"]}

@app.get("/api/snapshots")
def snapshots(limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
    """Fail closed until snapshots are rebuilt from trusted unit NAV."""
    return legacy_untrusted_projection(
        "snapshots",
        offset=offset,
        limit=limit,
    )

@app.get("/api/returns")
def returns_metrics():
    """Fail closed instead of exposing invalid legacy TWR/simple returns."""
    return legacy_untrusted_projection("returns")

@app.get("/api/trade-analysis")
def trade_analysis():
    eng = get_engine()
    return eng["trade"].analyze()

@app.get("/api/companies")
def companies():
    """Return observed companies from finance_intel database."""
    import sqlite3
    db_path = Path(__file__).resolve().parents[1] / "finance_intel/data/finance_intel.db"
    if not db_path.exists():
        return {"companies": [], "note": "finance_intel database not found"}
    try:
        con = sqlite3.connect(str(db_path))
        cur = con.cursor()
        cur.execute("SELECT symbol, name, market, thesis, invalidation FROM companies")
        rows = cur.fetchall()
        con.close()
        return {
            "companies": [
                {"symbol": r[0], "name": r[1], "market": r[2],
                 "thesis": r[3] or "", "invalidation": r[4] or ""}
                for r in rows
            ]
        }
    except Exception as e:
        return {"companies": [], "error": str(e)}

@app.get("/api/events")
def events(limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0),
           event_type: str = Query(None)):
    data = load_csv("broker_economic_event_v4.csv")
    if event_type:
        data = [e for e in data if e["event_type"] == event_type]
    return {"total": len(data), "offset": offset, "limit": limit,
            "events": data[offset:offset+limit]}


@app.get("/api/investment-events")
def investment_events(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    min_materiality: int = Query(0, ge=0, le=100),
    event_type: str | None = Query(None),
    symbol: str | None = Query(None),
):
    """查询 NAS 长期重大事件索引；接口只读。"""
    store = InvestmentEventStore(data_root=ROOT / "data")
    return store.list_events(
        limit=limit,
        offset=offset,
        min_materiality=min_materiality,
        event_type=event_type,
        symbol=symbol,
    )


@app.get("/api/investment-events/{event_id}")
def investment_event_detail(event_id: str):
    store = InvestmentEventStore(data_root=ROOT / "data")
    result = store.get_event(event_id)
    if result is None:
        raise HTTPException(status_code=404, detail="investment event not found")
    return result


@app.get("/api/research/institutions")
def research_institutions():
    """返回机构样本量、可靠性得分、证据权重及人工治理状态。"""
    rows = ResearchReportStore(data_root=ROOT / "data").list_institutions()
    return {"total": len(rows), "institutions": rows}


@app.get("/api/research/reports")
def research_reports(
    institution_id: str | None = Query(None),
    symbol: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
):
    rows = ResearchReportStore(data_root=ROOT / "data").list_reports(
        institution_id=institution_id,
        symbol=symbol,
        limit=limit,
    )
    return {"total": len(rows), "reports": rows}


@app.get("/api/chain")
def chain():
    return {"entries": load_csv("broker_balance_chain_v4.csv")}

@app.get("/api/positions")
def positions():
    eng = get_engine()
    snap = eng["state"].snapshot(eng["state"].summary()["verification"]["snapshot_date"])
    return snap

# ── Dashboard HTML ──────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard():
    html = (
        Path(__file__).parent / "static" / "dashboard.html"
    ).read_text(encoding="utf-8")
    return HTMLResponse(html)
