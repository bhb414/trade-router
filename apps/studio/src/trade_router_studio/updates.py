from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4
from zipfile import BadZipFile, ZipFile


RELEASE_API = "https://api.github.com/repos/bhb414/trade-router/releases/latest"
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_CHECKSUM_BYTES = 16 * 1024


async def check_for_update(current_version: str) -> dict[str, object]:
    return await asyncio.to_thread(_check, current_version)


async def prepare_update(
    current_version: str,
    *,
    workspace_root: Path,
    install_root: Path | None = None,
) -> dict[str, object]:
    return await asyncio.to_thread(
        _prepare,
        current_version,
        workspace_root.resolve(),
        (install_root or discover_install_root()).resolve(),
    )


def discover_install_root() -> Path:
    configured = os.environ.get("TRADE_ROUTER_INSTALL_DIR", "").strip()
    candidates = [Path(configured)] if configured else []
    candidates.extend(Path(__file__).resolve().parents)
    for candidate in candidates:
        if (candidate / "run.cmd").is_file() and (candidate / "pyproject.toml").is_file():
            return candidate.resolve()
    raise RuntimeError("Trade Router 설치 폴더를 확인하지 못했습니다.")


def _github_request(url: str) -> Request:
    return Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Trade-Router-Updater",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def _latest_release() -> dict[str, object] | None:
    try:
        with urlopen(_github_request(RELEASE_API), timeout=10) as response:
            payload = json.load(response)
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise RuntimeError(f"GitHub가 업데이트 확인을 거부했습니다. (HTTP {exc.code})") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("GitHub에 연결하지 못했습니다.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub 배포 정보를 해석하지 못했습니다.")
    return payload


def _release_details(
    payload: dict[str, object] | None,
    current_version: str,
) -> dict[str, object]:
    if payload is None:
        return {
            "current": current_version,
            "latest": None,
            "available": False,
            "can_apply": False,
            "message": "아직 등록된 GitHub 배포가 없습니다.",
        }

    latest = str(payload.get("tag_name", "")).lstrip("v")
    available = bool(
        latest and _version_tuple(latest) > _version_tuple(current_version)
    )
    assets = payload.get("assets")
    asset_rows = assets if isinstance(assets, list) else []
    archive_name = f"trade-router-v{latest}-windows.zip"
    checksum_name = f"{archive_name}.sha256"
    archive = next(
        (
            item
            for item in asset_rows
            if isinstance(item, dict) and item.get("name") == archive_name
        ),
        None,
    )
    checksum = next(
        (
            item
            for item in asset_rows
            if isinstance(item, dict) and item.get("name") == checksum_name
        ),
        None,
    )
    can_apply = bool(available and archive and checksum)
    if can_apply:
        message = f"새 버전 {latest}을 적용할 수 있습니다."
    elif available:
        message = f"새 버전 {latest}이 있지만 Windows 업데이트 파일이 아직 없습니다."
    else:
        message = "현재 버전이 최신입니다."
    return {
        "current": current_version,
        "latest": latest or None,
        "available": available,
        "can_apply": can_apply,
        "url": payload.get("html_url"),
        "asset_name": archive_name if archive else None,
        "message": message,
        "_archive_url": archive.get("browser_download_url") if archive else None,
        "_checksum_url": checksum.get("browser_download_url") if checksum else None,
    }


def _public_details(details: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in details.items() if not key.startswith("_")}


def _check(current_version: str) -> dict[str, object]:
    return _public_details(_release_details(_latest_release(), current_version))


def _prepare(
    current_version: str,
    workspace_root: Path,
    install_root: Path,
) -> dict[str, object]:
    if not (install_root / "run.cmd").is_file() or not (
        install_root / "pyproject.toml"
    ).is_file():
        raise RuntimeError("업데이트할 Trade Router 설치 폴더가 올바르지 않습니다.")

    details = _release_details(_latest_release(), current_version)
    if not details["available"]:
        raise RuntimeError(str(details["message"]))
    if not details["can_apply"]:
        raise RuntimeError(str(details["message"]))

    version = str(details["latest"])
    updates_root = workspace_root / "updates"
    updates_root.mkdir(parents=True, exist_ok=True)
    transaction = updates_root / f"v{version}-{uuid4().hex[:8]}"
    transaction.mkdir(parents=True)
    archive_path = transaction / str(details["asset_name"])
    payload_root = transaction / "payload"

    try:
        _download_file(str(details["_archive_url"]), archive_path, MAX_ARCHIVE_BYTES)
        checksum_text = _download_bytes(
            str(details["_checksum_url"]), MAX_CHECKSUM_BYTES
        ).decode("utf-8-sig")
        expected = _checksum_for_asset(checksum_text, archive_path.name)
        actual = _sha256(archive_path)
        if actual.casefold() != expected.casefold():
            raise RuntimeError("업데이트 파일의 SHA-256 검증에 실패했습니다.")
        _extract_verified_archive(archive_path, payload_root)
        _validate_payload(payload_root, version)
    except Exception:
        shutil.rmtree(transaction, ignore_errors=True)
        raise

    pending = {
        "version": version,
        "install_root": str(install_root),
        "payload_root": str(payload_root),
        "archive_sha256": actual,
        "created_at": datetime.now().astimezone().isoformat(),
    }
    pending_path = updates_root / "pending-update.json"
    temporary = pending_path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(pending, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(pending_path)
    return {
        "current": current_version,
        "latest": version,
        "prepared": True,
        "message": f"버전 {version} 다운로드와 검증을 마쳤습니다. 재시작하며 적용합니다.",
    }


def _download_file(url: str, destination: Path, maximum_bytes: int) -> None:
    try:
        with urlopen(_github_request(url), timeout=30) as response, destination.open(
            "wb"
        ) as output:
            declared = int(response.headers.get("Content-Length") or 0)
            if declared > maximum_bytes:
                raise RuntimeError("업데이트 파일이 허용 크기를 초과합니다.")
            received = 0
            while chunk := response.read(1024 * 1024):
                received += len(chunk)
                if received > maximum_bytes:
                    raise RuntimeError("업데이트 파일이 허용 크기를 초과합니다.")
                output.write(chunk)
    except HTTPError as exc:
        raise RuntimeError(f"업데이트 파일 다운로드가 거부되었습니다. (HTTP {exc.code})") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("업데이트 파일을 다운로드하지 못했습니다.") from exc


def _download_bytes(url: str, maximum_bytes: int) -> bytes:
    try:
        with urlopen(_github_request(url), timeout=15) as response:
            value = response.read(maximum_bytes + 1)
    except HTTPError as exc:
        raise RuntimeError(f"업데이트 검증 파일을 받지 못했습니다. (HTTP {exc.code})") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("업데이트 검증 파일을 받지 못했습니다.") from exc
    if len(value) > maximum_bytes:
        raise RuntimeError("업데이트 검증 파일이 허용 크기를 초과합니다.")
    return value


def _checksum_for_asset(text: str, asset_name: str) -> str:
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == asset_name:
            digest = parts[0]
            if re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                return digest
    raise RuntimeError("업데이트 SHA-256 값을 찾지 못했습니다.")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_archive_path(name: str) -> PurePosixPath:
    normalized = PurePosixPath(name.replace("\\", "/"))
    if (
        normalized.is_absolute()
        or not normalized.parts
        or any(part in {"", ".", ".."} for part in normalized.parts)
        or ":" in normalized.parts[0]
    ):
        raise RuntimeError("업데이트 압축 파일에 안전하지 않은 경로가 있습니다.")
    return normalized


def _extract_verified_archive(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    destination_root = destination.resolve()
    try:
        with ZipFile(archive_path) as archive:
            for item in archive.infolist():
                relative = _safe_archive_path(item.filename)
                target = destination.joinpath(*relative.parts).resolve()
                if target != destination_root and destination_root not in target.parents:
                    raise RuntimeError("업데이트 압축 경로가 설치 범위를 벗어납니다.")
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if (item.external_attr >> 16) & 0o170000 == 0o120000:
                    raise RuntimeError("업데이트 압축 파일에는 심볼릭 링크를 넣을 수 없습니다.")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    except BadZipFile as exc:
        raise RuntimeError("업데이트 ZIP 파일이 손상되었습니다.") from exc


def _validate_payload(payload_root: Path, expected_version: str) -> None:
    required = (
        payload_root / "run.cmd",
        payload_root / "tools" / "run.ps1",
        payload_root / "pyproject.toml",
        payload_root / "release-manifest.json",
    )
    if not all(path.is_file() for path in required):
        raise RuntimeError("업데이트 파일에 필수 실행 파일이 없습니다.")
    try:
        manifest = json.loads(required[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("업데이트 파일 목록을 읽지 못했습니다.") from exc
    if str(manifest.get("version")) != expected_version:
        raise RuntimeError("업데이트 버전과 파일 목록의 버전이 다릅니다.")
    rows = manifest.get("files")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("업데이트 파일 목록이 비어 있습니다.")
    listed: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("업데이트 파일 목록 형식이 올바르지 않습니다.")
        relative = _safe_archive_path(str(row.get("path", "")))
        relative_text = relative.as_posix()
        if relative_text in listed:
            raise RuntimeError("업데이트 파일 목록에 중복 경로가 있습니다.")
        listed.add(relative_text)
        path = payload_root.joinpath(*relative.parts)
        expected = str(row.get("sha256", ""))
        if not path.is_file() or not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
            raise RuntimeError("업데이트 파일 목록에 누락되거나 잘못된 파일이 있습니다.")
        if _sha256(path).casefold() != expected.casefold():
            raise RuntimeError(f"업데이트 내부 파일 검증에 실패했습니다: {relative_text}")


def _version_tuple(value: str) -> tuple[int, int, int, int]:
    values = [int(item) for item in re.findall(r"\d+", value)[:4]]
    values.extend([0] * (4 - len(values)))
    return tuple(values)  # type: ignore[return-value]
