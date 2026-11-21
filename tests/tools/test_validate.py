from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import duckdb
import numpy as np
import pytest

from eye.config import Settings
from eye.contracts import MetricDef, Series, SeriesPoint
from eye.registry.engine import MetricEngine
from eye.registry.loader import Registry, load_registry
from eye.sim.engine import Simulator
from eye.tools.drill import decompose, mix_rate_split
from eye.tools.identity import parse_identity
from eye.tools.validate import lagged_correlation, validate_breakdown, validate_identity


@pytest.fixture
def weighted_engine() -> MetricEngine:
    with duckdb.connect() as con:
        con.execute(
            "CREATE TABLE facts (ts TIMESTAMPTZ, country VARCHAR, num DOUBLE, den DOUBLE)"
        )
        con.execute(
            "INSERT INTO facts VALUES ('2020-01-01', 'DE', 100, 1), ('2020-01-01', 'FR', 900, 3)"
        )
        metrics = {
            name: MetricDef(
                name=name,
                description=name,
                unit=unit,
                domain="orders",
                kind="sql",
                dimensions=["country"],
                sql=f"SELECT ts, country, {column} AS num FROM facts",
            )
            for name, unit, column in (
                ("gmv", "eur", "num"),
                ("orders", "count", "den"),
            )
        }
        metrics["aov"] = MetricDef(
            name="aov",
            description="Weighted order value",
            unit="eur",
            domain="orders",
            kind="formula",
            dimensions=["country"],
            expression="gmv / orders",
        )
        registry = Registry(
            metrics,
            {name: () for name in metrics},
            {"gmv": False, "orders": False},
            [],
            [],
        )
        yield MetricEngine(con, registry)


def test_average_breakdown_pools_weights(weighted_engine: MetricEngine) -> None:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    assert weighted_engine.metric_value("aov", start) == 250
    result = validate_breakdown(
        weighted_engine, "aov", "country", start, start + timedelta(hours=1)
    )
    assert result.passed
    assert result.evidence["n"] == 1


@pytest.mark.parametrize(
    "num, den, passed",
    [(0, 0, True), (10, 0, False), (None, 0, False), (0, None, False)],
)
def test_breakdown_zero_denominator_segment(
    weighted_engine: MetricEngine, num: float | None, den: float | None, passed: bool
) -> None:
    weighted_engine._con.execute(
        "UPDATE facts SET num = ?, den = ? WHERE country = 'FR'", [num, den]
    )
    start = datetime(2020, 1, 1, tzinfo=UTC)
    result = validate_breakdown(
        weighted_engine, "aov", "country", start, start + timedelta(hours=1)
    )
    assert result.passed is passed
    if passed:
        assert result.evidence["n"] == 1


def test_validation_defaults_to_simulated_data_clock(
    weighted_engine: MetricEngine,
) -> None:
    result = validate_breakdown(weighted_engine, "orders", "country")
    assert result.passed
    assert result.evidence["n"] == 1
    assert result.evidence["skipped"] == 14 * 24 - 1
    assert validate_identity(weighted_engine, "orders = orders").passed
    assert validate_breakdown(weighted_engine, "aov", "country").passed
    assert lagged_correlation(weighted_engine, "orders", "orders").n == 0
    weighted_engine._con.execute("DELETE FROM facts")
    with pytest.raises(ValueError, match="without simulated history"):
        validate_breakdown(weighted_engine, "orders", "country")


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


def test_default_window_on_fresh_simulator_baseline(
    baseline: tuple[Path, datetime],
) -> None:
    path, _ = baseline
    with duckdb.connect(str(path), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM partition_status").fetchone()[0] == 0
        engine = MetricEngine(con, load_registry(con))
        result = validate_breakdown(engine, "orders", "country")
        assert result.passed
        assert result.evidence["n"] == 4 * 24


class SyntheticEngine(MetricEngine):
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


def test_identity_checks_single_dimension_slices() -> None:
    start = datetime(2020, 1, 1, tzinfo=UTC)

    class SliceEngine(MetricEngine):
        def __init__(self) -> None:
            self._registry = SimpleNamespace(
                get=lambda name: SimpleNamespace(dimensions=["country", "platform"])
            )

        def query_metric(
            self,
            name: str,
            a: datetime,
            b: datetime,
            segment: dict | None = None,
            group_by: list[str] = (),
        ) -> list[Series]:
            if group_by:
                dim = group_by[0]
                return [
                    Series(metric=name, segment={dim: value}, points=[])
                    for value in (
                        {"country": ["DE", "FR"], "platform": ["web", "ios"]}[dim]
                    )
                ]
            segment = segment or {}
            value = 4 / 2 ** len(segment)
            if name == "lhs" and segment == {"country": "DE"}:
                value = 3
            return [
                Series(
                    metric=name,
                    segment=segment,
                    points=[SeriesPoint(ts=start, value=value)],
                )
            ]

    result = validate_identity(
        SliceEngine(), "lhs = rhs", start, start + timedelta(hours=1)
    )
    assert not result.passed
    failed = [g["segment"] for g in result.evidence["groups"] if not g["passed"]]
    assert failed == [{"country": "DE"}]


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
    result = mix_rate_split(
        SyntheticEngine({}), "ratio", "country", start, start + timedelta(hours=1)
    )
    assert sum(result.totals.values()) == pytest.approx(result.delta, abs=1e-9)
    assert sum(r.delta for r in result.contributions) == pytest.approx(
        result.delta, abs=1e-9
    )
    monkeypatch.setattr("eye.tools.drill.ratio_sums", lambda *args, **kwargs: {})
    with pytest.raises(ValueError, match="positive denominator"):
        mix_rate_split(
            SyntheticEngine({}), "ratio", "country", start, start + timedelta(hours=1)
        )
