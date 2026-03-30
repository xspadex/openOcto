"""Tests for config encoding/decoding (join tokens)."""

import pytest
from openocto.config import encode_token, decode_token


class TestTokenRoundTrip:
    def test_basic_roundtrip(self):
        token = encode_token("https://redis.example.com", "mytoken123", "workspace1")
        result = decode_token(token)
        assert result["redis_url"] == "https://redis.example.com"
        assert result["redis_token"] == "mytoken123"
        assert result["workspace"] == "workspace1"
        assert "proxy_url" not in result

    def test_roundtrip_with_proxy(self):
        token = encode_token(
            "https://redis.example.com", "tok",
            "ws", proxy_url="https://proxy.workers.dev"
        )
        result = decode_token(token)
        assert result["redis_url"] == "https://redis.example.com"
        assert result["redis_token"] == "tok"
        assert result["workspace"] == "ws"
        assert result["proxy_url"] == "https://proxy.workers.dev"

    def test_empty_proxy_not_included(self):
        token = encode_token("https://r.io", "t", "w", proxy_url="")
        result = decode_token(token)
        assert "proxy_url" not in result

    def test_special_characters(self):
        token = encode_token(
            "https://redis.io/path?foo=bar&baz=1",
            "tok+en/with=special==chars",
            "my workspace"
        )
        result = decode_token(token)
        assert result["redis_url"] == "https://redis.io/path?foo=bar&baz=1"
        assert result["redis_token"] == "tok+en/with=special==chars"

    def test_unicode_workspace(self):
        token = encode_token("https://r.io", "t", "workspace-日本語")
        result = decode_token(token)
        assert result["workspace"] == "workspace-日本語"

    def test_long_values(self):
        long_url = "https://redis.example.com/" + "a" * 500
        long_token = "x" * 500
        token = encode_token(long_url, long_token, "ws")
        result = decode_token(token)
        assert result["redis_url"] == long_url
        assert result["redis_token"] == long_token


class TestTokenFormat:
    def test_starts_with_prefix(self):
        token = encode_token("https://r.io", "t", "w")
        assert token.startswith("octo://")

    def test_invalid_prefix_raises(self):
        with pytest.raises(ValueError, match="Invalid token"):
            decode_token("http://something")

    def test_empty_token_raises(self):
        with pytest.raises(ValueError):
            decode_token("")

    def test_garbage_base64_raises(self):
        with pytest.raises(Exception):
            decode_token("octo://not-valid-base64!!!")

    def test_valid_base64_but_not_json(self):
        import base64
        b64 = base64.urlsafe_b64encode(b"not json").decode()
        with pytest.raises(Exception):
            decode_token(f"octo://{b64}")
