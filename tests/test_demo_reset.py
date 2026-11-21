import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from eye.config import Settings

SPEC = importlib.util.spec_from_file_location(
    "demo_reset", Path(__file__).resolve().parents[1] / "scripts/demo_reset.py"
)
assert SPEC and SPEC.loader
resetter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(resetter)


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resetter, "check_services", lambda settings: None)


def test_reset_removes_runtime_and_preserves_caches(
    tmp_path: Path, offline: None, capsys: pytest.CaptureFixture[str]
) -> None:
    removed = [
        "data/world.duckdb",
        "data/world.duckdb.wal",
        "data/ops.duckdb",
        "data/ops.duckdb.wal",
        "data/graph.json",
        "data/graph.json.tmp",
        "external/selleros/data/sellers.json",
        "external/selleros/data/actions.jsonl",
        "external/selleros/data/nested/state.json",
    ]
    kept = [
        "recordings/core.jsonl",
        "external/selleros/data/llm_cache.jsonl",
        "data/llm_cache.jsonl",
        "data/unrelated.txt",
    ]
    for name in removed + kept:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("recorded")
    settings = Settings()
    settings.llm.cache_path = Path(kept[0])
    resetter.reset(settings, tmp_path)
    output = capsys.readouterr().out
    for name in removed:
        assert not (tmp_path / name).exists()
        assert f"Removed {tmp_path / name}" in output
    for name in kept:
        assert (tmp_path / name).read_text() == "recorded"


def test_empty_reset_is_repeatable(tmp_path: Path, offline: None) -> None:
    resetter.reset(Settings(), tmp_path)
    resetter.reset(Settings(), tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "cache_path",
    [
        "data/world.duckdb",
        "data/ops.duckdb",
        "data/world.duckdb.wal",
        "data/ops.duckdb.wal",
        "data/graph.json",
        "data/graph.json.tmp",
    ],
)
def test_runtime_cache_overlap_refused_before_deletion(
    tmp_path: Path, offline: None, cache_path: str
) -> None:
    cache = tmp_path / cache_path
    cache.parent.mkdir(parents=True)
    cache.write_text("recorded")
    settings = Settings()
    settings.llm.cache_path = Path(cache_path)
    with pytest.raises(RuntimeError, match="LLM cache overlaps runtime state"):
        resetter.reset(settings, tmp_path)
    assert cache.read_text() == "recorded"


def test_locked_database_refuses_before_deletion(tmp_path: Path, offline: None) -> None:
    path = tmp_path / resetter.OPS_DB
    path.parent.mkdir()
    path.touch()
    world = tmp_path / resetter.WORLD_DB
    world.touch()
    code = """
import fcntl, sys
with open(sys.argv[1], 'r+b') as handle:
    fcntl.lockf(handle, fcntl.LOCK_EX)
    print('locked', flush=True)
    sys.stdin.read()
"""
    with subprocess.Popen(
        [sys.executable, "-c", code, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    ) as child:
        try:
            assert child.stdout.readline().strip() == "locked"
            with pytest.raises(RuntimeError, match="database is locked"):
                resetter.reset(Settings(), tmp_path)
            assert world.exists() and path.exists()
        finally:
            child.communicate(timeout=5)


def test_service_process_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        resetter.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess([], 0, "123 uv run eye-core\n"),
    )
    with pytest.raises(RuntimeError, match="service process 123"):
        resetter.check_services(Settings())


def test_warehouse_is_never_deleted(tmp_path: Path, offline: None) -> None:
    settings = Settings()
    settings.data.source = "duckdb"
    with pytest.raises(RuntimeError, match="warehouse untouched"):
        resetter.reset(settings, tmp_path)


def test_occupied_service_port_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        resetter.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess([], 0, ""),
    )

    class Probe:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, timeout: float) -> None:
            pass

        def connect_ex(self, address: tuple[str, int]) -> int:
            return 0

    monkeypatch.setattr(resetter.socket, "socket", Probe)
    with pytest.raises(RuntimeError, match="service port 8000"):
        resetter.check_services(Settings())
