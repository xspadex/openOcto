"""Tests for Codex/Claude backend wiring."""

from openocto.agent_repl import _build_special_backend_command
from openocto.agent_serve import detect_backends, process_codex_cli


def test_build_claude_command(monkeypatch):
    monkeypatch.setattr("openocto.agent_repl._has", lambda cmd: True)
    cmd = _build_special_backend_command(
        "claude",
        model="sonnet",
        extra_args="--dangerously-skip-permissions",
    )
    assert cmd == "claude --model sonnet --dangerously-skip-permissions"


def test_build_codex_command(monkeypatch):
    monkeypatch.setattr("openocto.agent_repl._has", lambda cmd: True)
    cmd = _build_special_backend_command(
        "codex",
        model="gpt-5",
        extra_args="--full-auto",
    )
    assert cmd == "codex --model gpt-5 --full-auto"


def test_detect_backends_includes_codex(monkeypatch):
    monkeypatch.setattr("openocto.agent_serve.shutil.which",
                        lambda cmd: "/usr/bin/fake" if cmd in ("claude", "codex") else None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    class FakeSocket:
        def settimeout(self, timeout):
            return None

        def connect(self, addr):
            raise OSError("offline")

        def close(self):
            return None

    monkeypatch.setattr("openocto.agent_serve.socket.socket", lambda *args, **kwargs: FakeSocket())

    backends = detect_backends()
    assert {"type": "claude-cli", "label": "Claude Code CLI"} in backends
    assert {"type": "codex-cli", "label": "Codex CLI"} in backends


def test_process_codex_cli_passes_model(monkeypatch):
    captured = {}

    class Result:
        stdout = "done"
        stderr = ""

    def fake_run(cmd, input=None, capture_output=None, text=None, timeout=None):
        captured["cmd"] = cmd
        captured["input"] = input
        return Result()

    monkeypatch.setattr("openocto.agent_serve.subprocess.run", fake_run)
    output = process_codex_cli(None, "agent", "hello", model="gpt-5")

    assert output == "done"
    assert captured["cmd"] == ["codex", "exec", "--model", "gpt-5", "--skip-git-repo-check", "-"]
    assert captured["input"] == "hello"
