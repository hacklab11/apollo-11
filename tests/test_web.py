from pathlib import Path


def test_web_uses_only_live_api(repo_root: Path) -> None:
    source = (repo_root / "web/app.js").read_text()
    for token in ("mockApi", "mockMode", "fixture", "mock/", "replayNext"):
        assert token not in source
    assert 'chat: (messages) => post("/chat", { messages })' in source
    assert "response.incident_ids.map" in source
    assert not (repo_root / "web/mock").exists()


def test_web_has_no_scenario_controls(repo_root: Path) -> None:
    markup = (repo_root / "web/index.html").read_text()
    assert "data-scenario" not in markup
    source = (repo_root / "web/app.js").read_text()
    assert "/scenarios" not in source
