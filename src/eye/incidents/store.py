"""Single-writer operations store, independent of the simulated world."""

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import duckdb

from eye.agents.reporter import Reporter
from eye.config import ReporterSettings
from eye.contracts import (
    Anomaly,
    Incident,
    InvestigationResult,
    SellerAlert,
    SellerAlertAck,
    SentinelDecision,
    TrailStep,
)


def canonical(segment: dict[str, str]) -> str:
    return ",".join(f"{key}={value}" for key, value in sorted(segment.items()))


def fingerprint(
    root_node: str, segment: dict[str, str], cause_type: str = "pending"
) -> str:
    return f"{root_node}|{canonical(segment)}|{cause_type}"


def _overlap(left: dict[str, str], right: dict[str, str]) -> bool:
    return all(right[key] == value for key, value in left.items() if key in right)


class IncidentStore:
    def __init__(
        self,
        path: str | Path = "data/ops.duckdb",
        *,
        path_exists: Callable[[str, str], bool] = lambda src, dst: False,
        reporter: Callable[[InvestigationResult], Awaitable[tuple[str, str]]]
        | None = None,
        notify: Callable[[SellerAlert], Awaitable[SellerAlertAck]] | None = None,
        settings: ReporterSettings | None = None,
    ) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = duckdb.connect(str(path))
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS incidents (id VARCHAR PRIMARY KEY, incident JSON, trigger_id VARCHAR, ticks INTEGER DEFAULT 0)"
        )
        self.db.execute(
            "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS lifecycle_ts TIMESTAMPTZ"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS trail_steps (id VARCHAR PRIMARY KEY, investigation_id VARCHAR, step JSON)"
        )
        self.path_exists = path_exists
        self.reporter = reporter
        self.notify = notify
        self.settings = settings or ReporterSettings()

    def close(self) -> None:
        self.db.close()

    def all(self) -> list[Incident]:
        return [
            Incident.model_validate_json(row[0])
            for row in self.db.execute(
                "SELECT incident FROM incidents ORDER BY json_extract_string(incident, '$.opened_ts'), id"
            ).fetchall()
        ]

    def get(self, incident_id: str) -> Incident | None:
        row = self.db.execute(
            "SELECT incident FROM incidents WHERE id = ?", [incident_id]
        ).fetchone()
        return Incident.model_validate_json(row[0]) if row else None

    def _save(self, incident: Incident) -> None:
        self.db.execute(
            "INSERT INTO incidents (id, incident) VALUES (?, ?) ON CONFLICT (id) DO UPDATE SET incident = excluded.incident",
            [incident.id, incident.model_dump_json()],
        )

    def add_step(self, step: TrailStep) -> None:
        self.db.execute(
            "INSERT INTO trail_steps VALUES (?, ?, ?) ON CONFLICT (id) DO UPDATE SET step = excluded.step",
            [step.id, step.investigation_id, step.model_dump_json()],
        )

    def trail(self, investigation_id: str) -> list[TrailStep]:
        steps = self.db.execute(
            "SELECT step FROM trail_steps WHERE investigation_id = ?",
            [investigation_id],
        ).fetchall()
        return sorted(
            (TrailStep.model_validate_json(row[0]) for row in steps),
            key=lambda step: step.n,
        )

    @staticmethod
    def _active(incident: Incident) -> bool:
        return incident.merged_into is None and incident.status in {
            "investigating",
            "open",
            "persisting",
        }

    def match(
        self,
        decision: SentinelDecision,
        graph: Callable[[str, str], bool] | None = None,
    ) -> Incident | None:
        if decision.action != "investigate":
            return None
        entry = decision.anomaly
        provisional = fingerprint(entry.metric, entry.segment)
        incidents = self.all()
        active = [item for item in incidents if self._active(item)]
        exact = [
            item
            for item in active
            if fingerprint(item.entry.metric, item.entry.segment) == provisional
        ]
        downstream = [
            item
            for item in active
            if item.root_cause
            and _overlap(item.root_cause.segment, entry.segment)
            and (graph or self.path_exists)(item.root_cause.root_node, entry.metric)
        ]
        for item in exact + downstream:
            item.occurrences += 1
            item.last_seen_ts = item.updated_ts = entry.ts
            self._save(item)
            return item
        return self._new(entry)

    def _new(self, entry: Anomaly) -> Incident:
        item = Incident(
            id=f"inc_{uuid4().hex}",
            fingerprint=fingerprint(entry.metric, entry.segment),
            title=entry.metric[:89],
            status="investigating",
            verdict=None,
            opened_ts=entry.ts,
            updated_ts=entry.ts,
            last_seen_ts=entry.ts,
            entry=entry,
            summary="Investigation pending.",
            impact_eur_per_hour=entry.impact_eur_per_hour,
            impact_eur_total=0,
            affected_actors=[],
            contributions=[],
            root_cause=None,
            hypotheses=[],
            investigation_ids=[],
            open_questions=[],
            suggested_actions=[],
        )
        self._save(item)
        return item

    def _merge(self, keeper: Incident, newer: Incident) -> Incident:
        keeper.investigation_ids = list(
            dict.fromkeys(keeper.investigation_ids + newer.investigation_ids)
        )
        keeper.related_incident_ids = list(
            dict.fromkeys(keeper.related_incident_ids + newer.related_incident_ids)
        )
        keeper.related_incident_ids = [
            id_
            for id_ in keeper.related_incident_ids
            if id_ not in {keeper.id, newer.id}
        ]
        keeper.occurrences += newer.occurrences
        keeper.last_seen_ts = max(keeper.last_seen_ts, newer.last_seen_ts)
        keeper.updated_ts = max(keeper.updated_ts, newer.updated_ts)
        keeper.affected_actors = list(
            dict.fromkeys(keeper.affected_actors + newer.affected_actors)
        )
        keeper.notifications.extend(newer.notifications)
        keeper.impact_eur_total += newer.impact_eur_total
        newer.merged_into = keeper.id
        self._save(newer)
        self._save(keeper)
        return keeper

    async def upsert(self, result: InvestigationResult) -> Incident:
        root = result.root_cause
        final = fingerprint(
            root.root_node if root else result.entry.metric,
            root.segment if root else result.entry.segment,
            root.cause_type if root else "unknown",
        )
        items = self.all()
        item = next(
            (
                i
                for i in items
                if i.merged_into is None
                and (
                    result.investigation_id in i.investigation_ids
                    or (i.status == "investigating" and i.entry.id == result.entry.id)
                )
            ),
            None,
        )
        reopened = next(
            (
                i
                for i in items
                if i.merged_into is None
                and i.status == "resolved"
                and i.fingerprint == final
                and i.resolved_ts is not None
                and timedelta(0)
                <= result.entry.ts - i.resolved_ts
                <= timedelta(hours=24)
            ),
            None,
        )
        if reopened is not None:
            if item is not None and item.id != reopened.id:
                item = self._merge(reopened, item)
            else:
                item = reopened
                item.occurrences += 1
        if item is None:
            item = self._new(result.entry)
        assert item is not None
        title, summary = await (self.reporter or Reporter().write)(result)
        item.fingerprint = final
        item.title, item.summary = title, summary
        item.status = {"INNOCENT": "innocent", "DATA_ISSUE": "deferred"}.get(
            result.verdict, "open"
        )
        item.verdict = result.verdict
        item.resolved_ts = None
        item.normal_ticks = 0
        item.updated_ts = item.last_seen_ts = result.entry.ts
        item.entry = result.entry
        item.impact_eur_per_hour = result.entry.impact_eur_per_hour
        for field in (
            "root_cause",
            "hypotheses",
            "contributions",
            "affected_actors",
            "open_questions",
            "suggested_actions",
        ):
            setattr(item, field, getattr(result, field))
        item.investigation_ids = list(
            dict.fromkeys(item.investigation_ids + [result.investigation_id])
        )
        self._save(item)
        self.db.execute(
            "UPDATE incidents SET trigger_id = ?, ticks = 0 WHERE id = ?",
            [result.trigger, item.id],
        )
        for candidate in self.all():
            other = self.get(candidate.id)
            assert other is not None
            if other.id == item.id or not self._active(other) or not self._active(item):
                continue
            trigger = self.db.execute(
                "SELECT trigger_id FROM incidents WHERE id = ?", [other.id]
            ).fetchone()[0]
            same_trigger = bool(result.trigger and trigger == result.trigger)
            chain = bool(
                root
                and other.root_cause
                and (
                    self.path_exists(root.root_node, other.root_cause.root_node)
                    or self.path_exists(other.root_cause.root_node, root.root_node)
                )
            )
            if other.fingerprint == item.fingerprint:
                older, newer = sorted([item, other], key=lambda i: (i.opened_ts, i.id))
                item = self._merge(older, newer)
            elif (
                same_trigger
                or chain
                or (root and trigger == root.id)
                or (other.root_cause and result.trigger == other.root_cause.id)
            ):
                item.related_incident_ids = list(
                    dict.fromkeys(item.related_incident_ids + [other.id])
                )
                other.related_incident_ids = list(
                    dict.fromkeys(other.related_incident_ids + [item.id])
                )
                self._save(other)
                self._save(item)
                item_trigger = next(
                    (h for h in item.hypotheses if h.id == result.trigger), None
                )
                other_trigger = next(
                    (h for h in other.hypotheses if h.id == result.trigger), None
                )
                if (
                    same_trigger
                    or (root and trigger == root.id)
                    or (other.root_cause and result.trigger == other.root_cause.id)
                ):
                    if other.root_cause and (
                        result.trigger == other.root_cause.id
                        or (other_trigger and other.root_cause.id == other_trigger.id)
                    ):
                        item = self._merge(other, item)
                    elif root and (
                        trigger == root.id
                        or (item_trigger and root.id == item_trigger.id)
                    ):
                        item = self._merge(item, other)
                    else:
                        older, newer = sorted(
                            [item, other], key=lambda i: (i.opened_ts, i.id)
                        )
                        item = self._merge(older, newer)
        if result.affected_sellers and self.notify:
            alert = SellerAlert(
                incident_id=item.id,
                seller_ids=result.affected_sellers,
                skus=[],
                summary=item.summary,
                recommended_action="; ".join(result.suggested_actions),
            )
            try:
                ack = SellerAlertAck.model_validate(await self.notify(alert))
                notification = {
                    "to": "selleros",
                    "ts": result.entry.ts.isoformat(),
                    "ok": ack.received,
                    "ack": ack.model_dump(),
                }
            except (OSError, RuntimeError, ValueError, TypeError):
                notification = {
                    "to": "selleros",
                    "ts": result.entry.ts.isoformat(),
                    "ok": False,
                }
            item.notifications.append(notification)
        self._save(item)
        return item

    def lifecycle(
        self,
        ts: datetime,
        z_lookup: Callable[
            [str, dict[str, str], datetime], float | tuple[float, float]
        ],
    ) -> list[Incident]:
        changed = []
        for item in self.all():
            last_tick = self.db.execute(
                "SELECT lifecycle_ts FROM incidents WHERE id = ?", [item.id]
            ).fetchone()[0]
            if (
                item.merged_into
                or item.status not in {"open", "persisting", "deferred"}
                or ts <= (last_tick or item.opened_ts)
            ):
                continue
            reading = z_lookup(item.entry.metric, item.entry.segment, ts)
            z, completeness = reading if isinstance(reading, tuple) else (reading, None)
            ticks = (
                self.db.execute(
                    "SELECT ticks FROM incidents WHERE id = ?", [item.id]
                ).fetchone()[0]
                + 1
            )
            if item.status == "deferred":
                if (
                    completeness is not None
                    and completeness >= 0.9
                    and abs(z) < self.settings.resolve_z
                ):
                    item.status = "resolved"
            else:
                item.normal_ticks = (
                    item.normal_ticks + 1 if abs(z) < self.settings.resolve_z else 0
                )
                if item.normal_ticks >= self.settings.resolve_after_ticks:
                    item.status = "resolved"
                elif abs(z) >= self.settings.resolve_z and ticks >= 2:
                    item.status = "persisting"
                if item.status != "resolved":
                    item.impact_eur_total += item.impact_eur_per_hour
            if item.status == "resolved":
                item.resolved_ts = ts
            item.updated_ts = ts
            self._save(item)
            self.db.execute(
                "UPDATE incidents SET ticks = ?, lifecycle_ts = ? WHERE id = ?",
                [ticks, ts, item.id],
            )
            changed.append(item)
        return changed
