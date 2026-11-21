"""Regenerate the UI mock responses from the test factories."""

import sys
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    sys.path.insert(0, str(ROOT / "tests"))
    import factories

    output = ROOT / "tests/fixtures/web"
    output.mkdir(parents=True, exist_ok=True)
    responses = {
        "status": factories.make_status(),
        "graph": factories.make_graph(),
        "metrics": [
            metric.model_dump(exclude={"sql"}) for metric in factories.make_metrics()
        ],
        "metric_orders": factories.make_orders_series(),
        "metric_def_orders": factories.make_orders_definition(),
        "add_metric_result": factories.make_add_metric_result(),
        "anomalies": factories.make_decisions(),
        "incidents": factories.make_incident_list(),
        "incident_s7": factories.make_incident_s7(),
        "incident_s6": factories.make_incident_s6(),
        "trail_s7": factories.make_trail_s7(),
        "scenarios": factories.make_scenarios(),
        "chat_response": factories.make_chat_response(),
        "truth": factories.make_truth(),
    }

    json = TypeAdapter(Any)
    for name, response in responses.items():
        (output / f"{name}.json").write_bytes(
            json.dump_json(response, indent=2) + b"\n"
        )

    events = "".join(
        event.model_dump_json() + "\n" for event in factories.make_events()
    )
    (output / "events.jsonl").write_text(events, encoding="utf-8")
    print(f"Wrote {len(responses) + 1} mock responses to tests/fixtures/web")


if __name__ == "__main__":
    main()
