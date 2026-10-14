from __future__ import annotations

import asyncio
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


RELEASE_API = "https://api.github.com/repos/bhb414/trade-router/releases/latest"


async def check_for_update(current_version: str) -> dict[str, object]:
    return await asyncio.to_thread(_check, current_version)


def _check(current_version: str) -> dict[str, object]:
    request = Request(
        RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"Trade-Router/{current_version}",
        },
    )
    try:
        with urlopen(request, timeout=5) as response:
            payload = json.load(response)
    except HTTPError as exc:
        if exc.code == 404:
            return {
                "current": current_version,
                "latest": None,
                "available": False,
                "message": "아직 등록된 GitHub 배포가 없습니다.",
            }
        raise RuntimeError(f"GitHub returned HTTP {exc.code}.") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError("GitHub에 연결하지 못했습니다.") from exc

    latest = str(payload.get("tag_name", "")).lstrip("v")
    return {
        "current": current_version,
        "latest": latest or None,
        "available": bool(latest and _version_tuple(latest) > _version_tuple(current_version)),
        "url": payload.get("html_url"),
        "message": (
            f"새 버전 {latest}이 있습니다."
            if latest and _version_tuple(latest) > _version_tuple(current_version)
            else "현재 버전이 최신입니다."
        ),
    }


def _version_tuple(value: str) -> tuple[int, ...]:
    result: list[int] = []
    for part in value.split("."):
        digits = "".join(character for character in part if character.isdigit())
        result.append(int(digits or 0))
    return tuple(result)
