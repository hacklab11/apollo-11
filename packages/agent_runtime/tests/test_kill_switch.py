import pytest

from agent_runtime import InMemoryKillSwitch, RunScope

SCOPE = RunScope(run_id="run-1", tenant_id="tenant-a")
OTHER = RunScope(run_id="run-2", tenant_id="tenant-b")


@pytest.mark.asyncio
async def test_run_switch_stops_only_that_run() -> None:
    switch = InMemoryKillSwitch()
    switch.kill_run("run-1", "operator")
    assert await switch.reason(SCOPE) == "run run-1 killed: operator"
    assert await switch.reason(OTHER) is None


@pytest.mark.asyncio
async def test_tenant_switch_stops_only_that_tenant() -> None:
    switch = InMemoryKillSwitch()
    switch.kill_tenant("tenant-a", "abuse")
    assert await switch.reason(SCOPE) == "tenant tenant-a killed: abuse"
    assert await switch.reason(OTHER) is None


@pytest.mark.asyncio
async def test_system_switch_stops_everything_and_clears() -> None:
    switch = InMemoryKillSwitch()
    switch.kill_system("incident")
    assert await switch.reason(OTHER) == "system killed: incident"
    switch.clear()
    assert await switch.reason(SCOPE) is None
