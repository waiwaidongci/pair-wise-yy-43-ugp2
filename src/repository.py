from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dispersant_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL UNIQUE,
                    approved_total REAL NOT NULL CHECK(approved_total > 0),
                    unit TEXT NOT NULL DEFAULT 'L',
                    allowed_areas TEXT NOT NULL,
                    valid_from TEXT,
                    valid_until TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS spray_usages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    batch_id INTEGER NOT NULL REFERENCES dispersant_batches(id) ON DELETE RESTRICT,
                    amount REAL NOT NULL CHECK(amount > 0),
                    started_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    area TEXT NOT NULL,
                    latitude REAL NOT NULL CHECK(latitude BETWEEN -90 AND 90),
                    longitude REAL NOT NULL CHECK(longitude BETWEEN -180 AND 180),
                    vessel TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','withdrawn')),
                    withdraw_reason TEXT,
                    withdrawn_by TEXT,
                    withdrawn_at TEXT,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE INDEX IF NOT EXISTS ix_spray_batch ON spray_usages(batch_id);
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    # ---- 消油剂批次台账 ----
    def create_batch(self, batch_no: str, approved_total: float, unit: str,
                     allowed_areas: List[str], valid_from: Optional[str],
                     valid_until: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO dispersant_batches(batch_no, approved_total, unit,
                       allowed_areas, valid_from, valid_until, version,
                       created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (batch_no, approved_total, unit, json.dumps(allowed_areas, ensure_ascii=False),
                     valid_from, valid_until, 1, actor, now, now),
                )
                batch_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("批次号已存在") from exc
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM dispersant_batches WHERE id=?", (batch_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("批次不存在")
        return self._batch(row)

    def list_batches(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM dispersant_batches ORDER BY id DESC"
            ).fetchall()
        return [self._batch(row) for row in rows]

    def update_batch(self, batch_id: int, approved_total: Optional[float],
                     allowed_areas: Optional[List[str]], valid_from: Optional[str],
                     valid_until: Optional[str], expected_version: Optional[int],
                     actor: str) -> Dict[str, Any]:
        now = utc_now()
        current = self.get_batch(batch_id)
        approved_total = current["approved_total"] if approved_total is None else approved_total
        allowed_areas = current["allowed_areas"] if allowed_areas is None else allowed_areas
        valid_from = current["valid_from"] if valid_from is None else valid_from
        valid_until = current["valid_until"] if valid_until is None else valid_until
        with self._lock, self.conn:
            if expected_version is None:
                cur = self.conn.execute(
                    """UPDATE dispersant_batches SET approved_total=?, allowed_areas=?,
                       valid_from=?, valid_until=?, version=version+1, updated_at=?
                       WHERE id=?""",
                    (approved_total, json.dumps(allowed_areas, ensure_ascii=False),
                     valid_from, valid_until, now, batch_id),
                )
            else:
                cur = self.conn.execute(
                    """UPDATE dispersant_batches SET approved_total=?, allowed_areas=?,
                       valid_from=?, valid_until=?, version=version+1, updated_at=?
                       WHERE id=? AND version=?""",
                    (approved_total, json.dumps(allowed_areas, ensure_ascii=False),
                     valid_from, valid_until, now, batch_id, expected_version),
                )
                if cur.rowcount == 0:
                    raise ConflictError("版本冲突，请刷新后重试")
        return self.get_batch(batch_id)

    @staticmethod
    def _batch(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["allowed_areas"] = json.loads(item["allowed_areas"])
        return item

    def used_amount(self, batch_id: int) -> float:
        with self._lock:
            row = self.conn.execute(
                "SELECT COALESCE(SUM(amount),0) AS n FROM spray_usages WHERE batch_id=? AND status='active'",
                (batch_id,),
            ).fetchone()
        return float(row["n"])

    # ---- 消油剂喷洒 ----
    def add_spray(self, item_id: int, batch_id: int, amount: float,
                  started_at: str, ended_at: str, area: str, latitude: float,
                  longitude: float, vessel: str, external_ref: Optional[str],
                  actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        self.get_batch(batch_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO spray_usages(item_id, batch_id, amount, started_at, ended_at,
                       area, latitude, longitude, vessel, status, external_ref,
                       created_by, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,'active',?,?,?)""",
                    (item_id, batch_id, amount, started_at, ended_at, area,
                     latitude, longitude, vessel, external_ref, actor, now),
                )
                spray_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("喷洒记录唯一标识已存在") from exc
        return self.get_spray(spray_id)

    def get_spray(self, spray_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM spray_usages WHERE id=?", (spray_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("喷洒记录不存在")
        return dict(row)

    def list_sprays(self, item_id: Optional[int] = None,
                    batch_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM spray_usages WHERE 1=1"
        params: List[Any] = []
        if item_id is not None:
            sql += " AND item_id=?"
            params.append(item_id)
        if batch_id is not None:
            sql += " AND batch_id=?"
            params.append(batch_id)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def active_sprays(self, batch_id: int, open_only: bool = True) -> List[Dict[str, Any]]:
        sql = """SELECT s.* FROM spray_usages s JOIN items i ON i.id=s.item_id
                 WHERE s.batch_id=? AND s.status='active'"""
        params: List[Any] = [batch_id]
        if open_only:
            sql += " AND i.status != 'closed'"
        sql += " ORDER BY s.id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def withdraw_spray(self, spray_id: int, reason: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE spray_usages SET status='withdrawn', withdraw_reason=?,
                   withdrawn_by=?, withdrawn_at=?
                   WHERE id=? AND status='active'""",
                (reason, actor, now, spray_id),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM spray_usages WHERE id=?", (spray_id,)
                ).fetchone()
                if exists is None:
                    raise NotFoundError("喷洒记录不存在")
                raise ConflictError("喷洒记录已撤回，不能重复撤回")
        return self.get_spray(spray_id)

    def close(self) -> None:
        with self._lock:
            self.conn.close()
