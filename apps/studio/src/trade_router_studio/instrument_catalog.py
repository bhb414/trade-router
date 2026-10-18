from __future__ import annotations

import asyncio
from datetime import datetime
from io import BytesIO
import json
from pathlib import Path
from typing import Any
import zipfile
from zoneinfo import ZoneInfo

import httpx


SEOUL = ZoneInfo("Asia/Seoul")
MASTER_URLS = {
    "KOSPI": "https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip",
    "KOSDAQ": "https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip",
}


class InstrumentCatalog:
    """Daily local catalogue for fast domestic instrument name suggestions."""

    def __init__(self, cache_path: Path) -> None:
        self._cache_path = cache_path
        self._symbols: dict[str, dict[str, str]] = {}
        self._loaded = False
        self._refresh_lock = asyncio.Lock()
        self._refresh_attempt_date: str | None = None
        self.last_error: str | None = None

    @staticmethod
    def parse_master(content: bytes, market: str) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for raw in content.splitlines():
            if len(raw) < 61:
                continue
            code = raw[0:9].decode("cp949", errors="ignore").strip()
            name = raw[21:61].decode("cp949", errors="ignore").strip()
            if len(code) > 6:
                code = code[-6:]
            if code and name:
                rows.append({"code": code.upper(), "name": name, "market": market})
        return rows

    def _load_cache(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
            values = payload.get("symbols", []) if isinstance(payload, dict) else []
            for item in values:
                if not isinstance(item, dict):
                    continue
                code = str(item.get("code") or "").strip().upper()
                name = str(item.get("name") or "").strip()
                market = str(item.get("market") or "").strip().upper()
                if code and name:
                    self._symbols[code] = {
                        "code": code,
                        "name": name,
                        "market": market,
                    }
        except (OSError, ValueError, TypeError):
            self._symbols = {}

    def _fresh_today(self) -> bool:
        try:
            modified = datetime.fromtimestamp(
                self._cache_path.stat().st_mtime,
                tz=SEOUL,
            )
        except OSError:
            return False
        return modified.date() == datetime.now(SEOUL).date()

    async def _download_market(
        self,
        client: httpx.AsyncClient,
        market: str,
        url: str,
    ) -> list[dict[str, str]]:
        response = await client.get(url)
        response.raise_for_status()
        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            members = [name for name in archive.namelist() if not name.endswith("/")]
            if not members:
                raise ValueError(f"{market} 종목 마스터 압축이 비어 있습니다.")
            content = archive.read(members[0])
        rows = self.parse_master(content, market)
        if not rows:
            raise ValueError(f"{market} 종목 마스터를 해석하지 못했습니다.")
        return rows

    async def refresh_if_needed(self) -> None:
        self._load_cache()
        today = datetime.now(SEOUL).date().isoformat()
        if self._fresh_today() or self._refresh_attempt_date == today:
            return
        async with self._refresh_lock:
            if self._fresh_today() or self._refresh_attempt_date == today:
                return
            self._refresh_attempt_date = today
            try:
                timeout = httpx.Timeout(20.0, connect=6.0)
                async with httpx.AsyncClient(
                    timeout=timeout,
                    follow_redirects=True,
                ) as client:
                    results = await asyncio.gather(
                        *(
                            self._download_market(client, market, url)
                            for market, url in MASTER_URLS.items()
                        )
                    )
                symbols: dict[str, dict[str, str]] = {}
                for rows in results:
                    for item in rows:
                        symbols[item["code"]] = item
                payload: dict[str, Any] = {
                    "updated_at": datetime.now(SEOUL).isoformat(),
                    "symbols": list(symbols.values()),
                }
                self._cache_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self._cache_path.with_suffix(
                    f"{self._cache_path.suffix}.tmp"
                )
                temporary.write_text(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8",
                )
                temporary.replace(self._cache_path)
                self._symbols = symbols
                self.last_error = None
            except (
                httpx.HTTPError,
                OSError,
                RuntimeError,
                ValueError,
                zipfile.BadZipFile,
                zipfile.LargeZipFile,
            ) as exc:
                # A stale catalogue remains useful while the download host is
                # unavailable. The chart can also fall back to names learned
                # from balances, conditions, rankings and order history.
                self.last_error = f"{type(exc).__name__}: {exc}"

    async def search(self, query: str, *, limit: int = 20) -> list[dict[str, str]]:
        await self.refresh_if_needed()
        normalized = query.strip().casefold()
        if not normalized:
            return []
        compact = "".join(normalized.split())

        def match(item: dict[str, str]) -> bool:
            code = item["code"].casefold()
            name = item["name"].casefold()
            return (
                normalized in code
                or normalized in name
                or compact in "".join(name.split())
            )

        def rank(item: dict[str, str]) -> tuple[int, str, str]:
            code = item["code"].casefold()
            name = item["name"].casefold()
            if normalized == code or normalized == name:
                priority = 0
            elif code.startswith(normalized) or name.startswith(normalized):
                priority = 1
            else:
                priority = 2
            return priority, name, code

        rows = [item for item in self._symbols.values() if match(item)]
        rows.sort(key=rank)
        return [dict(item) for item in rows[:limit]]

    async def name(self, code: str) -> str | None:
        await self.refresh_if_needed()
        item = self._symbols.get(code.strip().upper())
        return item["name"] if item else None

