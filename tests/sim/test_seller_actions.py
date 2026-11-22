import importlib
import json
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from eye.config import Settings
from eye.sim import engine
from eye.sim.engine import Hour, Simulator
from eye.sim.market import Market
from eye.sim.seller_actions import JournalSellerActionFeed, SellerAction
from eye.sim.source import _world
from eye.sim.world import World, build_world

TS = datetime(2026, 11, 27, tzinfo=UTC)


def journal_record(status: str = "reverted", **fields: object) -> dict:
    return {
        "incident_id": "restore",
        "seller_id": "S-14",
        "sku": "E02",
        "status": status,
        **(
            {"price": 9.9, "restore_price": 99.0, "ts": TS.isoformat()}
            if status == "reverted"
            else {}
        ),
        **fields,
    }


def write_records(path: Path, *records: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def test_feed_missing_empty_dedup_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "actions.jsonl"
    feed = JournalSellerActionFeed(path)
    assert feed.pending() == []
    path.touch()
    assert feed.pending() == []
    revert = journal_record()
    pause = journal_record("paused")
    write_records(path, revert, revert, pause)
    actions = feed.pending()
    assert [(a.kind, a.price) for a in actions] == [
        ("set_price", 99),
        ("pause_repricer", None),
    ]
    assert feed.pending() == actions
    feed.ack([actions[0].key])
    assert feed.pending() == actions[1:]
    feed.ack([actions[1].key])
    assert feed.pending() == []
    assert JournalSellerActionFeed(path).pending() == actions
    write_records(path, pause, revert)
    assert JournalSellerActionFeed(path).pending() == list(reversed(actions))
    assert feed.pending() == []
    with pytest.raises(ValidationError):
        actions[0].price = 20


@pytest.mark.parametrize("tail", [b'{"status":', b'{"status":\n', b"{}", b'{"x":"\xc3'])
def test_feed_ignores_interrupted_tail(tmp_path: Path, tail: bytes) -> None:
    path = tmp_path / "actions.jsonl"
    write_records(path, journal_record())
    complete = path.read_bytes()
    path.write_bytes(complete + tail)
    feed = JournalSellerActionFeed(path)
    actions = feed.pending()
    assert len(actions) == 1
    feed.ack([actions[0].key])
    write_records(path, journal_record(), journal_record("paused"))
    assert [a.kind for a in feed.pending()] == ["pause_repricer"]


def test_feed_corrupt_earlier_line_raises(tmp_path: Path) -> None:
    path = tmp_path / "actions.jsonl"
    path.write_bytes(b'{"broken":\n{}\n')
    with pytest.raises(json.JSONDecodeError):
        JournalSellerActionFeed(path).pending()


def test_feed_skips_legacy_revert_without_restore_price(tmp_path: Path) -> None:
    path = tmp_path / "actions.jsonl"
    write_records(path, journal_record(restore_price=None), journal_record("paused"))
    assert [a.kind for a in JournalSellerActionFeed(path).pending()] == [
        "pause_repricer"
    ]


@pytest.mark.parametrize("price", [0, -1, float("inf"), float("nan")])
def test_feed_rejects_invalid_price(tmp_path: Path, price: float) -> None:
    path = tmp_path / "actions.jsonl"
    write_records(path, journal_record(restore_price=price))
    with pytest.raises(ValidationError):
        JournalSellerActionFeed(path).pending()


@pytest.mark.parametrize(
    ("kind", "price"),
    [("set_price", None), ("pause_repricer", 10), ("unknown", None)],
)
def test_action_validation(kind: str, price: float | None) -> None:
    with pytest.raises(ValidationError):
        SellerAction(key="invalid", seller_id="S-14", sku="E02", kind=kind, price=price)


@pytest.mark.parametrize("kind", ["set_price", "pause_repricer"])
def test_market_seller_override_survives_repricers(kind: str) -> None:
    world = build_world(42, joined_ts=TS)
    market = Market(world)
    offer = world.offers["E02"][0]
    offer.price = 9.9
    offer.repricer_mode = "beat"
    action = SellerAction(
        key="manual",
        seller_id=offer.seller_id,
        sku=offer.sku,
        kind=kind,
        price=99 if kind == "set_price" else None,
    )
    assert market.apply_seller_actions([action]) == [action]
    expected = 99 if kind == "set_price" else 9.9
    assert offer.price == expected
    market.run_repricers()
    assert offer.price == expected
    assert offer.repricer_mode == "fixed"
    assert market.apply_seller_actions([]) == []
    assert (
        market.apply_seller_actions(
            [
                action.model_copy(update={"sku": "missing"}),
                action.model_copy(update={"seller_id": "missing"}),
            ]
        )
        == []
    )


def small_world(seed: int, *, joined_ts: datetime) -> World:
    world = build_world(seed, joined_ts=joined_ts)
    keep = {"E02", "H01", "F01", "T01"}
    world.skus = {k: v for k, v in world.skus.items() if k in keep}
    world.offers = {k: v for k, v in world.offers.items() if k in keep}
    sellers = {o.seller_id for offers in world.offers.values() for o in offers}
    world.sellers = {k: v for k, v in world.sellers.items() if k in sellers}
    world.advertisers = {}
    return world


@pytest.fixture
def small_sim_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(engine, "build_world", small_world)
    settings = Settings()
    settings.sim = settings.sim.model_copy(update={"history_days": 0})
    return settings


@pytest.mark.parametrize("stale", [None, "price", "timestamp"])
def test_file_store_revert_reaches_marketplace(
    small_sim_settings: Settings,
    tmp_path: Path,
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    stale: str | None,
) -> None:
    monkeypatch.syspath_prepend(str(repo_root / "external" / "selleros"))
    data = importlib.import_module("selleros.data")
    directory = tmp_path / "selleros"
    directory.mkdir()
    feed = JournalSellerActionFeed(directory / "actions.jsonl")
    with closing(
        Simulator(small_sim_settings, db_path=":memory:", seller_action_feed=feed)
    ) as sim:
        sim.selleros_export.path = directory / "sellers.json"
        offer = next(o for o in sim.world.offers["E02"] if o.seller_id.startswith("S-"))
        old = offer.price
        sim.selleros_export.write(sim.now - timedelta(hours=1))
        offer.price = round(offer.cost / 10, 2)
        sim.selleros_export.record_manual_price(
            offer.seller_id, offer.sku, offer.price, sim.now
        )
        sim.selleros_export.write(sim.now)
        store = data.FileSellerStore(directory)
        seller = next(s for s in store.read().sellers if s.seller_id == offer.seller_id)
        intent = data.price_intent(
            seller, next(s for s in seller.skus if s.sku == offer.sku)
        )
        assert intent is not None and intent.verdict == "likely_error"
        assert store.revert_manual_price(
            "integration",
            seller.seller_id,
            offer.sku,
            intent,
            idempotency_key="integration",
        )
        if stale == "price":
            # The live offer is restored before the next SellerOS snapshot.
            offer.price = old
            offer.repricer_mode = "beat"
        elif stale == "timestamp":
            sim.selleros_export.record_manual_price(
                offer.seller_id, offer.sku, offer.price, sim.now + timedelta(minutes=1)
            )
        sim.step()
        if stale is not None:
            assert offer.repricer_mode != "fixed"
            assert feed.pending() == []
            with sim.store.cursor() as reader:
                assert reader.execute(
                    "SELECT count(*) FROM seller_actions"
                ).fetchone() == (0,)
            return
        assert offer.price == old
        sim.market.run_repricers()
        assert offer.price == old
        sim.step()
        assert offer.price == old
        with sim.store.cursor() as reader:
            assert reader.execute("SELECT count(*) FROM seller_actions").fetchone() == (
                1,
            )
            assert reader.execute(
                "SELECT price FROM offers_hourly WHERE seller_id = ? AND sku = ? ORDER BY ts",
                [offer.seller_id, offer.sku],
            ).fetchall() == [(old,), (old,)]
        history = [
            p
            for p in sim.selleros_export.history[offer.seller_id]
            if p["sku"] == offer.sku
        ]
        assert history[-1]["price"] == old
        assert history[-1]["source"] == "manual"


def test_reconstruction_replays_actions_at_consumed_hour(
    small_sim_settings: Settings,
    tmp_path: Path,
) -> None:
    path = tmp_path / "world.duckdb"
    with _world(small_sim_settings, None, path) as (sim, _):
        sim.step()
        offer = next(o for o in sim.world.offers["E02"] if o.seller_id.startswith("S-"))
        directory = sim.selleros_export.path.parent
        action = journal_record(seller_id=offer.seller_id, restore_price=77)
        write_records(directory / "actions.jsonl", action)
        applied_ts = sim.now
        sim.step()
        assert offer.price == 77
        expected = sim.world
        expected_history = sim.selleros_export.history
        expected_now = sim.now
    with _world(small_sim_settings, None, path) as (resumed, _):
        assert resumed.now == expected_now
        assert resumed.world == expected
        assert resumed.selleros_export.history == expected_history
        with resumed.store.cursor() as reader:
            assert reader.execute("SELECT ts FROM seller_actions").fetchall() == [
                (applied_ts,)
            ]
        # The default live feed re-reads the journal, but recorded keys must not reapply.
        resumed.step()
        with resumed.store.cursor() as reader:
            assert reader.execute("SELECT count(*) FROM seller_actions").fetchone() == (
                1,
            )
        offer = next(
            o for o in resumed.world.offers["E02"] if o.seller_id == action["seller_id"]
        )
        assert offer.price == 77


def test_replay_preserves_same_hour_action_order(
    small_sim_settings: Settings, tmp_path: Path
) -> None:
    path = tmp_path / "world.duckdb"
    with _world(small_sim_settings, None, path) as (sim, _):
        offer = next(o for o in sim.world.offers["E02"] if o.seller_id.startswith("S-"))
        write_records(
            sim.selleros_export.path.with_name("actions.jsonl"),
            journal_record(seller_id=offer.seller_id, restore_price=77),
            journal_record(
                seller_id=offer.seller_id, restore_price=88, incident_id="second"
            ),
        )
        sim.step()
        assert offer.price == 88
        expected = sim.world
    with _world(small_sim_settings, None, path) as (sim, _):
        assert sim.world == expected


def test_new_journal_action_waits_until_after_reconstruction(
    small_sim_settings: Settings, tmp_path: Path
) -> None:
    path = tmp_path / "world.duckdb"
    with _world(small_sim_settings, None, path) as (sim, _):
        sim.step()
        offer = next(o for o in sim.world.offers["E02"] if o.seller_id.startswith("S-"))
        seller_id = offer.seller_id
        original_price = offer.price
        write_records(
            sim.selleros_export.path.with_name("actions.jsonl"),
            journal_record(seller_id=seller_id, restore_price=77),
        )
        expected_now = sim.now
    with _world(small_sim_settings, None, path) as (sim, _):
        offer = next(o for o in sim.world.offers["E02"] if o.seller_id == seller_id)
        assert sim.now == expected_now
        assert offer.price == original_price
        sim.step()
        assert offer.price == 77
        with sim.store.cursor() as reader:
            assert reader.execute("SELECT ts FROM seller_actions").fetchall() == [
                (expected_now,)
            ]


def test_unknown_offer_is_consumed_without_crashing(
    small_sim_settings: Settings,
) -> None:
    with closing(Simulator(small_sim_settings, db_path=":memory:")) as sim:
        write_records(
            sim.selleros_export.path.with_name("actions.jsonl"),
            journal_record(seller_id="missing"),
        )
        sim.step()
        sim.step()
        with sim.store.cursor() as reader:
            assert reader.execute("SELECT count(*) FROM seller_actions").fetchone() == (
                1,
            )


def test_failed_step_retries_unacknowledged_actions(
    small_sim_settings: Settings,
) -> None:
    with closing(Simulator(small_sim_settings, db_path=":memory:")) as sim:
        path = sim.selleros_export.path.with_name("actions.jsonl")
        offer = next(o for o in sim.world.offers["E02"] if o.seller_id.startswith("S-"))
        write_records(path, journal_record(seller_id=offer.seller_id, restore_price=77))
        feed = JournalSellerActionFeed(path)
        sim.seller_action_feed = feed
        pending = feed.pending()
        ts = sim.now

        def fail_before_commit(sim: Simulator, hour: Hour) -> None:
            raise OSError("tick failed before commit")

        sim.pre_store_hooks.append(fail_before_commit)
        with pytest.raises(OSError, match="tick failed before commit"):
            sim.step()
        assert sim.now == ts
        assert offer.price == 77
        assert feed.pending() == pending
        assert sim.selleros_export.history.get(offer.seller_id, []) == []
        with sim.store.cursor() as reader:
            assert reader.execute("SELECT count(*) FROM seller_actions").fetchone() == (
                0,
            )
        sim.pre_store_hooks.remove(fail_before_commit)
        offer.price = 1
        sim.step()
        assert offer.price == 77
        assert feed.pending() == []
        exported = json.loads(sim.selleros_export.path.read_text())
        seller = next(
            s for s in exported["sellers"] if s["seller_id"] == offer.seller_id
        )
        assert [
            entry for entry in seller["price_history"] if entry["sku"] == "E02"
        ] == [{"ts": ts.isoformat(), "sku": "E02", "price": 77, "source": "manual"}]
        sim.step()
        with sim.store.cursor() as reader:
            assert reader.execute(
                "SELECT action_key, ts FROM seller_actions"
            ).fetchall() == [(pending[0].key, ts)]


def test_committed_actions_are_not_reapplied_after_ack_failure(
    small_sim_settings: Settings,
) -> None:
    class RetryingAckFeed(JournalSellerActionFeed):
        fail_ack = True

        def ack(self, keys: list[str]) -> None:
            if self.fail_ack:
                self.fail_ack = False
                raise OSError("ack unavailable")
            super().ack(keys)

    with closing(Simulator(small_sim_settings, db_path=":memory:")) as sim:
        path = sim.selleros_export.path.with_name("actions.jsonl")
        offer = next(o for o in sim.world.offers["E02"] if o.seller_id.startswith("S-"))
        write_records(path, journal_record(seller_id=offer.seller_id, restore_price=77))
        feed = RetryingAckFeed(path)
        sim.seller_action_feed = feed
        ts = sim.now
        with pytest.raises(OSError, match="ack unavailable"):
            sim.step()
        assert sim.now == ts + timedelta(hours=1)
        assert offer.price == 77
        assert len(feed.pending()) == 1
        offer.price = 88
        sim.step()
        assert offer.price == 88
        assert feed.pending() == []
        with sim.store.cursor() as reader:
            assert reader.execute("SELECT ts FROM seller_actions").fetchall() == [(ts,)]
            assert reader.execute(
                "SELECT count(DISTINCT ts) FROM traffic_hourly"
            ).fetchone() == (2,)


def test_backfill_does_not_consume_live_actions(small_sim_settings: Settings) -> None:
    small_sim_settings.sim = small_sim_settings.sim.model_copy(
        update={"history_days": 1}
    )
    with closing(Simulator(small_sim_settings, db_path=":memory:")) as sim:
        write_records(
            sim.selleros_export.path.with_name("actions.jsonl"),
            journal_record("paused", seller_id="S-14"),
        )
        sim.now -= timedelta(hours=1)
        sim.step()
        with sim.store.cursor() as reader:
            assert reader.execute("SELECT count(*) FROM seller_actions").fetchone() == (
                0,
            )
        sim.step()
        with sim.store.cursor() as reader:
            assert reader.execute("SELECT ts FROM seller_actions").fetchall() == [
                (small_sim_settings.sim.start_ts,)
            ]
