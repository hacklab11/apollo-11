from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest

from eye.config import Settings
from eye.sim.engine import Simulator
from eye.sim.source import open_sim_source
from eye.sim.store import WORLD_DB


def test_sim_source_ignores_warehouse_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    warehouse = tmp_path / "production.duckdb"
    warehouse.write_bytes(b"real warehouse must remain unchanged")
    settings = Settings.model_validate(
        {"data": {"source": "sim", "path": str(warehouse)}}
    )
    sim = Mock(spec=Simulator)

    @contextmanager
    def world(
        actual_settings: Settings, scenarios: ModuleType | None, path: Path
    ) -> Iterator[tuple[Simulator, None]]:
        assert actual_settings is settings
        assert path == WORLD_DB
        assert path != warehouse
        yield sim, None

    monkeypatch.setattr("eye.sim.source._world", world)
    with open_sim_source(settings) as source:
        assert source.sim is sim
    assert warehouse.read_bytes() == b"real warehouse must remain unchanged"
