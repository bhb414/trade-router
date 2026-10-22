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


def _origin_matches(flow_id: object, node_ids: object, request: dict[str, Any]) -> bool:
    selected_flows = request.get("sell_buy_flow_ids") or ()
    selected_nodes = request.get("sell_buy_node_refs") or ()
    if not selected_flows and not selected_nodes:
        return True
    return flow_id in selected_flows or any(
        ref.get("flow_id") == flow_id and ref.get("node_id") in (node_ids or ())
        for ref in selected_nodes
    )


def _execution_timestamp(value: object) -> float:
    timestamp = datetime.fromisoformat(str(value))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=SEOUL)
    return timestamp.timestamp()


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
        self._origin_balance_observations: dict[tuple[str, str], tuple[Decimal, float, float]] = {}
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
                CREATE TABLE IF NOT EXISTS buy_origin_adjustments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_alias TEXT NOT NULL,
                    code TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                );
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
        query = "SELECT rowid AS accounting_sequence, * FROM order_ledger"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
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

    def _accounting_fill_events(self, *, account_alias: str | None = None) -> list[dict[str, Any]]:
        clauses = ["CAST(filled_quantity AS REAL) > 0"]
        values: list[Any] = []
        if account_alias is not None:
            clauses.append("account_alias = ?")
            values.append(account_alias)
        rows = self._fetchall(
            "SELECT rowid AS accounting_sequence, * FROM order_ledger WHERE "
            + " AND ".join(clauses) + " ORDER BY rowid ASC", values,
        )
        fill_query = "SELECT f.rowid AS fill_sequence, f.* FROM order_fills f JOIN order_ledger o ON o.id = f.order_id"
        fill_values: list[Any] = []
        if account_alias is not None:
            fill_query += " WHERE o.account_alias = ?"
            fill_values.append(account_alias)
        fill_rows = self._fetchall(fill_query, fill_values)
        fills_by_order: dict[str, list[sqlite3.Row]] = {}
        for fill in fill_rows:
            fills_by_order.setdefault(str(fill["order_id"]), []).append(fill)
        events: list[dict[str, Any]] = []
        for raw in rows:
            original = self._order_row(raw)
            cumulative = Decimal(str(original["filled_quantity"] or "0"))
            remaining = cumulative
            covered_amount = Decimal("0")
            covered_commission = Decimal("0")
            covered_tax = Decimal("0")
            order_fills = sorted(
                fills_by_order.get(str(original["id"]), []),
                key=lambda fill: (_execution_timestamp(fill["filled_at"]), int(fill["fill_sequence"])),
            )
            for fill in order_fills:
                quantity = min(remaining, Decimal(str(fill["quantity"])))
                if quantity <= 0:
                    continue
                ratio = quantity / Decimal(str(fill["quantity"]))
                commission = Decimal(str(fill["commission"])) * ratio
                tax = Decimal(str(fill["tax"])) * ratio
                events.append({
                    **original,
                    "filled_quantity": str(quantity),
                    "average_fill_price": str(fill["price"]),
                    "commission": str(commission), "tax": str(tax),
                    "costs_complete": bool(fill["costs_complete"]),
                    "accounting_at": str(fill["filled_at"]),
                    "accounting_sequence": int(fill["fill_sequence"]),
                })
                remaining -= quantity
                covered_amount += quantity * Decimal(str(fill["price"]))
                covered_commission += commission
                covered_tax += tax
            if remaining > 0:
                # Older ledgers may have only cumulative snapshots. Preserve
                # their known quantity without applying it at order-submission
                # time, which can precede another strategy's intervening sale.
                amount = cumulative * Decimal(str(original.get("average_fill_price") or "0")) - covered_amount
                events.append({
                    **original,
                    "filled_quantity": str(remaining),
                    "average_fill_price": str(amount / remaining) if amount > 0 else None,
                    "commission": str(max(Decimal("0"), Decimal(str(original.get("commission") or "0")) - covered_commission)),
                    "tax": str(max(Decimal("0"), Decimal(str(original.get("tax") or "0")) - covered_tax)),
                    "accounting_at": str(original["updated_at"]),
                })
        events.sort(key=lambda row: (_execution_timestamp(row["accounting_at"]), int(row["accounting_sequence"])))
        return events

    def strategy_accounting_orders(
        self,
        strategy_id: str,
        *,
        account_alias: str | None = None,
    ) -> list[dict[str, Any]]:
        """Project fills onto their owners without transferring external inventory.

        An explicit ACCOUNT sell consumes the submitting strategy's lots first,
        then other strategy lots in acquisition order. Any remaining quantity
        belongs to untracked account inventory and has no strategy cost basis.
        Replaying individual fills in execution order preserves both ownership
        and price attribution across partial fills and restarts.
        Broker orders stay attributed to their submitting strategy in the ledger.
        """
        events = self._accounting_fill_events(account_alias=account_alias)
        lots: dict[tuple[str, str], list[dict[str, Any]]] = {}
        attributed: list[dict[str, Any]] = []
        for sequence, row in enumerate(events):
            row["accounting_sequence"] = sequence
            request = row["request"]
            code = str(request.get("code") or "").strip()
            if not code:
                continue
            owner = str(row["strategy_id"])
            quantity = Decimal(str(row["filled_quantity"] or "0"))
            key = (str(row["account_alias"]), code)
            inventory = lots.setdefault(key, [])
            side = str(request.get("side") or "").upper()
            if side == "BUY":
                inventory.append({"owner": owner, "quantity": quantity, "buy_flow_id": request.get("buy_flow_id"), "node_ids": request.get("buy_rule_node_ids") or ()})
                if owner == strategy_id:
                    attributed.append(row)
                continue
            if side != "SELL":
                continue
            candidates = [lot for lot in inventory if lot["owner"] == owner and _origin_matches(lot.get("buy_flow_id"), lot["node_ids"], request)]
            if request.get("sell_position_scope") == "ACCOUNT":
                candidates += [lot for lot in inventory if lot["owner"] != owner]
            remaining = quantity
            allocations: dict[str, Decimal] = {}
            for lot in candidates:
                consumed = min(remaining, lot["quantity"])
                if consumed <= 0:
                    continue
                lot["quantity"] -= consumed
                remaining -= consumed
                lot_owner = lot["owner"]
                allocations[lot_owner] = allocations.get(lot_owner, Decimal("0")) + consumed
                if remaining <= 0:
                    break
            lots[key] = [lot for lot in inventory if lot["quantity"] > 0]
            allocated = allocations.get(strategy_id, Decimal("0"))
            if allocated <= 0:
                continue
            ratio = allocated / quantity
            projected = {
                **row,
                "request": {**request, "quantity": str(allocated)},
                "filled_quantity": str(allocated),
                "commission": str(Decimal(str(row.get("commission") or "0")) * ratio),
                "tax": str(Decimal(str(row.get("tax") or "0")) * ratio),
                "execution_strategy_id": owner,
                "attributed_strategy_id": strategy_id,
                "source_filled_quantity": str(quantity),
            }
            if owner != strategy_id:
                projected["message"] = f"계좌 잔고 대상 전략({owner})의 매도로 이 전략 보유분 {allocated}주를 정리했습니다."
            attributed.append(projected)
        return list(reversed(attributed))

    def strategy_buy_origin_lots(
        self,
        strategy_id: str,
        *,
        account_alias: str,
        actual_quantities: dict[str, Decimal] | None = None,
        as_of: datetime | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Replay confirmed fill origins, never assigning legacy/manual inventory.

        Account-level sells consume their owner's lots first, then account FIFO.
        A linked sell consumes only its selected origins. Observed external
        reductions are remembered separately from trade accounting so a later
        manual purchase cannot resurrect an already removed buy origin.
        """
        with self._lock, self._connection:
            events = self._accounting_fill_events(account_alias=account_alias)
            adjustments = self._fetchall(
                "SELECT * FROM buy_origin_adjustments WHERE account_alias = ? ORDER BY id",
                (account_alias,),
            )
            timeline = [(_execution_timestamp(row["accounting_at"]), 0, index, row) for index, row in enumerate(events)]
            timeline += [(_execution_timestamp(row["observed_at"]), 1, int(row["id"]), row) for row in adjustments]
            inventory: dict[str, list[dict[str, Any]]] = {}

            def consume(candidates: list[dict[str, Any]], amount: Decimal) -> None:
                for lot in candidates:
                    taken = min(amount, lot["quantity"])
                    lot["quantity"] -= taken
                    amount -= taken
                    if amount <= 0:
                        break

            for _, kind, _, row in sorted(timeline, key=lambda item: item[:3]):
                if kind == 1:
                    consume(inventory.get(str(row["code"]), []), Decimal(str(row["quantity"])))
                    continue
                request = row["request"]
                code = str(request.get("code") or "")
                lots = inventory.setdefault(code, [])
                owner = str(row["strategy_id"])
                quantity = Decimal(str(row["filled_quantity"]))
                if request.get("side") == "BUY":
                    lots.append({"owner": owner, "flow_id": request.get("buy_flow_id"), "node_ids": tuple(request.get("buy_rule_node_ids") or ()), "quantity": quantity})
                elif request.get("side") == "SELL":
                    selected = bool(request.get("sell_buy_flow_ids") or request.get("sell_buy_node_refs"))
                    candidates = [lot for lot in lots if lot["owner"] == owner and _origin_matches(lot["flow_id"], lot["node_ids"], request)]
                    if request.get("sell_position_scope") == "ACCOUNT" and not selected:
                        candidates += [lot for lot in lots if lot["owner"] != owner]
                    consume(candidates, quantity)

            if actual_quantities is not None:
                unresolved_codes = {str(row["request"].get("code") or "") for row in self.unresolved_orders(account_alias=account_alias)}
                observed = _execution_timestamp(as_of.isoformat()) if as_of is not None else None
                latest_by_code: dict[str, float] = {}
                for timestamp, kind, _, row in timeline:
                    code = str(row["code"] if kind else row["request"].get("code") or "")
                    latest_by_code[code] = max(latest_by_code.get(code, 0), timestamp)
                for code, lots in inventory.items():
                    observation_key = (account_alias, code)
                    actual = max(Decimal("0"), actual_quantities.get(code, Decimal("0")))
                    missing = sum((lot["quantity"] for lot in lots), Decimal("0")) - actual
                    if missing <= 0:
                        self._origin_balance_observations.pop(observation_key, None)
                        continue
                    # Never persist an old balance or an unconfirmed pending
                    # fill as an external sell. Returned quantities still obey
                    # the broker snapshot's conservative cap.
                    # KIS may return its one-second cached payload with a new
                    # as_of value. Require two consistent observations beyond
                    # that cache interval and a fill-settlement grace period.
                    latest = latest_by_code.get(code, 0)
                    if observed is not None and observed >= latest + 5 and code not in unresolved_codes:
                        previous = self._origin_balance_observations.get(observation_key)
                        if previous is not None and previous[:2] == (missing, latest) and observed >= previous[2] + 2:
                            self._connection.execute(
                                "INSERT INTO buy_origin_adjustments(account_alias, code, quantity, observed_at) VALUES (?, ?, ?, ?)",
                                (account_alias, code, str(missing), as_of.isoformat()),
                            )
                            self._origin_balance_observations.pop(observation_key, None)
                        elif previous is None or previous[:2] != (missing, latest):
                            self._origin_balance_observations[observation_key] = (missing, latest, observed)
                    else:
                        self._origin_balance_observations.pop(observation_key, None)
                    consume(lots, missing)

            result: dict[str, list[dict[str, Any]]] = {}
            for code, lots in inventory.items():
                for lot in lots:
                    flow_id = lot["flow_id"]
                    if lot["owner"] != strategy_id or not flow_id or lot["quantity"] <= 0:
                        continue
                    result.setdefault(code, []).append({"flow_id": str(flow_id), "node_ids": lot["node_ids"], "quantity": lot["quantity"]})
            return result

    def strategy_buy_flow_quantities(
        self, strategy_id: str, *, account_alias: str,
        actual_quantities: dict[str, Decimal] | None = None,
        as_of: datetime | None = None,
    ) -> dict[str, dict[str, Decimal]]:
        result: dict[str, dict[str, Decimal]] = {}
        for code, lots in self.strategy_buy_origin_lots(
            strategy_id, account_alias=account_alias,
            actual_quantities=actual_quantities, as_of=as_of,
        ).items():
            quantities = result.setdefault(code, {})
            for lot in lots:
                flow_id = lot["flow_id"]
                quantities[flow_id] = quantities.get(flow_id, Decimal("0")) + lot["quantity"]
        return result

    def strategy_position_quantities(
        self,
        strategy_id: str,
        *,
        account_alias: str | None = None,
    ) -> dict[str, Decimal]:
        """Derive remaining owned quantities, including explicit account sells."""
        quantities: dict[str, Decimal] = {}
        for row in reversed(self.strategy_accounting_orders(strategy_id, account_alias=account_alias)):
            request = row["request"]
            code = str(request["code"])
            quantity = Decimal(str(row["filled_quantity"]))
            change = quantity if request["side"] == "BUY" else -quantity
            quantities[code] = quantities.get(code, Decimal("0")) + change
        return {code: quantity for code, quantity in quantities.items() if quantity > 0}

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
                "UPDATE buy_origin_adjustments SET account_alias = ? WHERE account_alias = ?",
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
            **({"accounting_sequence": row["accounting_sequence"]} if "accounting_sequence" in row.keys() else {}),
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
