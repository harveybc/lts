import json

from app.model_runner_heartbeat import write_runner_heartbeat


def test_runner_heartbeat_creates_parent_and_replaces_atomically(tmp_path):
    path = tmp_path / "new-state" / "heartbeat.json"
    first = write_runner_heartbeat(
        path, schema="test.runner.heartbeat.v1", payload={"state": "waiting"}
    )
    second = write_runner_heartbeat(
        path, schema="test.runner.heartbeat.v1", payload={"state": "active"}
    )
    assert first["state"] == "waiting"
    assert json.loads(path.read_text(encoding="utf-8")) == second
    assert second["state"] == "active"
    assert not path.with_name("heartbeat.json.tmp").exists()
