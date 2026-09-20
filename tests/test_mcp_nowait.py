import json

from openocto import mcp_server


class Relay:
    def __init__(self):
        self.polled = []
        self.updated = []

    def submit_task(self, terminal, **kwargs):
        return "task-123"

    def poll_task(self, terminal, task_id=None):
        self.polled.append((terminal, task_id))
        return {
            "id": task_id,
            "status": "DONE",
            "exit_code": 0,
            "output": '{"status":"DEPLOYED"}',
        }

    def update_task(self, terminal, updates, task_id=None):
        self.updated.append((terminal, updates, task_id))


def test_remote_run_nowait_returns_task_id(monkeypatch):
    relay = Relay()
    monkeypatch.setattr(mcp_server, "_get_relay", lambda: relay)

    output, is_error = mcp_server.handle_remote_run(
        {
            "terminal": "yellow",
            "command": "echo ok",
            "nowait": True,
        }
    )

    assert is_error is False
    assert json.loads(output) == {
        "status": "SUBMITTED",
        "taskId": "task-123",
        "terminal": "yellow",
    }


def test_remote_logs_queries_exact_task_id(monkeypatch):
    relay = Relay()
    monkeypatch.setattr(mcp_server, "_get_relay", lambda: relay)

    output, is_error = mcp_server.handle_remote_logs(
        {
            "terminal": "yellow",
            "task_id": "task-123",
            "tail": 20,
        }
    )

    assert is_error is False
    assert relay.polled == [("yellow", "task-123")]
    assert "[Task DONE] (exit code: 0)" in output
    assert '{"status":"DEPLOYED"}' in output


def test_remote_kill_targets_exact_task_id(monkeypatch):
    relay = Relay()
    monkeypatch.setattr(mcp_server, "_get_relay", lambda: relay)
    relay.poll_task = lambda terminal, task_id=None: {
        "id": task_id,
        "status": "RUNNING",
    }

    output, is_error = mcp_server.handle_remote_kill(
        {
            "terminal": "yellow",
            "task_id": "task-123",
        }
    )

    assert is_error is False
    assert relay.updated == [
        ("yellow", {"status": "KILL"}, "task-123")
    ]
    assert "task-123" in output
