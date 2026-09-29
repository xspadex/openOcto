"""Worker daemon - runs on remote terminals.

Features:
- Persistent working directory across commands
- Streaming output (pushed to Redis incrementally)
- Built-in file operations (cat, edit, glob, grep)
- Kill support (checks for KILL status during execution)
"""

import base64
import io
import json
import os
import re
import signal
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from .relay import Relay, RelayError
from .config import load_config
from .storage import get_storage_client, StorageError
from .permissions import check_permission

MAX_OUTPUT = 200_000
HEARTBEAT_INTERVAL = 30  # only used for re-register fallback
STREAM_INTERVAL = 2  # seconds between output pushes
STREAM_TAIL_BYTES = 8192  # bytes to read from tail during streaming (keeps SSH polls fast)
LOG_DIR = "$HOME/.octo/logs"  # shell-expanded on remote
LOG_MIN_SECONDS = 10   # auto-delete log if command finishes under this
LOG_MAX_FILES = 20     # keep only the N most recent log files per terminal

# Polling intervals by mode
COOL_POLL_MAX = 180    # cool mode ceiling: once per 3 minutes
COOL_BACKOFF = 1.05    # cool mode: multiply interval after each empty poll (~1h to reach max)
WAKE_POLL_MIN = 1      # wake mode: fastest (just finished a task)
WAKE_POLL_MAX = 15     # wake mode: slowest (idle for a while)
WAKE_BACKOFF = 1.5     # wake mode: multiply interval after each empty poll
WAKE_TIMEOUT = 300     # wake mode: auto-cool after 5 min idle

# LAN HTTP server
LAN_PORT = 9527
REDIS_TRANSFER_MAX = 512_000  # 512KB - max file size for Redis relay
TRANSFER_KEY_TTL = 600  # 10 minutes TTL for transfer keys


def _ps_quote(s: str) -> str:
    """Quote a string for PowerShell (single quotes, double '' to escape)."""
    return "'" + s.replace("'", "''") + "'"


def _resolve_pwsh() -> str:
    """Resolve PowerShell 7 without falling back to Windows PowerShell 5.1."""
    override = os.environ.get("OCTO_PWSH", "").strip()
    if override:
        candidate = os.path.expandvars(os.path.expanduser(override))
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
        if os.path.isfile(candidate):
            return candidate
        raise FileNotFoundError(
            f"OCTO_PWSH points to a missing executable: {candidate}"
        )

    resolved = shutil.which("pwsh")
    if resolved:
        return resolved

    candidates = [
        os.path.join(
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            "PowerShell", "7", "pwsh.exe",
        ),
        os.path.join(
            os.environ.get("LOCALAPPDATA", ""),
            "Microsoft", "WindowsApps", "pwsh.exe",
        ),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate

    raise FileNotFoundError(
        "PowerShell 7 (pwsh.exe) is required for Windows terminals. "
        "Install PowerShell 7 and ensure pwsh.exe is on PATH, or set OCTO_PWSH."
    )


def _get_lan_ip() -> str:
    """Get local LAN IP, preferring real LAN addresses over virtual adapters (198.18.x.x)."""
    import ipaddress

    def _score(ip: str) -> int:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return -1
        if ip.startswith("198.18.") or ip.startswith("198.19."):
            return 0  # IANA benchmarking range — Apple/WSL virtual adapters
        if addr.is_loopback or addr.is_link_local:
            return 0
        if addr.is_private:
            return 2  # real LAN (192.168.x, 10.x, 172.16-31.x)
        return 1

    candidates = []

    # Method 1: `hostname -I` lists all IPv4 addresses on Linux
    if sys.platform.startswith("linux"):
        try:
            out = subprocess.check_output(["hostname", "-I"],
                                          timeout=3, stderr=subprocess.DEVNULL).decode()
            candidates.extend(out.split())
        except Exception:
            pass

    # Method 2: `ifconfig -a` / `ipconfig getifaddr` on macOS
    if sys.platform == "darwin":
        try:
            out = subprocess.check_output(["ifconfig", "-a"],
                                          timeout=3, stderr=subprocess.DEVNULL).decode()
            import re
            candidates.extend(re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out))
        except Exception:
            pass

    # Method 3: UDP connect — last resort (may pick wrong interface)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(("8.8.8.8", 80))
        candidates.append(s.getsockname()[0])
        s.close()
    except (OSError, socket.error):
        pass

    candidates = [ip for ip in candidates
                  if ip and not ipaddress.ip_address(ip).is_loopback]
    if not candidates:
        return ""
    candidates.sort(key=_score, reverse=True)
    return candidates[0]


def _is_safe_path(path: str, base_dir: str = None) -> bool:
    """Validate a path is absolute and doesn't traverse outside allowed areas.
    Blocks null bytes and relative path tricks."""
    if not path or "\x00" in path:
        return False
    abs_path = os.path.abspath(path)
    # Block common sensitive paths
    sensitive = ("/etc/shadow", "/etc/passwd", "/.ssh/", "/.octo/config.json",
                 "/.env", "/credentials", "/.aws/")
    for s in sensitive:
        if s in abs_path:
            return False
    if base_dir:
        return abs_path.startswith(os.path.abspath(base_dir))
    return True


class _LanFileHandler(BaseHTTPRequestHandler):
    """HTTP handler for LAN file transfer."""

    def log_message(self, format, *args):
        pass  # Suppress default logging

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
            return

        if self.path == "/wake":
            wake_event = getattr(self.server, "_wake_event", None)
            if wake_event:
                wake_event.set()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
            return

        if self.path.startswith("/file?"):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            file_path = query.get("path", [None])[0]
            if not file_path or not _is_safe_path(file_path):
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"Access denied")
                return
            if not os.path.isfile(file_path):
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"File not found")
                return
            try:
                size = os.path.getsize(file_path)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size))
                self.send_header("X-Filename", os.path.basename(file_path))
                self.end_headers()
                with open(file_path, "rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except Exception:
                self.send_response(500)
                self.end_headers()
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path.startswith("/receive"):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            dest = query.get("dest", [None])[0]
            if not dest or not _is_safe_path(dest):
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"Access denied")
                return

            content_length = int(self.headers.get("Content-Length", 0))
            if content_length <= 0:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Empty body")
                return

            try:
                dest_dir = os.path.dirname(dest)
                if dest_dir:
                    os.makedirs(dest_dir, exist_ok=True)
                with open(dest, "wb") as f:
                    remaining = content_length
                    while remaining > 0:
                        chunk = self.rfile.read(min(65536, remaining))
                        if not chunk:
                            break
                        f.write(chunk)
                        remaining -= len(chunk)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "status": "ok",
                    "path": dest,
                    "size": content_length,
                }).encode())
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b"Internal error")
            return

        self.send_response(404)
        self.end_headers()


class Daemon:
    def __init__(self, relay: Relay, name: str, tags: list, cwd: str = None, ssh: str = None,
                 log_min_seconds: int = None, log_max_files: int = None,
                 lan_port: int = None, verbose: bool = False,
                 owner_identity: str = "",
                 cool_max_interval: int = None, cool_idle_timeout: int = None):
        self.relay = relay
        self.name = name
        self.tags = tags
        self.ssh = ssh  # e.g. "user@gpu-server"
        self.verbose = verbose
        self._owner_identity = owner_identity  # identity of who started this daemon
        self.log_min_seconds = log_min_seconds if log_min_seconds is not None else LOG_MIN_SECONDS
        self.log_max_files = log_max_files if log_max_files is not None else LOG_MAX_FILES
        self._cool_max_interval = cool_max_interval if cool_max_interval is not None else COOL_POLL_MAX
        self._cool_idle_timeout = cool_idle_timeout if cool_idle_timeout is not None else WAKE_TIMEOUT
        self._running = True
        self._current_proc = None  # legacy single-task mode
        self._ssh_control = f"/tmp/.octo_ssh_{name}"

        # Parallel task management
        self._max_parallel = 5
        self._max_parallel_ssh = 3
        self._running_tasks = {}  # {task_id: (thread, process)}
        self._task_lock = threading.Lock()
        self._wake_event = threading.Event()  # set by LAN /wake to interrupt cool sleep
        self._lan_port = lan_port or LAN_PORT
        self._lan_server = None
        self._lan_ip = ""

        # Permission system (loaded on start for public networks)
        self._network_mode = None  # "personal" or "public"
        self._acl_cache = None
        self._acl_last_refresh = 0
        self._ACL_REFRESH_INTERVAL = 30  # seconds

        # For SSH mode, cwd defaults to home dir (resolved on first connect)
        # For local mode, cwd defaults to current dir
        self.cwd = cwd or ("~" if ssh else os.getcwd())

        # Detect shell: SSH always uses bash; Windows requires PowerShell 7.
        self._powershell_executable = None
        if ssh:
            self._shell = "bash"
        elif sys.platform == "win32":
            self._shell = "powershell"
            self._powershell_executable = _resolve_pwsh()
        else:
            self._shell = os.path.basename(os.environ.get("SHELL", "/bin/bash"))

    @staticmethod
    def _kill_proc_tree(proc):
        """Kill process and all its children."""
        if sys.platform == "win32":
            # Windows: use taskkill /T to kill entire process tree
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, timeout=10,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass
        else:
            # Unix: kill process group
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass

    def _start_lan_server(self):
        """Start LAN HTTP file server in background thread."""
        try:
            if self.ssh:
                # SSH daemon runs locally — bind to localhost only (for /wake)
                self._lan_ip = "127.0.0.1"
                bind_addr = "127.0.0.1"
            else:
                self._lan_ip = _get_lan_ip()
                if not self._lan_ip:
                    print("[octo] Could not detect LAN IP, skipping LAN server.")
                    return
                bind_addr = "0.0.0.0"
            server = HTTPServer((bind_addr, self._lan_port), _LanFileHandler)
            server._wake_event = self._wake_event
            self._lan_server = server
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            print(f"[octo] LAN server: http://{self._lan_ip}:{self._lan_port}")
        except OSError as e:
            print(f"[octo] LAN server failed to start: {e}")
            self._lan_ip = ""

    def _load_network_mode(self):
        """Check network mode once at startup."""
        try:
            meta = self.relay.get_network_meta()
            self._network_mode = meta.get("mode", "personal") if meta else "personal"
        except RelayError:
            self._network_mode = "personal"
        if self._network_mode == "public":
            print(f"[octo] Network mode: PUBLIC (permissions enforced)")
            self._refresh_acl()
        else:
            print(f"[octo] Network mode: PERSONAL (no permissions)")

    def _refresh_acl(self):
        """Refresh ACL cache from Redis."""
        try:
            self._acl_cache = self.relay.get_acl(self.name)
            self._acl_last_refresh = time.time()
        except RelayError:
            pass

    def _check_permission(self, task: dict) -> tuple:
        """Check if a task is authorized. Returns (allowed, reason).

        Personal network: always allowed.
        Public network: check ACL + verify signature.
        """
        if self._network_mode != "public":
            return True, "ok"

        requester = task.get("requester", "")
        task_type = task.get("type", "shell")

        # Owner always has full access
        if self._owner_identity and requester == self._owner_identity:
            return True, "ok"

        # Refresh ACL periodically
        if time.time() - self._acl_last_refresh > self._ACL_REFRESH_INTERVAL:
            self._refresh_acl()

        # Check role-based permission
        allowed, reason = check_permission(self._acl_cache, requester, task_type)
        if not allowed:
            return False, reason

        # Verify signature
        if not requester:
            return False, "no requester identity"

        signature = task.get("signature", "")
        if not signature:
            return False, "no signature (public network requires signed tasks)"

        # Get requester's public key from identity registry
        identity_info = self.relay.get_identity(requester)
        if not identity_info:
            return False, f"unknown identity: {requester}"

        public_key_b64 = identity_info.get("public_key", "")
        if not public_key_b64:
            return False, f"no public key for: {requester}"

        # Lazy import signing module
        from .signing import verify_signature, build_sign_payload
        payload = build_sign_payload(task.get("id", ""), task_type, requester)
        if not verify_signature(payload, signature, public_key_b64):
            return False, f"invalid signature from: {requester}"

        return True, "ok"

    def start(self):
        # SSH mode: establish persistent ControlMaster connection
        if self.ssh:
            self._ssh_connect()

        # Start LAN HTTP server for file transfer
        self._start_lan_server()

        self.relay.register(self.name, self.tags, {
            "cwd": self.cwd,
            "ssh": self.ssh or "",
            "shell": self._shell,
            "shell_version": "7" if self._powershell_executable else "",
            "platform": sys.platform,
            "lan_ip": self._lan_ip,
            "lan_port": self._lan_port,
            "owner": self._owner_identity,
        })
        self.relay.set_mode(self.name, "wake")
        mode_str = f"SSH → {self.ssh}" if self.ssh else "LOCAL"
        print(f"[octo] Terminal '{self.name}' registered ({mode_str}, shell: {self._shell}, tags: {self.tags})")
        print(f"[octo] Cool: idle after {self._cool_idle_timeout}s, max interval {self._cool_max_interval}s")
        print(f"[octo] Working directory: {self.cwd}")
        print(f"[octo] Daemon running (WAKE mode). Waiting for tasks...")

        # Load network mode (personal vs public)
        self._load_network_mode()

        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

        self._poll_loop()

    def _shutdown(self, signum, frame):
        print(f"\n[octo] Shutting down...")
        self._running = False
        if self._current_proc:
            self._kill_proc_tree(self._current_proc)
        if self.ssh:
            self._ssh_disconnect()
        try:
            self.relay.unregister(self.name)
            print(f"[octo] Terminal '{self.name}' unregistered.")
        except RelayError:
            pass
        sys.exit(0)

    # ---- SSH ----

    @property
    def _use_control_master(self) -> bool:
        """ControlMaster uses Unix sockets, not available on Windows."""
        return sys.platform != "win32"

    def _ssh_base(self) -> list:
        """Base SSH command with ControlPath if supported."""
        alive = ["-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=6"]
        connect = ["-o", "ConnectTimeout=10"]
        if self._use_control_master:
            return ["ssh", "-o", f"ControlPath={self._ssh_control}", *connect, *alive]
        return ["ssh", *connect, *alive]

    def _is_control_master_alive(self) -> bool:
        """Check if the SSH ControlMaster process is still running."""
        if not self._use_control_master:
            return True
        try:
            r = subprocess.run(
                ["ssh", "-O", "check", "-o", f"ControlPath={self._ssh_control}", self.ssh],
                capture_output=True, timeout=5,
            )
            return r.returncode == 0
        except Exception:
            return False

    def _ssh_connect(self):
        """Establish SSH connection. Uses ControlMaster on Unix."""
        print(f"[octo] Connecting to {self.ssh}...")

        if self._use_control_master:
            r = subprocess.run(
                ["ssh", "-MNf",
                 "-o", f"ControlPath={self._ssh_control}",
                 "-o", "ControlPersist=yes",
                 "-o", "ServerAliveInterval=30",
                 "-o", "ServerAliveCountMax=3",
                 self.ssh],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            if r.returncode != 0:
                raise RuntimeError(f"SSH connection failed: {r.stderr.strip()}")
            print(f"[octo] SSH ControlMaster established.")
        else:
            # Windows: verify SSH connectivity with a simple command
            r = subprocess.run(
                ["ssh", "-o", "BatchMode=yes",
                 "-o", "ServerAliveInterval=30",
                 self.ssh, "echo ok"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            if r.returncode != 0:
                raise RuntimeError(f"SSH connection failed: {r.stderr.strip()}")
            print(f"[octo] SSH connection verified.")

        # Resolve initial cwd (~ → actual path)
        if self.cwd == "~":
            result = self._ssh_exec("pwd")
            self.cwd = result.stdout.strip() or "/root"

    def _ssh_disconnect(self):
        """Close SSH connection."""
        if self._use_control_master:
            subprocess.run(
                ["ssh", "-O", "exit",
                 "-o", f"ControlPath={self._ssh_control}",
                 self.ssh],
                capture_output=True, timeout=5,
            )
        print(f"[octo] SSH connection closed.")

    def _ssh_exec(self, command: str, timeout: int = 30) -> subprocess.CompletedProcess:
        """Execute a single command over SSH (blocking)."""
        return subprocess.run(
            [*self._ssh_base(), self.ssh, command],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )

    def _ssh_popen(self, command: str) -> subprocess.Popen:
        """Execute a command over SSH (streaming, non-blocking)."""
        return subprocess.Popen(
            [*self._ssh_base(), self.ssh, command],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    @property
    def _running_count(self) -> int:
        with self._task_lock:
            return len(self._running_tasks)

    @property
    def _max_concurrent(self) -> int:
        return self._max_parallel_ssh if self.ssh else self._max_parallel

    def _register_task(self, task_id: str, thread: threading.Thread, proc=None):
        with self._task_lock:
            self._running_tasks[task_id] = (thread, proc)

    def _unregister_task(self, task_id: str):
        with self._task_lock:
            self._running_tasks.pop(task_id, None)

    def _poll_loop(self):
        current_interval = WAKE_POLL_MIN
        last_mode = "wake"
        wake_idle_since = time.time()
        consecutive_failures = 0

        while self._running:
            try:
                # Check capacity before popping from queue
                if self._running_count < self._max_concurrent:
                    # Pop from queue + heartbeat in one call
                    task_id, mode = self.relay.poll_queue_heartbeat(self.name)
                else:
                    # At capacity, just heartbeat (no queue pop)
                    task_id = None
                    mode = self.relay.get_mode(self.name)
                    self.relay.heartbeat(self.name)

                if consecutive_failures > 0:
                    print(f"[octo] Connection recovered after {consecutive_failures} failures.")
                    consecutive_failures = 0

                # Handle mode change
                if mode != last_mode:
                    if mode == "wake":
                        current_interval = WAKE_POLL_MIN
                        wake_idle_since = time.time()
                        print(f"[octo] Mode: WAKE")
                    else:
                        # Start cool mode at WAKE_POLL_MAX, gradually backoff
                        current_interval = WAKE_POLL_MAX
                        wake_idle_since = None
                        running = self._running_count
                        print(f"[octo] Mode: COOL (starting at {current_interval}s, {running} tasks running)")
                    last_mode = mode

                # Got a new task from queue
                got_work = False
                if task_id:
                    task = self.relay.poll_task(self.name, task_id)
                    if task and task.get("status") == "PENDING":
                        # Mark legacy key as RUNNING immediately so the legacy
                        # check below never sees PENDING for this task, even
                        # after the per-task key is deleted by the MCP caller.
                        self.relay.update_task(self.name, {"status": "RUNNING"},
                                               expected_id=task_id)
                        # Dispatch in a new thread
                        cwd_snapshot = self.cwd
                        t = threading.Thread(
                            target=self._dispatch_parallel,
                            args=(task, task_id, cwd_snapshot),
                            daemon=True
                        )
                        self._register_task(task_id, t)
                        t.start()
                        got_work = True
                        running = self._running_count
                        print(f"[octo] Task {task_id} started ({running}/{self._max_concurrent} slots)")

                # Also check legacy single-task key for old clients.
                # Only dispatch if the task doesn't have a per-task key
                # (i.e. it was submitted by an old client that only writes
                # the legacy key). New clients write both, so the per-task
                # key existing means the queue path already handled it.
                if not got_work:
                    legacy = self.relay.poll_task(self.name)
                    if legacy and legacy.get("status") == "PENDING":
                        legacy_id = legacy.get("id", "legacy")
                        # Skip if per-task key exists (new client — handled via queue)
                        per_task = self.relay.poll_task(self.name, task_id=legacy_id)
                        if per_task is not None:
                            pass  # new client task, queue path handles it
                        elif legacy_id not in self._running_tasks:
                            cwd_snapshot = self.cwd
                            t = threading.Thread(
                                target=self._dispatch_legacy,
                                args=(legacy, cwd_snapshot),
                                daemon=True
                            )
                            self._register_task(legacy_id, t)
                            t.start()
                            got_work = True

                if got_work:
                    current_interval = WAKE_POLL_MIN
                    wake_idle_since = time.time()
                    if last_mode == "cool":
                        last_mode = "wake"
                        self.relay.set_mode(self.name, "wake")
                        print(f"[octo] Mode: WAKE (task received)")
                elif last_mode == "wake":
                    current_interval = min(
                        current_interval * WAKE_BACKOFF, WAKE_POLL_MAX
                    )
                    now = time.time()
                    if wake_idle_since and (now - wake_idle_since > self._cool_idle_timeout):
                        if self._running_count == 0:
                            self.relay.set_mode(self.name, "cool")
                            current_interval = WAKE_POLL_MAX
                            last_mode = "cool"
                            wake_idle_since = None
                            print(f"[octo] Auto-cooled after {self._cool_idle_timeout}s idle.")
                elif last_mode == "cool":
                    # Gradual backoff in cool mode: slowly increase interval
                    # from WAKE_POLL_MAX to cool_max_interval
                    current_interval = min(
                        current_interval * COOL_BACKOFF, self._cool_max_interval
                    )

            except RelayError as e:
                consecutive_failures += 1
                print(f"[octo] Poll error ({consecutive_failures}x): {e}")
                if consecutive_failures >= 3:
                    try:
                        self.relay.register(self.name, self.tags, {
                            "cwd": self.cwd,
                            "ssh": self.ssh or "",
                            "shell": self._shell,
                            "shell_version": "7" if self._powershell_executable else "",
                            "platform": sys.platform,
                            "lan_ip": self._lan_ip,
                            "lan_port": self._lan_port,
                            "owner": self._owner_identity,
                        })
                        print(f"[octo] Re-registered after {consecutive_failures} failures.")
                        consecutive_failures = 0
                    except Exception:
                        pass
                if consecutive_failures >= 10 and consecutive_failures % 10 == 0:
                    if self.relay._has_proxy and self.relay._has_direct:
                        old = "proxy" if self.relay._use_proxy else "direct"
                        self.relay._use_proxy = not self.relay._use_proxy
                        new = "proxy" if self.relay._use_proxy else "direct"
                        print(f"[octo] Switching relay: {old} → {new}")
            except Exception as e:
                print(f"[octo] Poll loop error: {e}")

            # Use wake_event so LAN /wake can interrupt the sleep instantly.
            # In cool mode this avoids extra Redis calls while still allowing
            # immediate wake-up when the MCP server pokes the daemon.
            self._wake_event.wait(timeout=current_interval)
            self._wake_event.clear()

    def _dispatch_parallel(self, task: dict, task_id: str, cwd: str):
        """Execute a task from the queue in its own thread."""
        try:
            # Override cwd if task specifies one
            task_cwd = task.get("cwd", cwd)
            saved_cwd = self.cwd
            self.cwd = task_cwd

            self._dispatch(task, task_id=task_id)

            # Only update global CWD if this is the only running task
            if self._running_count <= 1:
                pass  # _dispatch already updates self.cwd
            else:
                self.cwd = saved_cwd  # restore, don't pollute global CWD
        except Exception as e:
            print(f"[octo] Task {task_id} error: {e}")
            try:
                self.relay.complete_task(self.name, f"Error: {e}", 1, task_id=task_id)
            except Exception:
                pass
        finally:
            self._unregister_task(task_id)
            running = self._running_count
            print(f"[octo] Task {task_id} finished ({running}/{self._max_concurrent} slots)")

    def _dispatch_legacy(self, task: dict, cwd: str):
        """Execute a task from the legacy single-key in its own thread."""
        try:
            task_cwd = task.get("cwd", cwd)
            self.cwd = task_cwd
            self._dispatch(task)
        except Exception as e:
            print(f"[octo] Legacy task error: {e}")
        finally:
            task_id = task.get("id", "legacy")
            self._unregister_task(task_id)

    # ---- Task Dispatch ----

    def _dispatch(self, task: dict, task_id: str = None):
        """Execute a task. If task_id is given, uses per-task key; else legacy single key."""
        task_type = task.get("type", "shell")
        tid = task_id or task["id"]
        requester = task.get("requester", "?")
        print(f"[octo] Task {tid} ({task_type}) from {requester}")
        if self.verbose:
            for k, v in task.items():
                if k not in ("id", "type", "status", "output", "exit_code", "created_at"):
                    val = str(v)
                    if len(val) > 200:
                        val = val[:200] + "..."
                    print(f"  [verbose] {k}: {val}")

        # Update status to RUNNING
        self.relay.update_task(self.name, {"status": "RUNNING"},
                               expected_id=tid, task_id=task_id)

        # Permission gate
        allowed, reason = self._check_permission(task)
        if not allowed:
            print(f"[octo] DENIED: {reason}")
            self.relay.complete_task(
                self.name,
                f"[octo] Permission denied: {reason}",
                1,
                expected_id=tid,
                task_id=task_id,
            )
            return

        handlers = {
            "shell": self._exec_shell,
            "cat": self._exec_cat,
            "edit": self._exec_edit,
            "glob": self._exec_glob,
            "grep": self._exec_grep,
            "transfer_download": self._exec_transfer_download,
            "clipboard_write": self._exec_clipboard_write,
            "clipboard_read": self._exec_clipboard_read,
            "inbox_receive": self._exec_inbox_receive,
            "inbox_list": self._exec_inbox_list,
            "metrics": self._exec_metrics,
        }

        handler = handlers.get(task_type)
        if not handler:
            self.relay.complete_task(self.name, f"Unknown task type: {task_type}", 1,
                                     expected_id=tid, task_id=task_id)
            return

        try:
            output, exit_code = handler(task)
        except Exception as e:
            output, exit_code = f"[octo] Internal error: {e}", 1

        # Free the task slot BEFORE result delivery.
        # Delivery can block for seconds on retries — don't let it prevent
        # new tasks from starting.
        self._unregister_task(task_id)

        # Truncate
        if len(output) > MAX_OUTPUT:
            output = (
                f"[octo] Output truncated ({len(output)} chars, showing last {MAX_OUTPUT})\n"
                + output[-MAX_OUTPUT:]
            )

        # Deliver result back (retry with exponential backoff)
        for attempt in range(5):
            try:
                # On retry after failure, try resetting proxy sticky flag
                # so the next attempt tries direct first.
                if attempt > 0 and self.relay._has_direct:
                    self.relay._use_proxy = False
                    self.relay._direct_fails = 0
                written = self.relay.complete_task(
                    self.name, output, exit_code, expected_id=tid, task_id=task_id)
                print(f"[octo] Task {tid}: complete_task written={written}, task_id={task_id}, output_len={len(output)}")
                if not written:
                    print(f"[octo] Task {tid}: result not delivered (key gone).")
                break
            except Exception as e:
                print(f"[octo] Failed to deliver result (attempt {attempt + 1}/5): {e}")
                if attempt < 4:
                    time.sleep(2 ** attempt)  # 1, 2, 4, 8s
        else:
            print(f"[octo] CRITICAL: Could not deliver task result after 5 attempts.")
            # Fallback: try marking FAILED with minimal payload so MCP side
            # doesn't wait forever.  The small body has a higher chance of
            # getting through than the original (possibly large) output.
            try:
                self.relay.complete_task(
                    self.name,
                    "[octo] Result delivery failed. Task completed but output lost.",
                    1,
                    expected_id=tid,
                    task_id=task_id,
                )
                print(f"[octo] Task {tid}: fallback FAILED marker delivered.")
            except Exception:
                pass  # truly unreachable — MCP side will hit its timeout

        status = "OK" if exit_code == 0 else f"FAILED (exit {exit_code})"
        print(f"[octo] Task {tid}: {status}")

        # Send notification if requested
        notify_target = task.get("notify")
        if notify_target:
            try:
                notify_msg = task.get("notify_message", "")
                elapsed = int(time.time() - task.get("created_at", time.time()))
                elapsed_str = f"{elapsed // 3600}h{(elapsed % 3600) // 60}m" if elapsed >= 3600 else f"{elapsed // 60}m{elapsed % 60}s"
                title = notify_msg if notify_msg else (
                    f"Task {'completed' if exit_code == 0 else 'failed'} on {self.name}"
                )
                # Include last few lines of output
                output_tail = output.strip().split("\n")[-3:]
                body = f"[{elapsed_str}] exit {exit_code}\n" + "\n".join(output_tail)
                self.relay.push_notification(
                    notify_target, title, body[:500],
                    source=self.name,
                    extra={"task_id": tid, "exit_code": exit_code},
                )
                print(f"[octo] Notification sent to '{notify_target}'")
            except Exception as e:
                print(f"[octo] Notification failed: {e}")

    # ---- Shell Execution (streaming + persistent cwd) ----

    def _cwd_file(self) -> str:
        """Temp file path for cwd tracking (must be accessible to both Python and shell)."""
        if sys.platform == "win32":
            return os.path.join(os.environ.get("TEMP", "C:\\Temp"), f".octo_cwd_{self.name}")
        return f"/tmp/.octo_cwd_{self.name}"

    def _wrap_bash(self, command: str, cwd_file: str, log_file: str = None) -> str:
        base = f'exec 3>&- 4>&- 5>&- 6>&- 7>&- 8>&- 9>&-; '
        cd = f'cd {shlex.quote(self.cwd)} 2>/dev/null || cd ~ 2>/dev/null || cd /; '
        save_cwd = f'pwd > {shlex.quote(cwd_file)}; '

        if log_file:
            # A: auto-delete log if command finishes under threshold
            cleanup_short = (
                f'__octo_elapsed=$(( $(date +%s) - __octo_start )); '
                f'if [ $__octo_elapsed -lt {self.log_min_seconds} ]; then '
                f'rm -f {log_file}; '
                f'else '
                f'ln -sf {log_file} {LOG_DIR}/{self.name}_latest.log; '
                f'fi; '
            )
            # B: keep only last N log files
            keep_n = self.log_max_files + 1  # +1 for tail -n +N meaning "starting from Nth"
            cleanup_old = (
                f'ls -t {LOG_DIR}/{self.name}_*.log 2>/dev/null '
                f'| grep -v _latest.log '
                f'| tail -n +{keep_n} '
                f'| xargs rm -f 2>/dev/null; '
            )
            # NOTE: Do NOT use `| tee` — it creates a pipe that prevents
            # nohup/background commands from finishing (pipe fd inheritance).
            # Instead, output goes to stdout (captured by Popen PIPE),
            # and we copy to log_file after the command finishes.
            return (
                f'{base}'
                f'mkdir -p {LOG_DIR}; '
                f'__octo_start=$(date +%s); '
                f'{cd}'
                f'{{ {command}\n}} 2>&1; '
                f'__octo_ec=$?; '
                f'{cleanup_short}'
                f'{cleanup_old}'
                f'{save_cwd}'
                f'exit $__octo_ec'
            )
        return (
            f'{base}'
            f'{cd}'
            f'{{ {command}\n}}; '
            f'__octo_ec=$?; '
            f'{save_cwd}'
            f'exit $__octo_ec'
        )

    def _wrap_powershell(self, command: str, cwd_file: str) -> str:
        return (
            f'[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n'
            f'$OutputEncoding = [System.Text.Encoding]::UTF8\n'
            f'$ErrorActionPreference = "Continue"\n'
            f'Set-Location -Path {_ps_quote(self.cwd)} -ErrorAction SilentlyContinue\n'
            f'if (-not (Test-Path -Path {_ps_quote(self.cwd)} -PathType Container)) {{ Set-Location $HOME }}\n'
            f'{command}\n'
            f'$__octo_ec = if ($LASTEXITCODE -ne $null) {{ $LASTEXITCODE }} else {{ if ($?) {{ 0 }} else {{ 1 }} }}\n'
            f'[System.IO.File]::WriteAllText({_ps_quote(cwd_file)}, (Get-Location).Path)\n'
            f'exit $__octo_ec\n'
        )

    def _exec_shell_ssh_detached(self, task: dict) -> tuple:
        """SSH mode: run command detached on remote, poll output via short SSH calls.
        This avoids long-lived SSH connections that get killed by firewalls."""
        command = task["command"]
        timeout = task.get("timeout", 3600)
        cwd_file = self._cwd_file()
        no_log = task.get("no_log", False)
        task_id = task["id"]

        log_file = None
        if not no_log:
            log_file = f"{LOG_DIR}/{self.name}_{task_id}.log"

        # Remote temp files for detached execution
        out_file = f"/tmp/octo_{task_id}.out"
        pid_file = f"/tmp/octo_{task_id}.pid"
        ec_file = f"/tmp/octo_{task_id}.ec"
        cmd_file = f"/tmp/octo_{task_id}_cmd.sh"

        # Base64-encode the command so the SSH/subshell cmdline doesn't
        # contain the literal command text.  Without this, `ps aux | grep`
        # or `pkill -f` matches the wrapper process and kills it.
        cmd_b64 = base64.b64encode((command + "\n").encode()).decode()
        write_cmd_file = (
            f'echo {shlex.quote(cmd_b64)} | base64 -d > {cmd_file}; '
        )

        # Build the remote script: run command in background, save pid and exit code
        cd_cmd = f'cd {shlex.quote(self.cwd)} 2>/dev/null || cd ~ 2>/dev/null || cd /;'
        save_cwd = f'pwd > {shlex.quote(cwd_file)};'
        log_tee = ""
        if log_file:
            log_tee = (
                f'mkdir -p {LOG_DIR}; '
                f'__octo_start=$(date +%s); '
            )

        # The detached script: run command from temp script file, capture
        # exit code, optionally save log.
        # NOTE: Do NOT use `| tee` here — it creates a pipe that prevents
        # nohup/background commands from completing (pipe fd stays open).
        # Instead, output goes to out_file via the outer redirect, and we
        # copy to log_file after the command finishes.
        if log_file:
            inner = (
                f'bash {cmd_file} 2>&1; '
                f'__octo_ec=$?; '
                f'rm -f {cmd_file}; '
                f'__octo_elapsed=$(( $(date +%s) - __octo_start )); '
                f'if [ $__octo_elapsed -lt {self.log_min_seconds} ]; then :; '
                f'else '
                f'cp {out_file} {log_file} 2>/dev/null; '
                f'ln -sf {log_file} {LOG_DIR}/{self.name}_latest.log; '
                f'fi; '
            )
        else:
            inner = (
                f'bash {cmd_file} 2>&1; '
                f'__octo_ec=$?; '
                f'rm -f {cmd_file}; '
            )

        remote_script = (
            f'{write_cmd_file}'
            f'exec 3>&- 4>&- 5>&- 6>&- 7>&- 8>&- 9>&-; '
            f'{log_tee}'
            f'{cd_cmd} '
            f'( {inner} {save_cwd} echo $__octo_ec > {ec_file} ) > {out_file} 2>&1 & '
            f'echo $! > {pid_file}; '
            f'disown; '
            f'echo "STARTED"'
        )

        if self.verbose:
            print(f"  [verbose] SSH remote script:\n{remote_script}")

        # Launch via short SSH call
        try:
            r = self._ssh_exec(remote_script, timeout=30)
            if self.verbose:
                print(f"  [verbose] SSH launch stdout: {(r.stdout or '')[:200]}")
                if r.stderr:
                    print(f"  [verbose] SSH launch stderr: {r.stderr[:200]}")
            if "STARTED" not in (r.stdout or ""):
                return f"Failed to start remote command: {r.stdout} {r.stderr}", 1
        except Exception as e:
            return f"SSH launch failed: {e}", 1

        # Poll loop: read output via short SSH calls
        start_time = time.time()
        last_output = ""

        while True:
            elapsed = time.time() - start_time

            # Timeout check
            if elapsed > timeout:
                try:
                    self._ssh_exec(
                        f'pid=$(cat {pid_file} 2>/dev/null) && kill -9 $pid 2>/dev/null; '
                        f'rm -f {pid_file} {ec_file} {cmd_file}',
                        timeout=10,
                    )
                except Exception:
                    pass
                return last_output + f"\n[octo] Command timed out after {timeout}s.\n", -1

            # Check kill signal from relay (per-task key)
            try:
                remote_task = self.relay.poll_task(self.name, task_id=task_id)
                if remote_task and remote_task.get("status") == "KILL":
                    try:
                        self._ssh_exec(
                            f'pid=$(cat {pid_file} 2>/dev/null) && kill -9 $pid 2>/dev/null; '
                            f'rm -f {pid_file} {ec_file} {cmd_file}',
                            timeout=10,
                        )
                    except Exception:
                        pass
                    return last_output + "\n[octo] Process killed by user.\n", -1
            except Exception:
                pass

            # Check status + read lightweight preview (tail) while running.
            # Only read full output once the command finishes, to avoid
            # transferring huge files over SSH on every poll cycle.
            try:
                r = self._ssh_exec(
                    f'wc -c < {out_file} 2>/dev/null || echo 0; '
                    f'echo "___OCTO_SEP___"; '
                    f'tail -c {STREAM_TAIL_BYTES} {out_file} 2>/dev/null; '
                    f'echo "___OCTO_SEP___"; '
                    f'cat {ec_file} 2>/dev/null || '
                    f'(pid=$(cat {pid_file} 2>/dev/null) && kill -0 $pid 2>/dev/null && echo RUNNING || echo DEAD)',
                    timeout=15,
                )
                parts = (r.stdout or "").split("___OCTO_SEP___")
                if len(parts) >= 3:
                    out_size = int(parts[0].strip() or "0")
                    tail_text = parts[1]
                    status_str = parts[2].strip()
                elif len(parts) >= 2:
                    out_size = 0
                    tail_text = parts[0]
                    status_str = parts[1].strip()
                else:
                    status_str = "RUNNING"
                    out_size = 0
                    tail_text = ""

                # While running, show tail preview with size info
                if status_str == "RUNNING":
                    if out_size > STREAM_TAIL_BYTES:
                        last_output = f"[{out_size} bytes, showing tail...]\n{tail_text}"
                    else:
                        last_output = tail_text

                    # Truncate if needed
                    if len(last_output) > MAX_OUTPUT:
                        last_output = "[truncated]...\n" + last_output[-MAX_OUTPUT:]

                    # Push streaming preview to relay
                    try:
                        written = self.relay.update_task(
                            self.name, {"output": last_output, "status": "RUNNING"},
                            expected_id=task_id,
                            task_id=task_id,
                        )
                        if not written:
                            print(f"[octo] Task {task_id}: per-task key gone, streaming update skipped.")
                    except Exception:
                        pass

                # Check if remote process was killed unexpectedly
                elif status_str == "DEAD":
                    print(f"[octo] Remote process died unexpectedly (likely killed by pkill/OOM).")
                    try:
                        self._ssh_exec(f'rm -f {out_file} {pid_file} {ec_file} {cmd_file}', timeout=10)
                    except Exception:
                        pass
                    return last_output + "\n[octo] Process died unexpectedly (killed externally?).\n", -1

                # Command finished — read full output once
                else:
                    try:
                        exit_code = int(status_str)
                    except ValueError:
                        exit_code = 0

                    # Read full output.  If tail already captured everything
                    # (output <= STREAM_TAIL_BYTES), skip the extra SSH call.
                    if out_size <= STREAM_TAIL_BYTES:
                        # tail_text IS the full output — no need for `cat`
                        last_output = tail_text
                    elif out_size <= MAX_OUTPUT:
                        r2 = self._ssh_exec(f'cat {out_file} 2>/dev/null', timeout=30)
                        last_output = r2.stdout or ""
                    else:
                        # File too large — read head + tail
                        half = MAX_OUTPUT // 2
                        r2 = self._ssh_exec(
                            f'head -c {half} {out_file} 2>/dev/null; '
                            f'echo "\\n[octo] ... truncated ({out_size} bytes) ...\\n"; '
                            f'tail -c {half} {out_file} 2>/dev/null',
                            timeout=30,
                        )
                        last_output = r2.stdout or ""

                    if len(last_output) > MAX_OUTPUT:
                        last_output = "[truncated]...\n" + last_output[-MAX_OUTPUT:]

                    # Defer cleanup and cwd update to a background thread so
                    # complete_task is called immediately and the MCP server
                    # sees DONE without waiting for extra SSH round-trips.
                    _of, _pf, _ef, _cf = out_file, pid_file, ec_file, cmd_file
                    _cwf = cwd_file
                    def _deferred_cleanup(_self=self, _of=_of, _pf=_pf, _ef=_ef, _cf=_cf, _cwf=_cwf):
                        try:
                            _self._ssh_exec(f'rm -f {_of} {_pf} {_ef} {_cf}', timeout=10)
                        except Exception:
                            pass
                        try:
                            r2 = _self._ssh_exec(
                                f'cat {shlex.quote(_cwf)} 2>/dev/null && rm -f {shlex.quote(_cwf)}',
                                timeout=10,
                            )
                            new_cwd = r2.stdout.strip()
                            if new_cwd:
                                _self.cwd = new_cwd
                        except Exception:
                            pass
                    threading.Thread(target=_deferred_cleanup, daemon=True).start()
                    return last_output, exit_code

            except Exception as e:
                # SSH poll failed — remote command may still be running, just retry
                print(f"[octo] SSH poll failed: {e}, retrying...")
                # ControlMaster may have dropped (firewall idle timeout, server restart).
                # Reconnect eagerly so the next poll goes through the fast mux path.
                if self._use_control_master and not self._is_control_master_alive():
                    print(f"[octo] ControlMaster dead, reconnecting...")
                    try:
                        self._ssh_connect()
                    except Exception as e2:
                        print(f"[octo] SSH reconnect failed: {e2}")

            time.sleep(STREAM_INTERVAL)

    def _exec_shell(self, task: dict) -> tuple:
        command = task["command"]
        timeout = task.get("timeout", 3600)
        cwd_file = self._cwd_file()
        no_log = task.get("no_log", False)

        # Generate log file path (shell-expanded $HOME)
        log_file = None
        if not no_log:
            log_file = f"{LOG_DIR}/{self.name}_{task['id']}.log"

        if self.ssh:
            return self._exec_shell_ssh_detached(task)

        # Write command to temp script to avoid self-kill:
        # Without this, the bash -c cmdline contains the user's command text,
        # so `ps aux | grep "pattern"` or `pkill -f "pattern"` matches the
        # wrapper process itself and kills it.
        cmd_file = f"/tmp/octo_{task['id']}_cmd.sh"
        use_cmd_file = False
        if self._shell != "powershell":
            try:
                fd = os.open(cmd_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(command + "\n")
                command = f"bash {shlex.quote(cmd_file)}"
                use_cmd_file = True
            except OSError:
                pass

        if self._shell == "powershell":
            wrapped = self._wrap_powershell(command, cwd_file)
            if self.verbose:
                print(f"  [verbose] PowerShell wrapped:\n{wrapped[:500]}")
            proc = subprocess.Popen(
                [
                    self._powershell_executable,
                    "-NoProfile", "-NonInteractive", "-Command", "-",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,
                start_new_session=True,
            )
            proc.stdin.write(wrapped.encode("utf-8"))
            proc.stdin.close()
        else:
            # bash/zsh/sh
            wrapped = self._wrap_bash(command, cwd_file, log_file=log_file)
            if self.verbose:
                print(f"  [verbose] Bash wrapped:\n{wrapped[:500]}")
            proc = subprocess.Popen(
                ["bash", "-c", wrapped],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        self._current_proc = proc

        # Reader thread collects output
        output_chunks = []
        lock = threading.Lock()
        reader_done = threading.Event()

        def reader():
            try:
                while True:
                    line = proc.stdout.readline()
                    if not line:
                        break
                    with lock:
                        output_chunks.append(line.decode(errors="replace"))
            except (ValueError, OSError):
                # stdout was force-closed, that's fine
                pass
            finally:
                reader_done.set()

        t = threading.Thread(target=reader, daemon=True)
        t.start()

        # Stream loop: push output + check kill + enforce timeout
        start_time = time.time()
        last_push = 0
        killed = False
        timed_out = False

        while proc.poll() is None:
            now = time.time()
            elapsed = now - start_time

            # Timeout check
            if elapsed > timeout:
                self._kill_proc_tree(proc)
                with lock:
                    output_chunks.append(
                        f"\n[octo] Command timed out after {timeout}s.\n"
                    )
                timed_out = True
                break

            if now - last_push >= STREAM_INTERVAL:
                with lock:
                    current = "".join(output_chunks)
                if len(current) > MAX_OUTPUT:
                    current = "[truncated]...\n" + current[-MAX_OUTPUT:]
                try:
                    task_id = task["id"]
                    written = self.relay.update_task(
                        self.name, {"output": current, "status": "RUNNING"},
                        expected_id=task_id,
                        task_id=task_id,
                    )
                    if not written:
                        # Per-task key gone (MCP caller timed out / cleared).
                        # Keep running — command is still useful.
                        print(f"[octo] Task {task_id}: per-task key gone, streaming update skipped.")
                    # Check for kill signal
                    remote = self.relay.poll_task(self.name, task_id=task_id)
                    if remote and remote.get("status") == "KILL":
                        self._kill_proc_tree(proc)
                        with lock:
                            output_chunks.append("\n[octo] Process killed by user.\n")
                        killed = True
                        break
                except RelayError:
                    pass
                last_push = now
            time.sleep(0.1)

        # Reap the process if it was killed
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._kill_proc_tree(proc)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass

        # bash exited (or was killed). Give reader a moment to drain,
        # then force-close the pipe to unblock it in case a nohup'd
        # child still holds the write end open.
        reader_done.wait(timeout=3)
        if not reader_done.is_set():
            try:
                proc.stdout.close()
            except OSError:
                pass
            reader_done.wait(timeout=2)

        self._current_proc = None

        with lock:
            final_output = "".join(output_chunks)

        exit_code = proc.returncode if proc.returncode is not None else -1
        if timed_out:
            exit_code = -1

        # Update cwd from temp file (local/powershell only; SSH handled in _exec_shell_ssh_detached)
        cwd_file = self._cwd_file()
        try:
            if os.path.exists(cwd_file):
                with open(cwd_file, encoding="utf-8") as f:
                    new_cwd = f.read().strip()
                if new_cwd and os.path.isdir(new_cwd):
                    self.cwd = new_cwd
                os.remove(cwd_file)
        except OSError:
            pass

        # Save output to log file (replaces tee which caused nohup/pipe hangs)
        if log_file:
            expanded_log = os.path.expandvars(log_file)
            expanded_dir = os.path.dirname(expanded_log)
            try:
                os.makedirs(expanded_dir, exist_ok=True)
                with open(expanded_log, "w", encoding="utf-8", errors="replace") as lf:
                    lf.write(final_output)
            except OSError:
                pass

        # Clean up temp command script
        if use_cmd_file:
            try:
                os.unlink(cmd_file)
            except OSError:
                pass

        return final_output, exit_code

    # ---- File Operations ----

    def _resolve_path(self, path: str) -> str:
        if not os.path.isabs(path):
            return os.path.join(self.cwd, path)
        return path

    def _exec_cat(self, task: dict) -> tuple:
        path = self._resolve_path(task["path"])
        offset = task.get("offset", 0)
        limit = task.get("limit", 2000)

        if self.ssh:
            # Use awk for offset/limit on remote
            end = offset + limit
            r = self._ssh_exec(
                f"awk 'NR>{offset} && NR<={end} {{printf \"%6d\\t%s\\n\", NR, $0}}' {shlex.quote(path)} "
                f"&& echo && wc -l < {shlex.quote(path)}",
                timeout=30,
            )
            if r.returncode != 0:
                return r.stdout.strip() or r.stderr.strip() or f"Failed to read {path}", 1
            lines = r.stdout.rstrip().split("\n")
            # Last line is total count from wc -l
            total_str = lines[-1].strip() if lines else "0"
            content_lines = lines[:-1] if lines else []
            result = "\n".join(content_lines)
            try:
                total = int(total_str)
                if total > end:
                    result += f"\n\n... ({total - end} more lines, {total} total)"
            except ValueError:
                pass
            return result, 0

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return f"File not found: {path}", 1
        except Exception as e:
            return str(e), 1

        total = len(lines)
        selected = lines[offset : offset + limit]
        out = []
        for i, line in enumerate(selected, start=offset + 1):
            out.append(f"{i:>6}\t{line.rstrip()}")
        result = "\n".join(out)
        if total > offset + limit:
            result += f"\n\n... ({total - offset - limit} more lines, {total} total)"
        return result, 0

    def _exec_edit(self, task: dict) -> tuple:
        path = self._resolve_path(task["path"])
        old = task["old"]
        new = task["new"]
        replace_all = task.get("replace_all", False)

        if self.ssh:
            # Use Python one-liner on remote for reliable string replacement
            py_script = (
                "import sys; "
                "content = open(sys.argv[1], encoding='utf-8', errors='replace').read(); "
                "old, new, ra = sys.argv[2], sys.argv[3], sys.argv[4]=='1'; "
                "c = content.count(old); "
                "("
                "  print(f'old_string not found in {sys.argv[1]}') or exit(1)"
                ") if c == 0 else None; "
                "("
                "  print(f'old_string matches {c} times. Use --replace-all.') or exit(1)"
                ") if c > 1 and not ra else None; "
                "n = content.replace(old, new) if ra else content.replace(old, new, 1); "
                "open(sys.argv[1], 'w', encoding='utf-8').write(n); "
                "r = c if ra else 1; "
                "print(f'Replaced {r} occurrence(s) in {sys.argv[1]}')"
            )
            ra_flag = "1" if replace_all else "0"
            r = self._ssh_exec(
                f"python3 -c {shlex.quote(py_script)} {shlex.quote(path)} "
                f"{shlex.quote(old)} {shlex.quote(new)} {ra_flag}",
                timeout=30,
            )
            output = r.stdout.strip() or r.stderr.strip()
            return output, r.returncode

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except FileNotFoundError:
            return f"File not found: {path}", 1
        except Exception as e:
            return str(e), 1

        if old not in content:
            return f"old_string not found in {path}", 1
        count = content.count(old)
        if count > 1 and not replace_all:
            return f"old_string matches {count} times. Use --replace-all.", 1

        if replace_all:
            new_content = content.replace(old, new)
            replaced = count
        else:
            new_content = content.replace(old, new, 1)
            replaced = 1

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_content)
        except Exception as e:
            return f"Write failed: {e}", 1
        return f"Replaced {replaced} occurrence(s) in {path}", 0

    def _exec_glob(self, task: dict) -> tuple:
        pattern = task["pattern"]
        path = task.get("path", self.cwd)
        path = self._resolve_path(path)

        if self.ssh:
            # Use find on remote, sorted by mtime
            r = self._ssh_exec(
                f"find {shlex.quote(path)} -name {shlex.quote(pattern)} -printf '%T@ %p\\n' 2>/dev/null "
                f"| sort -rn | head -500 | cut -d' ' -f2-",
                timeout=30,
            )
            output = r.stdout.strip()
            return (output or "No matches."), r.returncode

        try:
            results = sorted(
                Path(path).glob(pattern),
                key=lambda p: p.stat().st_mtime if p.exists() else 0,
                reverse=True,
            )
        except Exception as e:
            return str(e), 1

        if not results:
            return "No matches.", 0
        lines = [str(p) for p in results[:500]]
        output = "\n".join(lines)
        if len(results) > 500:
            output += f"\n... ({len(results) - 500} more files)"
        return output, 0

    def _exec_grep(self, task: dict) -> tuple:
        pattern = task["pattern"]
        path = task.get("path", self.cwd)
        file_glob = task.get("glob", "*")
        path = self._resolve_path(path)

        if self.ssh:
            r = self._ssh_exec(
                f"grep -rn --include={shlex.quote(file_glob)} "
                f"{shlex.quote(pattern)} {shlex.quote(path)} 2>/dev/null | head -1000",
                timeout=60,
            )
            output = r.stdout.strip()
            return (output or "No matches."), 0

        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"Invalid regex: {e}", 1

        results = []
        try:
            for fp in Path(path).rglob(file_glob):
                if not fp.is_file():
                    continue
                try:
                    with open(fp, "r", encoding="utf-8", errors="replace") as f:
                        for i, line in enumerate(f, 1):
                            if regex.search(line):
                                results.append(f"{fp}:{i}:{line.rstrip()}")
                                if len(results) >= 1000:
                                    break
                except (PermissionError, OSError):
                    continue
                if len(results) >= 1000:
                    break
        except Exception as e:
            return str(e), 1

        if not results:
            return "No matches.", 0
        output = "\n".join(results)
        if len(results) >= 1000:
            output += "\n... (truncated at 1000 matches)"
        return output, 0

    # ---- Transfer Operations ----

    def _exec_transfer_download(self, task: dict) -> tuple:
        """Download a file from a URL (LAN, R2, or Redis base64) and save locally."""
        url = task.get("url", "")
        redis_key = task.get("redis_key", "")
        dest = task.get("dest", "")

        if not dest:
            return "Missing dest path", 1

        # Ensure dest directory exists
        dest_dir = os.path.dirname(dest)
        if dest_dir:
            os.makedirs(dest_dir, exist_ok=True)

        if url:
            # Download from URL (LAN HTTP or presigned R2/S3)
            try:
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=300) as resp:
                    with open(dest, "wb") as f:
                        while True:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            f.write(chunk)
                size = os.path.getsize(dest)
                return f"Downloaded to {dest} ({size} bytes)", 0
            except Exception as e:
                return f"Download failed: {e}", 1

        elif redis_key:
            # Download from Redis (base64 encoded)
            try:
                raw = self.relay._request("GET", redis_key)
                if not raw:
                    return f"Transfer key not found: {redis_key}", 1
                data = base64.b64decode(raw)
                with open(dest, "wb") as f:
                    f.write(data)
                # Clean up the transfer key
                self.relay._request("DEL", redis_key)
                return f"Downloaded to {dest} ({len(data)} bytes)", 0
            except Exception as e:
                return f"Redis download failed: {e}", 1

        return "No url or redis_key provided", 1

    def _exec_clipboard_write(self, task: dict) -> tuple:
        """Write text to system clipboard."""
        text = task.get("text", "")
        if not text:
            return "No text provided", 1

        if sys.platform == "darwin":
            proc = subprocess.run(["pbcopy"], input=text.encode(), capture_output=True)
            return ("Clipboard updated", 0) if proc.returncode == 0 else (proc.stderr.decode(), 1)
        elif sys.platform == "win32":
            proc = subprocess.run(["clip"], input=text.encode(), capture_output=True)
            return ("Clipboard updated", 0) if proc.returncode == 0 else (proc.stderr.decode(), 1)
        elif sys.platform.startswith("linux"):
            # Try xclip, xsel, or wl-copy
            for cmd in [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"], ["wl-copy"]]:
                try:
                    proc = subprocess.run(cmd, input=text.encode(), capture_output=True, timeout=5)
                    if proc.returncode == 0:
                        return "Clipboard updated", 0
                except FileNotFoundError:
                    continue
            # Android: use termux-clipboard-set if available
            try:
                proc = subprocess.run(["termux-clipboard-set"], input=text.encode(), capture_output=True, timeout=5)
                if proc.returncode == 0:
                    return "Clipboard updated", 0
            except FileNotFoundError:
                pass
            return "No clipboard tool found (install xclip, xsel, or wl-clipboard)", 1
        return f"Unsupported platform: {sys.platform}", 1

    # ---- Inbox ----

    def _inbox_dir(self) -> str:
        if self.ssh:
            return "$HOME/.octo/inbox"
        return os.path.join(os.path.expanduser("~"), ".octo", "inbox")

    def _inbox_manifest(self) -> str:
        return os.path.join(self._inbox_dir(), ".manifest.json")

    def _load_manifest(self) -> list:
        path = self._inbox_manifest()
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return []

    def _save_manifest(self, entries: list) -> None:
        path = self._inbox_manifest()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(entries, f, indent=2)

    def _exec_inbox_receive(self, task: dict) -> tuple:
        """Receive a file into the inbox."""
        filename = task.get("filename", "")
        sender = task.get("sender", "unknown")
        note = task.get("note", "")
        url = task.get("url", "")
        redis_key = task.get("redis_key", "")

        if not filename:
            return "Missing filename", 1

        inbox = self._inbox_dir()
        os.makedirs(inbox, exist_ok=True)
        dest = os.path.join(inbox, filename)

        # Avoid overwrite: append timestamp if exists
        if os.path.exists(dest):
            name, ext = os.path.splitext(filename)
            filename = f"{name}_{int(time.time())}{ext}"
            dest = os.path.join(inbox, filename)

        # Download the file
        if url:
            try:
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=300) as resp:
                    with open(dest, "wb") as f:
                        while True:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            f.write(chunk)
            except Exception as e:
                return f"Download failed: {e}", 1
        elif redis_key:
            try:
                raw = self.relay._request("GET", redis_key)
                if not raw:
                    return f"Transfer key not found", 1
                data = base64.b64decode(raw)
                with open(dest, "wb") as f:
                    f.write(data)
                self.relay._request("DEL", redis_key)
            except Exception as e:
                return f"Redis download failed: {e}", 1
        else:
            return "No url or redis_key provided", 1

        size = os.path.getsize(dest)

        # Update manifest
        manifest = self._load_manifest()
        manifest.insert(0, {
            "filename": filename,
            "sender": sender,
            "note": note,
            "size": size,
            "received_at": int(time.time()),
        })
        # Keep max 100 entries, clean up old files (>7 days)
        cutoff = int(time.time()) - 7 * 86400
        cleaned = []
        for entry in manifest[:100]:
            if entry.get("received_at", 0) < cutoff:
                old_path = os.path.join(inbox, entry["filename"])
                try:
                    os.remove(old_path)
                except OSError:
                    pass
            else:
                cleaned.append(entry)
        self._save_manifest(cleaned)

        result = f"Received: {filename} ({size} bytes) from {sender}"
        if note:
            result += f"\nNote: {note}"
        return result, 0

    def _exec_inbox_list(self, task: dict) -> tuple:
        """List inbox contents."""
        manifest = self._load_manifest()
        if not manifest:
            return "Inbox is empty.", 0
        lines = []
        for entry in manifest:
            age = int(time.time()) - entry.get("received_at", 0)
            if age < 60:
                ago = f"{age}s ago"
            elif age < 3600:
                ago = f"{age // 60}m ago"
            elif age < 86400:
                ago = f"{age // 3600}h ago"
            else:
                ago = f"{age // 86400}d ago"
            size_kb = entry.get("size", 0) / 1024
            line = f"{entry['filename']}  {size_kb:.1f}KB  from {entry.get('sender', '?')}  {ago}"
            if entry.get("note"):
                line += f"\n  Note: {entry['note']}"
            lines.append(line)
        return "\n".join(lines), 0

    def _exec_clipboard_read(self, task: dict) -> tuple:
        """Read text from system clipboard."""
        if sys.platform == "darwin":
            proc = subprocess.run(["pbpaste"], capture_output=True, text=True)
            return (proc.stdout, 0) if proc.returncode == 0 else (proc.stderr, 1)
        elif sys.platform == "win32":
            proc = subprocess.run(
                ["powershell", "-command", "Get-Clipboard"],
                capture_output=True, text=True,
            )
            return (proc.stdout.strip(), 0) if proc.returncode == 0 else (proc.stderr, 1)
        elif sys.platform.startswith("linux"):
            for cmd in [["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"], ["wl-paste"]]:
                try:
                    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                    if proc.returncode == 0:
                        return proc.stdout, 0
                except FileNotFoundError:
                    continue
            try:
                proc = subprocess.run(["termux-clipboard-get"], capture_output=True, text=True, timeout=5)
                if proc.returncode == 0:
                    return proc.stdout, 0
            except FileNotFoundError:
                pass
            return "No clipboard tool found", 1
        return f"Unsupported platform: {sys.platform}", 1

    # ---- GPU Metrics ----

    def _exec_metrics(self, task: dict) -> tuple:
        """Collect GPU metrics + training metrics from TensorBoard tfevents."""
        logdir = task.get("logdir", "")
        tail_n = task.get("tail", 20)

        parts = []

        # 1. GPU info via nvidia-smi
        gpu_info = self._collect_gpu_info()
        if gpu_info:
            parts.append(gpu_info)

        # 2. Training metrics from tfevents
        if logdir:
            logdir = self._resolve_path(logdir)
        tb_info = self._collect_tfevents(logdir, tail_n)
        if tb_info:
            parts.append(tb_info)

        if not parts:
            return "No GPU or training metrics found.", 0
        return "\n\n".join(parts), 0

    def _collect_gpu_info(self) -> str:
        """Query nvidia-smi for GPU status."""
        if self.ssh:
            r = self._ssh_exec(
                "nvidia-smi --query-gpu=index,name,temperature.gpu,utilization.gpu,"
                "memory.used,memory.total,power.draw --format=csv,noheader,nounits 2>/dev/null",
                timeout=10,
            )
            raw = r.stdout.strip()
        else:
            try:
                proc = subprocess.run(
                    ["nvidia-smi", "--query-gpu=index,name,temperature.gpu,utilization.gpu,"
                     "memory.used,memory.total,power.draw", "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=10,
                )
                raw = proc.stdout.strip()
            except (FileNotFoundError, subprocess.TimeoutExpired):
                return ""
        if not raw:
            return ""

        lines = ["## GPU Status"]
        for row in raw.strip().split("\n"):
            cols = [c.strip() for c in row.split(",")]
            if len(cols) >= 7:
                idx, name, temp, util, mem_used, mem_total, power = cols[:7]
                mem_pct = int(float(mem_used) / float(mem_total) * 100) if float(mem_total) > 0 else 0
                lines.append(
                    f"GPU {idx}: {name} | {temp}°C | util {util}% | "
                    f"VRAM {mem_used}/{mem_total} MiB ({mem_pct}%) | {power}W"
                )
            else:
                lines.append(row)

        # Running processes
        if self.ssh:
            r2 = self._ssh_exec(
                "nvidia-smi --query-compute-apps=pid,gpu_index,used_memory,process_name "
                "--format=csv,noheader,nounits 2>/dev/null",
                timeout=10,
            )
            procs_raw = r2.stdout.strip()
        else:
            try:
                proc2 = subprocess.run(
                    ["nvidia-smi", "--query-compute-apps=pid,gpu_index,used_memory,process_name",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=10,
                )
                procs_raw = proc2.stdout.strip()
            except (FileNotFoundError, subprocess.TimeoutExpired):
                procs_raw = ""
        if procs_raw:
            lines.append("Processes:")
            for row in procs_raw.strip().split("\n"):
                cols = [c.strip() for c in row.split(",")]
                if len(cols) >= 4:
                    pid, gpu_idx, mem = cols[0], cols[1], cols[2]
                    pname = ",".join(cols[3:]).rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
                    lines.append(f"  GPU {gpu_idx} | PID {pid} | {mem} MiB | {pname}")

        return "\n".join(lines)

    def _collect_tfevents(self, logdir: str, tail_n: int) -> str:
        """Read recent scalars from TensorBoard tfevents files."""
        if not logdir:
            logdir = self._find_tfevents_dir()
        if not logdir:
            return ""

        try:
            return self._read_tfevents_tbparse(logdir, tail_n)
        except Exception:
            pass
        try:
            return self._read_tfevents_manual(logdir, tail_n)
        except Exception as e:
            return f"## Training Metrics\nFound tfevents in {logdir} but failed to parse: {e}"

    def _find_tfevents_dir(self) -> str:
        """Auto-discover directory containing tfevents files."""
        if self.ssh:
            return ""
        search_dirs = []
        for candidate in [self.cwd,
                          os.path.join(self.cwd, "runs"),
                          os.path.join(self.cwd, "logs"),
                          os.path.join(self.cwd, "output"),
                          os.path.join(self.cwd, "outputs"),
                          os.path.join(self.cwd, "tb_logs")]:
            if os.path.isdir(candidate):
                search_dirs.append(candidate)

        best_dir = ""
        best_mtime = 0
        for search_dir in search_dirs:
            for root, dirs, files in os.walk(search_dir):
                for f in files:
                    if "tfevents" in f:
                        fpath = os.path.join(root, f)
                        mtime = os.path.getmtime(fpath)
                        if mtime > best_mtime:
                            best_mtime = mtime
                            best_dir = root
        return best_dir

    def _read_tfevents_tbparse(self, logdir: str, tail_n: int) -> str:
        """Read tfevents using tbparse library."""
        from tbparse import SummaryReader

        reader = SummaryReader(logdir)
        df = reader.scalars
        if df is None or df.empty:
            return ""

        lines = [f"## Training Metrics ({logdir})"]
        tags = df["tag"].unique().tolist()
        latest_step = df["step"].max()
        lines.append(f"Latest step: {latest_step}")
        lines.append("")

        # Summary: latest value per tag
        summary_parts = []
        for tag in tags:
            tag_df = df[df["tag"] == tag].sort_values("step")
            if not tag_df.empty:
                last_row = tag_df.iloc[-1]
                summary_parts.append(f"{tag}: {last_row['value']:.6g}")
        lines.append(" | ".join(summary_parts))
        lines.append("")

        # Recent history for key metrics
        loss_tags = [t for t in tags if "loss" in t.lower()]
        other_tags = [t for t in tags if "loss" not in t.lower()]
        show_tags = (loss_tags + other_tags)[:5]

        for tag in show_tags:
            tag_df = df[df["tag"] == tag].sort_values("step").tail(tail_n)
            lines.append(f"### {tag}")
            for _, row in tag_df.iterrows():
                lines.append(f"  step {int(row['step']):>8d} | {row['value']:.6g}")

        return "\n".join(lines)

    def _read_tfevents_manual(self, logdir: str, tail_n: int) -> str:
        """Read tfevents using tensorboard EventAccumulator."""
        from tensorboard.backend.event_processing.event_accumulator import EventAccumulator

        ea = EventAccumulator(logdir)
        ea.Reload()

        scalar_tags = ea.Tags().get("scalars", [])
        if not scalar_tags:
            return ""

        lines = [f"## Training Metrics ({logdir})"]
        summary_parts = []
        for tag in scalar_tags:
            events = ea.Scalars(tag)
            if events:
                summary_parts.append(f"{tag}: {events[-1].value:.6g}")
        lines.append(" | ".join(summary_parts))
        lines.append("")

        loss_tags = [t for t in scalar_tags if "loss" in t.lower()]
        other_tags = [t for t in scalar_tags if "loss" not in t.lower()]
        show_tags = (loss_tags + other_tags)[:5]

        for tag in show_tags:
            events = ea.Scalars(tag)[-tail_n:]
            lines.append(f"### {tag}")
            for e in events:
                lines.append(f"  step {int(e.step):>8d} | {e.value:.6g}")

        return "\n".join(lines)
