from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trade_router import OrderStatus


SEOUL = ZoneInfo("Asia/Seoul")


class RuntimeStoreRestartRequired(RuntimeError):
    """The runtime journal must be reopened by the supervising launcher."""


_ORDER_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.SUBMITTING: frozenset(
        {
            OrderStatus.ACKNOWLEDGED,
            OrderStatus.REJECTED,
            OrderStatus.UNKNOWN_RECONCILING,
        }
    ),
    OrderStatus.ACKNOWLEDGED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.CANCELLED,
            OrderStatus.UNKNOWN_RECONCILING,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.FILLED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.CANCELLED,
            OrderStatus.UNKNOWN_RECONCILING,
        }
    ),
    OrderStatus.CANCEL_PENDING: frozenset(
        {
            OrderStatus.ACKNOWLEDGED,
            OrderStatus.CANCELLED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.UNKNOWN_RECONCILING,
        }
    ),
    OrderStatus.UNKNOWN_RECONCILING: frozenset(
        {
            OrderStatus.ACKNOWLEDGED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.CANCELLED,
        }
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
}


class RuntimeStore:
    """Small WAL-backed runtime journal kept outside the program directory."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.recovery: dict[str, Any] | None = None
        self._connection = self._connect()
        try:
            self._verify_integrity_locked()
        except sqlite3.DatabaseError as exc:
            if not self._is_corruption_error(exc):
                self._connection.close()
                raise
            self._connection.close()
            backup = self._archive_corrupt_database(exc)
            self._connection = self._connect()
            self.recovery = {
                "recovered_at": datetime.now(SEOUL).isoformat(),
                "backup_path": str(backup),
                "message": (
                    "실행 기록 DB 손상을 자동 복구했습니다. 원본은 백업했으며 "
                    "안전을 위해 계좌·전략 주문 허용은 해제되었습니다."
                ),
            }
        with self._lock, self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runtime_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_events_occurred_at
                    ON runtime_events(occurred_at DESC);

                CREATE TABLE IF NOT EXISTS order_ledger (
                    id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    account_alias TEXT NOT NULL,
                    client_order_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    provider_order_id TEXT,
                    filled_quantity TEXT NOT NULL DEFAULT '0',
                    average_fill_price TEXT,
                    commission TEXT NOT NULL DEFAULT '0',
                    tax TEXT NOT NULL DEFAULT '0',
                    costs_complete INTEGER NOT NULL DEFAULT 0,
                    provider_metadata_json TEXT NOT NULL DEFAULT '{}',
                    message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(strategy_id, client_order_id)
                );
                CREATE INDEX IF NOT EXISTS idx_order_ledger_unresolved
                    ON order_ledger(status, updated_at DESC);

                CREATE TABLE IF NOT EXISTS order_provider_ids (
                    account_alias TEXT NOT NULL,
                    provider_order_id TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    registered_at TEXT NOT NULL,
                    UNIQUE(account_alias, provider_order_id, order_id),
                    FOREIGN KEY(order_id) REFERENCES order_ledger(id)
                );
                CREATE INDEX IF NOT EXISTS idx_order_provider_ids_lookup
                    ON order_provider_ids(account_alias, provider_order_id, registered_at DESC);

                CREATE TABLE IF NOT EXISTS order_fills (
                    id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    cumulative_quantity TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    price TEXT NOT NULL,
                    commission TEXT NOT NULL DEFAULT '0',
                    tax TEXT NOT NULL DEFAULT '0',
                    costs_complete INTEGER NOT NULL DEFAULT 0,
                    filled_at TEXT NOT NULL,
                    UNIQUE(order_id, cumulative_quantity),
                    FOREIGN KEY(order_id) REFERENCES order_ledger(id)
                );
                CREATE INDEX IF NOT EXISTS idx_order_fills_order
                    ON order_fills(order_id, filled_at);

                CREATE TABLE IF NOT EXISTS provider_events (
                    provider TEXT NOT NULL,
                    account_alias TEXT NOT NULL,
                    event_key TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    PRIMARY KEY(provider, account_alias, event_key)
                );

                CREATE TABLE IF NOT EXISTS order_permissions (
                    scope_type TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    armed INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(scope_type, scope_id)
                );

                CREATE TABLE IF NOT EXISTS instrument_names (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    source TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )
            # Existing user databases predate the diagnostic message column.
            columns = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(order_ledger)"
                ).fetchall()
            }
            if "message" not in columns:
                self._connection.execute(
                    "ALTER TABLE order_ledger ADD COLUMN message TEXT"
                )
            if "filled_quantity" not in columns:
                self._connection.execute(
                    "ALTER TABLE order_ledger ADD COLUMN filled_quantity TEXT NOT NULL DEFAULT '0'"
                )
            if "average_fill_price" not in columns:
                self._connection.execute(
                    "ALTER TABLE order_ledger ADD COLUMN average_fill_price TEXT"
                )
            if "commission" not in columns:
                self._connection.execute(
                    "ALTER TABLE order_ledger ADD COLUMN commission TEXT NOT NULL DEFAULT '0'"
                )
            if "tax" not in columns:
                self._connection.execute(
                    "ALTER TABLE order_ledger ADD COLUMN tax TEXT NOT NULL DEFAULT '0'"
                )
            if "costs_complete" not in columns:
                self._connection.execute(
                    "ALTER TABLE order_ledger ADD COLUMN costs_complete INTEGER NOT NULL DEFAULT 0"
                )
            if "provider_metadata_json" not in columns:
                self._connection.execute(
                    "ALTER TABLE order_ledger ADD COLUMN provider_metadata_json TEXT NOT NULL DEFAULT '{}'"
                )
            self._connection.execute(
                """
                INSERT OR IGNORE INTO order_provider_ids(
                    account_alias, provider_order_id, order_id, registered_at
                )
                SELECT account_alias, provider_order_id, id, updated_at
                FROM order_ledger
                WHERE provider_order_id IS NOT NULL AND provider_order_id != ''
                """
            )
        if self.recovery is not None:
            self.record(
                source="system",
                event_type="runtime.database.recovered",
                message=str(self.recovery["message"]),
                details={"backup_path": self.recovery["backup_path"]},
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=30,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _verify_integrity_locked(self) -> None:
        rows = self._connection.execute("PRAGMA quick_check").fetchall()
        results = [str(row[0]) for row in rows]
        if results != ["ok"]:
            raise sqlite3.DatabaseError(
                "database integrity check failed: " + "; ".join(results[:10])
            )

    @staticmethod
    def _is_corruption_error(exc: sqlite3.DatabaseError) -> bool:
        message = str(exc).casefold()
        return any(
            marker in message
            for marker in (
                "database disk image is malformed",
                "file is not a database",
                "database integrity check failed",
                "database corruption",
            )
        )

    def _archive_corrupt_database(self, exc: sqlite3.DatabaseError) -> Path:
        stamp = datetime.now(SEOUL).strftime("%Y%m%d-%H%M%S")
        backup = self.path.parent / "recovery" / f"runtime-{stamp}-{uuid.uuid4().hex[:8]}"
        backup.mkdir(parents=True, exist_ok=False)
        sources = [
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ]
        copied: list[Path] = []
        try:
            for source in sources:
                if source.exists():
                    shutil.copy2(source, backup / source.name)
                    copied.append(source)
            (backup / "recovery.json").write_text(
                json.dumps(
                    {
                        "recovered_at": datetime.now(SEOUL).isoformat(),
                        "database": str(self.path),
                        "error": str(exc),
                        "files": [source.name for source in copied],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            shutil.rmtree(backup, ignore_errors=True)
            raise
        for source in reversed(copied):
            source.unlink(missing_ok=True)
        return backup

    def _reopen_after_read_failure_locked(
        self, _exc: sqlite3.DatabaseError
    ) -> None:
        try:
            self._connection.close()
        except sqlite3.Error:
            pass
        self._connection = self._connect()
        try:
            self._verify_integrity_locked()
        except sqlite3.DatabaseError as retry_exc:
            if self._is_corruption_error(retry_exc):
                raise RuntimeStoreRestartRequired(
                    "실행 기록 DB 손상이 확인되어 자동 복구를 위해 다시 시작합니다."
                ) from retry_exc
            raise

    def _fetchall(
        self,
        query: str,
        values: tuple[Any, ...] | list[Any] = (),
    ) -> list[sqlite3.Row]:
        with self._lock:
            try:
                return self._connection.execute(query, values).fetchall()
            except sqlite3.DatabaseError as exc:
                self._reopen_after_read_failure_locked(exc)
                return self._connection.execute(query, values).fetchall()

    def _fetchone(
        self,
        query: str,
        values: tuple[Any, ...] | list[Any] = (),
    ) -> sqlite3.Row | None:
        with self._lock:
            try:
                return self._connection.execute(query, values).fetchone()
            except sqlite3.DatabaseError as exc:
                self._reopen_after_read_failure_locked(exc)
                return self._connection.execute(query, values).fetchone()

    def record(
        self,
        *,
        source: str,
        event_type: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> int:
        occurred_at = datetime.now(SEOUL).isoformat()
        safe_details = details or {}
        encoded = json.dumps(safe_details, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO runtime_events(
                    occurred_at, source, event_type, message, details_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (occurred_at, source, event_type, message, encoded),
            )
            self._prune_locked()
            return int(cursor.lastrowid)

    def events(
        self,
        *,
        limit: int = 200,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        safe_limit = min(max(limit, 1), 1_000)
        query = (
            "SELECT id, occurred_at, source, event_type, message, details_json "
            "FROM runtime_events"
        )
        values: list[Any] = []
        if source:
            query += " WHERE source = ?"
            values.append(source)
        query += " ORDER BY id DESC LIMIT ?"
        values.append(safe_limit)
        rows = self._fetchall(query, values)
        return [
            {
                "id": row["id"],
                "occurred_at": row["occurred_at"],
                "source": row["source"],
                "event_type": row["event_type"],
                "message": row["message"],
                "details": json.loads(row["details_json"]),
            }
            for row in rows
        ]

    def remember_instrument_names(
        self,
        names: dict[str, str],
        *,
        source: str | None = None,
    ) -> None:
        """Persist safe display names learned from broker responses."""

        normalized = {
            str(code).strip().upper(): str(name).strip()
            for code, name in names.items()
            if str(code).strip() and str(name).strip()
        }
        if not normalized:
            return
        updated_at = datetime.now(SEOUL).isoformat()
        with self._lock, self._connection:
            self._connection.executemany(
                """
                INSERT INTO instrument_names(code, name, source, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    name = excluded.name,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                [
                    (code, name, source, updated_at)
                    for code, name in normalized.items()
                ],
            )

    def instrument_names(self) -> dict[str, str]:
        rows = self._fetchall(
            "SELECT code, name FROM instrument_names ORDER BY updated_at ASC"
        )
        return {str(row["code"]): str(row["name"]) for row in rows}

    def service_results(
        self,
        business_date: str,
        *,
        source: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Aggregate normalized Studio service outcomes for one trading day."""

        clauses = ["occurred_at LIKE ?", "event_type LIKE 'service.%'"]
        values: list[Any] = [f"{business_date}%"]
        if source:
            clauses.append("source = ?")
            values.append(source)
        query = (
            "SELECT id, occurred_at, event_type, message, details_json "
            "FROM runtime_events WHERE "
            + " AND ".join(clauses)
            + " ORDER BY id ASC"
        )
        rows = self._fetchall(query, values)

        results: dict[str, dict[str, Any]] = {}
        for row in rows:
            parts = str(row["event_type"]).split(".")
            if len(parts) < 4:
                continue
            service = parts[1]
            operation = ".".join(parts[2:-1])
            outcome = parts[-1]
            if outcome not in {"success", "error"}:
                continue
            details = json.loads(row["details_json"])
            item = results.setdefault(
                service,
                {
                    "service": service,
                    "total": 0,
                    "success": 0,
                    "error": 0,
                    "duration_total_ms": 0.0,
                    "duration_samples": 0,
                    "last_at": None,
                    "last_operation": None,
                    "last_outcome": None,
                    "last_message": None,
                    "last_details": {},
                },
            )
            item["total"] += 1
            item[outcome] += 1
            duration = details.get("duration_ms")
            if isinstance(duration, (int, float)):
                item["duration_total_ms"] += float(duration)
                item["duration_samples"] += 1
            item["last_at"] = row["occurred_at"]
            item["last_operation"] = operation
            item["last_outcome"] = outcome
            item["last_message"] = row["message"]
            item["last_details"] = details

        for item in results.values():
            samples = int(item.pop("duration_samples"))
            duration_total = float(item.pop("duration_total_ms"))
            item["average_duration_ms"] = (
                round(duration_total / samples, 1) if samples else None
            )
            item["state"] = (
                "error" if item["last_outcome"] == "error" else "ok"
            )
        return results

    def service_events(
        self,
        business_date: str,
        *,
        source: str | None = None,
        limit_per_service: int = 20,
    ) -> dict[str, list[dict[str, Any]]]:
        """Return recent normalized service outcomes grouped for the UI tabs."""

        clauses = ["occurred_at LIKE ?", "event_type LIKE 'service.%'"]
        values: list[Any] = [f"{business_date}%"]
        if source:
            clauses.append("source = ?")
            values.append(source)
        query = (
            "SELECT id, occurred_at, source, event_type, message, details_json "
            "FROM runtime_events WHERE "
            + " AND ".join(clauses)
            + " ORDER BY id DESC"
        )
        rows = self._fetchall(query, values)

        grouped: dict[str, list[dict[str, Any]]] = {}
        safe_limit = min(max(int(limit_per_service), 1), 100)
        for row in rows:
            parts = str(row["event_type"]).split(".")
            if len(parts) < 4 or parts[-1] not in {"success", "error"}:
                continue
            service = parts[1]
            items = grouped.setdefault(service, [])
            if len(items) >= safe_limit:
                continue
            details = json.loads(row["details_json"])
            items.append(
                {
                    "id": row["id"],
                    "occurred_at": row["occurred_at"],
                    "source": row["source"],
                    "operation": ".".join(parts[2:-1]),
                    "outcome": parts[-1],
                    "message": row["message"],
                    "duration_ms": details.get("duration_ms"),
                    "details": details,
                }
            )
        return grouped

    def reserve_order(
        self,
        *,
        strategy_id: str,
        account_alias: str,
        client_order_id: str,
        request: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Atomically reserve an idempotency key before broker submission.

        Repeating an identical request returns the original ledger row. Reusing
        the key for different order contents is rejected.
        """

        strategy_id = strategy_id.strip()
        account_alias = account_alias.strip()
        client_order_id = client_order_id.strip()
        if not strategy_id or not account_alias or not client_order_id:
            raise ValueError("Strategy, account, and client order id are required.")
        encoded = json.dumps(
            request,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        now = datetime.now(SEOUL).isoformat()
        with self._lock, self._connection:
            existing = self._connection.execute(
                """
                SELECT * FROM order_ledger
                WHERE strategy_id = ? AND client_order_id = ?
                """,
                (strategy_id, client_order_id),
            ).fetchone()
            if existing is not None:
                row = self._order_row(existing)
                if row["account_alias"] != account_alias or row["request"] != request:
                    raise ValueError(
                        "client_order_id가 다른 주문 내용에 이미 사용되었습니다."
                    )
                return row, False

            order_id = f"order-{uuid.uuid4().hex}"
            self._connection.execute(
                """
                INSERT INTO order_ledger(
                    id, strategy_id, account_alias, client_order_id, status,
                    request_json, provider_order_id, message, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    order_id,
                    strategy_id,
                    account_alias,
                    client_order_id,
                    OrderStatus.SUBMITTING.value,
                    encoded,
                    now,
                    now,
                ),
            )
            created = self._connection.execute(
                "SELECT * FROM order_ledger WHERE id = ?",
                (order_id,),
            ).fetchone()
            return self._order_row(created), True

    def update_order(
        self,
        order_id: str,
        *,
        status: OrderStatus | str,
        provider_order_id: str | None = None,
        filled_quantity: Decimal | str | None = None,
        average_fill_price: Decimal | str | None = None,
        commission: Decimal | str | None = None,
        tax: Decimal | str | None = None,
        costs_complete: bool | None = None,
        provider_metadata: dict[str, Any] | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        requested_status = OrderStatus(status)
        now = datetime.now(SEOUL).isoformat()
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT rowid AS _rowid, * FROM order_ledger WHERE id = ?",
                (order_id,),
            ).fetchone()
            # A power loss or an interrupted older build can leave the SQLite
            # primary-key index inconsistent while the table row is still
            # readable.  Reconciliation must be able to close that historical
            # order instead of permanently blocking the account.  The table
            # scan is only a recovery path; healthy databases use the index.
            if existing is None:
                existing = self._connection.execute(
                    "SELECT rowid AS _rowid, * FROM order_ledger NOT INDEXED WHERE id = ?",
                    (order_id,),
                ).fetchone()
            if existing is None:
                raise KeyError("Order ledger entry was not found.")
            current_status = OrderStatus(existing["status"])
            if requested_status != current_status and requested_status not in _ORDER_TRANSITIONS[current_status]:
                raise ValueError(
                    f"Invalid order transition: {current_status.value} -> {requested_status.value}"
                )
            cursor = self._connection.execute(
                """
                UPDATE order_ledger
                SET status = ?, provider_order_id = COALESCE(?, provider_order_id),
                    filled_quantity = COALESCE(?, filled_quantity),
                    average_fill_price = COALESCE(?, average_fill_price),
                    commission = COALESCE(?, commission),
                    tax = COALESCE(?, tax),
                    costs_complete = COALESCE(?, costs_complete),
                    provider_metadata_json = COALESCE(?, provider_metadata_json),
                    message = COALESCE(?, message), updated_at = ?
                WHERE rowid = ?
                """,
                (
                    requested_status.value,
                    provider_order_id,
                    str(filled_quantity) if filled_quantity is not None else None,
                    str(average_fill_price) if average_fill_price is not None else None,
                    str(commission) if commission is not None else None,
                    str(tax) if tax is not None else None,
                    (1 if costs_complete else 0) if costs_complete is not None else None,
                    (json.dumps(provider_metadata, ensure_ascii=False, separators=(",", ":")) if provider_metadata is not None else None),
                    message,
                    now,
                    existing["_rowid"],
                ),
            )
            assert cursor.rowcount == 1
            if provider_order_id:
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO order_provider_ids(
                        account_alias, provider_order_id, order_id, registered_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        existing["account_alias"],
                        provider_order_id,
                        order_id,
                        now,
                    ),
                )
            row = self._connection.execute(
                "SELECT * FROM order_ledger WHERE rowid = ?",
                (existing["_rowid"],),
            ).fetchone()
            return self._order_row(row)

    def record_fill(
        self,
        *,
        order_id: str,
        cumulative_quantity: Decimal,
        quantity: Decimal,
        price: Decimal,
        commission: Decimal = Decimal("0"),
        tax: Decimal = Decimal("0"),
        costs_complete: bool = False,
        filled_at: datetime | None = None,
    ) -> dict[str, Any] | None:
        if quantity <= 0 or price <= 0:
            return None
        fill_id = f"fill-{uuid.uuid4().hex}"
        timestamp = (filled_at or datetime.now(SEOUL)).isoformat()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO order_fills(
                    id, order_id, cumulative_quantity, quantity, price,
                    commission, tax, costs_complete, filled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fill_id,
                    order_id,
                    str(cumulative_quantity),
                    str(quantity),
                    str(price),
                    str(commission),
                    str(tax),
                    1 if costs_complete else 0,
                    timestamp,
                ),
            )
            if cursor.rowcount == 0:
                return None
        return {
            "id": fill_id,
            "order_id": order_id,
            "quantity": str(quantity),
            "price": str(price),
            "commission": str(commission),
            "tax": str(tax),
            "costs_complete": costs_complete,
            "filled_at": timestamp,
        }

    def claim_provider_event(
        self, *, provider: str, account_alias: str, event_key: str
    ) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO provider_events(
                    provider, account_alias, event_key, received_at
                ) VALUES (?, ?, ?, ?)
                """,
                (provider, account_alias, event_key, datetime.now(SEOUL).isoformat()),
            )
            return cursor.rowcount == 1

    def fills(
        self, *, order_id: str | None = None, strategy_id: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if order_id:
            clauses.append("f.order_id = ?")
            values.append(order_id)
        if strategy_id:
            clauses.append("o.strategy_id = ?")
            values.append(strategy_id)
        query = "SELECT f.* FROM order_fills f JOIN order_ledger o ON o.id = f.order_id"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY f.filled_at, f.id"
        rows = self._fetchall(query, values)
        return [
            {
                "id": row["id"],
                "order_id": row["order_id"],
                "quantity": row["quantity"],
                "price": row["price"],
                "commission": row["commission"],
                "tax": row["tax"],
                "costs_complete": bool(row["costs_complete"]),
                "filled_at": row["filled_at"],
            }
            for row in rows
        ]

    def orders(
        self,
        *,
        strategy_id: str | None = None,
        account_alias: str | None = None,
        statuses: tuple[OrderStatus | str, ...] = (),
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if strategy_id:
            clauses.append("strategy_id = ?")
            values.append(strategy_id)
        if account_alias:
            clauses.append("account_alias = ?")
            values.append(account_alias)
        if statuses:
            normalized = [OrderStatus(status).value for status in statuses]
            clauses.append("status IN (" + ",".join("?" for _ in normalized) + ")")
            values.extend(normalized)
        query = "SELECT * FROM order_ledger"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        values.append(min(max(limit, 1), 1_000))
        rows = self._fetchall(query, values)
        return [self._order_row(row) for row in rows]

    def unresolved_orders(
        self,
        *,
        strategy_id: str | None = None,
        account_alias: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.orders(
            strategy_id=strategy_id,
            account_alias=account_alias,
            statuses=(
                OrderStatus.SUBMITTING,
                OrderStatus.ACKNOWLEDGED,
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.CANCEL_PENDING,
                OrderStatus.UNKNOWN_RECONCILING,
            ),
            limit=1_000,
        )

    def strategy_position_quantities(
        self,
        strategy_id: str,
        *,
        account_alias: str | None = None,
    ) -> dict[str, Decimal]:
        """Derive strategy-owned quantities from confirmed fills in the ledger."""

        clauses = ["strategy_id = ?", "CAST(filled_quantity AS REAL) > 0"]
        values: list[Any] = [strategy_id]
        if account_alias is not None:
            clauses.append("account_alias = ?")
            values.append(account_alias)
        query = (
            "SELECT request_json, filled_quantity FROM order_ledger WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at ASC, id ASC"
        )
        rows = self._fetchall(query, values)
        quantities: dict[str, Decimal] = {}
        for row in rows:
            request = json.loads(row["request_json"])
            code = str(request.get("code") or "").strip()
            if not code:
                continue
            filled = Decimal(str(row["filled_quantity"] or "0"))
            if str(request.get("side") or "").upper() == "BUY":
                quantities[code] = quantities.get(code, Decimal("0")) + filled
            elif str(request.get("side") or "").upper() == "SELL":
                quantities[code] = max(
                    Decimal("0"),
                    quantities.get(code, Decimal("0")) - filled,
                )
        return {
            code: quantity
            for code, quantity in quantities.items()
            if quantity > 0
        }

    def order(self, order_id: str) -> dict[str, Any] | None:
        row = self._fetchone(
            "SELECT * FROM order_ledger WHERE id = ?",
            (order_id,),
        )
        return self._order_row(row) if row is not None else None

    def order_by_client_id(
        self,
        strategy_id: str,
        client_order_id: str,
    ) -> dict[str, Any] | None:
        row = self._fetchone(
            """
            SELECT * FROM order_ledger
            WHERE strategy_id = ? AND client_order_id = ?
            """,
            (strategy_id, client_order_id),
        )
        return self._order_row(row) if row is not None else None

    def order_by_provider_id(
        self, account_alias: str, provider_order_id: str
    ) -> dict[str, Any] | None:
        row = self._fetchone(
            """
            SELECT ledger.* FROM order_provider_ids AS aliases
            JOIN order_ledger AS ledger ON ledger.id = aliases.order_id
            WHERE aliases.account_alias = ? AND aliases.provider_order_id = ?
            ORDER BY ledger.created_at DESC LIMIT 1
            """,
            (account_alias, provider_order_id),
        )
        return self._order_row(row) if row is not None else None

    def set_order_permission(
        self,
        scope_type: str,
        scope_id: str,
        *,
        armed: bool,
    ) -> dict[str, Any]:
        if scope_type not in {"account", "strategy", "strategy_file"}:
            raise ValueError(
                "Order permission scope must be account, strategy or strategy_file."
            )
        clean_id = scope_id.strip()
        if not clean_id:
            raise ValueError("Order permission scope id is required.")
        updated_at = datetime.now(SEOUL).isoformat()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO order_permissions(scope_type, scope_id, armed, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(scope_type, scope_id) DO UPDATE SET
                    armed = excluded.armed,
                    updated_at = excluded.updated_at
                """,
                (scope_type, clean_id, 1 if armed else 0, updated_at),
            )
        return {
            "scope_type": scope_type,
            "scope_id": clean_id,
            "armed": armed,
            "updated_at": updated_at,
        }

    def delete_order_permission(self, scope_type: str, scope_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM order_permissions WHERE scope_type = ? AND scope_id = ?",
                (scope_type, scope_id),
            )

    def rename_account_alias(self, old_alias: str, new_alias: str) -> None:
        """Move account-owned runtime history to a renamed settings alias."""

        old = old_alias.strip()
        new = new_alias.strip()
        if not old or not new or old == new:
            return
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE order_ledger SET account_alias = ? WHERE account_alias = ?",
                (new, old),
            )
            self._connection.execute(
                "UPDATE order_provider_ids SET account_alias = ? WHERE account_alias = ?",
                (new, old),
            )
            self._connection.execute(
                "UPDATE provider_events SET account_alias = ? WHERE account_alias = ?",
                (new, old),
            )
            previous = self._connection.execute(
                """
                SELECT armed FROM order_permissions
                WHERE scope_type = 'account' AND scope_id = ?
                """,
                (old,),
            ).fetchone()
            if previous is not None:
                self._connection.execute(
                    """
                    INSERT INTO order_permissions(scope_type, scope_id, armed, updated_at)
                    VALUES ('account', ?, ?, ?)
                    ON CONFLICT(scope_type, scope_id) DO UPDATE SET
                        armed = excluded.armed,
                        updated_at = excluded.updated_at
                    """,
                    (
                        new,
                        int(previous["armed"]),
                        datetime.now(SEOUL).isoformat(),
                    ),
                )
                self._connection.execute(
                    """
                    DELETE FROM order_permissions
                    WHERE scope_type = 'account' AND scope_id = ?
                    """,
                    (old,),
                )

    def has_order_permission(self, scope_type: str, scope_id: str) -> bool:
        row = self._fetchone(
            """
            SELECT 1 FROM order_permissions
            WHERE scope_type = ? AND scope_id = ?
            """,
            (scope_type, scope_id),
        )
        return row is not None

    def order_permission(self, scope_type: str, scope_id: str) -> bool:
        row = self._fetchone(
            """
            SELECT armed FROM order_permissions
            WHERE scope_type = ? AND scope_id = ?
            """,
            (scope_type, scope_id),
        )
        return bool(row["armed"]) if row is not None else False

    def order_permissions(self) -> list[dict[str, Any]]:
        rows = self._fetchall(
            """
            SELECT scope_type, scope_id, armed, updated_at
            FROM order_permissions ORDER BY scope_type, scope_id
            """
        )
        return [
            {
                "scope_type": row["scope_type"],
                "scope_id": row["scope_id"],
                "armed": bool(row["armed"]),
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def daily_buy_reserved(self, strategy_id: str, business_date: str) -> Decimal:
        """Return the day's non-rejected buy reservations from normalized JSON."""

        total = Decimal("0")
        rows = self._fetchall(
            """
            SELECT request_json FROM order_ledger
            WHERE strategy_id = ? AND created_at LIKE ?
              AND status != ?
            """,
            (strategy_id, f"{business_date}%", OrderStatus.REJECTED.value),
        )
        for row in rows:
            request = json.loads(row["request_json"])
            if request.get("side") == "BUY":
                total += Decimal(str(request.get("reserved_amount", "0")))
        return total

    def account_daily_buy_reserved(
        self,
        account_alias: str,
        business_date: str,
    ) -> Decimal:
        total = Decimal("0")
        rows = self._fetchall(
            """
            SELECT request_json FROM order_ledger
            WHERE account_alias = ? AND created_at LIKE ?
              AND status != ?
            """,
            (account_alias, f"{business_date}%", OrderStatus.REJECTED.value),
        )
        for row in rows:
            request = json.loads(row["request_json"])
            if request.get("side") == "BUY":
                total += Decimal(str(request.get("reserved_amount", "0")))
        return total

    def daily_buy_count(self, strategy_id: str, business_date: str) -> int:
        """Count non-rejected buy submissions for a strategy on a business day."""

        rows = self._fetchall(
            """
            SELECT request_json FROM order_ledger
            WHERE strategy_id = ? AND created_at LIKE ?
              AND status != ?
            """,
            (strategy_id, f"{business_date}%", OrderStatus.REJECTED.value),
        )
        return sum(
            1
            for row in rows
            if json.loads(row["request_json"]).get("side") == "BUY"
        )

    def account_daily_buy_count(
        self,
        account_alias: str,
        business_date: str,
    ) -> int:
        """Count non-rejected buy submissions for an account on a business day."""

        rows = self._fetchall(
            """
            SELECT request_json FROM order_ledger
            WHERE account_alias = ? AND created_at LIKE ?
              AND status != ?
            """,
            (account_alias, f"{business_date}%", OrderStatus.REJECTED.value),
        )
        return sum(
            1
            for row in rows
            if json.loads(row["request_json"]).get("side") == "BUY"
        )

    def unresolved_for_instrument(
        self,
        *,
        strategy_id: str,
        account_alias: str,
        code: str,
    ) -> list[dict[str, Any]]:
        return [
            row
            for row in self.unresolved_orders(strategy_id=strategy_id)
            if row["account_alias"] == account_alias
            and row["request"].get("code") == code
        ]

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _prune_locked(self) -> None:
        cutoff = (datetime.now(SEOUL) - timedelta(days=7)).isoformat()
        self._connection.execute(
            "DELETE FROM runtime_events WHERE occurred_at < ?",
            (cutoff,),
        )
        self._connection.execute(
            """
            DELETE FROM runtime_events
            WHERE id NOT IN (
                SELECT id FROM runtime_events ORDER BY id DESC LIMIT 100000
            )
            """
        )

    @staticmethod
    def _order_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "strategy_id": row["strategy_id"],
            "account_alias": row["account_alias"],
            "client_order_id": row["client_order_id"],
            "status": row["status"],
            "request": json.loads(row["request_json"]),
            "provider_order_id": row["provider_order_id"],
            "filled_quantity": row["filled_quantity"],
            "average_fill_price": row["average_fill_price"],
            "commission": row["commission"],
            "tax": row["tax"],
            "costs_complete": bool(row["costs_complete"]),
            "provider_metadata": json.loads(row["provider_metadata_json"] or "{}"),
            "message": row["message"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
