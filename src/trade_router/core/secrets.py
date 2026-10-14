from __future__ import annotations

import ctypes
import json
import os
import secrets as random_secrets
import tempfile
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Any, Protocol

from ..errors import RouterError


class SecretStore(Protocol):
    def get(self, key: str) -> dict[str, Any] | str | None: ...

    def set(self, key: str, value: dict[str, Any] | str) -> None: ...

    def delete(self, key: str) -> None: ...

    def contains(self, key: str) -> bool: ...


class MemorySecretStore:
    def __init__(self) -> None:
        self._values: dict[str, dict[str, Any] | str] = {}

    def get(self, key: str) -> dict[str, Any] | str | None:
        return self._values.get(key)

    def set(self, key: str, value: dict[str, Any] | str) -> None:
        self._values[key] = value

    def delete(self, key: str) -> None:
        self._values.pop(key, None)

    def contains(self, key: str) -> bool:
        return key in self._values


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


_CRYPTPROTECT_UI_FORBIDDEN = 0x01
_MAGIC = b"TRDPAPI1\x00"
_ENTROPY = b"trade-router:secrets:v1"


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data)
    blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    return blob, buffer


def _dpapi_protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise RouterError("DPAPI_UNAVAILABLE", "DPAPI secret storage requires Windows.")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    source, source_buf = _blob(data)
    entropy, entropy_buf = _blob(_ENTROPY)
    output = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(source),
        "Trade Router",
        ctypes.byref(entropy),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output),
    )
    _ = source_buf, entropy_buf
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise RouterError("DPAPI_UNAVAILABLE", "DPAPI secret storage requires Windows.")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    source, source_buf = _blob(data)
    entropy, entropy_buf = _blob(_ENTROPY)
    output = _DataBlob()
    description = wintypes.LPWSTR()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(source),
        ctypes.byref(description),
        ctypes.byref(entropy),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output),
    )
    _ = source_buf, entropy_buf
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        if description:
            kernel32.LocalFree(description)
        kernel32.LocalFree(output.pbData)


class DpapiSecretStore:
    """Small current-user DPAPI vault stored as one atomically replaced binary file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        payload = self.path.read_bytes()
        if not payload.startswith(_MAGIC):
            raise RouterError(
                "SECRET_STORE_CORRUPT", "Secret store has an unknown format."
            )
        try:
            plain = _dpapi_unprotect(payload[len(_MAGIC) :])
            loaded = json.loads(plain.decode("utf-8"))
        except Exception as exc:
            raise RouterError(
                "SECRET_STORE_UNREADABLE",
                "The Windows user that created the secret store must open it.",
            ) from exc
        if not isinstance(loaded, dict):
            raise RouterError(
                "SECRET_STORE_CORRUPT", "Secret store root must be an object."
            )
        return loaded

    def _save(self, values: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        plain = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        encrypted = _MAGIC + _dpapi_protect(plain)
        fd, temporary = tempfile.mkstemp(
            prefix="secrets-", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encrypted)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def get(self, key: str) -> dict[str, Any] | str | None:
        with self._lock:
            return self._load().get(key)

    def set(self, key: str, value: dict[str, Any] | str) -> None:
        with self._lock:
            values = self._load()
            values[key] = value
            self._save(values)

    def delete(self, key: str) -> None:
        with self._lock:
            values = self._load()
            if key in values:
                del values[key]
                self._save(values)

    def contains(self, key: str) -> bool:
        with self._lock:
            return key in self._load()

    def installation_token(self) -> str:
        key = "installation.api_token"
        existing = self.get(key)
        if isinstance(existing, str) and existing:
            return existing
        token = random_secrets.token_urlsafe(48)
        self.set(key, token)
        return token
