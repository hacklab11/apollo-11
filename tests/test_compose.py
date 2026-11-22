from pathlib import Path

import yaml


def test_compose_auth_credentials_are_isolated(repo_root: Path) -> None:
    compose = yaml.safe_load((repo_root / "compose.yaml").read_text())
    services = compose["services"]
    auth = services["auth"]
    policy = yaml.safe_load((repo_root / "config/auth.yaml").read_text())
    secret_names = {client["secret_env"] for client in policy["clients"].values()}
    assert auth["command"] == ["eye-auth"]
    assert "auth-keys:/app/data/auth" in auth["volumes"]
    assert auth["environment"]["EYE_AUTH_KEY_PATH"] == "/app/data/auth/key.pem"
    assert "/healthz" in auth["healthcheck"]["test"][-1]
    assert "auth-keys" in compose["volumes"]
    for name in secret_names:
        assert auth["environment"][name] == "${" + name + ":-}"
    for name, service in services.items():
        assert not service.get("env_file")
        if name == "auth":
            continue
        environment = service.get("environment", {})
        if name == "eye-specialists":
            assert secret_names.intersection(environment) == {
                f"EYE_AUTH_{agent}_SECRET"
                for agent in ("GROWTH", "MARKETPLACE", "DISCOVERY")
            }
        else:
            assert not secret_names.intersection(environment)
        if "EYE_AUTH_CLIENT_ID" in environment:
            assert environment["EYE_AUTH_CLIENT_ID"] == name
            secret = policy["clients"][name]["secret_env"]
            assert environment["EYE_AUTH_CLIENT_SECRET"] == "${" + secret + ":-}"
        if "EYE_AUTH_CLIENT_ID" in environment or name == "eye-specialists":
            assert environment["EYE_AUTH_URL"] == auth["environment"]["EYE_AUTH_URL"]
            assert service["depends_on"]["auth"] == {"condition": "service_healthy"}
        assert not any(v.startswith("auth-keys:") for v in service.get("volumes", []))


def test_continuous_simulator_compose_boundary(repo_root: Path) -> None:
    compose = yaml.safe_load((repo_root / "compose.yaml").read_text())
    services = compose["services"]
    sim = services["sim"]
    assert "sim-generate" not in services
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
    for name in ("eye-core", "selleros", "eye-data"):
        assert not any(v.startswith("sim-world:") for v in services[name]["volumes"])
    data = services["eye-data"]
    assert "EYE_WAREHOUSE_PATH" not in data["environment"]
    assert data["environment"]["SIM_LAKE_DIR"] == sim["environment"]["SIM_LAKE_DIR"]
    assert data["depends_on"]["sim"] == {"condition": "service_healthy"}
    assert "sim-lake:/app/data/lake:ro" in data["volumes"]
    assert data["networks"] == ["warehouse"]
    assert "ports" not in data
    core = services["eye-core"]
    assert core["environment"]["EYE_DATA_URL"] == "http://eye-data:8150"
    assert core["depends_on"]["eye-data"] == {"condition": "service_healthy"}
    assert core["networks"] == ["eye", "warehouse", "agents"]
    assert "eye-data:/app/data" in core["volumes"]
    assert not any(v.startswith("sim-lake:") for v in core["volumes"])
    assert "seller-data:/app/packages/selleros/data" in sim["volumes"]
    assert (
        "seller-data:/app/packages/selleros/data" not in services["eye-core"]["volumes"]
    )
    assert services["eye-mcp"]["ports"] == ["127.0.0.1:${MCP_PORT:-8110-8119}:8100"]


def test_agents_have_only_internal_networks(repo_root: Path) -> None:
    compose = yaml.safe_load((repo_root / "compose.yaml").read_text())
    for name in (
        "sentinel",
        "detective",
        "skeptic",
        "reporter",
        "cartographer",
        "eye-specialists",
    ):
        service = compose["services"][name]
        assert all(compose["networks"][n]["internal"] for n in service["networks"])
        assert "ports" not in service
        assert service["environment"]["EYE_CORE_URL"] == "http://eye-core:8000"
        assert service["environment"]["EYE_MCP_URL"] == "http://eye-mcp:8100/mcp"
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            assert service["environment"][key] == "http://egress-proxy:3128"
        assert "scripted-model" not in service["environment"]["NO_PROXY"]
    assert compose["services"]["egress-proxy"]["networks"] == ["agents", "eye"]
