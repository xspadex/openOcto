"""Relay layer - all communication with Upstash Redis REST API.

This is the single network interface for the entire project.
Uses only stdlib (urllib) - zero external dependencies.

Supports two connection modes:
  1. Direct: connect to Upstash Redis REST API (HTTPS, default)
  2. Proxy: connect via a Cloudflare Worker proxy (HTTP/HTTPS)

When both are configured, direct is tried first; proxy is used as fallback.
When only proxy_url is set, proxy-only mode is used (CF Worker proxy).
"""

import json
import time
import urllib.request
import urllib.error
from typing import Any, Optional


class RelayError(Exception):
    pass


class Relay:
    """Upstash Redis REST API client with optional CF Worker proxy fallback."""

    def __init__(self, redis_url: str = "", redis_token: str = "",
                 workspace: str = "default", proxy_url: str = ""):
        self.redis_url = redis_url.rstrip("/") if redis_url else ""
        self.redis_token = redis_token
        self.workspace = workspace
        self.proxy_url = proxy_url.rstrip("/") if proxy_url else ""
        self._use_proxy = False  # sticky fallback within session
        self._direct_fails = 0  # consecutive direct failures
        self._STICKY_THRESHOLD = 5  # go sticky after N consecutive direct failures

    @property
    def _has_direct(self) -> bool:
        return bool(self.redis_url and self.redis_token)

    @property
    def _has_proxy(self) -> bool:
        return bool(self.proxy_url)

    def _key(self, *parts: str) -> str:
        return ":".join(["octo", self.workspace, *parts])

    # Max URL length before switching to POST (most CDNs/proxies cap at ~8KB)
    _MAX_URL_LEN = 4096

    def _do_request(self, url: str, headers: dict, timeout: int = 15,
                    post_body: bytes = None) -> Any:
        """Execute a single HTTP request and return parsed result."""
        if post_body is not None:
            req = urllib.request.Request(url, data=post_body, method="POST")
            req.add_header("Content-Type", "application/json")
        else:
            req = urllib.request.Request(url)
        req.add_header("User-Agent", "openocto/0.1")
        for k, v in headers.items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            if not raw:
                return None
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                raise RelayError(f"Invalid JSON from relay: {raw[:200]}")
            return data.get("result")

    def _request(self, *args: str, _retries: int = 3) -> Any:
        """Execute a Redis REST command. Tries direct first, falls back to proxy."""
        path = "/" + "/".join(urllib.request.quote(str(a), safe="") for a in args)

        # Determine order: if sticky proxy or no direct, go proxy first
        if self._use_proxy and self._has_proxy:
            targets = [("proxy", self.proxy_url, {"X-Octo-Workspace": self.workspace})]
            if self._has_direct:
                targets.append(("direct", self.redis_url,
                                {"Authorization": f"Bearer {self.redis_token}"}))
        elif self._has_direct:
            targets = [("direct", self.redis_url,
                        {"Authorization": f"Bearer {self.redis_token}"})]
            if self._has_proxy:
                targets.append(("proxy", self.proxy_url,
                                {"X-Octo-Workspace": self.workspace}))
        elif self._has_proxy:
            targets = [("proxy", self.proxy_url, {"X-Octo-Workspace": self.workspace})]
        else:
            raise RelayError("No relay configured. Run 'octo init'.")

        # If URL would be too long, switch to POST with JSON body.
        # Upstash REST API accepts POST / with body: ["CMD", "arg1", ...]
        full_url_sample = (targets[0][1] if targets else "") + path
        if len(full_url_sample) > self._MAX_URL_LEN:
            post_body = json.dumps([str(a) for a in args]).encode()
            use_post = True
        else:
            post_body = None
            use_post = False

        last_error = None
        for mode, base_url, headers in targets:
            for attempt in range(_retries):
                try:
                    if use_post:
                        result = self._do_request(
                            base_url + "/", headers, post_body=post_body)
                    else:
                        result = self._do_request(base_url + path, headers)
                    if mode == "direct":
                        self._direct_fails = 0  # reset on success
                    elif mode == "proxy" and not self._use_proxy:
                        # Only go sticky after enough consecutive direct failures
                        if self._direct_fails >= self._STICKY_THRESHOLD:
                            self._use_proxy = True
                    return result
                except urllib.error.HTTPError as e:
                    body = e.read().decode() if e.fp else ""
                    if e.code == 429 and mode == "proxy" and self._has_direct:
                        # Proxy rate-limited — unstick and fall through to direct
                        self._use_proxy = False
                        self._direct_fails = 0
                        last_error = RelayError(f"Proxy rate-limited: {body}")
                        break  # try next target (direct)
                    raise RelayError(f"Redis request failed ({e.code}): {body}")
                except (urllib.error.URLError, ConnectionError, OSError) as e:
                    if mode == "direct":
                        self._direct_fails += 1
                    last_error = e
                    if attempt < _retries - 1:
                        time.sleep(1)
                        continue
                    break  # try next target

        raise RelayError(f"Connection failed (direct + proxy): {last_error}")

    # ---- Terminal Registration ----

    def register(self, name: str, tags: list, meta: Optional[dict] = None) -> None:
        info = json.dumps({
            "tags": tags,
            "meta": meta or {},
            "registered_at": int(time.time()),
            "last_heartbeat": int(time.time()),
        }, separators=(",", ":"))
        self._request("HSET", self._key("terminals"), name, info)

    def heartbeat(self, name: str) -> None:
        raw = self._request("HGET", self._key("terminals"), name)
        if not raw:
            return
        info = json.loads(raw)
        info["last_heartbeat"] = int(time.time())
        self._request(
            "HSET", self._key("terminals"), name,
            json.dumps(info, separators=(",", ":"))
        )

    def unregister(self, name: str) -> None:
        self._request("HDEL", self._key("terminals"), name)
        self._request("DEL", self._key("task", name))

    def list_terminals(self) -> list:
        raw = self._request("HGETALL", self._key("terminals"))
        if not raw:
            return []
        terminals = []
        for i in range(0, len(raw), 2):
            name = raw[i]
            info = json.loads(raw[i + 1])
            elapsed = int(time.time()) - info.get("last_heartbeat", 0)
            info["name"] = name
            info["online"] = elapsed < 360
            info["last_seen_ago"] = elapsed
            terminals.append(info)
        return terminals

    # ---- Task Management (v2: parallel queue + per-task keys) ----

    TASK_TTL = 3600  # 1 hour auto-expire for task keys
    QUEUE_MAX = 20   # max pending tasks per terminal

    @staticmethod
    def generate_task_id(target: str) -> str:
        """Generate a unique task ID."""
        import uuid
        return f"{int(time.time())}-{uuid.uuid4().hex[:8]}"

    def _task_key(self, target: str, task_id: str) -> str:
        """Per-task Redis key."""
        return self._key("task", target, task_id)

    def _queue_key(self, target: str) -> str:
        """Pending task queue key."""
        return self._key("queue", target)

    def submit_task(self, target: str, task_type: str = "shell",
                    requester: str = "", signature: str = "",
                    task_id: str = None, **kwargs) -> str:
        """Submit a task to the queue. Returns task_id."""
        task_id = task_id or self.generate_task_id(target)

        # Check queue length
        qlen = self._request("LLEN", self._queue_key(target))
        if qlen and int(qlen) >= self.QUEUE_MAX:
            raise RelayError(f"Queue full ({self.QUEUE_MAX} pending). Wait for tasks to complete.")

        task = {
            "id": task_id,
            "type": task_type,
            "status": "PENDING",
            "output": "",
            "exit_code": None,
            "created_at": int(time.time()),
        }
        if requester:
            task["requester"] = requester
        if signature:
            task["signature"] = signature
        task.update(kwargs)

        # SET the task key with TTL
        self._request(
            "SET", self._task_key(target, task_id),
            json.dumps(task, separators=(",", ":"))
        )
        self._request("EXPIRE", self._task_key(target, task_id), str(self.TASK_TTL))

        # LPUSH to queue (FIFO: LPUSH + RPOP)
        self._request("LPUSH", self._queue_key(target), task_id)

        # Also write to legacy single-task key for backward compat
        self._request(
            "SET", self._key("task", target),
            json.dumps(task, separators=(",", ":"))
        )

        # Wake target
        self.set_mode(target, "wake")
        return task_id

    def poll_task(self, target: str, task_id: str = None) -> Optional[dict]:
        """Poll a specific task by ID, or the legacy single-task key."""
        if task_id:
            raw = self._request("GET", self._task_key(target, task_id))
        else:
            # Legacy: single-task key
            raw = self._request("GET", self._key("task", target))
        if not raw:
            return None
        return json.loads(raw)

    def pop_queue(self, target: str) -> Optional[str]:
        """Pop next pending task_id from queue. Returns None if empty."""
        return self._request("RPOP", self._queue_key(target))

    def queue_length(self, target: str) -> int:
        """Get number of pending tasks in queue."""
        result = self._request("LLEN", self._queue_key(target))
        return int(result) if result else 0

    # Lua: RPOP queue + HGET mode + update heartbeat, all in 1 request.
    # KEYS[1]=queue key, KEYS[2]=terminals hash key, KEYS[3]=modes hash key
    # ARGV[1]=terminal name, ARGV[2]=current timestamp
    # Returns {task_id_or_empty, mode_or_empty}
    _POLL_QUEUE_HB_SCRIPT = (
        'local task_id = redis.call("RPOP", KEYS[1]) or "" '
        'local mode = redis.call("HGET", KEYS[3], ARGV[1]) or "" '
        'local raw = redis.call("HGET", KEYS[2], ARGV[1]) '
        'if raw then '
        '  local info = cjson.decode(raw) '
        '  info["last_heartbeat"] = tonumber(ARGV[2]) '
        '  redis.call("HSET", KEYS[2], ARGV[1], cjson.encode(info)) '
        'end '
        'return {task_id, mode}'
    )

    def poll_queue_heartbeat(self, target: str) -> tuple:
        """Pop queue + check mode + update heartbeat in a single Redis call.
        Returns (task_id_or_None, mode_str)."""
        try:
            result = self._request(
                "EVAL", self._POLL_QUEUE_HB_SCRIPT, "3",
                self._queue_key(target),
                self._key("terminals"),
                self._key("modes"),
                target, str(int(time.time()))
            )
            tid = result[0] if result and len(result) > 0 and result[0] else None
            mode_raw = result[1] if result and len(result) > 1 else ""
            mode = mode_raw if mode_raw in ("wake", "cool") else "cool"
            return tid, mode
        except RelayError:
            # Fallback: separate calls
            tid = self.pop_queue(target)
            mode = self.get_mode(target)
            self.heartbeat(target)
            return tid, mode

    # Keep old poll_heartbeat for backward compat with old daemons
    _POLL_HB_SCRIPT = (
        'local task = redis.call("GET", KEYS[1]) or "" '
        'local mode = redis.call("HGET", KEYS[3], ARGV[1]) or "" '
        'local raw = redis.call("HGET", KEYS[2], ARGV[1]) '
        'if raw then '
        '  local info = cjson.decode(raw) '
        '  info["last_heartbeat"] = tonumber(ARGV[2]) '
        '  redis.call("HSET", KEYS[2], ARGV[1], cjson.encode(info)) '
        'end '
        'return {task, mode}'
    )

    def poll_heartbeat(self, target: str) -> tuple:
        """Legacy: Poll single task key + heartbeat. For old daemons."""
        try:
            result = self._request(
                "EVAL", self._POLL_HB_SCRIPT, "3",
                self._key("task", target),
                self._key("terminals"),
                self._key("modes"),
                target, str(int(time.time()))
            )
            task_raw = result[0] if result and len(result) > 0 else ""
            mode_raw = result[1] if result and len(result) > 1 else ""
            task = json.loads(task_raw) if task_raw else None
            mode = mode_raw if mode_raw in ("wake", "cool") else "cool"
            return task, mode
        except RelayError:
            task = self.poll_task(target)
            mode = self.get_mode(target)
            self.heartbeat(target)
            return task, mode

    def update_task(self, target: str, updates: dict,
                    expected_id: str = None, task_id: str = None) -> bool:
        """Update task fields. Uses per-task key if task_id given, else legacy key."""
        if task_id:
            key = self._task_key(target, task_id)
        else:
            key = self._key("task", target)
        eid = expected_id or ""
        updates_json = json.dumps(updates, separators=(",", ":"))
        try:
            result = self._request("EVAL", self._CAS_UPDATE_SCRIPT,
                                   "1", key, eid, updates_json)
            # Upstash REST may return int 1 or string "1" for Lua return values
            return str(result) == "1"
        except RelayError:
            task_data = self.poll_task(target, task_id)
            if not task_data:
                return False
            if expected_id and task_data.get("id") != expected_id:
                return False
            task_data.update(updates)
            self._request("SET", key, json.dumps(task_data, separators=(",", ":")))
            return True

    # Lua script for atomic compare-and-swap task update.
    _CAS_UPDATE_SCRIPT = (
        'local raw = redis.call("GET", KEYS[1]) '
        'if not raw then return 0 end '
        'local task = cjson.decode(raw) '
        'if ARGV[1] ~= "" and task.id ~= ARGV[1] then return 0 end '
        'local updates = cjson.decode(ARGV[2]) '
        'for k, v in pairs(updates) do task[k] = v end '
        'redis.call("SET", KEYS[1], cjson.encode(task)) '
        'return 1'
    )

    def complete_task(self, target: str, output: str, exit_code: int,
                      expected_id: str = None, task_id: str = None) -> bool:
        """Mark task as DONE. Uses direct SET for per-task keys (simpler,
        more reliable for large outputs — avoids EVAL Lua script overhead)."""
        if task_id:
            key = self._task_key(target, task_id)
            task_data = {
                "id": expected_id or task_id,
                "status": "DONE",
                "output": output,
                "exit_code": exit_code,
            }
            self._request("SET", key, json.dumps(task_data, separators=(",", ":")))
            return True
        # Legacy path: use CAS update
        return self.update_task(target, {
            "status": "DONE",
            "output": output,
            "exit_code": exit_code,
        }, expected_id=expected_id)

    def clear_task(self, target: str, task_id: str = None) -> None:
        """Delete a task key."""
        if task_id:
            self._request("DEL", self._task_key(target, task_id))
        else:
            self._request("DEL", self._key("task", target))

    # ---- Terminal Mode (wake / cool) ----

    def set_mode(self, name: str, mode: str) -> None:
        """Set terminal polling mode: 'wake' or 'cool'."""
        self._request("HSET", self._key("modes"), name, mode)

    def get_mode(self, name: str) -> str:
        """Get terminal polling mode. Default is 'cool'."""
        result = self._request("HGET", self._key("modes"), name)
        return result if result in ("wake", "cool") else "cool"

    # ---- Network Meta ----

    def get_network_meta(self) -> Optional[dict]:
        """Get network metadata (mode, created_by, etc.)."""
        raw = self._request("GET", self._key("meta"))
        if not raw:
            return None
        return json.loads(raw)

    def set_network_meta(self, meta: dict) -> None:
        """Set network metadata."""
        self._request(
            "SET", self._key("meta"),
            json.dumps(meta, separators=(",", ":"))
        )

    # ---- Identity Registration ----

    def register_identity(self, name: str, info: dict) -> bool:
        """Register a device identity. Returns False if name already taken."""
        info_json = json.dumps(info, separators=(",", ":"))
        # HSETNX = set only if not exists
        result = self._request("HSETNX", self._key("identities"), name, info_json)
        return str(result) == "1"

    def get_identity(self, name: str) -> Optional[dict]:
        """Get identity info by name."""
        raw = self._request("HGET", self._key("identities"), name)
        if not raw:
            return None
        return json.loads(raw)

    def update_identity(self, name: str, info: dict) -> None:
        """Update an existing identity."""
        self._request(
            "HSET", self._key("identities"), name,
            json.dumps(info, separators=(",", ":"))
        )

    def remove_identity(self, name: str) -> None:
        """Remove a device identity."""
        self._request("HDEL", self._key("identities"), name)

    def list_identities(self) -> dict:
        """List all identities. Returns {name: info_dict, ...}."""
        raw = self._request("HGETALL", self._key("identities"))
        if not raw:
            return {}
        result = {}
        for i in range(0, len(raw), 2):
            result[raw[i]] = json.loads(raw[i + 1])
        return result

    # ---- ACL ----

    def get_acl(self, terminal: str) -> Optional[dict]:
        """Get ACL for a terminal."""
        raw = self._request("GET", self._key("acl", terminal))
        if not raw:
            return None
        return json.loads(raw)

    def set_acl(self, terminal: str, acl_data: dict) -> None:
        """Set ACL for a terminal."""
        self._request(
            "SET", self._key("acl", terminal),
            json.dumps(acl_data, separators=(",", ":"))
        )

    def delete_acl(self, terminal: str) -> None:
        """Delete ACL for a terminal."""
        self._request("DEL", self._key("acl", terminal))

    # ---- Invites ----

    def create_invite(self, code: str, data: dict, ttl: int = 604800) -> None:
        """Create an invite code with TTL (default 7 days)."""
        self._request(
            "SET", self._key("invite", code),
            json.dumps(data, separators=(",", ":"))
        )
        self._request("EXPIRE", self._key("invite", code), str(ttl))

    def get_invite(self, code: str) -> Optional[dict]:
        """Get invite data by code."""
        raw = self._request("GET", self._key("invite", code))
        if not raw:
            return None
        return json.loads(raw)

    def consume_invite(self, code: str) -> Optional[dict]:
        """Atomically get and delete an invite (one-time use).
        Returns invite data if it existed, None if already consumed."""
        # Lua: atomic GET + DEL — only one caller gets the data
        script = (
            'local raw = redis.call("GET", KEYS[1]) '
            'if not raw then return nil end '
            'redis.call("DEL", KEYS[1]) '
            'return raw'
        )
        try:
            raw = self._request("EVAL", script, "1", self._key("invite", code))
            if not raw:
                return None
            return json.loads(raw)
        except RelayError:
            # Fallback: non-atomic GET + DEL
            raw = self._request("GET", self._key("invite", code))
            if not raw:
                return None
            self._request("DEL", self._key("invite", code))
            return json.loads(raw)

    def delete_invite(self, code: str) -> None:
        """Delete an invite code."""
        self._request("DEL", self._key("invite", code))

    # ---- Activity Feed ----

    def push_feed(self, entry: dict) -> None:
        """Push an activity entry to the feed (keeps last 100)."""
        entry.setdefault("ts", int(time.time()))
        self._request(
            "LPUSH", self._key("feed"),
            json.dumps(entry, separators=(",", ":"))
        )
        self._request("LTRIM", self._key("feed"), "0", "99")

    def get_feed(self, count: int = 20) -> list:
        """Get recent feed entries."""
        raw = self._request("LRANGE", self._key("feed"), "0", str(count - 1))
        if not raw:
            return []
        return [json.loads(r) for r in raw]
