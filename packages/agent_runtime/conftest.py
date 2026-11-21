"""Keep the standalone agent_runtime suite out of other projects' test runs."""

from pathlib import Path

import pytest


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    project = Path(__file__).resolve().parent
    if config.rootpath != project and collection_path == project / "tests":
        return True
    return None
