"""国信模拟盘原始导出的隔离归档。

本模块只保存原始文件、SHA-256 和最小审计元数据。它故意不解析列名、不推断费用、
不生成订单或成交；真实样本到达并完成字段级人工验收前，状态始终是
``PENDING_REAL_SAMPLE`` / ``RAW_ONLY``。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .event_store import resolve_data_root
from .warehouse import Warehouse


EXPORT_KINDS = {"ORDER", "CANCELLATION", "FILL", "FEE", "CASH", "POSITION"}
ACCOUNT_PROOF_SOURCES = {"BROKER_EXPORT", "BROKER_UI_AND_EXPORT"}


def _required(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _timestamp(value: Any, name: str) -> str:
    text = _required(value, name).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class BrokerSimulationArchive:
    """只读导出归档；不包含券商字段映射或交易规范化能力。"""

    def __init__(
        self,
        *,
        data_root: str | Path | None = None,
        db_path: str | Path | None = None,
    ):
        self.data_root = resolve_data_root(data_root)
        self.db_path = Path(
            db_path
            or os.getenv("DUCKDB_PATH", "")
            or self.data_root / "finance.duckdb"
        )
        self.archive_root = self.data_root / "raw" / "broker_simulation_exports"

    def archive_export(
        self,
        source_path: str | Path,
        *,
        export_kind: str,
        exported_at: str,
        account_mode: str,
        account_proof_source: str,
        account_proof_reference: str,
        confirmation_reference: str,
        broker: str = "GUOSEN",
        client_name: str = "GOLDSUN_PC",
    ) -> dict[str, Any]:
        """归档一份经人工确认来自模拟账户的原始导出。

        ``account_proof_reference`` 应指向本地人工核验记录或脱敏截图编号，不应包含
        账号、密码、短信验证码或设备令牌。
        """

        source = Path(source_path)
        if not source.is_file():
            raise FileNotFoundError(source)
        kind = _required(export_kind, "export_kind").upper()
        if kind not in EXPORT_KINDS:
            raise ValueError(f"unsupported export_kind: {kind}")
        mode = _required(account_mode, "account_mode").upper()
        if mode != "SIMULATION":
            raise PermissionError("only explicitly proven SIMULATION exports are accepted")
        proof_source = _required(
            account_proof_source, "account_proof_source"
        ).upper()
        if proof_source not in ACCOUNT_PROOF_SOURCES:
            raise PermissionError(
                "account proof must come from BROKER_EXPORT or BROKER_UI_AND_EXPORT"
            )
        proof_reference = _required(
            account_proof_reference, "account_proof_reference"
        )
        confirmation = _required(
            confirmation_reference, "confirmation_reference"
        )
        exported = _timestamp(exported_at, "exported_at")
        broker_name = _required(broker, "broker").upper()
        client = _required(client_name, "client_name").upper()

        source_hash = _sha256(source)
        artifact_id = f"bsx_{source_hash[:20]}"
        received = datetime.now(timezone.utc)
        destination_dir = (
            self.archive_root
            / f"{received.year:04d}"
            / f"{received.month:02d}"
            / artifact_id
        )
        suffix = "".join(source.suffixes).lower()
        archived_file = destination_dir / f"original{suffix}"
        manifest_file = destination_dir / "manifest.json"
        archive_relpath = destination_dir.relative_to(self.data_root).as_posix()

        warehouse = Warehouse(self.db_path)
        warehouse.init()
        existing = warehouse.query(
            "SELECT artifact_id, export_kind, archive_relpath, mapping_status, "
            "normalization_status FROM broker_sim_export_archive "
            "WHERE source_sha256 = ?",
            [source_hash],
        )
        if existing:
            warehouse.close()
            return {**existing[0], "status": "EXISTS", "source_sha256": source_hash}

        destination_dir.mkdir(parents=True, exist_ok=True)
        if archived_file.exists() or manifest_file.exists():
            warehouse.close()
            raise FileExistsError(destination_dir)
        temp_file = destination_dir / ".original.building"
        try:
            shutil.copyfile(source, temp_file)
            if _sha256(temp_file) != source_hash:
                raise OSError("archived copy hash mismatch")
            os.replace(temp_file, archived_file)
            manifest = {
                "schema_version": "foundf.broker-simulation-export.v1",
                "artifact_id": artifact_id,
                "export_kind": kind,
                "broker": broker_name,
                "client_name": client,
                "exported_at": exported,
                "received_at": received.isoformat(),
                "source_filename": source.name,
                "source_sha256": source_hash,
                "source_size_bytes": source.stat().st_size,
                "account_mode": mode,
                "account_proof_source": proof_source,
                "account_proof_reference": proof_reference,
                "confirmation_reference": confirmation,
                "mapping_status": "PENDING_REAL_SAMPLE",
                "normalization_status": "RAW_ONLY",
                "field_mapping": None,
            }
            with open(manifest_file, "x", encoding="utf-8") as handle:
                json.dump(manifest, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            archived_file.chmod(0o440)
            manifest_file.chmod(0o440)
            warehouse.insert(
                "broker_sim_export_archive",
                [
                    {
                        "artifact_id": artifact_id,
                        "export_kind": kind,
                        "broker": broker_name,
                        "client_name": client,
                        "exported_at": exported,
                        "received_at": received.isoformat(),
                        "source_filename": source.name,
                        "source_sha256": source_hash,
                        "source_size_bytes": source.stat().st_size,
                        "account_mode": mode,
                        "account_proof_reference": proof_reference,
                        "confirmation_reference": confirmation,
                        "archive_relpath": archive_relpath,
                        "mapping_status": "PENDING_REAL_SAMPLE",
                        "normalization_status": "RAW_ONLY",
                    }
                ],
            )
        except Exception:
            temp_file.unlink(missing_ok=True)
            archived_file.unlink(missing_ok=True)
            manifest_file.unlink(missing_ok=True)
            try:
                destination_dir.rmdir()
            except OSError:
                pass
            warehouse.close()
            raise
        warehouse.close()
        return {
            "status": "ARCHIVED_RAW_ONLY",
            "artifact_id": artifact_id,
            "export_kind": kind,
            "source_sha256": source_hash,
            "archive_relpath": archive_relpath,
            "mapping_status": "PENDING_REAL_SAMPLE",
            "normalization_status": "RAW_ONLY",
        }

    def verify_archive(self, artifact_id: str) -> dict[str, Any]:
        """只读复核数据库索引、manifest 与原始文件哈希。"""

        with Warehouse(self.db_path) as warehouse:
            rows = warehouse.query(
                "SELECT * FROM broker_sim_export_archive WHERE artifact_id = ?",
                [_required(artifact_id, "artifact_id")],
            )
        if not rows:
            return {"status": "MISSING_INDEX", "artifact_id": artifact_id}
        row = rows[0]
        directory = self.data_root / row["archive_relpath"]
        manifest_path = directory / "manifest.json"
        originals = [
            path
            for path in directory.iterdir()
            if path.is_file() and path.name.startswith("original")
        ] if directory.exists() else []
        if not manifest_path.is_file() or len(originals) != 1:
            return {"status": "MISSING_ARCHIVE", "artifact_id": artifact_id}
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"status": "INVALID_MANIFEST", "artifact_id": artifact_id}
        actual_hash = _sha256(originals[0])
        expected_hash = row["source_sha256"]
        valid = (
            actual_hash == expected_hash
            and manifest.get("source_sha256") == expected_hash
            and manifest.get("artifact_id") == artifact_id
            and manifest.get("field_mapping") is None
        )
        return {
            "status": "VALID" if valid else "HASH_MISMATCH",
            "artifact_id": artifact_id,
            "source_sha256": actual_hash,
        }
