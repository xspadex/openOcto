"""Tests for Relay key generation and utility methods."""

import time
import pytest
from openocto.relay import Relay


def _make_relay(workspace="testws"):
    """Create a Relay instance without connecting (for testing pure methods)."""
    r = Relay.__new__(Relay)
    r.redis_url = ""
    r.redis_token = ""
    r.workspace = workspace
    r.proxy_url = ""
    r._use_proxy = False
    return r


class TestKeyGeneration:
    def test_key_simple(self):
        r = _make_relay("myws")
        assert r._key("terminals") == "octo:myws:terminals"

    def test_key_multi_parts(self):
        r = _make_relay("ws")
        assert r._key("task", "gpu", "abc123") == "octo:ws:task:gpu:abc123"

    def test_key_empty_workspace(self):
        r = _make_relay("")
        assert r._key("test") == "octo::test"

    def test_task_key(self):
        r = _make_relay("ws")
        assert r._task_key("gpu", "t123") == "octo:ws:task:gpu:t123"

    def test_queue_key(self):
        r = _make_relay("ws")
        assert r._queue_key("gpu") == "octo:ws:queue:gpu"


class TestTaskIdGeneration:
    def test_format(self):
        tid = Relay.generate_task_id("gpu")
        parts = tid.split("-")
        assert len(parts) == 2
        # First part is timestamp (integer)
        assert parts[0].isdigit()
        # Second part is 8 hex chars
        assert len(parts[1]) == 8
        int(parts[1], 16)  # should not raise

    def test_unique(self):
        ids = {Relay.generate_task_id("gpu") for _ in range(100)}
        assert len(ids) == 100

    def test_timestamp_is_current(self):
        before = int(time.time())
        tid = Relay.generate_task_id("gpu")
        after = int(time.time())
        ts = int(tid.split("-")[0])
        assert before <= ts <= after


class TestConnectionFlags:
    def test_direct_mode(self):
        r = _make_relay()
        r.redis_url = "https://redis.example.com"
        r.redis_token = "tok"
        r.proxy_url = ""
        assert r._has_direct is True
        assert r._has_proxy is False

    def test_proxy_only_mode(self):
        r = _make_relay()
        r.redis_url = ""
        r.redis_token = ""
        r.proxy_url = "https://proxy.workers.dev"
        assert r._has_direct is False
        assert r._has_proxy is True

    def test_both_modes(self):
        r = _make_relay()
        r.redis_url = "https://redis.example.com"
        r.redis_token = "tok"
        r.proxy_url = "https://proxy.workers.dev"
        assert r._has_direct is True
        assert r._has_proxy is True

    def test_no_mode(self):
        r = _make_relay()
        r.redis_url = ""
        r.redis_token = ""
        r.proxy_url = ""
        assert r._has_direct is False
        assert r._has_proxy is False
