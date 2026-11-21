"""Reset demo runtime state without discarding recorded LLM responses."""

import argparse
import fcntl
import os
import shlex
import socket
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path

from eye.config import Settings, load_settings
from eye.graph.store import GRAPH_PATH
from eye.incidents.store import OPS_DB
from eye.sim.selleros_export import SELLEROS_PATH
from eye.sim.store import WORLD_DB

ROOT = Path(__file__).resolve().parents[1]


def check_services(settings: Settings) -> None:
    processes = subprocess.run(
        ["ps", "-eo", "pid=,args="], check=True, capture_output=True, text=True
    )
    for line in processes.stdout.splitlines():
        pid, command = line.strip().split(maxsplit=1)
        if int(pid) == os.getpid():
            continue
        try:
            names = {Path(arg).name for arg in shlex.split(command)}
        except ValueError:
            continue
        if names & {"dev.sh", "eye-core", "eye-specialists", "eye-mcp", "selleros"}:
            raise RuntimeError(f"service process {pid} appears to be running")
    for port in settings.ports.model_dump().values():
        with socket.socket() as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError(f"service port {port} is in use")


def reset(settings: Settings, root: Path = ROOT) -> None:
    if settings.data.source != "sim":
        raise RuntimeError("demo reset requires data.source: sim; warehouse untouched")
    check_services(settings)
    databases = [root / WORLD_DB, root / OPS_DB]
    graph = root / GRAPH_PATH
    selleros = root / SELLEROS_PATH.parent
    keep = {
        (root / settings.llm.cache_path).resolve(),
        (selleros / "llm_cache.jsonl").resolve(),
    }
    if selleros.is_symlink():
        raise RuntimeError(f"SellerOS data directory is a symlink: {selleros}")
    targets = [
        *databases,
        *(Path(f"{path}.wal") for path in databases),
        graph,
        graph.with_suffix(".json.tmp"),
        *(path for path in selleros.rglob("*") if path.is_file() or path.is_symlink()),
    ]
    if any(path.resolve() in keep for path in targets[:6]):
        raise RuntimeError("LLM cache overlaps runtime state; check settings")
    targets = [path for path in targets if path.resolve() not in keep]
    with ExitStack() as stack:
        for path in databases:
            if path.exists():
                handle = stack.enter_context(path.open("r+b"))
                try:
                    fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    raise RuntimeError(f"database is locked: {path}") from None
        for path in targets:
            if path.exists() or path.is_symlink():
                path.unlink()
                print(f"Removed {path}")
        for directory in sorted(selleros.rglob("*"), reverse=True):
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        if selleros.exists() and not any(selleros.iterdir()):
            selleros.rmdir()
    for path in sorted(keep):
        print(f"Kept LLM cache {path}")
    print("Reset complete. Start ./scripts/dev.sh, then run the demo at tick 0.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, default=ROOT / "config/settings.yaml")
    args = parser.parse_args()
    os.chdir(ROOT)
    try:
        reset(load_settings(args.settings))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(
            f"Reset refused: {exc}. Stop ./scripts/dev.sh and all services first.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
