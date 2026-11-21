import os
import signal
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

import duckdb
import pytest

LAUNCHER = Path(__file__).resolve().parents[1] / "scripts/dev.sh"


@pytest.mark.skipif(os.name != "posix", reason="requires Bash and POSIX process groups")
@pytest.mark.parametrize(
    ("startup_signal", "exit_code"),
    [(None, 23), ("INT", 130), ("TERM", 143)],
    ids=["service-exits", "ctrl-c-during-launch", "term-during-launch"],
)
def test_dev_launcher_releases_database_on_shutdown(
    tmp_path: Path, startup_signal: str | None, exit_code: int
) -> None:
    fake_uv = tmp_path / "uv"
    fake_uv.write_text('#!/bin/sh\nexec "$TEST_PYTHON" "$TEST_SERVICE" "$@"\n')
    fake_uv.chmod(0o700)
    service = tmp_path / "service.py"
    service.write_text(
        """
import os
import signal
import sys
import time
from pathlib import Path

directory = Path(os.environ["TEST_DIR"])
name = sys.argv[-1]
(directory / f"{name}.pgrp").write_text(str(os.getpgrp()))
ready = directory / "core-ready"
if name == "eye-core":
    import duckdb

    with duckdb.connect(str(directory / "world.duckdb")):
        ready.touch()
        signal.pause()
elif name == "eye-specialists":
    while not ready.exists():
        time.sleep(0.01)
    sys.exit(23)
else:
    signal.pause()
"""
    )
    # Force a signal between spawning Core and recording its process group.
    startup = tmp_path / "bash_env"
    startup.write_text(
        """
interrupt_startup() {
    until [[ -f "$TEST_DIR/core-ready" ]]; do sleep 0.01; done
    kill -s "$TEST_STARTUP_SIGNAL" "$$"
}
set -T
record_pid='pids+=("$!")'
trap '[[ $BASH_COMMAND != "$record_pid" ]] || interrupt_startup' DEBUG
"""
    )
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "BASH_ENV": str(startup) if startup_signal else "",
        "TEST_DIR": str(tmp_path),
        "TEST_PYTHON": sys.executable,
        "TEST_SERVICE": str(service),
        "TEST_STARTUP_SIGNAL": startup_signal or "",
    }

    with subprocess.Popen(
        ["bash", str(LAUNCHER)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        text=True,
    ) as launcher:
        try:
            output, _ = launcher.communicate(timeout=10)
            assert launcher.returncode == exit_code, output
            with duckdb.connect(str(tmp_path / "world.duckdb")):
                pass
        except (AssertionError, OSError, duckdb.IOException, subprocess.TimeoutExpired):
            if launcher.poll() is None:
                launcher.kill()
            for group in tmp_path.glob("*.pgrp"):
                with suppress(ProcessLookupError, ValueError):
                    os.killpg(int(group.read_text()), signal.SIGKILL)
            raise
