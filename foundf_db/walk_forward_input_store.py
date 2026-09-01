"""Walk-Forward 历史输入资产的不可变归档与 fail-closed 就绪度检查。

输入 manifest、历史成分来源文件和价格口径证明先进入 immutable raw layer；
DuckDB 只保存哈希索引。价格文件已归档、仓库行情覆盖完整、总回报口径经人工批准
是三项独立条件，任一缺失都不能进入 Walk-Forward 研究。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from .event_store import resolve_data_root
from .warehouse import Warehouse


SCHEMA_VERSION = "foundf.walk_forward_inputs.v1"
# v2 合同（真 PIT 宇宙重建 Phase 3）：memberships 语义不变，价格序列覆盖要求
# 从「全 research 区间覆盖」放宽为「覆盖该 symbol 自身 membership 区间 ∩
# research 区间」，从而装得下中途退市的标的；基准序列仍要求全区间覆盖。
SCHEMA_VERSION_V2 = "foundf/walk_forward_inputs/v2"
READINESS_SCHEMA = "foundf.walk_forward_input_readiness.v1"
TOTAL_RETURN_BASIS = "TOTAL_RETURN_ADJUSTED"
ALLOWED_PRICE_FIELDS = {"close", "close_x_adj_factor"}
ALLOWED_ADJUSTMENT_METHODS = {
    "VENDOR_TOTAL_RETURN_SERIES",
    "RAW_PRICE_PLUS_VENDOR_ADJ_FACTOR",
}


class WalkForwardInputError(ValueError):
    """Stable validation failure that is safe to expose in governance."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(code if not detail else f"{code}: {detail}")


def _required(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WalkForwardInputError(code)
    return value.strip()


def _iso_date(value: Any, code: str) -> date:
    try:
        return date.fromisoformat(_required(value, code))
    except ValueError as exc:
        raise WalkForwardInputError(code) from exc


def _timestamp(value: Any, code: str) -> str:
    raw = _required(value, code).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise WalkForwardInputError(code) from exc
    if parsed.tzinfo is None:
        raise WalkForwardInputError(code)
    return parsed.astimezone(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest(value: Any, code: str) -> str:
    raw = _required(value, code).lower()
    if len(raw) != 64 or any(char not in "0123456789abcdef" for char in raw):
        raise WalkForwardInputError(code)
    return raw


def _positive_integer(value: Any, code: str) -> int:
    if isinstance(value, bool):
        raise WalkForwardInputError(code)
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise WalkForwardInputError(code) from exc
    if result <= 0 or result != value:
        raise WalkForwardInputError(code)
    return result


def _relative_path(value: Any, code: str) -> str:
    raw = _required(value, code)
    if "\\" in raw:
        raise WalkForwardInputError(code)
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise WalkForwardInputError(code)
    canonical = path.as_posix()
    if canonical != raw:
        raise WalkForwardInputError(code)
    return canonical


def _artifact(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WalkForwardInputError(code)
    return {
        "path": _relative_path(value.get("path"), f"{code}_PATH_INVALID"),
        "sha256": _digest(value.get("sha256"), f"{code}_SHA256_INVALID"),
        "size_bytes": _positive_integer(
            value.get("size_bytes"), f"{code}_SIZE_INVALID"
        ),
    }


def canonical_price_hash(rows: Sequence[Mapping[str, Any]]) -> str:
    """Hash the exact warehouse fields consumed by the research engine."""

    canonical: list[dict[str, Any]] = []
    for row in rows:
        day = row.get("date")
        if isinstance(day, datetime):
            day = day.date()
        if isinstance(day, date):
            day = day.isoformat()
        values: dict[str, Any] = {
            "date": str(day),
            "symbol": str(row.get("symbol") or ""),
            "open": row.get("open"),
            "close": row.get("close"),
            "adj_factor": row.get("adj_factor"),
            "source": str(row.get("source") or ""),
        }
        for field in ("open", "close", "adj_factor"):
            if values[field] is not None:
                number = float(values[field])
                if not math.isfinite(number):
                    raise WalkForwardInputError("WAREHOUSE_PRICE_NONFINITE")
                values[field] = number
        canonical.append(values)
    canonical.sort(key=lambda item: (item["symbol"], item["date"]))
    payload = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_sources(value: Any) -> set[str]:
    if not isinstance(value, list) or not value:
        raise WalkForwardInputError("SOURCES_MISSING")
    seen: set[str] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise WalkForwardInputError("SOURCE_INVALID", str(index))
        source_id = _required(raw.get("source_id"), "SOURCE_ID_MISSING")
        if source_id in seen:
            raise WalkForwardInputError("SOURCE_ID_DUPLICATE", source_id)
        seen.add(source_id)
        _required(raw.get("provider"), "SOURCE_PROVIDER_MISSING")
        _required(raw.get("dataset_version"), "SOURCE_VERSION_MISSING")
        _timestamp(raw.get("retrieved_at"), "SOURCE_RETRIEVED_AT_INVALID")
        _required(raw.get("license_reference"), "SOURCE_LICENSE_MISSING")
        if raw.get("storage_permitted") is not True:
            raise WalkForwardInputError("SOURCE_STORAGE_PERMISSION_MISSING")
    return seen


def _validate_memberships(
    value: Any, source_ids: set[str], research_start: date, research_end: date
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise WalkForwardInputError("MEMBERSHIPS_MISSING")
    output: list[dict[str, Any]] = []
    by_symbol: dict[str, list[tuple[date, date]]] = {}
    for raw in value:
        if not isinstance(raw, Mapping):
            raise WalkForwardInputError("MEMBERSHIP_INVALID")
        symbol = _required(raw.get("symbol"), "MEMBERSHIP_SYMBOL_MISSING")
        start = _iso_date(raw.get("effective_from"), "MEMBERSHIP_START_INVALID")
        end = (
            _iso_date(raw.get("effective_to"), "MEMBERSHIP_END_INVALID")
            if raw.get("effective_to") is not None
            else research_end
        )
        if end < start:
            raise WalkForwardInputError("MEMBERSHIP_RANGE_INVALID", symbol)
        source_id = _required(raw.get("source_id"), "MEMBERSHIP_SOURCE_MISSING")
        if source_id not in source_ids:
            raise WalkForwardInputError("MEMBERSHIP_SOURCE_UNKNOWN", source_id)
        intervals = by_symbol.setdefault(symbol, [])
        clipped_start, clipped_end = max(start, research_start), min(end, research_end)
        if clipped_start <= clipped_end:
            if any(clipped_start <= old_end and old_start <= clipped_end for old_start, old_end in intervals):
                raise WalkForwardInputError("MEMBERSHIP_INTERVAL_OVERLAP", symbol)
            intervals.append((clipped_start, clipped_end))
        output.append(
            {
                "symbol": symbol,
                "effective_from": start.isoformat(),
                "effective_to": (
                    raw.get("effective_to") if raw.get("effective_to") is not None else None
                ),
                "source_id": source_id,
            }
        )
    return sorted(output, key=lambda item: (item["symbol"], item["effective_from"]))


def _validate_price(
    raw: Any, source_ids: set[str], code: str, *, benchmark: bool
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise WalkForwardInputError(code)
    symbol = _required(raw.get("symbol"), f"{code}_SYMBOL_MISSING")
    source_id = _required(raw.get("source_id"), f"{code}_SOURCE_MISSING")
    if source_id not in source_ids:
        raise WalkForwardInputError(f"{code}_SOURCE_UNKNOWN", source_id)
    basis = _required(raw.get("basis"), f"{code}_BASIS_MISSING")
    if basis != TOTAL_RETURN_BASIS:
        raise WalkForwardInputError("TOTAL_RETURN_ADJUSTMENT_NOT_PROVEN", symbol)
    field = _required(raw.get("price_field"), f"{code}_FIELD_MISSING")
    if field not in ALLOWED_PRICE_FIELDS:
        raise WalkForwardInputError("PRICE_FIELD_UNSUPPORTED", symbol)
    method = _required(
        raw.get("adjustment_method"), f"{code}_ADJUSTMENT_METHOD_MISSING"
    )
    if method not in ALLOWED_ADJUSTMENT_METHODS:
        raise WalkForwardInputError("ADJUSTMENT_METHOD_UNSUPPORTED", symbol)
    if (
        field == "close_x_adj_factor"
        and method != "RAW_PRICE_PLUS_VENDOR_ADJ_FACTOR"
    ) or (
        field == "close"
        and method != "VENDOR_TOTAL_RETURN_SERIES"
    ):
        raise WalkForwardInputError("PRICE_FIELD_METHOD_INCONSISTENT", symbol)
    start = _iso_date(raw.get("data_start"), f"{code}_START_INVALID")
    end = _iso_date(raw.get("data_end"), f"{code}_END_INVALID")
    if end < start:
        raise WalkForwardInputError(f"{code}_RANGE_INVALID", symbol)
    first_session = _iso_date(
        raw.get("first_session"), f"{code}_FIRST_SESSION_INVALID"
    )
    last_session = _iso_date(
        raw.get("last_session"), f"{code}_LAST_SESSION_INVALID"
    )
    if not start <= first_session <= last_session <= end:
        raise WalkForwardInputError(f"{code}_SESSION_RANGE_INVALID", symbol)
    result = {
        "symbol": symbol,
        "is_benchmark": benchmark,
        "benchmark_id": (
            _required(raw.get("benchmark_id"), "BENCHMARK_ID_MISSING")
            if benchmark
            else None
        ),
        "basis": basis,
        "price_field": field,
        "adjustment_method": method,
        "source_id": source_id,
        "warehouse_source": _required(
            raw.get("warehouse_source"), f"{code}_WAREHOUSE_SOURCE_MISSING"
        ),
        "data_start": start.isoformat(),
        "data_end": end.isoformat(),
        "first_session": first_session.isoformat(),
        "last_session": last_session.isoformat(),
        "expected_row_count": _positive_integer(
            raw.get("expected_row_count"), f"{code}_ROW_COUNT_INVALID"
        ),
        "warehouse_sha256": _digest(
            raw.get("warehouse_sha256"), f"{code}_WAREHOUSE_SHA256_INVALID"
        ),
        "artifact": _artifact(raw.get("artifact"), f"{code}_ARTIFACT_INVALID"),
    }
    return result


def validate_manifest(raw: Any) -> dict[str, Any]:
    """Validate and normalize one input manifest without touching storage.

    按 schema_version 分派：v1 走原全覆盖校验（存量 bundle 行为不变），
    v2 走「membership 区间 ∩ research 区间」的逐 symbol 覆盖校验。
    """

    if not isinstance(raw, Mapping):
        raise WalkForwardInputError("INPUT_SCHEMA_UNSUPPORTED")
    schema = raw.get("schema_version")
    if schema == SCHEMA_VERSION:
        return _validate_manifest_v1(raw)
    if schema == SCHEMA_VERSION_V2:
        return _validate_manifest_v2(raw)
    raise WalkForwardInputError("INPUT_SCHEMA_UNSUPPORTED")


def _validate_manifest_common(
    raw: Mapping[str, Any], schema_version: str
) -> dict[str, Any]:
    """v1/v2 共用的字段校验与规范化；覆盖要求由调用方各自追加。"""

    generated_at = _timestamp(raw.get("generated_at"), "GENERATED_AT_INVALID")
    as_of = _iso_date(raw.get("as_of"), "AS_OF_INVALID")
    research_start = _iso_date(raw.get("research_start"), "RESEARCH_START_INVALID")
    research_end = _iso_date(raw.get("research_end"), "RESEARCH_END_INVALID")
    if research_end < research_start or as_of < research_end:
        raise WalkForwardInputError("RESEARCH_RANGE_INVALID")
    source_ids = _validate_sources(raw.get("sources"))
    memberships = _validate_memberships(
        raw.get("memberships"), source_ids, research_start, research_end
    )
    prices_raw = raw.get("price_series")
    if not isinstance(prices_raw, list) or not prices_raw:
        raise WalkForwardInputError("PRICE_SERIES_MISSING")
    prices = [
        _validate_price(item, source_ids, "PRICE", benchmark=False)
        for item in prices_raw
    ]
    benchmark = _validate_price(
        raw.get("benchmark"), source_ids, "BENCHMARK", benchmark=True
    )
    symbols = [item["symbol"] for item in prices]
    if len(set(symbols)) != len(symbols) or benchmark["symbol"] in set(symbols):
        raise WalkForwardInputError("PRICE_SYMBOL_DUPLICATE")
    member_symbols = {item["symbol"] for item in memberships}
    missing = member_symbols - set(symbols)
    extra = set(symbols) - member_symbols
    if missing:
        raise WalkForwardInputError("MEMBER_PRICE_EVIDENCE_MISSING", ",".join(sorted(missing)))
    if extra:
        raise WalkForwardInputError("PRICE_EVIDENCE_WITHOUT_MEMBER", ",".join(sorted(extra)))
    return {
        "schema_version": schema_version,
        "dataset_id": _required(raw.get("dataset_id"), "DATASET_ID_MISSING"),
        "universe_id": _required(raw.get("universe_id"), "UNIVERSE_ID_MISSING"),
        "generated_at": generated_at,
        "as_of": as_of.isoformat(),
        "research_start": research_start.isoformat(),
        "research_end": research_end.isoformat(),
        "sources": list(raw["sources"]),
        "membership_artifact": _artifact(
            raw.get("membership_artifact"), "MEMBERSHIP_ARTIFACT_INVALID"
        ),
        "memberships": memberships,
        "price_series": prices,
        "benchmark": benchmark,
    }


def _validate_manifest_v1(raw: Mapping[str, Any]) -> dict[str, Any]:
    """v1：每条价格序列（含基准）都必须覆盖全 research 区间。"""

    manifest = _validate_manifest_common(raw, SCHEMA_VERSION)
    research_start = _iso_date(manifest["research_start"], "RESEARCH_START_INVALID")
    research_end = _iso_date(manifest["research_end"], "RESEARCH_END_INVALID")
    for item in [*manifest["price_series"], manifest["benchmark"]]:
        if (
            _iso_date(item["data_start"], "PRICE_START_INVALID") > research_start
            or _iso_date(item["data_end"], "PRICE_END_INVALID") < research_end
        ):
            raise WalkForwardInputError("PRICE_DECLARED_COVERAGE_INSUFFICIENT", item["symbol"])
    return manifest


def _validate_manifest_v2(raw: Mapping[str, Any]) -> dict[str, Any]:
    """v2：逐 symbol 覆盖「自身 membership 区间 ∩ research 区间」。

    退市标的只需覆盖到 min(effective_to, research_end)，晚上市标的只需从
    max(effective_from, research_start) 起覆盖；同一 symbol 多段 membership
    （退市再上市）逐段都要求覆盖。基准序列仍要求覆盖全 research 区间。
    """

    manifest = _validate_manifest_common(raw, SCHEMA_VERSION_V2)
    research_start = _iso_date(manifest["research_start"], "RESEARCH_START_INVALID")
    research_end = _iso_date(manifest["research_end"], "RESEARCH_END_INVALID")
    member_intervals: dict[str, list[tuple[date, date]]] = {}
    for item in manifest["memberships"]:
        start = _iso_date(item["effective_from"], "MEMBERSHIP_START_INVALID")
        end = (
            _iso_date(item["effective_to"], "MEMBERSHIP_END_INVALID")
            if item["effective_to"] is not None
            else research_end
        )
        clipped_start, clipped_end = max(start, research_start), min(end, research_end)
        if clipped_start <= clipped_end:
            member_intervals.setdefault(item["symbol"], []).append(
                (clipped_start, clipped_end)
            )
    for item in manifest["price_series"]:
        data_start = _iso_date(item["data_start"], "PRICE_START_INVALID")
        data_end = _iso_date(item["data_end"], "PRICE_END_INVALID")
        for required_start, required_end in member_intervals.get(
            item["symbol"], []
        ):
            if data_start > required_start or data_end < required_end:
                raise WalkForwardInputError(
                    "PRICE_DECLARED_COVERAGE_INSUFFICIENT", item["symbol"]
                )
    benchmark = manifest["benchmark"]
    if (
        _iso_date(benchmark["data_start"], "PRICE_START_INVALID") > research_start
        or _iso_date(benchmark["data_end"], "PRICE_END_INVALID") < research_end
    ):
        raise WalkForwardInputError(
            "PRICE_DECLARED_COVERAGE_INSUFFICIENT", benchmark["symbol"]
        )
    return manifest


def _declared_artifacts(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    artifacts = [manifest["membership_artifact"]] + [
        item["artifact"] for item in [*manifest["price_series"], manifest["benchmark"]]
    ]
    output: dict[str, dict[str, Any]] = {}
    for item in artifacts:
        prior = output.get(item["path"])
        if prior is not None and prior != item:
            raise WalkForwardInputError("ARTIFACT_DECLARATION_CONFLICT", item["path"])
        output[item["path"]] = item
    return output


def _verify_source_tree(root: Path, manifest_name: str, artifacts: Mapping[str, Mapping[str, Any]]) -> None:
    expected = set(artifacts)
    observed: set[str] = set()
    for item in root.rglob("*"):
        if item.is_symlink():
            raise WalkForwardInputError("ARTIFACT_SYMLINK_FORBIDDEN")
        if item.is_file():
            relative = item.relative_to(root).as_posix()
            if relative != manifest_name:
                observed.add(relative)
    if observed != expected:
        missing = sorted(expected - observed)
        extra = sorted(observed - expected)
        raise WalkForwardInputError("ARTIFACT_SET_MISMATCH", f"missing={missing},extra={extra}")
    for relative, declaration in artifacts.items():
        path = root / relative
        if not path.is_file() or path.stat().st_size != declaration["size_bytes"]:
            raise WalkForwardInputError("ARTIFACT_SIZE_MISMATCH", relative)
        if _sha256(path) != declaration["sha256"]:
            raise WalkForwardInputError("ARTIFACT_HASH_MISMATCH", relative)


class WalkForwardInputStore:
    """Immutable raw archive plus queryable DuckDB indices."""

    def __init__(
        self,
        *,
        data_root: str | Path | None = None,
        db_path: str | Path | None = None,
    ) -> None:
        self.data_root = resolve_data_root(data_root)
        self.db_path = Path(
            db_path or os.getenv("DUCKDB_PATH", "") or self.data_root / "finance.duckdb"
        )
        self.archive_root = self.data_root / "raw" / "walk_forward_inputs"

    def archive_bundle(self, manifest_path: str | Path) -> dict[str, Any]:
        source_manifest = Path(manifest_path)
        if not source_manifest.is_file() or source_manifest.is_symlink():
            raise FileNotFoundError(source_manifest)
        try:
            raw = json.loads(source_manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise WalkForwardInputError("MANIFEST_JSON_INVALID") from exc
        manifest = validate_manifest(raw)
        artifacts = _declared_artifacts(manifest)
        _verify_source_tree(source_manifest.parent, source_manifest.name, artifacts)
        manifest_hash = _sha256(source_manifest)
        bundle_id = f"wfi_{manifest_hash[:20]}"
        destination = self.archive_root / bundle_id
        received_at = datetime.now(timezone.utc)

        with Warehouse(self.db_path) as warehouse:
            warehouse.init()
            existing = warehouse.query(
                "SELECT bundle_id FROM walk_forward_input_bundle WHERE manifest_sha256 = ?",
                [manifest_hash],
            )
            if existing:
                verification = self.verify_bundle(existing[0]["bundle_id"])
                return {**verification, "status": "EXISTS"}

        self.archive_root.mkdir(parents=True, exist_ok=True)
        building = self.archive_root / f".{bundle_id}.building"
        if destination.exists() or building.exists():
            raise FileExistsError(destination)
        building.mkdir(mode=0o750)
        try:
            shutil.copyfile(source_manifest, building / "manifest.json")
            for relative in artifacts:
                target = building / relative
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
                shutil.copyfile(source_manifest.parent / relative, target)
            _verify_source_tree(building, "manifest.json", artifacts)
            for item in building.rglob("*"):
                item.chmod(0o750 if item.is_dir() else 0o440)
            os.replace(building, destination)

            price_rows = []
            for item in [*manifest["price_series"], manifest["benchmark"]]:
                price_rows.append(
                    {
                        "bundle_id": bundle_id,
                        "symbol": item["symbol"],
                        "is_benchmark": item["is_benchmark"],
                        "benchmark_id": item["benchmark_id"],
                        "basis": item["basis"],
                        "price_field": item["price_field"],
                        "adjustment_method": item["adjustment_method"],
                        "source_id": item["source_id"],
                        "warehouse_source": item["warehouse_source"],
                        "data_start": item["data_start"],
                        "data_end": item["data_end"],
                        "first_session": item["first_session"],
                        "last_session": item["last_session"],
                        "expected_row_count": item["expected_row_count"],
                        "warehouse_sha256": item["warehouse_sha256"],
                        "artifact_relpath": (
                            destination.relative_to(self.data_root) / item["artifact"]["path"]
                        ).as_posix(),
                        "artifact_sha256": item["artifact"]["sha256"],
                        "artifact_size_bytes": item["artifact"]["size_bytes"],
                    }
                )
            with Warehouse(self.db_path) as warehouse:
                warehouse.init()
                warehouse.execute("BEGIN TRANSACTION")
                try:
                    warehouse.insert(
                        "walk_forward_input_bundle",
                        [{
                            "bundle_id": bundle_id,
                            "dataset_id": manifest["dataset_id"],
                            "universe_id": manifest["universe_id"],
                            "generated_at": manifest["generated_at"],
                            "as_of": manifest["as_of"],
                            "research_start": manifest["research_start"],
                            "research_end": manifest["research_end"],
                            "received_at": received_at.isoformat(),
                            "manifest_sha256": manifest_hash,
                            "archive_relpath": destination.relative_to(self.data_root).as_posix(),
                            "membership_count": len(manifest["memberships"]),
                            "distinct_symbol_count": len({item["symbol"] for item in manifest["memberships"]}),
                            "price_series_count": len(manifest["price_series"]),
                            "benchmark_symbol": manifest["benchmark"]["symbol"],
                            "archive_status": "HASH_VERIFIED_RAW_ONLY",
                        }],
                    )
                    warehouse.insert(
                        "walk_forward_universe_membership",
                        [{"bundle_id": bundle_id, **item} for item in manifest["memberships"]],
                    )
                    warehouse.insert("walk_forward_price_evidence", price_rows)
                    warehouse.execute("COMMIT")
                except Exception:
                    warehouse.execute("ROLLBACK")
                    raise
        except Exception:
            shutil.rmtree(building, ignore_errors=True)
            shutil.rmtree(destination, ignore_errors=True)
            raise
        return {
            "schema_version": manifest["schema_version"],
            "status": "ARCHIVED_HASH_VERIFIED_RAW_ONLY",
            "bundle_id": bundle_id,
            "manifest_sha256": manifest_hash,
            "distinct_symbol_count": len({item["symbol"] for item in manifest["memberships"]}),
            "basis_approved": False,
            "production_change_allowed": False,
            "automatic_trade_allowed": False,
        }

    def verify_bundle(self, bundle_id: str) -> dict[str, Any]:
        identity = _required(bundle_id, "BUNDLE_ID_MISSING")
        if not self.db_path.is_file():
            raise WalkForwardInputError("BUNDLE_NOT_FOUND")
        with Warehouse(self.db_path) as warehouse:
            if not warehouse.table_exists("walk_forward_input_bundle"):
                raise WalkForwardInputError("BUNDLE_NOT_FOUND")
            rows = warehouse.query(
                "SELECT * FROM walk_forward_input_bundle WHERE bundle_id = ?", [identity]
            )
            evidence_rows = warehouse.query(
                "SELECT * FROM walk_forward_price_evidence WHERE bundle_id = ? ORDER BY symbol",
                [identity],
            )
            membership_rows = warehouse.query(
                "SELECT symbol, effective_from, effective_to, source_id "
                "FROM walk_forward_universe_membership WHERE bundle_id = ? "
                "ORDER BY symbol, effective_from",
                [identity],
            )
        if not rows:
            raise WalkForwardInputError("BUNDLE_NOT_FOUND")
        record = rows[0]
        archive = self.data_root / record["archive_relpath"]
        manifest_path = archive / "manifest.json"
        if archive.is_symlink() or not manifest_path.is_file() or manifest_path.is_symlink():
            raise WalkForwardInputError("ARCHIVE_MANIFEST_MISSING")
        if _sha256(manifest_path) != record["manifest_sha256"]:
            raise WalkForwardInputError("ARCHIVE_MANIFEST_HASH_MISMATCH")
        try:
            manifest = validate_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            raise WalkForwardInputError("ARCHIVE_MANIFEST_INVALID") from exc
        artifacts = _declared_artifacts(manifest)
        _verify_source_tree(archive, "manifest.json", artifacts)
        expected_bundle = {
            "dataset_id": manifest["dataset_id"],
            "universe_id": manifest["universe_id"],
            "generated_at": manifest["generated_at"],
            "as_of": manifest["as_of"],
            "research_start": manifest["research_start"],
            "research_end": manifest["research_end"],
            "membership_count": len(manifest["memberships"]),
            "distinct_symbol_count": len(
                {item["symbol"] for item in manifest["memberships"]}
            ),
            "price_series_count": len(manifest["price_series"]),
            "benchmark_symbol": manifest["benchmark"]["symbol"],
            "archive_status": "HASH_VERIFIED_RAW_ONLY",
        }

        def comparable(value: Any) -> Any:
            if isinstance(value, datetime):
                return value.astimezone(timezone.utc).isoformat()
            if isinstance(value, date):
                return value.isoformat()
            return value

        if any(
            comparable(record.get(key)) != expected
            for key, expected in expected_bundle.items()
        ):
            raise WalkForwardInputError("ARCHIVE_INDEX_INCONSISTENT")
        observed_memberships = [
            {key: comparable(row[key]) for key in ("symbol", "effective_from", "effective_to", "source_id")}
            for row in membership_rows
        ]
        if observed_memberships != manifest["memberships"]:
            raise WalkForwardInputError("ARCHIVE_MEMBERSHIP_INDEX_INCONSISTENT")
        manifest_price = {
            item["symbol"]: item
            for item in [*manifest["price_series"], manifest["benchmark"]]
        }
        if set(manifest_price) != {row["symbol"] for row in evidence_rows}:
            raise WalkForwardInputError("ARCHIVE_PRICE_INDEX_INCONSISTENT")
        for row in evidence_rows:
            expected = manifest_price[row["symbol"]]
            expected_values = {
                "is_benchmark": expected["is_benchmark"],
                "benchmark_id": expected["benchmark_id"],
                "basis": expected["basis"],
                "price_field": expected["price_field"],
                "adjustment_method": expected["adjustment_method"],
                "source_id": expected["source_id"],
                "warehouse_source": expected["warehouse_source"],
                "data_start": expected["data_start"],
                "data_end": expected["data_end"],
                "first_session": expected["first_session"],
                "last_session": expected["last_session"],
                "expected_row_count": expected["expected_row_count"],
                "warehouse_sha256": expected["warehouse_sha256"],
                "artifact_sha256": expected["artifact"]["sha256"],
                "artifact_size_bytes": expected["artifact"]["size_bytes"],
            }
            if any(
                comparable(row.get(key)) != value
                for key, value in expected_values.items()
            ):
                raise WalkForwardInputError("ARCHIVE_PRICE_INDEX_INCONSISTENT")
        return {
            "schema_version": manifest["schema_version"],
            "status": "HASH_VERIFIED_RAW_ONLY",
            "bundle_id": identity,
            "manifest_sha256": record["manifest_sha256"],
            "distinct_symbol_count": record["distinct_symbol_count"],
            "production_change_allowed": False,
            "automatic_trade_allowed": False,
        }

    def approve_total_return_basis(
        self,
        bundle_id: str,
        *,
        reviewer: str,
        confirmation_reference: str,
    ) -> dict[str, Any]:
        """Record a separate human review; never implies strategy/trading approval."""

        verified = self.verify_bundle(bundle_id)
        approved_at = datetime.now(timezone.utc).isoformat()
        row = {
            "bundle_id": verified["bundle_id"],
            "manifest_sha256": verified["manifest_sha256"],
            "reviewer": _required(reviewer, "REVIEWER_MISSING"),
            "confirmation_reference": _required(
                confirmation_reference, "CONFIRMATION_REFERENCE_MISSING"
            ),
            "approved_at": approved_at,
            "decision": "APPROVED",
        }
        with Warehouse(self.db_path) as warehouse:
            warehouse.init()
            existing = warehouse.query(
                "SELECT * FROM walk_forward_basis_approval WHERE bundle_id = ?",
                [bundle_id],
            )
            if existing:
                if any(existing[0][key] != value for key, value in row.items() if key != "approved_at"):
                    raise WalkForwardInputError("BASIS_APPROVAL_CONFLICT")
                return {
                    "status": "EXISTS",
                    "bundle_id": bundle_id,
                    "basis_approved": True,
                    "production_change_allowed": False,
                    "automatic_trade_allowed": False,
                }
            warehouse.insert("walk_forward_basis_approval", [row])
        return {
            "status": "BASIS_APPROVED_FOR_RESEARCH_ONLY",
            "bundle_id": bundle_id,
            "basis_approved": True,
            "production_change_allowed": False,
            "automatic_trade_allowed": False,
        }

    def readiness(self, *, minimum_universe_size: int = 100) -> dict[str, Any]:
        """Evaluate the latest archived bundle against current warehouse rows."""

        base = {
            "schema_version": READINESS_SCHEMA,
            "status": "MISSING",
            "bundle_id": None,
            "minimum_universe_size": minimum_universe_size,
            "distinct_symbol_count": 0,
            "minimum_active_universe": 0,
            "series_expected": 0,
            "series_verified": 0,
            "basis_approved": False,
            "blockers": ["WALK_FORWARD_INPUT_BUNDLE_MISSING"],
            "production_change_allowed": False,
            "automatic_trade_allowed": False,
        }
        if not self.db_path.is_file():
            return base
        with Warehouse(self.db_path) as warehouse:
            if not warehouse.table_exists("walk_forward_input_bundle"):
                return base
            bundles = warehouse.query(
                "SELECT * FROM walk_forward_input_bundle ORDER BY received_at DESC, bundle_id DESC LIMIT 1"
            )
        if not bundles:
            return base
        bundle = bundles[0]
        blockers: list[str] = []
        try:
            self.verify_bundle(bundle["bundle_id"])
        except (WalkForwardInputError, OSError):
            blockers.append("WALK_FORWARD_INPUT_ARCHIVE_INVALID")
        with Warehouse(self.db_path) as warehouse:
            memberships = warehouse.query(
                "SELECT symbol, effective_from, effective_to FROM walk_forward_universe_membership "
                "WHERE bundle_id = ? ORDER BY symbol, effective_from",
                [bundle["bundle_id"]],
            )
            series = warehouse.query(
                "SELECT * FROM walk_forward_price_evidence WHERE bundle_id = ? ORDER BY symbol",
                [bundle["bundle_id"]],
            )
            approvals = warehouse.query(
                "SELECT manifest_sha256 FROM walk_forward_basis_approval "
                "WHERE bundle_id = ? AND decision = 'APPROVED'",
                [bundle["bundle_id"]],
            )
            basis_approved = bool(
                approvals and approvals[0]["manifest_sha256"] == bundle["manifest_sha256"]
            )
            if not basis_approved:
                blockers.append("TOTAL_RETURN_BASIS_APPROVAL_MISSING")

            research_start = bundle["research_start"]
            research_end = bundle["research_end"]
            candidates = {research_start}
            for item in memberships:
                start = item["effective_from"]
                end = item["effective_to"]
                if research_start <= start <= research_end:
                    candidates.add(start)
                if end is not None and research_start <= end < research_end:
                    candidates.add(end + timedelta(days=1))
            active_counts = [
                len(
                    {
                        item["symbol"]
                        for item in memberships
                        if item["effective_from"] <= point
                        and (item["effective_to"] is None or item["effective_to"] >= point)
                    }
                )
                for point in candidates
            ]
            minimum_active = min(active_counts, default=0)
            if minimum_active < minimum_universe_size:
                blockers.append("POINT_IN_TIME_UNIVERSE_TOO_SMALL")

            verified = 0
            for item in series:
                rows = warehouse.query(
                    "SELECT date, symbol, open, close, adj_factor, source FROM daily_price "
                    "WHERE symbol = ? AND date >= ? AND date <= ? ORDER BY date",
                    [item["symbol"], item["data_start"], item["data_end"]],
                )
                if len(rows) != item["expected_row_count"]:
                    blockers.append(f"WAREHOUSE_ROW_COUNT_MISMATCH:{item['symbol']}")
                    continue
                if (
                    not rows
                    or rows[0]["date"] != item["first_session"]
                    or rows[-1]["date"] != item["last_session"]
                ):
                    blockers.append(f"WAREHOUSE_DATE_COVERAGE_MISMATCH:{item['symbol']}")
                    continue
                if any(row["source"] != item["warehouse_source"] for row in rows):
                    blockers.append(f"WAREHOUSE_SOURCE_MISMATCH:{item['symbol']}")
                    continue
                if item["price_field"] == "close_x_adj_factor" and any(
                    row["adj_factor"] is None for row in rows
                ):
                    blockers.append(f"WAREHOUSE_ADJ_FACTOR_MISSING:{item['symbol']}")
                    continue
                if canonical_price_hash(rows) != item["warehouse_sha256"]:
                    blockers.append(f"WAREHOUSE_CONTENT_HASH_MISMATCH:{item['symbol']}")
                    continue
                verified += 1

        unique_blockers = list(dict.fromkeys(blockers))
        return {
            **base,
            "status": "READY_FOR_RESEARCH" if not unique_blockers else "NOT_READY",
            "bundle_id": bundle["bundle_id"],
            "manifest_sha256": bundle["manifest_sha256"],
            "dataset_id": bundle["dataset_id"],
            "universe_id": bundle["universe_id"],
            "as_of": str(bundle["as_of"]),
            "research_start": str(bundle["research_start"]),
            "research_end": str(bundle["research_end"]),
            "distinct_symbol_count": bundle["distinct_symbol_count"],
            "minimum_active_universe": minimum_active,
            "series_expected": len(series),
            "series_verified": verified,
            "basis_approved": basis_approved,
            "blockers": unique_blockers,
        }


def inspect_walk_forward_inputs(
    *,
    data_root: str | Path | None = None,
    db_path: str | Path | None = None,
    minimum_universe_size: int = 100,
) -> dict[str, Any]:
    """Read-only convenience entry used by API and governance projections."""

    try:
        return WalkForwardInputStore(
            data_root=data_root, db_path=db_path
        ).readiness(minimum_universe_size=minimum_universe_size)
    except Exception:
        return {
            "schema_version": READINESS_SCHEMA,
            "status": "UNAVAILABLE",
            "bundle_id": None,
            "minimum_universe_size": minimum_universe_size,
            "distinct_symbol_count": 0,
            "minimum_active_universe": 0,
            "series_expected": 0,
            "series_verified": 0,
            "basis_approved": False,
            "blockers": ["WALK_FORWARD_INPUT_STATUS_UNAVAILABLE"],
            "production_change_allowed": False,
            "automatic_trade_allowed": False,
        }
