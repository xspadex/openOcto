"""Tests for task signing utilities."""

from openocto.signing import build_sign_payload


class TestBuildSignPayload:
    def test_basic(self):
        result = build_sign_payload("task123", "shell", "alice")
        assert result == "task123:shell:alice"

    def test_with_special_chars(self):
        result = build_sign_payload("1234-abcd", "shell", "bob's_laptop")
        assert result == "1234-abcd:shell:bob's_laptop"

    def test_empty_requester(self):
        result = build_sign_payload("t1", "cat", "")
        assert result == "t1:cat:"

    def test_all_task_types(self):
        for task_type in ("shell", "cat", "edit", "glob", "grep", "kill", "metrics"):
            result = build_sign_payload("id", task_type, "dev")
            assert f":{task_type}:" in result

    def test_deterministic(self):
        a = build_sign_payload("t", "shell", "r")
        b = build_sign_payload("t", "shell", "r")
        assert a == b
