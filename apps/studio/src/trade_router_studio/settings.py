from __future__ import annotations

import ctypes
import json
import os
import threading
import uuid
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Any

from .paths import WorkspacePaths


PROVIDERS: dict[str, dict[str, Any]] = {
    "kis": {
        "id": "kis",
        "name": "한국투자증권 KIS",
        "key_label": "App Key",
        "secret_label": "App Secret",
        "account_product_code": True,
    },
    "namuh": {
        "id": "namuh",
        "name": "NH투자증권 NAMUH PLUG",
        "key_label": "App Key",
        "secret_label": "App Secret Key",
        "account_product_code": False,
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
        return list(PROVIDERS.values())

    def list_connections(self) -> list[dict[str, Any]]:
        with self._lock:
            state = self._load_state()
            secrets = self._load_secrets()
            return [
                {
                    **connection,
                    "has_credentials": connection["id"] in secrets["connections"],
                }
                for connection in state["connections"]
            ]

    def add_connection(
        self, *, provider: str, label: str, app_key: str, app_secret: str
    ) -> dict[str, Any]:
        provider = provider.strip().lower()
        label = label.strip()
        app_key = app_key.strip()
        app_secret = app_secret.strip()
        if provider not in PROVIDERS:
            raise SettingsError("지원하지 않는 증권사입니다.")
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
                "label": label,
                "created_at": datetime.now().astimezone().isoformat(),
            }
            secrets = self._load_secrets()
            secrets["connections"][connection["id"]] = {
                "app_key": app_key,
                "app_secret": app_secret,
            }
            state["connections"].append(connection)
            self._write_secrets(secrets)
            self._write_state(state)
            return {**connection, "has_credentials": True}

    def remove_connection(self, connection_id: str) -> None:
        with self._lock:
            state = self._load_state()
            if any(account["connection_id"] == connection_id for account in state["accounts"]):
                raise SettingsError("이 API 연결을 사용하는 계좌를 먼저 삭제하세요.")
            before = len(state["connections"])
            state["connections"] = [
                item for item in state["connections"] if item["id"] != connection_id
            ]
            if len(state["connections"]) == before:
                raise SettingsError("API 연결을 찾을 수 없습니다.")
            secrets = self._load_secrets()
            secrets["connections"].pop(connection_id, None)
            self._write_secrets(secrets)
            self._write_state(state)

    def get_credentials(self, connection_id: str) -> dict[str, str]:
        with self._lock:
            credentials = self._load_secrets()["connections"].get(connection_id)
            if not credentials:
                raise SettingsError("저장된 API 인증정보가 없습니다.")
            return dict(credentials)

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
                        "provider": connection.get("provider"),
                        "connection_label": connection.get("label", "삭제된 연결"),
                        "masked_account": _mask_account(account_number),
                    }
                )
            return result

    def add_account(
        self,
        *,
        alias: str,
        connection_id: str,
        account_number: str,
        product_code: str = "",
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
                "connection_label": connection["label"],
                "masked_account": _mask_account(account_number),
            }

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
            return {"version": 1, "connections": [], "accounts": []}
        payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        return {
            "version": 1,
            "connections": list(payload.get("connections", [])),
            "accounts": list(payload.get("accounts", [])),
        }

    def _load_secrets(self) -> dict[str, dict[str, Any]]:
        if not self._secret_path.exists():
            return {"connections": {}, "accounts": {}}
        decrypted = self._protector.unprotect(self._secret_path.read_bytes())
        payload = json.loads(decrypted.decode("utf-8"))
        return {
            "connections": dict(payload.get("connections", {})),
            "accounts": dict(payload.get("accounts", {})),
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
