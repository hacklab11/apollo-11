import json
import logging
import sys

import pytest

from eye import main as core
from eye.orchestrator import _log_error


@pytest.mark.parametrize("existing_handler", [False, True])
def test_core_warnings_are_json_lines_on_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    existing_handler: bool,
) -> None:
    root = logging.getLogger()
    monkeypatch.setattr(
        root,
        "handlers",
        [logging.StreamHandler(sys.stderr)] if existing_handler else [],
    )
    monkeypatch.setattr(root, "level", root.level)

    def stop_after_warning() -> None:
        _log_error("stdout_logging_test", ValueError("not logged"))
        raise RuntimeError("stop before starting services")

    monkeypatch.setattr(core, "load_settings", stop_after_warning)
    try:
        for _ in range(2):
            with pytest.raises(RuntimeError, match="stop before starting services"):
                core.main()
        captured = capsys.readouterr()
        assert captured.err == ""
        lines = captured.out.splitlines()
        assert len(lines) == 2
        for line in lines:
            record = json.loads(line)
            assert record["event"] == "stdout_logging_test"
            assert record["component"] == "orchestrator"
            assert record["error"] == "ValueError"
    finally:
        for handler in root.handlers:
            handler.close()
