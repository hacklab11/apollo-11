from pathlib import Path


def test_web_uses_only_live_api(repo_root: Path) -> None:
    source = (repo_root / "web/app.js").read_text()
    for token in ("mockApi", "mockMode", "fixture", "mock/", "replayNext"):
        assert token not in source
    assert 'chat: (messages) => post("/chat", { messages })' in source
    assert "response.incident_ids.map" in source
    assert not (repo_root / "web/mock").exists()
