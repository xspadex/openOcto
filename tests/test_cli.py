"""Tests for CLI task polling behavior."""

from openocto import cli


class StubRelay:
    def __init__(self, task):
        self.task = task
        self.polled = []
        self.cleared = []
        self.updated = []

    def poll_task(self, target, task_id=None):
        self.polled.append((target, task_id))
        return self.task

    def clear_task(self, target, task_id=None):
        self.cleared.append((target, task_id))

    def update_task(self, target, updates, task_id=None):
        self.updated.append((target, updates, task_id))


def test_poll_streaming_uses_per_task_key():
    relay = StubRelay({
        "id": "123-abc",
        "status": "DONE",
        "output": "",
        "exit_code": 0,
    })

    exit_code = cli._poll_streaming(relay, "gpu", task_id="123-abc")

    assert exit_code == 0
    assert relay.polled == [("gpu", "123-abc")]
    assert relay.cleared == [("gpu", "123-abc")]


def test_poll_streaming_legacy_mode_still_uses_legacy_key():
    relay = StubRelay({
        "id": "legacy",
        "status": "DONE",
        "output": "",
        "exit_code": 0,
    })

    exit_code = cli._poll_streaming(relay, "gpu")

    assert exit_code == 0
    assert relay.polled == [("gpu", None)]
    assert relay.cleared == [("gpu", None)]
