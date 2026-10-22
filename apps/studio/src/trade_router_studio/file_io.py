"""Read snapshots without blocking their atomic replacement on Windows."""
from __future__ import annotations

import os
from pathlib import Path
from typing import TextIO


def open_shared_text(path: Path, *, encoding: str = "utf-8") -> TextIO:
    if os.name != "nt":
        return path.open("r", encoding=encoding)

    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    # Read-only access with FILE_SHARE_READ | WRITE | DELETE lets a writer
    # replace the path while this reader finishes reading its old snapshot.
    handle = create_file(str(path), 0x80000000, 0x1 | 0x2 | 0x4, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close_handle(handle)
        raise
    try:
        return os.fdopen(fd, "r", encoding=encoding)
    except BaseException:
        os.close(fd)
        raise


def read_shared_text(path: Path, *, encoding: str = "utf-8") -> str:
    with open_shared_text(path, encoding=encoding) as stream:
        return stream.read()


def snapshot_backup_path(target: Path) -> Path:
    """Known recovery location for ReplaceFileW's partially completed rename."""
    return target.with_name(target.name + ".replace-backup")


def replace_snapshot(source: Path, target: Path) -> None:
    """Replace a snapshot, retaining the previous Windows target for recovery.

    ReplaceFileW is not an all-or-nothing operation on failure. Supplying a
    backup preserves the old target on error 1176, and gives it a known name
    on error 1177. Callers must retain/recover this backup, including after a
    process exits between the native rename steps.
    """
    if os.name != "nt" or not target.exists():
        os.replace(source, target)
        return
    import ctypes
    from ctypes import wintypes

    replace_file = ctypes.WinDLL("kernel32", use_last_error=True).ReplaceFileW
    replace_file.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                            wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID]
    replace_file.restype = wintypes.BOOL
    if not replace_file(str(target), str(source), str(snapshot_backup_path(target)), 0, None, None):
        error = ctypes.get_last_error()
        if error == 2 and not target.exists():
            os.replace(source, target)
            return
        raise ctypes.WinError(error)
