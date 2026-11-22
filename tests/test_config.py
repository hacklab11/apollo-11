from datetime import UTC, datetime
from pathlib import Path

import pytest

from eye.config import Settings, load_settings


def test_checked_in_settings_equal_defaults(repo_root: Path) -> None:
    settings = load_settings(repo_root / "config/settings.yaml")
    defaults = Settings()
    assert settings.llm.prices["nvidia/nemotron-3.5-lightning:free"].input_per_mtok == 0
    assert (
        settings.llm.prices["nvidia/nemotron-3-ultra-550b-a55b:free"].output_per_mtok
        == 0
    )
    defaults.llm.prices = settings.llm.prices
    assert settings == defaults
    assert settings.sim.history_days == 28
    assert settings.sim.start_ts == datetime(2026, 11, 27, tzinfo=UTC)
    assert settings.llm.model_for("detective") == "claude-opus-5-5"
    assert settings.llm.effort["detective"] == "high"
    assert settings.llm.cache_path == Path("data/llm_cache.jsonl")


def test_partial_settings_preserve_defaults(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text(
        """
sim:
  history_days: 21
  start_ts: 2026-11-27T01:00:00+01:00
llm:
  models:
    detective: test-model
  effort:
    reporter: low
""",
        encoding="utf-8",
    )

    settings = load_settings(path)

    assert settings.sim.history_days == 21
    assert settings.sim.seed == 42
    assert settings.sim.start_ts == datetime(2026, 11, 27, tzinfo=UTC)
    assert settings.llm.model_for("detective") == "test-model"
    assert settings.llm.model_for("sentinel") == "claude-opus-5-5"
    assert settings.llm.effort["reporter"] == "low"
    assert settings.llm.effort["sentinel"] == "low"
    assert settings.ports == Settings().ports


@pytest.mark.parametrize(
    ("yaml_text", "message"),
    [
        ("sim: {history_days: 20}", r"sim\.history_days: .*21 to 60"),
        ("ports: {mcp: 65536}", r"ports\.mcp: .*1024 to 65535"),
        ("sim: {seconds_per_sim_hour: 0}", r"sim\.seconds_per_sim_hour: .*0\.5 to 60"),
        ("sim: {seed: -1}", r"sim\.seed: .*greater than or equal to 0"),
        ("sim: {autostart: 1}", r"sim\.autostart: .*boolean"),
        ("sim: {start_ts: '2026-11-27T00:00:00'}", r"sim\.start_ts: .*timezone"),
        ("llm: {mode: test}", r"llm\.mode: .*'live', 'record' or 'replay'"),
        ("llm: {effort: {reporter: boosted}}", r"llm\.effort\.reporter: .*'max'"),
        ("a2a: {selleros_url: local}", r"a2a\.selleros_url: .*URL"),
        ("ports: {unknown: 8001}", r"ports\.unknown: .*Extra inputs"),
        ("[]", r"settings: .*dictionary"),
    ],
)
def test_invalid_setting_names_key_and_allowed_values(
    tmp_path: Path, yaml_text: str, message: str
) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_settings(path)
