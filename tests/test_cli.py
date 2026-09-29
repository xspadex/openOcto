"""Tests for CLI task polling behavior."""

from types import SimpleNamespace

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


def test_cmd_run_wakes_daemon_after_submitting_task(monkeypatch, capsys):
    events = []

    class RunRelay:
        def list_terminals(self):
            return [{
                "name": "gpu",
                "online": True,
                "last_seen_ago": 0,
                "meta": {},
            }]

        def submit_task(self, target, **kwargs):
            events.append(("submit", target))
            return kwargs["task_id"]

    relay = RunRelay()
    monkeypatch.setattr(cli, "_get_relay", lambda: relay)
    monkeypatch.setattr(cli, "_get_auth_kwargs", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        cli, "_wake_daemon",
        lambda actual_relay, target: events.append(("wake", target)),
    )
    args = SimpleNamespace(
        target="gpu",
        command="pwd",
        timeout=None,
        no_log=False,
        notify=None,
        notify_message="",
        nowait=True,
    )

    cli.cmd_run(args)

    assert events == [("submit", "gpu"), ("wake", "gpu")]
    assert "Task submitted (nowait):" in capsys.readouterr().err


def test_cmd_logs_queries_exact_task_id(monkeypatch, capsys):
    relay = StubRelay(
        {
            "id": "gpu-task-123",
            "status": "DONE",
            "exit_code": 0,
            "output": '{"status":"DEPLOYED"}',
        }
    )
    monkeypatch.setattr(cli, "_get_relay", lambda: relay)
    args = SimpleNamespace(
        target="gpu",
        task_id="gpu-task-123",
        follow=False,
        tail=20,
    )

    cli.cmd_logs(args)

    assert relay.polled == [("gpu", "gpu-task-123")]
    assert '{"status":"DEPLOYED"}' in capsys.readouterr().out


def test_cmd_kill_targets_exact_task_id(monkeypatch, capsys):
    relay = StubRelay(
        {
            "id": "gpu-task-123",
            "status": "RUNNING",
            "exit_code": None,
            "output": "",
        }
    )
    monkeypatch.setattr(cli, "_get_relay", lambda: relay)
    args = SimpleNamespace(target="gpu", task_id="gpu-task-123")

    cli.cmd_kill(args)

    assert relay.polled == [("gpu", "gpu-task-123")]
    assert relay.updated == [
        ("gpu", {"status": "KILL"}, "gpu-task-123")
    ]
    assert "gpu-task-123" in capsys.readouterr().out


def test_wake_daemon_pokes_registered_lan_endpoint(monkeypatch):
    class RelayWithLanMeta:
        def list_terminals(self):
            return [{
                "name": "gpu",
                "meta": {"lan_ip": "192.168.1.10", "lan_port": 9527},
            }]

    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    def fake_urlopen(request, timeout):
        requests.append((request.full_url, timeout))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    cli._wake_daemon(RelayWithLanMeta(), "gpu")

    assert requests == [("http://192.168.1.10:9527/wake", 2)]
