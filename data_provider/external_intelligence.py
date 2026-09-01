"""外部信息发现、来源证据与可信路由。

搜索结果只负责发现线索，不能直接成为投资事实。是否允许持久化由供应商合同决定，
默认均为禁止；只有显式确认存储权后才允许写入长期数据资产。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol
from urllib.parse import urlparse

import requests


@dataclass(frozen=True)
class ExternalEvidence:
    provider: str
    title: str
    url: str
    snippet: str
    published_at: str | None
    retrieved_at: str
    authority_level: str
    license_mode: str
    storage_allowed: bool
    query: str
    content_hash: str

    @classmethod
    def build(
        cls,
        *,
        provider: str,
        title: str,
        url: str,
        snippet: str,
        published_at: str | None,
        authority_level: str,
        license_mode: str,
        storage_allowed: bool,
        query: str,
    ) -> "ExternalEvidence":
        raw = f"{provider}|{title}|{url}|{snippet}|{published_at}"
        return cls(
            provider=provider,
            title=title,
            url=url,
            snippet=snippet,
            published_at=published_at,
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            authority_level=authority_level,
            license_mode=license_mode,
            storage_allowed=storage_allowed,
            query=query,
            content_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        )


class EvidenceProvider(Protocol):
    name: str

    def available(self) -> bool: ...

    def search(self, query: str, count: int = 10) -> list[ExternalEvidence]: ...


HttpGet = Callable[..., Any]


def _env_true(name: str) -> bool:
    return os.getenv(name, "").strip().lower() == "true"


def external_source_status(
    *,
    investing_import_path: str | Path = "data/import/investing_authorized.json",
) -> dict[str, Any]:
    """返回不含密钥的只读接入状态，供运维和 Dashboard 检查。"""

    investing_path = Path(investing_import_path)
    bloomberg_enabled = _env_true("BLOOMBERG_ENABLED")
    bloomberg_entitled = _env_true("BLOOMBERG_ENTITLEMENT_CONFIRMED")
    try:
        import blpapi  # noqa: F401

        bloomberg_sdk = True
    except ImportError:
        bloomberg_sdk = False

    bloomberg_state = "DISABLED"
    if bloomberg_enabled and not bloomberg_entitled:
        bloomberg_state = "ENTITLEMENT_NOT_CONFIRMED"
    elif bloomberg_enabled and bloomberg_entitled and not bloomberg_sdk:
        bloomberg_state = "SDK_NOT_INSTALLED"
    elif bloomberg_enabled and bloomberg_entitled and bloomberg_sdk:
        bloomberg_state = "READY"

    investing_license = _env_true("INVESTING_EXPORT_LICENSE_CONFIRMED")
    investing_state = "DISABLED"
    if investing_license and not investing_path.exists():
        investing_state = "AUTHORIZED_EXPORT_MISSING"
    elif investing_license and investing_path.exists():
        investing_state = "READY"

    brave_configured = bool(os.getenv("BRAVE_SEARCH_API_KEY", "").strip())
    google_keys = bool(
        os.getenv("GOOGLE_CSE_API_KEY", "").strip()
        and os.getenv("GOOGLE_CSE_ID", "").strip()
    )
    google_existing = _env_true("GOOGLE_CSE_EXISTING_CUSTOMER")
    return {
        "external_intelligence_enabled": _env_true(
            "EXTERNAL_INTELLIGENCE_ENABLED"
        ),
        "policy": {
            "search_results_are_facts": False,
            "independent_corroboration_required": True,
            "default_storage_allowed": False,
        },
        "providers": {
            "bloomberg": {
                "state": bloomberg_state,
                "role": "LICENSED_PRIMARY",
                "storage_allowed": _env_true("BLOOMBERG_STORAGE_ALLOWED"),
            },
            "investing_authorized_import": {
                "state": investing_state,
                "role": "AUTHORIZED_SECONDARY",
                "storage_allowed": investing_license and investing_path.exists(),
            },
            "brave_search": {
                "state": "READY" if brave_configured else "API_KEY_MISSING",
                "role": "DISCOVERY_ONLY",
                "storage_allowed": _env_true("BRAVE_SEARCH_STORAGE_ALLOWED"),
            },
            "google_custom_search": {
                "state": (
                    "READY"
                    if google_keys and google_existing
                    else "LEGACY_CUSTOMER_CONFIRMATION_REQUIRED"
                    if google_keys
                    else "API_KEY_OR_ENGINE_ID_MISSING"
                ),
                "role": "DISCOVERY_ONLY",
                "storage_allowed": False,
                "sunset_date": "2027-01-01",
            },
        },
    }


class BraveSearchProvider:
    """Brave 正式 Search API；默认仅瞬时使用，不持久化搜索结果。"""

    name = "brave_search"
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        storage_allowed: bool | None = None,
        http_get: HttpGet = requests.get,
        timeout: float = 15.0,
    ):
        self.api_key = api_key or os.getenv("BRAVE_SEARCH_API_KEY", "")
        self.storage_allowed = (
            storage_allowed
            if storage_allowed is not None
            else os.getenv("BRAVE_SEARCH_STORAGE_ALLOWED", "").lower() == "true"
        )
        self.http_get = http_get
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.api_key)

    def search(self, query: str, count: int = 10) -> list[ExternalEvidence]:
        if not self.available():
            return []
        response = self.http_get(
            self.endpoint,
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": self.api_key,
            },
            params={
                "q": query,
                "count": min(max(int(count), 1), 20),
                "search_lang": "zh-hans",
                "safesearch": "moderate",
                "freshness": "pm",
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        raw = response.json()
        return [
            ExternalEvidence.build(
                provider=self.name,
                title=str(item.get("title", "")),
                url=str(item.get("url", "")),
                snippet=str(item.get("description", "")),
                published_at=item.get("page_age"),
                authority_level="DISCOVERY_ONLY",
                license_mode=(
                    "SEARCH_API_STORAGE_CONFIRMED"
                    if self.storage_allowed
                    else "TRANSIENT_SEARCH_RESULT"
                ),
                storage_allowed=self.storage_allowed,
                query=query,
            )
            for item in raw.get("web", {}).get("results", [])
            if item.get("url") and item.get("title")
        ]


class GoogleCustomSearchProvider:
    """仅供既有 Google Custom Search 客户迁移期使用。"""

    name = "google_custom_search"
    endpoint = "https://customsearch.googleapis.com/customsearch/v1"

    def __init__(
        self,
        api_key: str | None = None,
        engine_id: str | None = None,
        *,
        existing_customer_confirmed: bool | None = None,
        http_get: HttpGet = requests.get,
        timeout: float = 15.0,
    ):
        self.api_key = api_key or os.getenv("GOOGLE_CSE_API_KEY", "")
        self.engine_id = engine_id or os.getenv("GOOGLE_CSE_ID", "")
        self.existing_customer_confirmed = (
            existing_customer_confirmed
            if existing_customer_confirmed is not None
            else os.getenv("GOOGLE_CSE_EXISTING_CUSTOMER", "").lower() == "true"
        )
        self.http_get = http_get
        self.timeout = timeout

    def available(self) -> bool:
        return bool(
            self.api_key and self.engine_id and self.existing_customer_confirmed
        )

    def search(self, query: str, count: int = 10) -> list[ExternalEvidence]:
        if not self.available():
            return []
        response = self.http_get(
            self.endpoint,
            params={
                "key": self.api_key,
                "cx": self.engine_id,
                "q": query,
                "num": min(max(int(count), 1), 10),
                "dateRestrict": "m1",
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        return [
            ExternalEvidence.build(
                provider=self.name,
                title=str(item.get("title", "")),
                url=str(item.get("link", "")),
                snippet=str(item.get("snippet", "")),
                published_at=None,
                authority_level="DISCOVERY_ONLY",
                license_mode="LEGACY_API_TRANSIENT",
                storage_allowed=False,
                query=query,
            )
            for item in response.json().get("items", [])
            if item.get("link") and item.get("title")
        ]


class InvestingAuthorizedImportProvider:
    """读取用户从英为财情授权导出的 JSON；严禁默认网页抓取。"""

    name = "investing_authorized_import"

    def __init__(
        self,
        path: str | Path = "data/import/investing_authorized.json",
        *,
        license_confirmed: bool | None = None,
    ):
        self.path = Path(path)
        self.license_confirmed = (
            license_confirmed
            if license_confirmed is not None
            else os.getenv("INVESTING_EXPORT_LICENSE_CONFIRMED", "").lower()
            == "true"
        )

    def available(self) -> bool:
        return self.license_confirmed and self.path.exists()

    def search(self, query: str, count: int = 10) -> list[ExternalEvidence]:
        if not self.available():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if raw.get("schema_version") != "foundf.investing_authorized.v1":
            raise ValueError("invalid Investing.com authorized export schema")
        if not raw.get("license_reference"):
            raise ValueError("license_reference is required")
        query_lower = query.lower()
        results = []
        for item in raw.get("items", []):
            url = str(item.get("url", ""))
            domain = urlparse(url).hostname or ""
            if not domain.endswith("investing.com"):
                continue
            haystack = f"{item.get('title', '')} {item.get('snippet', '')}".lower()
            if query_lower and query_lower not in haystack:
                continue
            results.append(
                ExternalEvidence.build(
                    provider=self.name,
                    title=str(item.get("title", "")),
                    url=url,
                    snippet=str(item.get("snippet", "")),
                    published_at=item.get("published_at"),
                    authority_level="AUTHORIZED_SECONDARY",
                    license_mode="USER_AUTHORIZED_EXPORT",
                    storage_allowed=True,
                    query=query,
                )
            )
        return results[: max(1, int(count))]


def assess_evidence(items: Iterable[ExternalEvidence]) -> dict[str, Any]:
    """评估来源独立性；搜索摘要永远不能单独成为决策事实。"""

    evidence = list(items)
    domains = {
        (urlparse(item.url).hostname or "").removeprefix("www.")
        for item in evidence
        if item.url
    }
    authoritative = [
        item
        for item in evidence
        if item.authority_level in {"PRIMARY", "LICENSED_PRIMARY"}
    ]
    authorized_secondary = [
        item for item in evidence if item.authority_level == "AUTHORIZED_SECONDARY"
    ]
    if authoritative and len(domains) >= 2:
        status = "CORROBORATED"
    elif authoritative or authorized_secondary:
        status = "REVIEW_REQUIRED"
    else:
        status = "DISCOVERY_ONLY"
    return {
        "status": status,
        "decision_eligible": status == "CORROBORATED",
        "evidence_count": len(evidence),
        "independent_domains": len(domains),
        "providers": sorted({item.provider for item in evidence}),
        "storage_allowed_count": sum(item.storage_allowed for item in evidence),
        "reason": (
            "搜索结果仅用于发现线索，至少需要一个官方/授权主源和第二独立来源复核。"
        ),
    }


class ExternalEvidenceRouter:
    def __init__(self, providers: Iterable[EvidenceProvider]):
        self.providers = list(providers)

    def search(self, query: str, count_per_provider: int = 10) -> dict[str, Any]:
        items: list[ExternalEvidence] = []
        provider_status = {}
        for provider in self.providers:
            if not provider.available():
                provider_status[provider.name] = "UNAVAILABLE"
                continue
            try:
                found = provider.search(query, count=count_per_provider)
                items.extend(found)
                provider_status[provider.name] = f"OK:{len(found)}"
            except Exception as exc:
                provider_status[provider.name] = f"ERROR:{type(exc).__name__}"
        deduped = {
            (item.url.rstrip("/"), item.title): item
            for item in items
        }
        evidence = list(deduped.values())
        return {
            "query": query,
            "items": [asdict(item) for item in evidence],
            "assessment": assess_evidence(evidence),
            "provider_status": provider_status,
        }


def persist_router_result(
    result: dict[str, Any],
    *,
    base_dir: str | Path = "data/raw/external_evidence",
) -> dict[str, Any]:
    """只持久化合同明确允许存储的结果；其余仅记录无正文审计。"""

    root = Path(base_dir)
    root.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date().isoformat()
    stored = [item for item in result.get("items", []) if item["storage_allowed"]]
    if stored:
        with open(root / f"evidence_{today}.jsonl", "a", encoding="utf-8") as handle:
            for item in stored:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    audit = {
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "query_hash": hashlib.sha256(
            str(result.get("query", "")).encode("utf-8")
        ).hexdigest(),
        "provider_status": result.get("provider_status", {}),
        "assessment": result.get("assessment", {}),
        "result_count": len(result.get("items", [])),
        "stored_count": len(stored),
        "transient_count": len(result.get("items", [])) - len(stored),
        "content_hashes": [
            item["content_hash"] for item in result.get("items", [])
        ],
    }
    with open(root / f"audit_{today}.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(audit, ensure_ascii=False) + "\n")
    return audit
