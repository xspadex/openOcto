"""Tests for S3 signature generation (AWS Signature V4)."""

import hashlib
from datetime import datetime, timezone
from unittest.mock import patch
from openocto.storage import S3Client


def _make_client():
    return S3Client(
        endpoint="https://s3.example.com",
        access_key="AKIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        bucket="test-bucket",
        region="us-east-1",
    )


class TestS3Sign:
    def test_sign_returns_required_headers(self):
        client = _make_client()
        headers = {"host": "s3.example.com"}
        payload_hash = hashlib.sha256(b"").hexdigest()

        signed = client._sign("GET", "/test-bucket/key", headers, payload_hash)

        assert "Authorization" in signed
        assert "x-amz-date" in signed
        assert "x-amz-content-sha256" in signed

    def test_authorization_format(self):
        client = _make_client()
        headers = {"host": "s3.example.com"}
        payload_hash = hashlib.sha256(b"").hexdigest()

        signed = client._sign("GET", "/test-bucket/key", headers, payload_hash)
        auth = signed["Authorization"]

        assert auth.startswith("AWS4-HMAC-SHA256 Credential=")
        assert "SignedHeaders=" in auth
        assert "Signature=" in auth
        assert "AKIAIOSFODNN7EXAMPLE" in auth

    def test_sign_deterministic(self):
        """Same inputs at same time produce same signature."""
        client = _make_client()
        payload_hash = hashlib.sha256(b"test").hexdigest()
        fixed_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

        with patch("openocto.storage.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_time
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            h1 = {"host": "s3.example.com"}
            s1 = client._sign("PUT", "/test-bucket/file.txt", h1, payload_hash)

            h2 = {"host": "s3.example.com"}
            s2 = client._sign("PUT", "/test-bucket/file.txt", h2, payload_hash)

        assert s1["Authorization"] == s2["Authorization"]

    def test_different_methods_different_sigs(self):
        client = _make_client()
        payload_hash = hashlib.sha256(b"").hexdigest()
        fixed_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

        with patch("openocto.storage.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_time
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            h1 = {"host": "s3.example.com"}
            s1 = client._sign("GET", "/test-bucket/key", h1, payload_hash)

            h2 = {"host": "s3.example.com"}
            s2 = client._sign("PUT", "/test-bucket/key", h2, payload_hash)

        assert s1["Authorization"] != s2["Authorization"]

    def test_different_paths_different_sigs(self):
        client = _make_client()
        payload_hash = hashlib.sha256(b"").hexdigest()
        fixed_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

        with patch("openocto.storage.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_time
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            h1 = {"host": "s3.example.com"}
            s1 = client._sign("GET", "/test-bucket/file1", h1, payload_hash)

            h2 = {"host": "s3.example.com"}
            s2 = client._sign("GET", "/test-bucket/file2", h2, payload_hash)

        assert s1["Authorization"] != s2["Authorization"]


class TestS3PresignDownload:
    def test_presign_url_format(self):
        client = _make_client()
        url = client.presign_download("transfer/file.txt", expires=600)

        assert url.startswith("https://s3.example.com/test-bucket/transfer/file.txt?")
        assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url
        assert "X-Amz-Credential=" in url
        assert "X-Amz-Expires=600" in url
        assert "X-Amz-Signature=" in url

    def test_presign_different_keys_different_urls(self):
        client = _make_client()
        url1 = client.presign_download("file1.txt")
        url2 = client.presign_download("file2.txt")
        assert url1 != url2

    def test_presign_different_expiry(self):
        client = _make_client()
        url1 = client.presign_download("file.txt", expires=60)
        url2 = client.presign_download("file.txt", expires=3600)
        assert "X-Amz-Expires=60" in url1
        assert "X-Amz-Expires=3600" in url2
