"""S3-compatible object storage client for file transfer relay.

Supports Cloudflare R2, AWS S3, and any S3-compatible service.
Uses AWS Signature Version 4 — zero external dependencies (stdlib only).
"""

import hashlib
import hmac
import json
import os
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone


class StorageError(Exception):
    pass


class S3Client:
    """Minimal S3-compatible client using AWS Signature V4."""

    def __init__(self, endpoint: str, access_key: str, secret_key: str,
                 bucket: str, region: str = "auto"):
        self.endpoint = endpoint.rstrip("/")
        self.access_key = access_key
        self.secret_key = secret_key
        self.bucket = bucket
        self.region = region
        # Extract host from endpoint
        parsed = urllib.parse.urlparse(self.endpoint)
        self.host = parsed.hostname
        # Build a no-proxy opener to bypass system proxy for large uploads
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({})  # empty = no proxy
        )

    def _sign(self, method: str, path: str, headers: dict,
              payload_hash: str, query: str = "") -> dict:
        """Generate AWS Signature V4 headers."""
        now = datetime.now(timezone.utc)
        datestamp = now.strftime("%Y%m%d")
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")

        headers["x-amz-date"] = amz_date
        headers["x-amz-content-sha256"] = payload_hash

        # Canonical request
        signed_header_keys = sorted(headers.keys())
        signed_headers = ";".join(signed_header_keys)
        canonical_headers = "".join(
            f"{k}:{headers[k]}\n" for k in signed_header_keys
        )
        canonical_request = "\n".join([
            method, path, query,
            canonical_headers, signed_headers, payload_hash,
        ])

        # String to sign
        scope = f"{datestamp}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256", amz_date, scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ])

        # Signing key
        def _hmac(key, msg):
            return hmac.new(key, msg.encode(), hashlib.sha256).digest()

        k_date = _hmac(f"AWS4{self.secret_key}".encode(), datestamp)
        k_region = _hmac(k_date, self.region)
        k_service = _hmac(k_region, "s3")
        k_signing = _hmac(k_service, "aws4_request")

        signature = hmac.new(
            k_signing, string_to_sign.encode(), hashlib.sha256
        ).hexdigest()

        headers["Authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return headers

    # Multipart upload chunk size: 20MB (fewer round trips at high latency)
    MULTIPART_CHUNK = 20 * 1024 * 1024
    # Threshold for using multipart upload: 20MB
    MULTIPART_THRESHOLD = 20 * 1024 * 1024
    # Number of concurrent part uploads
    MULTIPART_CONCURRENCY = 4

    def upload(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        """Upload data to bucket. Uses multipart for large files. Returns the object key."""
        if len(data) > self.MULTIPART_THRESHOLD:
            return self._multipart_upload(key, data, content_type)
        return self._simple_upload(key, data, content_type)

    def upload_file(self, key: str, file_path: str, content_type: str = "application/octet-stream") -> str:
        """Upload a file to bucket without reading it all into memory. Returns the object key."""
        file_size = os.path.getsize(file_path)
        if file_size > self.MULTIPART_THRESHOLD:
            return self._multipart_upload_file(key, file_path, file_size, content_type)
        with open(file_path, "rb") as f:
            return self._simple_upload(key, f.read(), content_type)

    def _simple_upload(self, key: str, data: bytes, content_type: str) -> str:
        """Single PUT upload for small files."""
        path = f"/{self.bucket}/{key}"
        payload_hash = hashlib.sha256(data).hexdigest()

        headers = {
            "content-type": content_type,
            "host": self.host,
        }
        headers = self._sign("PUT", path, headers, payload_hash)

        url = f"{self.endpoint}{path}"
        req = urllib.request.Request(url, data=data, method="PUT")
        for k, v in headers.items():
            req.add_header(k, v)

        try:
            with self._opener.open(req, timeout=300) as resp:
                return key
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            raise StorageError(f"Upload failed ({e.code}): {body}")

    def _initiate_multipart(self, key: str, content_type: str) -> str:
        """Initiate a multipart upload. Returns upload ID."""
        path = f"/{self.bucket}/{key}"
        payload_hash = hashlib.sha256(b"").hexdigest()

        headers = {
            "content-type": content_type,
            "host": self.host,
        }
        headers = self._sign("POST", path, headers, payload_hash, query="uploads=")

        url = f"{self.endpoint}{path}?uploads="
        req = urllib.request.Request(url, data=b"", method="POST")
        for k, v in headers.items():
            req.add_header(k, v)

        try:
            with self._opener.open(req, timeout=60) as resp:
                body = resp.read().decode()
                # Parse upload ID from XML response
                import re
                m = re.search(r"<UploadId>(.+?)</UploadId>", body)
                if not m:
                    raise StorageError(f"No UploadId in response: {body[:200]}")
                return m.group(1)
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            raise StorageError(f"Initiate multipart failed ({e.code}): {body}")

    def _upload_part(self, key: str, upload_id: str, part_number: int, data: bytes,
                     retries: int = 3) -> str:
        """Upload a single part. Returns ETag. Retries on transient errors."""
        path = f"/{self.bucket}/{key}"
        payload_hash = hashlib.sha256(data).hexdigest()
        query = f"partNumber={part_number}&uploadId={urllib.parse.quote(upload_id, safe='')}"

        last_err = None
        for attempt in range(retries):
            headers = {
                "content-length": str(len(data)),
                "host": self.host,
            }
            headers = self._sign("PUT", path, headers, payload_hash, query=query)

            url = f"{self.endpoint}{path}?{query}"
            req = urllib.request.Request(url, data=data, method="PUT")
            for k, v in headers.items():
                req.add_header(k, v)

            try:
                with self._opener.open(req, timeout=300) as resp:
                    etag = resp.headers.get("ETag", "").strip('"')
                    return etag
            except urllib.error.HTTPError as e:
                body = e.read().decode() if e.fp else ""
                if e.code in (500, 503) and attempt < retries - 1:
                    last_err = e
                    time.sleep(2 ** attempt)
                    continue
                raise StorageError(f"Upload part {part_number} failed ({e.code}): {body}")
            except Exception as e:
                last_err = e
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise StorageError(f"Upload part {part_number} failed: {e}")
        raise StorageError(f"Upload part {part_number} failed after {retries} retries: {last_err}")

    def _complete_multipart(self, key: str, upload_id: str, parts: list) -> None:
        """Complete a multipart upload. parts = [(part_number, etag), ...]."""
        path = f"/{self.bucket}/{key}"
        query = f"uploadId={urllib.parse.quote(upload_id, safe='')}"

        # Build XML body
        xml_parts = "".join(
            f"<Part><PartNumber>{num}</PartNumber><ETag>{etag}</ETag></Part>"
            for num, etag in parts
        )
        body = f"<CompleteMultipartUpload>{xml_parts}</CompleteMultipartUpload>"
        body_bytes = body.encode()
        payload_hash = hashlib.sha256(body_bytes).hexdigest()

        headers = {
            "content-type": "application/xml",
            "host": self.host,
        }
        headers = self._sign("POST", path, headers, payload_hash, query=query)

        url = f"{self.endpoint}{path}?{query}"
        req = urllib.request.Request(url, data=body_bytes, method="POST")
        for k, v in headers.items():
            req.add_header(k, v)

        try:
            with self._opener.open(req, timeout=60) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            raise StorageError(f"Complete multipart failed ({e.code}): {body}")

    def _abort_multipart(self, key: str, upload_id: str) -> None:
        """Abort a multipart upload (cleanup on failure)."""
        path = f"/{self.bucket}/{key}"
        query = f"uploadId={urllib.parse.quote(upload_id, safe='')}"
        payload_hash = hashlib.sha256(b"").hexdigest()

        headers = {"host": self.host}
        headers = self._sign("DELETE", path, headers, payload_hash, query=query)

        url = f"{self.endpoint}{path}?{query}"
        req = urllib.request.Request(url, method="DELETE")
        for k, v in headers.items():
            req.add_header(k, v)

        try:
            with self._opener.open(req, timeout=30) as resp:
                resp.read()
        except Exception:
            pass  # best-effort cleanup

    def _multipart_upload(self, key: str, data: bytes, content_type: str) -> str:
        """Multipart upload from bytes with parallel part uploads."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        upload_id = self._initiate_multipart(key, content_type)
        total = len(data)
        chunks = []
        offset = 0
        part_num = 1
        while offset < total:
            chunks.append((part_num, data[offset:offset + self.MULTIPART_CHUNK]))
            offset += self.MULTIPART_CHUNK
            part_num += 1

        parts = [None] * len(chunks)
        uploaded = [0]

        def _upload_one(idx: int, pnum: int, chunk: bytes):
            etag = self._upload_part(key, upload_id, pnum, chunk)
            uploaded[0] += len(chunk)
            pct = min(100, int(uploaded[0] / total * 100))
            print(f"\r[octo] Uploading: {pct}% ({uploaded[0]}/{total})", end="", flush=True)
            return idx, etag

        try:
            with ThreadPoolExecutor(max_workers=self.MULTIPART_CONCURRENCY) as ex:
                futures = {ex.submit(_upload_one, i, pnum, chunk): i
                           for i, (pnum, chunk) in enumerate(chunks)}
                for fut in as_completed(futures):
                    idx, etag = fut.result()  # raises on error
                    parts[idx] = (chunks[idx][0], etag)
            print()
            self._complete_multipart(key, upload_id, parts)
            return key
        except Exception:
            self._abort_multipart(key, upload_id)
            raise

    def _multipart_upload_file(self, key: str, file_path: str, file_size: int, content_type: str) -> str:
        """Multipart upload from file path with parallel part uploads."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        upload_id = self._initiate_multipart(key, content_type)

        # Pre-read all chunks to allow parallel upload (memory: CONCURRENCY * CHUNK_SIZE at a time)
        # For very large files we read all parts upfront — acceptable since CHUNK is 20MB
        chunks = []
        with open(file_path, "rb") as f:
            part_num = 1
            while True:
                chunk = f.read(self.MULTIPART_CHUNK)
                if not chunk:
                    break
                chunks.append((part_num, chunk))
                part_num += 1

        parts = [None] * len(chunks)
        uploaded = [0]

        def _upload_one(idx: int, pnum: int, chunk: bytes):
            etag = self._upload_part(key, upload_id, pnum, chunk)
            uploaded[0] += len(chunk)
            pct = min(100, int(uploaded[0] / file_size * 100))
            print(f"\r[octo] Uploading: {pct}% ({uploaded[0]}/{file_size})", end="", flush=True)
            return idx, etag

        try:
            with ThreadPoolExecutor(max_workers=self.MULTIPART_CONCURRENCY) as ex:
                futures = {ex.submit(_upload_one, i, pnum, chunk): i
                           for i, (pnum, chunk) in enumerate(chunks)}
                for fut in as_completed(futures):
                    idx, etag = fut.result()
                    parts[idx] = (chunks[idx][0], etag)
            print()
            self._complete_multipart(key, upload_id, parts)
            return key
        except Exception:
            self._abort_multipart(key, upload_id)
            raise

    def download(self, key: str) -> bytes:
        """Download object from bucket."""
        path = f"/{self.bucket}/{key}"
        payload_hash = hashlib.sha256(b"").hexdigest()

        headers = {"host": self.host}
        headers = self._sign("GET", path, headers, payload_hash)

        url = f"{self.endpoint}{path}"
        req = urllib.request.Request(url, method="GET")
        for k, v in headers.items():
            req.add_header(k, v)

        try:
            with self._opener.open(req, timeout=300) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            raise StorageError(f"Download failed ({e.code}): {body}")

    def delete(self, key: str) -> None:
        """Delete object from bucket."""
        path = f"/{self.bucket}/{key}"
        payload_hash = hashlib.sha256(b"").hexdigest()

        headers = {"host": self.host}
        headers = self._sign("DELETE", path, headers, payload_hash)

        url = f"{self.endpoint}{path}"
        req = urllib.request.Request(url, method="DELETE")
        for k, v in headers.items():
            req.add_header(k, v)

        try:
            with self._opener.open(req, timeout=30) as resp:
                pass
        except urllib.error.HTTPError as e:
            if e.code != 404:
                body = e.read().decode() if e.fp else ""
                raise StorageError(f"Delete failed ({e.code}): {body}")

    def presign_download(self, key: str, expires: int = 600) -> str:
        """Generate a presigned download URL (valid for `expires` seconds)."""
        now = datetime.now(timezone.utc)
        datestamp = now.strftime("%Y%m%d")
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        scope = f"{datestamp}/{self.region}/s3/aws4_request"

        path = f"/{self.bucket}/{key}"
        query_params = {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": f"{self.access_key}/{scope}",
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": str(expires),
            "X-Amz-SignedHeaders": "host",
        }
        query_string = "&".join(
            f"{k}={urllib.parse.quote(v, safe='')}"
            for k, v in sorted(query_params.items())
        )

        canonical_request = "\n".join([
            "GET", path, query_string,
            f"host:{self.host}\n", "host",
            "UNSIGNED-PAYLOAD",
        ])

        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256", amz_date, scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ])

        def _hmac(key, msg):
            return hmac.new(key, msg.encode(), hashlib.sha256).digest()

        k_date = _hmac(f"AWS4{self.secret_key}".encode(), datestamp)
        k_region = _hmac(k_date, self.region)
        k_service = _hmac(k_region, "s3")
        k_signing = _hmac(k_service, "aws4_request")

        signature = hmac.new(
            k_signing, string_to_sign.encode(), hashlib.sha256
        ).hexdigest()

        return f"{self.endpoint}{path}?{query_string}&X-Amz-Signature={signature}"


def get_storage_client(config: dict):
    """Create S3Client from config dict. Returns None if not configured."""
    storage = config.get("storage")
    if not storage:
        return None
    required = ["endpoint", "access_key", "secret_key", "bucket"]
    for k in required:
        if not storage.get(k):
            return None
    return S3Client(
        endpoint=storage["endpoint"],
        access_key=storage["access_key"],
        secret_key=storage["secret_key"],
        bucket=storage["bucket"],
        region=storage.get("region", "auto"),
    )
