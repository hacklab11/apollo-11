from pathlib import Path

import yaml


def test_continuous_simulator_compose_boundary(repo_root: Path) -> None:
    compose = yaml.safe_load((repo_root / "compose.yaml").read_text())
    services = compose["services"]
    sim = services["sim"]
    generator = services["sim-generate"]
    assert "eye-data:/app/data" in generator["volumes"]
    assert "seller-data:/app/packages/selleros/data" not in generator["volumes"]
    assert sim["image"] == compose["x-service"]["image"]
    assert sim["env_file"] == compose["x-service"]["env_file"]
    assert sim["command"] == ["eye-sim", "serve"]
    assert sim["environment"]["SIM_HOST"] == "0.0.0.0"
    assert sim["environment"]["SIM_PORT"] == "8100"
    assert sim["environment"]["SIM_LAKE_DIR"] == "/app/data/lake"
    assert sim["ports"] == ["127.0.0.1:${SIM_PORT:-8100}:8100"]
    assert sim["healthcheck"]["interval"] == compose["x-health"]["interval"]
    assert "/health'" in sim["healthcheck"]["test"][-1]
    assert "sim-world:/app/data" in sim["volumes"]
    assert "sim-lake:/app/data/lake" in sim["volumes"]
    assert {"sim-world", "sim-lake"} <= compose["volumes"].keys()
    assert services["selleros"]["depends_on"]["sim"] == {"condition": "service_healthy"}
    for name in ("eye-core", "selleros", "eye-data", "sim-generate"):
        assert not any(v.startswith("sim-world:") for v in services[name]["volumes"])
    data = services["eye-data"]
    assert data["environment"]["EYE_WAREHOUSE_PATH"] == "/app/world/world.duckdb"
    assert data["depends_on"]["sim-generate"] == {
        "condition": "service_completed_successfully"
    }
    assert "eye-data:/app/world:ro" in data["volumes"]
    core = services["eye-core"]
    assert core["environment"]["EYE_DATA_URL"] == "http://eye-data:8150"
    assert core["depends_on"]["eye-data"] == {"condition": "service_healthy"}
    assert core["networks"] == ["eye", "warehouse"]
    assert "eye-data:/app/data" in core["volumes"]
    for service in (core, data, generator):
        assert not any(v.startswith("sim-lake:") for v in service["volumes"])
    assert "seller-data:/app/packages/selleros/data" in sim["volumes"]
    assert (
        "seller-data:/app/packages/selleros/data" not in services["eye-core"]["volumes"]
    )
    assert services["eye-mcp"]["ports"] == ["127.0.0.1:${MCP_PORT:-8110-8119}:8100"]
