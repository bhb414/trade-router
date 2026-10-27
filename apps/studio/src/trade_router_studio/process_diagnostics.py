"""Allowlisted process failure metadata; never retain traceback text or paths."""
from __future__ import annotations

import re


EXCEPTION_TYPES = frozenset({
    "PermissionError", "FileNotFoundError", "OSError", "RuntimeError", "ValueError",
    "TypeError", "KeyError", "IndexError", "AttributeError", "NameError", "SyntaxError",
    "ImportError", "ModuleNotFoundError", "ZeroDivisionError", "OverflowError",
    "MemoryError", "TimeoutError", "ConnectionError", "ConnectionResetError",
    "BrokenPipeError", "CancelledError", "JSONDecodeError", "InvalidOperation",
    "RouterError", "ReadTimeout", "ConnectTimeout", "ConnectError", "ReadError",
    "RemoteProtocolError", "HTTPStatusError", "WebSocketDisconnect",
    "RuntimeStoreRestartRequired", "MissingProviderOrderId",
})
FAILURE_AREAS = frozenset({"STATE_PERSISTENCE", "STATE_RECOVERY", "RUNTIME_DATABASE", "UNKNOWN"})
EXIT_REASONS = frozenset({"USER_STOPPED", "PROCESS_COMPLETED", "PROCESS_FAILED", "PROCESS_EXITED"})


def failure_line(line: str) -> dict[str, object] | None:
    """Project only an exception's final line, not arbitrary log contents."""
    match = re.match(r"^(?:[A-Za-z_]\w*\.)*([A-Za-z_]\w*):", line[:8192])
    if not match:
        return None
    exception_type = match.group(1)
    if exception_type not in EXCEPTION_TYPES:
        return None
    windows_error = re.search(r"\[WinError ([0-9]{1,5})\]", line[:8192])
    area = "UNKNOWN"
    if "전략 상태" in line or "Strategy state" in line:
        area = "STATE_RECOVERY" if "복구" in line or "recover" in line.lower() else "STATE_PERSISTENCE"
    if "strategy-state" in line:
        area = "STATE_PERSISTENCE"
    if exception_type == "RuntimeStoreRestartRequired":
        area = "RUNTIME_DATABASE"
    return {"exception_type": exception_type,
            "winerror": int(windows_error.group(1)) if windows_error else None,
            "failure_area": area}


def safe_exit_details(details: object) -> dict[str, object]:
    """Revalidate persisted metadata at export; unknown fields never escape."""
    source = details if isinstance(details, dict) else {}
    code = source.get("exit_code")
    code = code if type(code) is int and -(2 ** 31) <= code <= 2 ** 32 - 1 else None
    requested = source.get("stop_requested")
    requested = requested if type(requested) is bool else None
    exception = source.get("exception_type")
    exception = exception if isinstance(exception, str) and exception in EXCEPTION_TYPES else None
    winerror = source.get("winerror")
    winerror = winerror if type(winerror) is int and 0 <= winerror <= 65535 else None
    area = source.get("failure_area")
    area = area if isinstance(area, str) and area in FAILURE_AREAS else "UNKNOWN"
    reason = "USER_STOPPED" if requested else (
        "PROCESS_COMPLETED" if code == 0 else "PROCESS_FAILED" if code is not None else "PROCESS_EXITED")
    return {"exit_code": code, "stop_requested": requested,
            "exception_type": exception, "winerror": winerror,
            "failure_area": area, "exit_reason": reason}
