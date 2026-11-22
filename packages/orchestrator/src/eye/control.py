"""Persistent human control plane and append-only audit ledger."""

import hashlib
import json
import threading
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool


class RunKilled(Exception):
    pass


def audit_hash(prev_hash: str, record: dict[str, Any]) -> str:
    return hashlib.sha256(
        (prev_hash + json.dumps(record, sort_keys=True, separators=(",", ":"))).encode()
    ).hexdigest()


class ControlPlane:
    def __init__(self, url: str) -> None:
        options: dict[str, Any] = {}
        if url.startswith("sqlite"):
            options["connect_args"] = {"check_same_thread": False}
            if url in {"sqlite://", "sqlite:///:memory:"}:
                options["poolclass"] = StaticPool
        self.engine = create_engine(url, **options)
        self.lock = threading.RLock()
        schema = MetaData()
        self.audit_log = Table(
            "audit_log",
            schema,
            Column("id", Integer, primary_key=True),
            Column("prev_hash", String, nullable=False),
            Column("hash", String, nullable=False),
            Column("actor", String, nullable=False),
            Column("action", String, nullable=False),
            Column("run_id", String),
            Column("incident_id", String),
            Column("agent", String),
            Column("detail", JSON, nullable=False),
            Column("ts", String, nullable=False),
        )
        self.switches = Table(
            "kill_switches",
            schema,
            Column("scope", String, primary_key=True),
            Column("target", String, primary_key=True),
            Column("active", Boolean, nullable=False),
        )
        self.approvals = Table(
            "approvals",
            schema,
            Column("id", String, primary_key=True),
            Column("run_id", String, nullable=False),
            Column("incident_id", String),
            Column("tool_name", String, nullable=False),
            Column("args", JSON, nullable=False),
            Column("status", String, nullable=False),
            Column("decided_by", String),
        )
        schema.create_all(self.engine)
        with self.engine.begin() as conn:
            if self.engine.dialect.name == "postgresql":
                conn.execute(
                    text(
                        "CREATE OR REPLACE FUNCTION reject_audit_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'audit_log is insert-only'; END $$"
                    )
                )
                conn.execute(
                    text("DROP TRIGGER IF EXISTS audit_immutable ON audit_log")
                )
                conn.execute(
                    text(
                        "CREATE TRIGGER audit_immutable BEFORE UPDATE OR DELETE ON audit_log FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation()"
                    )
                )
            else:
                for operation in ("UPDATE", "DELETE"):
                    conn.execute(
                        text(
                            f"CREATE TRIGGER IF NOT EXISTS audit_no_{operation.lower()} BEFORE {operation} ON audit_log BEGIN SELECT RAISE(ABORT, 'audit_log is insert-only'); END"
                        )
                    )

    def close(self) -> None:
        self.engine.dispose()

    def _audit(
        self,
        conn: Any,
        *,
        actor: str,
        action: str,
        run_id: str | None = None,
        incident_id: str | None = None,
        agent: str | None = None,
        detail: dict | None = None,
    ) -> str:
        if self.engine.dialect.name == "postgresql":
            conn.execute(text("LOCK TABLE audit_log IN EXCLUSIVE MODE"))
        previous = (
            conn.execute(
                select(self.audit_log.c.hash)
                .order_by(self.audit_log.c.id.desc())
                .limit(1)
            ).scalar()
            or "0" * 64
        )
        record = {
            "actor": actor,
            "action": action,
            "run_id": run_id,
            "incident_id": incident_id,
            "agent": agent,
            "detail": detail or {},
            "ts": datetime.now(UTC).isoformat(),
        }
        digest = audit_hash(previous, record)
        conn.execute(
            self.audit_log.insert().values(**record, prev_hash=previous, hash=digest)
        )
        return digest

    def audit(self, **record: Any) -> str:
        with self.lock, self.engine.begin() as conn:
            return self._audit(conn, **record)

    def audit_rows(self) -> list[dict]:
        with self.lock, self.engine.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    select(self.audit_log).order_by(self.audit_log.c.id)
                ).mappings()
            ]

    def set_kill(
        self,
        scope: Literal["run", "tenant", "system"],
        target: str,
        active: bool,
        actor: str,
    ) -> None:
        with self.lock, self.engine.begin() as conn:
            predicate = (self.switches.c.scope == scope) & (
                self.switches.c.target == target
            )
            if conn.execute(select(self.switches).where(predicate)).first():
                conn.execute(
                    self.switches.update().where(predicate).values(active=active)
                )
            else:
                conn.execute(
                    self.switches.insert().values(
                        scope=scope, target=target, active=active
                    )
                )
            self._audit(
                conn,
                actor=actor,
                action="kill" if active else "clear_kill",
                detail={"scope": scope, "target": target},
            )

    def check(self, run_id: str, tenant: str) -> None:
        with self.lock, self.engine.connect() as conn:
            rows = conn.execute(
                select(self.switches).where(self.switches.c.active.is_(True))
            ).mappings()
            if any(
                row["scope"] == "system"
                or (row["scope"], row["target"])
                in {("run", run_id), ("tenant", tenant)}
                for row in rows
            ):
                raise RunKilled(run_id)

    def pending(
        self,
        run_id: str,
        incident_id: str | None,
        tool_name: str,
        args: dict,
        *,
        identifier: str | None = None,
    ) -> str:
        identifier = identifier or uuid4().hex
        with self.lock, self.engine.begin() as conn:
            conn.execute(
                self.approvals.insert().values(
                    id=identifier,
                    run_id=run_id,
                    incident_id=incident_id,
                    tool_name=tool_name,
                    args=args,
                    status="pending",
                )
            )
            self._audit(
                conn,
                actor="orchestrator",
                action="approval_requested",
                run_id=run_id,
                incident_id=incident_id,
                detail={"approval_id": identifier, "tool_name": tool_name},
            )
        return identifier

    def approval(self, identifier: str) -> dict | None:
        with self.lock, self.engine.connect() as conn:
            row = (
                conn.execute(
                    select(self.approvals).where(self.approvals.c.id == identifier)
                )
                .mappings()
                .one_or_none()
            )
            return dict(row) if row is not None else None

    def ensure_pending(
        self, identifier: str, run_id: str, incident_id: str, tool_name: str, args: dict
    ) -> dict:
        row = self.approval(identifier)
        if row is None:
            try:
                self.pending(
                    run_id, incident_id, tool_name, args, identifier=identifier
                )
            except IntegrityError:
                # Another service replica may have registered the same action.
                row = self.approval(identifier)
                if row is None:
                    raise
            else:
                row = self.approval(identifier)
        assert row is not None
        if (
            row["tool_name"] != tool_name
            or row["args"] != args
            or row["incident_id"] != incident_id
        ):
            raise ValueError("approval does not match the requested action")
        return row

    def approval_rows(self) -> list[dict]:
        with self.lock, self.engine.connect() as conn:
            return [
                dict(row) for row in conn.execute(select(self.approvals)).mappings()
            ]

    def decide(self, identifier: str, approved: bool, actor: str) -> None:
        with self.lock, self.engine.begin() as conn:
            updated = conn.execute(
                self.approvals.update()
                .where(
                    (self.approvals.c.id == identifier)
                    & (self.approvals.c.status == "pending")
                )
                .values(status="approved" if approved else "rejected", decided_by=actor)
            )
            if updated.rowcount != 1:
                raise ValueError("unknown or already decided approval")
            self._audit(
                conn,
                actor=actor,
                action="approval_decided",
                detail={"approval_id": identifier, "approved": approved},
            )
