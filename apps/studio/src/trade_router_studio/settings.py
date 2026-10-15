from __future__ import annotations

import ctypes
import hashlib
import json
import os
import threading
import uuid
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Any

from trade_router import AccessToken, ConnectionTarget, TokenScopeKey

from .paths import WorkspacePaths
from .broker_policies import rest_rate_limit_profile, websocket_rate_limit_profile


PROVIDERS: dict[str, dict[str, Any]] = {
    "kis": {
        "id": "kis",
        "name": "한국투자증권 KIS",
        "key_label": "App Key",
        "secret_label": "App Secret",
        "account_product_code": True,
        "targets": [
            {"id": "PAPER", "name": "모의투자"},
            {"id": "LIVE", "name": "실전"},
        ],
    },
    "namuh": {
        "id": "namuh",
        "name": "NH투자증권 NAMUH PLUG",
        "key_label": "App Key",
        "secret_label": "App Secret Key",
        "account_product_code": False,
        "targets": [
            {"id": "PAPER", "name": "모의투자"},
            {"id": "LIVE", "name": "실전"},
        ],
    },
}


class SettingsError(ValueError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


class DpapiSecretProtector:
    """Encrypts data for the current Windows user with DPAPI."""

    _flags = 0x01  # CRYPTPROTECT_UI_FORBIDDEN
    _entropy = b"TradeRouter/settings/v1"

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Trade Router secure storage currently requires Windows.")
        self._crypt32 = ctypes.windll.crypt32
        self._kernel32 = ctypes.windll.kernel32
        self._crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        self._crypt32.CryptProtectData.restype = wintypes.BOOL
        self._crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel32.LocalFree.restype = ctypes.c_void_p

    @staticmethod
    def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
        buffer = ctypes.create_string_buffer(data)
        blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
        return blob, buffer

    def protect(self, data: bytes) -> bytes:
        source, source_buffer = self._blob(data)
        entropy, entropy_buffer = self._blob(self._entropy)
        output = _DataBlob()
        _ = source_buffer, entropy_buffer
        if not self._crypt32.CryptProtectData(
            ctypes.byref(source),
            "Trade Router",
            ctypes.byref(entropy),
            None,
            None,
            self._flags,
            ctypes.byref(output),
        ):
            raise ctypes.WinError()
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            self._kernel32.LocalFree(output.pbData)

    def unprotect(self, data: bytes) -> bytes:
        source, source_buffer = self._blob(data)
        entropy, entropy_buffer = self._blob(self._entropy)
        output = _DataBlob()
        _ = source_buffer, entropy_buffer
        if not self._crypt32.CryptUnprotectData(
            ctypes.byref(source),
            None,
            ctypes.byref(entropy),
            None,
            None,
            self._flags,
            ctypes.byref(output),
        ):
            raise ctypes.WinError()
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            self._kernel32.LocalFree(output.pbData)


class SettingsStore:
    def __init__(
        self,
        paths: WorkspacePaths,
        protector: DpapiSecretProtector | None = None,
    ) -> None:
        self.paths = paths
        self._protector = protector or DpapiSecretProtector()
        self._state_path = paths.config / "settings.json"
        self._secret_path = paths.config / "secrets.dpapi"
        self._lock = threading.RLock()

    def providers(self) -> list[dict[str, Any]]:
        return [
            {
                **provider,
                "targets": [
                    {
                        **target,
                        "rest_rate_limit": rest_rate_limit_profile(
                            provider["id"], target["id"]
                        ),
                        "websocket_rate_limit": websocket_rate_limit_profile(
                            provider["id"], target["id"]
                        ),
                    }
                    for target in provider["targets"]
                ],
            }
            for provider in PROVIDERS.values()
        ]

    def list_connections(self) -> list[dict[str, Any]]:
        with self._lock:
            state = self._load_state()
            secrets = self._load_secrets()
            return [
                {
                    **connection,
                    "has_credentials": connection["id"] in secrets["connections"],
                    "rest_rate_limit": rest_rate_limit_profile(
                        str(connection["provider"]),
                        str(connection["target"]),
                    ),
                    "websocket_rate_limit": websocket_rate_limit_profile(
                        str(connection["provider"]),
                        str(connection["target"]),
                    ),
                }
                for connection in state["connections"]
            ]

    def get_connection(self, connection_id: str) -> dict[str, Any]:
        with self._lock:
            connection = next(
                (
                    item
                    for item in self._load_state()["connections"]
                    if item["id"] == connection_id
                ),
                None,
            )
            if connection is None:
                raise SettingsError("API 연결을 찾을 수 없습니다.")
            return dict(connection)

    def rate_limit_identity(self, connection_id: str) -> str:
        """Opaque scope shared by every connection using the same API key.

        The App Key never leaves encrypted settings or appears in diagnostics.
        The target remains part of the identity because KIS live and paper use
        different gateways and different official pacing.
        """

        with self._lock:
            connection = self.get_connection(connection_id)
            credentials = self.get_credentials(connection_id)
            return _connection_scope_identity(
                str(connection["provider"]),
                str(connection["target"]),
                str(credentials["app_key"]),
            )

    def resolve_connection(self, reference: str) -> dict[str, Any]:
        clean = reference.strip()
        with self._lock:
            matches = [
                item
                for item in self._load_state()["connections"]
                if item["id"] == clean or item["label"].casefold() == clean.casefold()
            ]
            if not matches:
                raise SettingsError("API 연결을 찾을 수 없습니다.")
            if len(matches) > 1:
                raise SettingsError("API 연결 이름이 중복되었습니다.")
            return dict(matches[0])

    def add_connection(
        self,
        *,
        provider: str,
        target: str,
        label: str,
        app_key: str,
        app_secret: str,
        realtime_user_id: str = "",
    ) -> dict[str, Any]:
        provider = provider.strip().lower()
        target = target.strip().upper()
        label = label.strip()
        app_key = app_key.strip()
        app_secret = app_secret.strip()
        realtime_user_id = realtime_user_id.strip()
        if provider not in PROVIDERS:
            raise SettingsError("지원하지 않는 증권사입니다.")
        supported_targets = {item["id"] for item in PROVIDERS[provider]["targets"]}
        if target not in supported_targets:
            raise SettingsError("선택한 증권사가 지원하는 접속 대상을 선택하세요.")
        if not label:
            raise SettingsError("API 연결 이름을 입력하세요.")
        if not app_key or not app_secret:
            raise SettingsError("API 키와 비밀키를 모두 입력하세요.")

        with self._lock:
            state = self._load_state()
            if any(item["label"].casefold() == label.casefold() for item in state["connections"]):
                raise SettingsError("같은 이름의 API 연결이 이미 있습니다.")
            connection = {
                "id": f"api-{uuid.uuid4().hex[:12]}",
                "provider": provider,
                "target": target,
                "label": label,
                "created_at": datetime.now().astimezone().isoformat(),
            }
            secrets = self._load_secrets()
            secrets["connections"][connection["id"]] = {
                "app_key": app_key,
                "app_secret": app_secret,
                "realtime_user_id": realtime_user_id,
            }
            state["connections"].append(connection)
            self._write_secrets(secrets)
            self._write_state(state)
            return {
                **connection,
                "has_credentials": True,
                "rest_rate_limit": rest_rate_limit_profile(provider, target),
                "websocket_rate_limit": websocket_rate_limit_profile(provider, target),
            }

    def remove_connection(self, connection_id: str) -> None:
        with self._lock:
            state = self._load_state()
            if any(account["connection_id"] == connection_id for account in state["accounts"]):
                raise SettingsError("이 API 연결을 사용하는 계좌를 먼저 삭제하세요.")
            before = len(state["connections"])
            removed_connection = next(
                (
                    item
                    for item in state["connections"]
                    if item["id"] == connection_id
                ),
                None,
            )
            state["connections"] = [
                item for item in state["connections"] if item["id"] != connection_id
            ]
            if len(state["connections"]) == before or removed_connection is None:
                raise SettingsError("API 연결을 찾을 수 없습니다.")
            secrets = self._load_secrets()
            removed_credentials = secrets["connections"].get(connection_id, {})
            removed_scope = _connection_scope_identity(
                str(removed_connection["provider"]),
                str(removed_connection["target"]),
                str(removed_credentials.get("app_key") or ""),
            )
            secrets["connections"].pop(connection_id, None)
            shared_scope_remains = any(
                item["id"] != connection_id
                and _connection_scope_identity(
                    str(item["provider"]),
                    str(item["target"]),
                    str(
                        secrets["connections"].get(item["id"], {}).get("app_key")
                        or ""
                    ),
                )
                == removed_scope
                for item in state["connections"]
            )
            secrets["tokens"] = {
                key: value
                for key, value in secrets["tokens"].items()
                if f":{connection_id}:" not in key
                and (shared_scope_remains or f":{removed_scope}:" not in key)
            }
            self._write_secrets(secrets)
            self._write_state(state)

    def get_credentials(self, connection_id: str) -> dict[str, str]:
        with self._lock:
            credentials = self._load_secrets()["connections"].get(connection_id)
            if not credentials:
                raise SettingsError("저장된 API 인증정보가 없습니다.")
            return dict(credentials)

    def load_token(self, scope: TokenScopeKey) -> AccessToken | None:
        with self._lock:
            payload = self._load_secrets()["tokens"].get(scope.storage_key)
            if not payload:
                return None
            try:
                issued_at = payload.get("issued_at")
                return AccessToken(
                    value=payload["value"],
                    token_type=payload.get("token_type", "Bearer"),
                    expires_at=datetime.fromisoformat(payload["expires_at"]),
                    issued_at=datetime.fromisoformat(issued_at) if issued_at else None,
                )
            except (KeyError, TypeError, ValueError):
                return None

    def save_token(self, scope: TokenScopeKey, token: AccessToken) -> None:
        with self._lock:
            secrets = self._load_secrets()
            secrets["tokens"][scope.storage_key] = {
                "value": token.value,
                "token_type": token.token_type,
                "expires_at": token.expires_at.isoformat(),
                "issued_at": token.issued_at.isoformat() if token.issued_at else None,
            }
            self._write_secrets(secrets)

    def delete_token(self, scope: TokenScopeKey) -> None:
        with self._lock:
            secrets = self._load_secrets()
            secrets["tokens"].pop(scope.storage_key, None)
            self._write_secrets(secrets)

    def list_accounts(self) -> list[dict[str, Any]]:
        with self._lock:
            state = self._load_state()
            secrets = self._load_secrets()
            connection_map = {item["id"]: item for item in state["connections"]}
            result = []
            for account in state["accounts"]:
                connection = connection_map.get(account["connection_id"], {})
                account_number = secrets["accounts"].get(account["id"], {}).get(
                    "account_number", ""
                )
                result.append(
                    {
                        **account,
                        "risk_limits": dict(account.get("risk_limits", {})),
                        "cost_rates": dict(account.get("cost_rates", {})),
                        "provider": connection.get("provider"),
                        "target": connection.get("target"),
                        "connection_label": connection.get("label", "삭제된 연결"),
                        "masked_account": _mask_account(account_number),
                    }
                )
            return result

    def get_account(self, reference: str) -> dict[str, Any]:
        clean = reference.strip()
        with self._lock:
            state = self._load_state()
            account = next(
                (
                    item
                    for item in state["accounts"]
                    if item["id"] == clean or item["alias"].casefold() == clean.casefold()
                ),
                None,
            )
            if account is None:
                raise SettingsError("거래 계좌를 찾을 수 없습니다.")
            connection = next(
                item
                for item in state["connections"]
                if item["id"] == account["connection_id"]
            )
            return {
                **account,
                "risk_limits": dict(account.get("risk_limits", {})),
                "cost_rates": dict(account.get("cost_rates", {})),
                "provider": connection["provider"],
                "target": connection["target"],
                "connection_label": connection["label"],
            }

    def get_account_number(self, account_id: str) -> str:
        with self._lock:
            account = self._load_secrets()["accounts"].get(account_id)
            if not account or not account.get("account_number"):
                raise SettingsError("저장된 계좌번호가 없습니다.")
            return str(account["account_number"])

    def add_account(
        self,
        *,
        alias: str,
        connection_id: str,
        account_number: str,
        product_code: str = "",
        risk_limits: dict[str, str | int | None] | None = None,
        cost_rates: dict[str, str | None] | None = None,
    ) -> dict[str, Any]:
        alias = alias.strip()
        account_number = "".join(character for character in account_number if character.isalnum())
        product_code = product_code.strip()
        if not alias:
            raise SettingsError("계좌 별칭을 입력하세요.")
        if not account_number:
            raise SettingsError("계좌번호를 입력하세요.")

        with self._lock:
            state = self._load_state()
            connection = next(
                (item for item in state["connections"] if item["id"] == connection_id),
                None,
            )
            if connection is None:
                raise SettingsError("등록된 API 연결을 선택하세요.")
            if any(item["alias"].casefold() == alias.casefold() for item in state["accounts"]):
                raise SettingsError("같은 별칭의 계좌가 이미 있습니다.")
            if connection["provider"] == "kis" and not product_code:
                raise SettingsError("KIS 계좌 상품코드를 입력하세요.")
            account = {
                "id": f"account-{uuid.uuid4().hex[:12]}",
                "alias": alias,
                "connection_id": connection_id,
                "product_code": product_code if connection["provider"] == "kis" else "",
                "risk_limits": {
                    key: value
                    for key, value in (risk_limits or {}).items()
                    if value not in {None, ""}
                },
                "cost_rates": {
                    key: str(value)
                    for key, value in (cost_rates or {}).items()
                    if value not in {None, ""}
                },
                "created_at": datetime.now().astimezone().isoformat(),
            }
            secrets = self._load_secrets()
            secrets["accounts"][account["id"]] = {"account_number": account_number}
            state["accounts"].append(account)
            self._write_secrets(secrets)
            self._write_state(state)
            return {
                **account,
                "provider": connection["provider"],
                "target": connection["target"],
                "connection_label": connection["label"],
                "masked_account": _mask_account(account_number),
            }

    def update_account_risk_limits(
        self,
        reference: str,
        risk_limits: dict[str, str | int | None],
    ) -> dict[str, Any]:
        allowed = {
            "max_order_amount",
            "max_daily_buy_amount",
            "max_exposure",
            "max_open_orders",
        }
        if set(risk_limits) - allowed:
            raise SettingsError("지원하지 않는 계좌 위험 한도 항목이 있습니다.")
        normalized = {
            key: value
            for key, value in risk_limits.items()
            if value not in {None, ""}
        }
        with self._lock:
            state = self._load_state()
            account = next(
                (
                    item
                    for item in state["accounts"]
                    if item["id"] == reference
                    or item["alias"].casefold() == reference.casefold()
                ),
                None,
            )
            if account is None:
                raise SettingsError("거래 계좌를 찾을 수 없습니다.")
            account["risk_limits"] = normalized
            self._write_state(state)
            return self.get_account(account["id"])

    def remove_account(self, account_id: str) -> None:
        with self._lock:
            state = self._load_state()
            before = len(state["accounts"])
            state["accounts"] = [
                item for item in state["accounts"] if item["id"] != account_id
            ]
            if len(state["accounts"]) == before:
                raise SettingsError("계좌를 찾을 수 없습니다.")
            secrets = self._load_secrets()
            secrets["accounts"].pop(account_id, None)
            self._write_secrets(secrets)
            self._write_state(state)

    def _load_state(self) -> dict[str, Any]:
        if not self._state_path.exists():
            return {"version": 2, "connections": [], "accounts": []}
        payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        connections = [
            self._normalize_connection(dict(item))
            for item in payload.get("connections", [])
        ]
        return {
            "version": 2,
            "connections": connections,
            "accounts": list(payload.get("accounts", [])),
        }

    @staticmethod
    def _normalize_connection(connection: dict[str, Any]) -> dict[str, Any]:
        if connection.get("target") in {"PAPER", "LIVE"}:
            return connection
        provider = connection.get("provider")
        label = str(connection.get("label", "")).casefold()
        if provider == "kis" and any(word in label for word in ("mock", "paper", "모의")):
            connection["target"] = "PAPER"
        else:
            connection["target"] = "LIVE"
        connection["target_inferred"] = True
        return connection

    def _load_secrets(self) -> dict[str, dict[str, Any]]:
        if not self._secret_path.exists():
            return {"connections": {}, "accounts": {}, "tokens": {}}
        decrypted = self._protector.unprotect(self._secret_path.read_bytes())
        payload = json.loads(decrypted.decode("utf-8"))
        return {
            "connections": dict(payload.get("connections", {})),
            "accounts": dict(payload.get("accounts", {})),
            "tokens": dict(payload.get("tokens", {})),
        }

    def _write_state(self, state: dict[str, Any]) -> None:
        self.paths.config.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            self._state_path,
            json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8"),
        )

    def _write_secrets(self, secrets: dict[str, Any]) -> None:
        self.paths.config.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(secrets, ensure_ascii=False).encode("utf-8")
        _atomic_write(self._secret_path, self._protector.protect(payload))


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def _mask_account(value: str) -> str:
    if len(value) <= 4:
        return "*" * len(value)
    if len(value) <= 8:
        return f"{'*' * (len(value) - 4)}{value[-4:]}"
    return f"{value[:2]}{'*' * (len(value) - 6)}{value[-4:]}"


def _connection_scope_identity(provider: str, target: str, app_key: str) -> str:
    material = "\0".join(
        (provider.lower(), target.upper(), app_key)
    ).encode("utf-8")
    fingerprint = hashlib.sha256(material).hexdigest()[:24]
    return f"{provider.lower()}:{target.lower()}:key-{fingerprint}"
