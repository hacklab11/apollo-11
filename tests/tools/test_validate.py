from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import duckdb
import numpy as np
import pytest

from eye.config import Settings
from eye.contracts import Series, SeriesPoint
from eye.registry.engine import MetricEngine
from eye.registry.loader import load_registry
from eye.sim.engine import Simulator
from eye.tools.drill import decompose, mix_rate_split
from eye.tools.identity import parse_identity
from eye.tools.validate import lagged_correlation, validate_breakdown, validate_identity


@pytest.fixture(scope="module")
def baseline(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, datetime]:
    settings = Settings()
    settings = settings.model_copy(
        update={"sim": settings.sim.model_copy(update={"history_days": 4})}
    )
    path = tmp_path_factory.mktemp("tools") / "world.duckdb"
    with closing(Simulator(settings, db_path=path)) as simulator:
        simulator.backfill()
    return path, settings.sim.start_ts


@pytest.mark.parametrize("identity_id", [f"F{i}" for i in range(1, 9)])
def test_baseline_identities(baseline: tuple[Path, datetime], identity_id: str) -> None:
    path, end = baseline
    start = end - timedelta(hours=6)
    with duckdb.connect(str(path), read_only=True) as con:
        registry = load_registry(con)
        engine = MetricEngine(con, registry)
        identity = next(i for i in registry.identities if i.id == identity_id)
        parsed = parse_identity(identity.text)
        names = [parsed.target, *(t.name for t in parsed.terms)]
        pending = [n for n in names if registry.get(n).status == "pending"]
        if pending:
            pytest.xfail(f"Main simulator does not populate identity inputs: {pending}")
        result = validate_identity(engine, identity.text, start, end)
        assert result.passed, (identity.id, result)
        assert result.evidence["median_residual"] < 0.005
        excluded = validate_identity(
            engine, identity.text, start, end, segments=[], scenario_hours=[start]
        )
        assert excluded.evidence["n"] == 5
        assert excluded.evidence["skipped"] == 1


def test_baseline_breakdowns(baseline: tuple[Path, datetime]) -> None:
    path, end = baseline
    start = end - timedelta(hours=6)
    with duckdb.connect(str(path), read_only=True) as con:
        engine = MetricEngine(con, load_registry(con))
        for metric in ("orders", "search_ctr", "checkout_conversion"):
            assert validate_breakdown(engine, metric, "country", start, end).passed
        split = mix_rate_split(
            engine,
            "search_ctr",
            "country",
            start,
            end,
            start - timedelta(days=1),
            end - timedelta(days=1),
        )
        assert sum(split.totals.values()) == pytest.approx(split.delta, abs=1e-9)
        result = decompose(
            engine,
            "gmv",
            start,
            end,
            start - timedelta(days=1),
            end - timedelta(days=1),
        )
        assert sum(r.share for r in result.contributions) == pytest.approx(1)


class SyntheticEngine:
    def __init__(self, values: dict[str, list[float | None]]) -> None:
        self.values = values
        self.start = datetime(2026, 1, 1, tzinfo=UTC)

    def query_metric(
        self, name: str, start: datetime, end: datetime, segment: dict | None = None
    ) -> list[Series]:
        return [
            Series(
                metric=name,
                segment={},
                points=[
                    SeriesPoint(ts=self.start + timedelta(hours=i), value=value)
                    for i, value in enumerate(self.values[name])
                    if start <= self.start + timedelta(hours=i) < end
                ],
            )
        ]


def test_shifted_correlation_and_empty() -> None:
    rng = np.random.default_rng(42)
    values = (100 + rng.normal(0, 8, 720)).tolist()
    engine = SyntheticEngine({"src": values, "dst": [None] * 24 + values[:-24]})
    result = lagged_correlation(
        engine,
        "src",
        "dst",
        48,
        engine.start + timedelta(days=9),
        engine.start + timedelta(days=30),
    )
    assert abs(result.best_lag - 24) <= 2
    assert result.corr > 0.99
    excluded = lagged_correlation(
        engine,
        "src",
        "dst",
        0,
        engine.start,
        engine.start + timedelta(hours=2),
        scenario_hours=[engine.start],
    )
    assert excluded.n == 0
    assert excluded.best_lag is None
    with pytest.raises(ValueError, match="nonnegative"):
        lagged_correlation(engine, "src", "dst", -1)


def test_identity_empty_mismatch_and_exclusion() -> None:
    engine = SyntheticEngine({"lhs": [1.0, 1.0, 2.0], "rhs": [1.0, 1.0, 20.0]})
    engine._registry = SimpleNamespace(get=lambda name: SimpleNamespace(dimensions=[]))
    end = engine.start + timedelta(hours=3)
    assert not validate_identity(engine, "lhs = rhs", engine.start, end).passed
    result = validate_identity(
        engine,
        "lhs = rhs",
        engine.start,
        end,
        scenario_hours=[end - timedelta(hours=1)],
    )
    assert result.passed
    assert result.evidence["n"] == 2
    empty = validate_identity(engine, "lhs = rhs", end, end + timedelta(hours=1))
    assert not empty.passed
    assert empty.evidence["n"] == 0
    with pytest.raises(ValueError, match="shared dimensions"):
        validate_identity(
            engine, "lhs = rhs", engine.start, end, segments=[{"country": "DE"}]
        )
    with pytest.raises(ValueError, match="precede"):
        validate_identity(engine, "lhs = rhs", end, engine.start)


def test_shift_share_entering_exiting_and_zero_denominator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime(2026, 1, 8, tzinfo=UTC)

    def sums(
        engine: object,
        name: str,
        a: datetime,
        b: datetime,
        segment: dict,
        group: list[str],
        *,
        by_ts: bool,
    ) -> dict:
        rows = (
            {("old",): (10.0, 20.0), ("shared",): (30.0, 60.0)}
            if a < start
            else {("new",): (60.0, 80.0), ("shared",): (10.0, 20.0)}
        )
        return {key: {a: pair} for key, pair in rows.items()}

    monkeypatch.setattr("eye.tools.drill.ratio_sums", sums)
    result = mix_rate_split(None, "ratio", "country", start, start + timedelta(hours=1))
    assert sum(result.totals.values()) == pytest.approx(result.delta, abs=1e-9)
    assert sum(r.delta for r in result.contributions) == pytest.approx(
        result.delta, abs=1e-9
    )
    monkeypatch.setattr("eye.tools.drill.ratio_sums", lambda *args, **kwargs: {})
    with pytest.raises(ValueError, match="positive denominator"):
        mix_rate_split(None, "ratio", "country", start, start + timedelta(hours=1))
