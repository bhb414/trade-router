from __future__ import annotations

import asyncio
import json
from pathlib import Path

from trade_router_studio.instrument_catalog import InstrumentCatalog


def test_instrument_catalog_searches_fresh_local_master(tmp_path: Path) -> None:
    cache = tmp_path / "instrument-catalog.json"
    cache.write_text(
        json.dumps(
            {
                "symbols": [
                    {"code": "005930", "name": "삼성전자", "market": "KOSPI"},
                    {"code": "028260", "name": "삼성물산", "market": "KOSPI"},
                    {"code": "000660", "name": "SK하이닉스", "market": "KOSPI"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    catalog = InstrumentCatalog(cache)

    results = asyncio.run(catalog.search("삼성", limit=10))

    assert [(item["code"], item["name"]) for item in results] == [
        ("028260", "삼성물산"),
        ("005930", "삼성전자"),
    ]
    assert asyncio.run(catalog.name("005930")) == "삼성전자"


def test_instrument_catalog_parses_kis_fixed_width_master() -> None:
    prefix = "005930".ljust(9).encode("ascii")
    standard = "KR7005930003".encode("ascii")
    name = "삼성전자".encode("cp949").ljust(40, b" ")
    content = prefix + standard + name + b"ignored fields\n"

    rows = InstrumentCatalog.parse_master(content, "KOSPI")

    assert rows == [{"code": "005930", "name": "삼성전자", "market": "KOSPI"}]

