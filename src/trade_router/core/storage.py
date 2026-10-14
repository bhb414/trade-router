from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiosqlite

from ..errors import RouterError
from ..models import (
    AccountPublic,
    CredentialUsage,
    Fill,
    Order,
    OrderRequest,
    OrderStatus,
    ProfilePublic,
    ProviderEnvironment,
    RiskLimits,
    StrategyPublic,
)
from .time import now_kst

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS profiles (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    environment TEXT NOT NULL,
    usage TEXT NOT NULL DEFAULT 'paper',
    display_name TEXT,
    settings_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    alias TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE RESTRICT,
    environment TEXT NOT NULL DEFAULT 'paper',
    masked_account TEXT NOT NULL,
    armed INTEGER NOT NULL DEFAULT 0,
    risk_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategies (
    id TEXT PRIMARY KEY,
    trading_account TEXT NOT NULL REFERENCES accounts(alias) ON DELETE RESTRICT,
    market_data TEXT NOT NULL REFERENCES profiles(id) ON DELETE RESTRICT,
    market_environment TEXT NOT NULL DEFAULT 'paper',
    armed INTEGER NOT NULL DEFAULT 0,
    risk_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    router_order_id TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    account_alias TEXT NOT NULL,
    code TEXT NOT NULL,
    side TEXT NOT NULL,
    status TEXT NOT NULL,
    request_json TEXT NOT NULL,
    order_json TEXT,
    reserved_notional TEXT NOT NULL DEFAULT '0',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(strategy_id, client_order_id)
);
CREATE INDEX IF NOT EXISTS idx_orders_unresolved
ON orders(status, account_alias, code);
CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY,
    router_order_id TEXT NOT NULL REFERENCES orders(router_order_id),
    fill_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
PRAGMA user_version=2;
"""


class SQLiteStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._migrate_environment_usage()
        await self._db.commit()

    async def _migrate_environment_usage(self) -> None:
        """Add credential usage and request environments to v1 databases."""
        profile_columns = {
            row[1]
            for row in await (
                await self.db.execute("PRAGMA table_info(profiles)")
            ).fetchall()
        }
        if "usage" not in profile_columns:
            await self.db.execute("ALTER TABLE profiles ADD COLUMN usage TEXT")
        await self.db.execute(
            "UPDATE profiles SET usage=COALESCE(usage, environment, 'paper')"
        )

        account_columns = {
            row[1]
            for row in await (
                await self.db.execute("PRAGMA table_info(accounts)")
            ).fetchall()
        }
        if "environment" not in account_columns:
            await self.db.execute("ALTER TABLE accounts ADD COLUMN environment TEXT")
        await self.db.execute(
            "UPDATE accounts SET environment=COALESCE(environment, "
            "(SELECT environment FROM profiles WHERE profiles.id=accounts.profile_id), 'paper')"
        )

        strategy_columns = {
            row[1]
            for row in await (
                await self.db.execute("PRAGMA table_info(strategies)")
            ).fetchall()
        }
        if "market_environment" not in strategy_columns:
            await self.db.execute(
                "ALTER TABLE strategies ADD COLUMN market_environment TEXT"
            )
        await self.db.execute(
            "UPDATE strategies SET market_environment=COALESCE(market_environment, "
            "(SELECT environment FROM profiles WHERE profiles.id=strategies.market_data), 'paper')"
        )
        await self.db.execute("PRAGMA user_version=2")

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("database is not open")
        return self._db

    async def add_profile(
        self,
        *,
        profile_id: str,
        provider: str,
        usage: CredentialUsage,
        display_name: str | None,
        settings: dict[str, Any],
    ) -> ProfilePublic:
        now = now_kst().isoformat()
        try:
            await self.db.execute(
                "INSERT INTO profiles(id,provider,environment,usage,display_name,settings_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    profile_id,
                    provider,
                    (
                        ProviderEnvironment.LIVE
                        if usage in {CredentialUsage.AUTO, CredentialUsage.SHARED}
                        else ProviderEnvironment(usage.value)
                    ).value,
                    usage.value,
                    display_name,
                    json.dumps(settings),
                    now,
                    now,
                ),
            )
            await self.db.commit()
        except aiosqlite.IntegrityError as exc:
            raise RouterError(
                "PROFILE_EXISTS",
                f"Profile '{profile_id}' already exists.",
                status_code=409,
            ) from exc
        return await self.get_profile(profile_id)

    async def list_profiles(self) -> list[ProfilePublic]:
        rows = await (
            await self.db.execute("SELECT * FROM profiles ORDER BY id")
        ).fetchall()
        return [self._profile(row) for row in rows]

    async def get_profile(self, profile_id: str) -> ProfilePublic:
        row = await (
            await self.db.execute("SELECT * FROM profiles WHERE id=?", (profile_id,))
        ).fetchone()
        if row is None:
            raise RouterError(
                "PROFILE_NOT_FOUND",
                f"Profile '{profile_id}' was not found.",
                status_code=404,
            )
        return self._profile(row)

    async def remove_profile(self, profile_id: str) -> None:
        try:
            cursor = await self.db.execute(
                "DELETE FROM profiles WHERE id=?", (profile_id,)
            )
            await self.db.commit()
        except aiosqlite.IntegrityError as exc:
            raise RouterError(
                "PROFILE_IN_USE",
                "Remove linked accounts and strategies first.",
                status_code=409,
            ) from exc
        if cursor.rowcount == 0:
            raise RouterError(
                "PROFILE_NOT_FOUND",
                f"Profile '{profile_id}' was not found.",
                status_code=404,
            )

    async def add_account(
        self,
        *,
        alias: str,
        profile_id: str,
        environment: ProviderEnvironment,
        masked_account: str,
        risk: RiskLimits,
    ) -> AccountPublic:
        await self.get_profile(profile_id)
        now = now_kst().isoformat()
        try:
            await self.db.execute(
                "INSERT INTO accounts(alias,profile_id,environment,masked_account,armed,risk_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    alias,
                    profile_id,
                    environment.value,
                    masked_account,
                    0,
                    risk.model_dump_json(),
                    now,
                    now,
                ),
            )
            await self.db.commit()
        except aiosqlite.IntegrityError as exc:
            raise RouterError(
                "ACCOUNT_EXISTS",
                f"Account alias '{alias}' already exists.",
                status_code=409,
            ) from exc
        return await self.get_account(alias)

    async def list_accounts(self) -> list[AccountPublic]:
        rows = await (
            await self.db.execute("SELECT * FROM accounts ORDER BY alias")
        ).fetchall()
        return [self._account(row) for row in rows]

    async def get_account(self, alias: str) -> AccountPublic:
        row = await (
            await self.db.execute("SELECT * FROM accounts WHERE alias=?", (alias,))
        ).fetchone()
        if row is None:
            raise RouterError(
                "ACCOUNT_NOT_FOUND",
                f"Account '{alias}' was not found.",
                status_code=404,
            )
        return self._account(row)

    async def set_account_armed(self, alias: str, armed: bool) -> AccountPublic:
        cursor = await self.db.execute(
            "UPDATE accounts SET armed=?,updated_at=? WHERE alias=?",
            (int(armed), now_kst().isoformat(), alias),
        )
        await self.db.commit()
        if cursor.rowcount == 0:
            raise RouterError(
                "ACCOUNT_NOT_FOUND",
                f"Account '{alias}' was not found.",
                status_code=404,
            )
        return await self.get_account(alias)

    async def add_strategy(
        self,
        *,
        strategy_id: str,
        trading_account: str,
        market_data: str,
        market_environment: ProviderEnvironment,
        risk: RiskLimits,
    ) -> StrategyPublic:
        await self.get_account(trading_account)
        await self.get_profile(market_data)
        now = now_kst().isoformat()
        try:
            await self.db.execute(
                "INSERT INTO strategies(id,trading_account,market_data,market_environment,armed,risk_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    strategy_id,
                    trading_account,
                    market_data,
                    market_environment.value,
                    0,
                    risk.model_dump_json(),
                    now,
                    now,
                ),
            )
            await self.db.commit()
        except aiosqlite.IntegrityError as exc:
            raise RouterError(
                "STRATEGY_EXISTS",
                f"Strategy '{strategy_id}' already exists.",
                status_code=409,
            ) from exc
        return await self.get_strategy(strategy_id)

    async def list_strategies(self) -> list[StrategyPublic]:
        rows = await (
            await self.db.execute("SELECT * FROM strategies ORDER BY id")
        ).fetchall()
        return [self._strategy(row) for row in rows]

    async def get_strategy(self, strategy_id: str) -> StrategyPublic:
        row = await (
            await self.db.execute("SELECT * FROM strategies WHERE id=?", (strategy_id,))
        ).fetchone()
        if row is None:
            raise RouterError(
                "STRATEGY_NOT_FOUND",
                f"Strategy '{strategy_id}' was not found.",
                status_code=404,
            )
        return self._strategy(row)

    async def set_strategy_armed(self, strategy_id: str, armed: bool) -> StrategyPublic:
        cursor = await self.db.execute(
            "UPDATE strategies SET armed=?,updated_at=? WHERE id=?",
            (int(armed), now_kst().isoformat(), strategy_id),
        )
        await self.db.commit()
        if cursor.rowcount == 0:
            raise RouterError(
                "STRATEGY_NOT_FOUND",
                f"Strategy '{strategy_id}' was not found.",
                status_code=404,
            )
        return await self.get_strategy(strategy_id)

    async def reserve_order(
        self,
        *,
        router_order_id: str,
        request: OrderRequest,
        reserved_notional: Decimal,
    ) -> bool:
        now = now_kst().isoformat()
        try:
            await self.db.execute(
                "INSERT INTO orders(router_order_id,client_order_id,strategy_id,account_alias,code,side,status,"
                "request_json,reserved_notional,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    router_order_id,
                    request.client_order_id,
                    request.strategy_id,
                    request.account_alias,
                    request.instrument.code,
                    request.side.value,
                    OrderStatus.SUBMITTING.value,
                    request.model_dump_json(),
                    str(reserved_notional),
                    now,
                    now,
                ),
            )
            await self.db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def save_order(
        self, order: Order, *, reserved_notional: Decimal | None = None
    ) -> None:
        values: list[Any] = [
            order.status.value,
            order.model_dump_json(),
            order.updated_at.isoformat(),
        ]
        sql = "UPDATE orders SET status=?,order_json=?,updated_at=?"
        if reserved_notional is not None:
            sql += ",reserved_notional=?"
            values.append(str(reserved_notional))
        sql += " WHERE router_order_id=?"
        values.append(order.router_order_id)
        await self.db.execute(sql, values)
        await self.db.commit()

    async def get_order_by_client(
        self, strategy_id: str, client_order_id: str
    ) -> Order | None:
        row = await (
            await self.db.execute(
                "SELECT order_json FROM orders WHERE strategy_id=? AND client_order_id=?",
                (strategy_id, client_order_id),
            )
        ).fetchone()
        if row is None or not row["order_json"]:
            return None
        return Order.model_validate_json(row["order_json"])

    async def get_order(self, router_order_id: str) -> Order:
        row = await (
            await self.db.execute(
                "SELECT order_json FROM orders WHERE router_order_id=?",
                (router_order_id,),
            )
        ).fetchone()
        if row is None or not row["order_json"]:
            raise RouterError(
                "ORDER_NOT_FOUND",
                f"Order '{router_order_id}' was not found.",
                status_code=404,
            )
        return Order.model_validate_json(row["order_json"])

    async def unresolved_records(self) -> list[dict[str, Any]]:
        rows = await (
            await self.db.execute(
                "SELECT * FROM orders WHERE status IN (?,?) ORDER BY created_at",
                (OrderStatus.SUBMITTING.value, OrderStatus.UNKNOWN_RECONCILING.value),
            )
        ).fetchall()
        return [dict(row) for row in rows]

    async def get_order_record_by_client(
        self, strategy_id: str, client_order_id: str
    ) -> dict[str, Any] | None:
        row = await (
            await self.db.execute(
                "SELECT * FROM orders WHERE strategy_id=? AND client_order_id=?",
                (strategy_id, client_order_id),
            )
        ).fetchone()
        return dict(row) if row else None

    async def list_orders(
        self,
        *,
        account_alias: str | None = None,
        strategy_id: str | None = None,
        limit: int = 200,
    ) -> list[Order]:
        where: list[str] = ["order_json IS NOT NULL"]
        params: list[Any] = []
        if account_alias:
            where.append("account_alias=?")
            params.append(account_alias)
        if strategy_id:
            where.append("strategy_id=?")
            params.append(strategy_id)
        params.append(limit)
        rows = await (
            await self.db.execute(
                f"SELECT order_json FROM orders WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT ?",
                params,
            )
        ).fetchall()
        return [Order.model_validate_json(row["order_json"]) for row in rows]

    async def save_fill(self, fill: Fill) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO fills(fill_id,router_order_id,fill_json,created_at) VALUES(?,?,?,?)",
            (
                fill.fill_id,
                fill.router_order_id,
                fill.model_dump_json(),
                fill.filled_at.isoformat(),
            ),
        )
        await self.db.commit()

    async def list_fills(
        self,
        *,
        account_alias: str | None = None,
        strategy_id: str | None = None,
        limit: int = 500,
    ) -> list[Fill]:
        where: list[str] = []
        params: list[Any] = []
        if account_alias:
            where.append("o.account_alias=?")
            params.append(account_alias)
        if strategy_id:
            where.append("o.strategy_id=?")
            params.append(strategy_id)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(limit)
        rows = await (
            await self.db.execute(
                f"SELECT f.fill_json FROM fills f JOIN orders o ON o.router_order_id=f.router_order_id "
                f"{clause} ORDER BY f.created_at DESC LIMIT ?",
                params,
            )
        ).fetchall()
        return [Fill.model_validate_json(row["fill_json"]) for row in rows]

    async def unresolved_count(self) -> int:
        statuses = (OrderStatus.SUBMITTING.value, OrderStatus.UNKNOWN_RECONCILING.value)
        row = await (
            await self.db.execute(
                "SELECT COUNT(*) AS count FROM orders WHERE status IN (?,?)", statuses
            )
        ).fetchone()
        return int(row["count"])

    async def has_symbol_lock(self, account_alias: str, code: str) -> bool:
        row = await (
            await self.db.execute(
                "SELECT 1 FROM orders WHERE account_alias=? AND code=? AND status IN (?,?,?,?) LIMIT 1",
                (
                    account_alias,
                    code,
                    OrderStatus.SUBMITTING.value,
                    OrderStatus.ACKNOWLEDGED.value,
                    OrderStatus.PARTIALLY_FILLED.value,
                    OrderStatus.UNKNOWN_RECONCILING.value,
                ),
            )
        ).fetchone()
        return row is not None

    async def open_order_count(
        self, *, account_alias: str, strategy_id: str | None = None
    ) -> int:
        statuses = (
            OrderStatus.SUBMITTING.value,
            OrderStatus.ACKNOWLEDGED.value,
            OrderStatus.PARTIALLY_FILLED.value,
            OrderStatus.CANCEL_PENDING.value,
            OrderStatus.UNKNOWN_RECONCILING.value,
        )
        sql = "SELECT COUNT(*) AS count FROM orders WHERE account_alias=? AND status IN (?,?,?,?,?)"
        params: list[Any] = [account_alias, *statuses]
        if strategy_id:
            sql += " AND strategy_id=?"
            params.append(strategy_id)
        row = await (await self.db.execute(sql, params)).fetchone()
        return int(row["count"])

    async def daily_buy_reserved(
        self, *, account_alias: str, strategy_id: str | None = None
    ) -> Decimal:
        day = now_kst().date().isoformat()
        sql = (
            "SELECT reserved_notional FROM orders "
            "WHERE account_alias=? AND side='BUY' AND substr(created_at,1,10)=? "
            "AND status NOT IN (?,?)"
        )
        params: list[Any] = [
            account_alias,
            day,
            OrderStatus.REJECTED.value,
            OrderStatus.CANCELLED.value,
        ]
        if strategy_id:
            sql += " AND strategy_id=?"
            params.append(strategy_id)
        rows = await (await self.db.execute(sql, params)).fetchall()
        return sum((Decimal(row["reserved_notional"]) for row in rows), Decimal(0))

    async def strategy_exposure(self, strategy_id: str) -> Decimal:
        rows = await (
            await self.db.execute(
                "SELECT order_json FROM orders WHERE strategy_id=? AND order_json IS NOT NULL",
                (strategy_id,),
            )
        ).fetchall()
        exposure = Decimal(0)
        for row in rows:
            order = Order.model_validate_json(row["order_json"])
            if order.filled_quantity <= 0 or order.average_fill_price is None:
                continue
            value = order.average_fill_price * order.filled_quantity
            exposure += value if order.side.value == "BUY" else -value
        return max(exposure, Decimal(0))

    @staticmethod
    def _profile(row: aiosqlite.Row) -> ProfilePublic:
        return ProfilePublic(
            id=row["id"],
            provider=row["provider"],
            usage=CredentialUsage(row["usage"]),
            display_name=row["display_name"],
            settings=json.loads(row["settings_json"]),
            secret_configured=False,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _account(row: aiosqlite.Row) -> AccountPublic:
        return AccountPublic(
            alias=row["alias"],
            profile_id=row["profile_id"],
            environment=ProviderEnvironment(row["environment"]),
            masked_account=row["masked_account"],
            armed=bool(row["armed"]),
            risk=RiskLimits.model_validate_json(row["risk_json"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _strategy(row: aiosqlite.Row) -> StrategyPublic:
        return StrategyPublic(
            id=row["id"],
            trading_account=row["trading_account"],
            market_data=row["market_data"],
            market_environment=ProviderEnvironment(row["market_environment"]),
            armed=bool(row["armed"]),
            risk=RiskLimits.model_validate_json(row["risk_json"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
