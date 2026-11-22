"""Generate and inspect a local synthetic world."""

import argparse
import json
from contextlib import closing
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import get_args

import duckdb

from eye.config import SimSettings, load_settings
from eye.sim.engine import Simulator
from eye.sim.scenarios import ScenarioId
from eye.sim.store import TABLES, WORLD_DB


def _play_schedule(value: str) -> dict[int, list[ScenarioId]]:
    schedule: dict[int, list[ScenarioId]] = {}
    for part in value.split(","):
        scenario, _, hours = part.strip().partition("@")
        if (
            scenario not in get_args(ScenarioId)
            or not hours.isascii()
            or not hours.isdecimal()
        ):
            raise argparse.ArgumentTypeError(
                "expected scenario IDs and nonnegative hours: S8@0"
            )
        schedule.setdefault(int(hours), []).append(scenario)
    return schedule


def _generate(
    days: int | None, seed: int | None, schedule: dict[int, list[ScenarioId]]
) -> None:
    inject = None
    if schedule:
        module_name = "eye.sim.scenarios"
        try:
            inject = import_module(module_name).inject
        except ModuleNotFoundError as exc:
            if exc.name != module_name:
                raise
            raise ValueError("scenarios not implemented") from None

    settings = load_settings()
    settings.sim = SimSettings.model_validate(
        settings.sim.model_dump()
        | ({} if days is None else {"history_days": days})
        | ({} if seed is None else {"seed": seed})
    )
    WORLD_DB.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=WORLD_DB.parent) as tmp:
        destination = Path(tmp) / WORLD_DB.name
        with closing(Simulator(settings, db_path=destination)) as sim:
            sim.backfill()
            if inject is not None:
                for index in range(max(schedule) + 1):
                    for scenario in schedule.get(index, ()):
                        inject(sim, scenario)
                    sim.step()
        # Replace only simulator tables while DuckDB holds the destination's writer lock.
        with duckdb.connect(str(WORLD_DB)) as con:
            con.execute("ATTACH ? AS generated (READ_ONLY)", [str(destination)])
            statements = [
                f"CREATE OR REPLACE TABLE {table} AS FROM generated.{table}"
                for table in TABLES
            ]
            con.execute(";".join(["BEGIN", *statements, "COMMIT"]))
    print(
        json.dumps(
            {
                "database": str(WORLD_DB),
                "history_hours": settings.sim.history_days * 24,
                "seed": settings.sim.seed,
            }
        )
    )


def _stats(con: duckdb.DuckDBPyConnection) -> None:
    start, end = con.execute("SELECT min(ts), max(ts) FROM traffic_hourly").fetchone()
    counts = {
        table: con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in TABLES
    }
    print(
        json.dumps(
            {
                "first_hour_utc": start.isoformat() if start else None,
                "last_hour_utc": end.isoformat() if end else None,
                "rows": counts,
            }
        )
    )


def _export(con: duckdb.DuckDBPyConnection, export_format: str, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for table in TABLES:
        destination = out / f"{table}.{export_format}"
        options = "FORMAT CSV, HEADER" if export_format == "csv" else "FORMAT PARQUET"
        con.execute(f"COPY {table} TO ? ({options})", [str(destination)])
    print(json.dumps({"out": str(out), "format": export_format, "tables": len(TABLES)}))


def main() -> None:
    parser = argparse.ArgumentParser(prog="eye-sim")
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate")
    generate.add_argument("--days", type=int)
    generate.add_argument("--seed", type=int)
    generate.add_argument("--play", type=_play_schedule, default={})
    export = commands.add_parser("export")
    export.add_argument("--format", required=True, choices=("parquet", "csv"))
    export.add_argument("--out", required=True, type=Path)
    commands.add_parser("stats")
    commands.add_parser("serve")
    args = parser.parse_args()
    try:
        if args.command == "serve":
            from eye.sim.service import serve

            serve()
        elif args.command == "generate":
            _generate(args.days, args.seed, args.play)
        else:
            with duckdb.connect(str(WORLD_DB), read_only=True) as con:
                con.execute("SET TimeZone = 'UTC'")
                if args.command == "stats":
                    _stats(con)
                else:
                    _export(con, args.format, args.out)
    except (ValueError, OSError, duckdb.Error) as exc:
        parser.error(str(exc))
