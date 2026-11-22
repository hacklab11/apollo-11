import copy
import hashlib
import json

import pytest

from eye.llm.client import _fingerprint_value, _request_fingerprint

from . import test_runtime as runtime

harness = runtime.harness


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (0.123456789012345, 0.123456789012346),
        (-1e-17, 0.0),
        ({"corr": [0.30000000000000004]}, {"corr": [0.3]}),
        ('{"corr": 0.30000000000000004}', '{"corr":0.3}'),
        (
            '{"samples": [0.30000000000000004]}\n\nRuntime budget: 5',
            '{"samples":[0.3]}\n\nRuntime budget: 5',
        ),
    ],
)
def test_fingerprint_ignores_numerical_noise(left, right):
    assert _request_fingerprint({"messages": left}) == _request_fingerprint(
        {"messages": right}
    )


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (0.3, 0.30000001),
        ({"corr": 0.3}, {"corr": -0.3}),
        ('{"corr":0.3}', '{"corr":0.4}'),
        ("model v1.01", "model v1.02"),
        ("{}", "[]"),
    ],
)
def test_fingerprint_preserves_meaningful_differences(left, right):
    assert _request_fingerprint({"messages": left}) != _request_fingerprint(
        {"messages": right}
    )


@pytest.mark.parametrize("value", [None, "", [], {}, "{invalid", "[invalid"])
def test_fingerprint_handles_empty_and_non_json_content(value):
    assert _fingerprint_value(value) == value


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_replay_fingerprint_does_not_change_provider_request(
    harness, tmp_path, legacy
):
    path = tmp_path / "cache.jsonl"
    request = {
        "model": "claude-opus-5-5",
        "max_tokens": 100,
        "betas": ["server-side-fallback-2026-07-01"],
        "system": [{"type": "text", "text": "Calculate"}],
        "tools": [],
        "messages": [{"role": "user", "content": '{"corr":0.30000000000000004}'}],
    }
    original = copy.deepcopy(request)
    client, sent = harness([runtime.final()], mode="record", cache_path=path)
    expected = await client.create(request)
    assert request == original
    assert sent[0]["messages"] == original["messages"]
    if legacy:
        identity = {
            "model": request["model"],
            "system": "Calculate",
            "tools": [],
            "messages": request["messages"],
        }
        key = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        entry = json.loads(path.read_text())
        entry.update(key=key, request_digest=key)
        path.write_text(json.dumps(entry) + "\n")
    else:
        request["messages"][0]["content"] = '{"corr":0.3}'
    client, sent = harness([], mode="replay", cache_path=path)
    assert await client.create(request) == expected
    assert sent == []
