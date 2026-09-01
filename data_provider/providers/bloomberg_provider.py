"""Bloomberg 官方 BLPAPI 可选适配器。

只有在用户拥有有效 Bloomberg Professional/SAPI/Data License 权限并显式确认
entitlement 后才启用。模块不包含、缓存或绕过任何 Bloomberg 凭据。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Callable


class BloombergReferenceProvider:
    name = "bloomberg"

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        entitlement_confirmed: bool | None = None,
        storage_allowed: bool | None = None,
        host: str | None = None,
        port: int | None = None,
        session_factory: Callable[[Any], Any] | None = None,
    ):
        self.enabled = (
            enabled
            if enabled is not None
            else os.getenv("BLOOMBERG_ENABLED", "").lower() == "true"
        )
        self.entitlement_confirmed = (
            entitlement_confirmed
            if entitlement_confirmed is not None
            else os.getenv("BLOOMBERG_ENTITLEMENT_CONFIRMED", "").lower()
            == "true"
        )
        self.storage_allowed = (
            storage_allowed
            if storage_allowed is not None
            else os.getenv("BLOOMBERG_STORAGE_ALLOWED", "").lower() == "true"
        )
        self.host = host or os.getenv("BLOOMBERG_HOST", "localhost")
        self.port = int(port or os.getenv("BLOOMBERG_PORT", "8194"))
        self.session_factory = session_factory

    def available(self) -> bool:
        if not self.enabled or not self.entitlement_confirmed:
            return False
        if self.session_factory is not None:
            return True
        try:
            import blpapi  # noqa: F401
        except ImportError:
            return False
        return True

    def fetch_reference(
        self,
        securities: list[str],
        fields: list[str],
        *,
        timeout_ms: int = 10000,
    ) -> dict[str, Any]:
        """通过 ``//blp/refdata`` 请求参考数据并保留授权元数据。"""

        if not self.available():
            return {
                "status": "UNAVAILABLE",
                "reason": "BLOOMBERG_LICENSE_OR_BLPAPI_NOT_READY",
                "items": [],
            }
        if not securities or not fields:
            raise ValueError("securities and fields are required")
        import blpapi

        options = blpapi.SessionOptions()
        options.setServerHost(self.host)
        options.setServerPort(self.port)
        session = (
            self.session_factory(options)
            if self.session_factory is not None
            else blpapi.Session(options)
        )
        if not session.start() or not session.openService("//blp/refdata"):
            return {"status": "ERROR", "reason": "BLPAPI_SESSION_FAILED", "items": []}
        service = session.getService("//blp/refdata")
        request = service.createRequest("ReferenceDataRequest")
        for security in securities:
            request.append("securities", security)
        for field in fields:
            request.append("fields", field)
        session.sendRequest(request)

        items = []
        finished = False
        while not finished:
            event = session.nextEvent(timeout_ms)
            event_type = event.eventType()
            for message in event:
                if message.hasElement("responseError"):
                    continue
                if not message.hasElement("securityData"):
                    continue
                data = message.getElement("securityData")
                for index in range(data.numValues()):
                    security_data = data.getValueAsElement(index)
                    field_data = security_data.getElement("fieldData")
                    values = {}
                    for field in fields:
                        if field_data.hasElement(field):
                            values[field] = str(field_data.getElementAsString(field))
                    items.append(
                        {
                            "security": security_data.getElementAsString("security"),
                            "fields": values,
                            "provider": self.name,
                            "retrieved_at": datetime.now(timezone.utc).isoformat(),
                            "authority_level": "LICENSED_PRIMARY",
                            "storage_allowed": self.storage_allowed,
                        }
                    )
            finished = event_type == blpapi.Event.RESPONSE
        session.stop()
        return {
            "status": "READY",
            "items": items,
            "storage_allowed": self.storage_allowed,
        }

