"""Tests for daemon utility functions (path validation, shell wrapping, PS quoting)."""

import io

import pytest
from openocto import daemon as daemon_module
from openocto.daemon import Daemon, _is_safe_path, _ps_quote, _resolve_pwsh


# ---- _is_safe_path ----

class TestIsSafePath:
    # Should PASS
    def test_normal_file(self):
        assert _is_safe_path("/home/user/file.txt") is True

    def test_deep_path(self):
        assert _is_safe_path("/work/project/src/model.py") is True

    def test_windows_path(self):
        assert _is_safe_path("C:\\Users\\user\\file.txt") is True

    def test_with_base_dir_inside(self):
        assert _is_safe_path("/home/user/sub/file.txt", base_dir="/home/user") is True

    # Should BLOCK
    def test_etc_shadow(self):
        assert _is_safe_path("/etc/shadow") is False

    def test_etc_passwd(self):
        assert _is_safe_path("/etc/passwd") is False

    def test_ssh_key(self):
        assert _is_safe_path("/home/user/.ssh/id_rsa") is False

    def test_ssh_dir(self):
        assert _is_safe_path("/root/.ssh/authorized_keys") is False

    def test_octo_config(self):
        assert _is_safe_path("/home/user/.octo/config.json") is False

    def test_env_file(self):
        assert _is_safe_path("/app/.env") is False

    def test_credentials(self):
        assert _is_safe_path("/home/user/.aws/credentials") is False

    def test_null_byte(self):
        assert _is_safe_path("/home/user/\x00evil") is False

    def test_empty_path(self):
        assert _is_safe_path("") is False

    def test_none_path(self):
        assert _is_safe_path(None) is False

    def test_with_base_dir_outside(self):
        assert _is_safe_path("/etc/hosts", base_dir="/home/user") is False

    def test_with_base_dir_traversal(self):
        # os.path.abspath normalizes ../
        assert _is_safe_path("/home/user/../root/file", base_dir="/home/user") is False


# ---- _ps_quote ----

class TestPsQuote:
    def test_simple_string(self):
        assert _ps_quote("hello") == "'hello'"

    def test_empty_string(self):
        assert _ps_quote("") == "''"

    def test_single_quote_escaped(self):
        assert _ps_quote("it's") == "'it''s'"

    def test_double_quotes_preserved(self):
        assert _ps_quote('say "hi"') == "'say \"hi\"'"

    def test_backslash_preserved(self):
        assert _ps_quote("C:\\path\\to\\file") == "'C:\\path\\to\\file'"

    def test_multiple_single_quotes(self):
        assert _ps_quote("a'b'c") == "'a''b''c'"

    def test_path_with_spaces(self):
        assert _ps_quote("C:\\Program Files\\app") == "'C:\\Program Files\\app'"


# ---- PowerShell 7 resolution and execution ----

class TestPowerShell7:
    def test_resolve_pwsh_from_path(self, monkeypatch):
        expected = "C:\\Program Files\\PowerShell\\7\\pwsh.exe"
        monkeypatch.delenv("OCTO_PWSH", raising=False)
        monkeypatch.setattr(
            daemon_module.shutil,
            "which",
            lambda command: expected if command == "pwsh" else None,
        )

        assert _resolve_pwsh() == expected

    def test_resolve_pwsh_does_not_fallback_to_powershell_51(self, monkeypatch):
        monkeypatch.delenv("OCTO_PWSH", raising=False)
        monkeypatch.setattr(daemon_module.shutil, "which", lambda command: None)
        monkeypatch.setattr(daemon_module.os.path, "isfile", lambda path: False)

        with pytest.raises(FileNotFoundError, match="PowerShell 7"):
            _resolve_pwsh()

    def test_shell_tasks_launch_resolved_pwsh(self, monkeypatch, tmp_path):
        executable = "C:\\Program Files\\PowerShell\\7\\pwsh.exe"
        launched = []

        class RecordingStdin:
            def __init__(self):
                self.data = b""

            def write(self, data):
                self.data += data

            def close(self):
                pass

        class CompletedProcess:
            def __init__(self):
                self.stdin = RecordingStdin()
                self.stdout = io.BytesIO()
                self.returncode = 0

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return self.returncode

        def fake_popen(args, **kwargs):
            proc = CompletedProcess()
            launched.append((args, kwargs, proc))
            return proc

        monkeypatch.setattr(daemon_module.subprocess, "Popen", fake_popen)

        worker = Daemon.__new__(Daemon)
        worker.name = "pwsh-test"
        worker.cwd = str(tmp_path)
        worker.ssh = None
        worker.verbose = False
        worker._shell = "powershell"
        worker._powershell_executable = executable
        worker._current_proc = None

        output, exit_code = worker._exec_shell({
            "id": "task-1",
            "command": "Write-Output ok",
            "timeout": 5,
            "no_log": True,
        })

        assert exit_code == 0
        assert output == ""
        assert launched[0][0][0] == executable
        assert b"Write-Output ok" in launched[0][2].stdin.data
