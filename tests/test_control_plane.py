import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from eye.control import ControlPlane, RunKilled, audit_hash


def test_audit_and_human_control() -> None:
    control = ControlPlane("sqlite://")
    assert control.audit_rows() == []
    control.audit(actor="person", action="start")
    control.set_kill("run", "run1", True, "person")
    with pytest.raises(RunKilled):
        control.check("run1", "default")
    control.check("other", "default")
    control.set_kill("run", "run1", False, "person")
    control.check("run1", "default")
    identifier = control.pending("run1", None, "question", {"question": "Continue?"})
    control.decide(identifier, True, "person")
    assert control.approval_rows()[0]["decided_by"] == "person"
    with pytest.raises(ValueError):
        control.decide(identifier, False, "other")
    previous = "0" * 64
    for row in control.audit_rows():
        assert row.pop("prev_hash") == previous
        digest = row.pop("hash")
        row.pop("id")
        assert audit_hash(previous, row) == digest
        previous = digest
    with pytest.raises(IntegrityError), control.engine.begin() as conn:
        conn.execute(text("DELETE FROM audit_log"))
    control.close()
