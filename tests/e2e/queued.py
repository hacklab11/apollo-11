"""Verify the real Core worker leaves a rate-limited tenant in DBOS's queue."""

import os
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from dbos import DBOSClient

from eye.contracts_v2 import Budget, TaskMetadata
from eye.control import ControlPlane


def main() -> None:
    url = os.environ["EYE_DATABASE_URL"]
    client = DBOSClient(system_database_url=url)
    control = ControlPlane(url)
    queue = client.register_queue("data-hours", on_conflict="never_update")
    previous = queue.partition_limiter
    tenant = "e2e-limits-" + uuid4().hex[:12]
    handles = []
    try:
        source = next(
            w
            for w in client.list_workflows(queue_name="data-hours")
            if w.name.endswith("run_hour")
        )
        queue.set_partition_limiter({"limit": 1, "period": 3600})
        control.set_kill("tenant", tenant, True, "e2e")
        for _ in range(2):
            run_id = "e2e-queued-" + uuid4().hex
            now = datetime.now(UTC)
            metadata = TaskMetadata(
                tenant_id=tenant,
                context_id=run_id,
                traceparent=f"00-{uuid4().hex}-{uuid4().hex[:16]}-01",
                budget=Budget(
                    tokens=0, cost_usd=0, deadline=now + timedelta(minutes=1)
                ),
            )
            handles.append(
                client.enqueue(
                    {
                        "workflow_name": source.name,
                        "queue_name": "data-hours",
                        "class_name": source.class_name,
                        "instance_name": source.config_name,
                        "workflow_id": run_id,
                        "queue_partition_key": tenant,
                    },
                    run_id,
                    now,
                    metadata,
                )
            )
        deadline = time.monotonic() + 20
        while handles[0].get_status().status != "SUCCESS":
            assert time.monotonic() < deadline, handles[0].get_status().status
            time.sleep(0.2)
        time.sleep(2)
        assert handles[1].get_status().status == "ENQUEUED"
        print("OK over-limit tenant remains ENQUEUED, not executed inline")
    finally:
        for handle in handles:
            client.cancel_workflow(handle.workflow_id)
        queue.set_partition_limiter(previous)
        control.set_kill("tenant", tenant, False, "e2e")
        control.close()
        client.destroy()


if __name__ == "__main__":
    main()
