"""MCP (Model Context Protocol) Server for openOcto.

Exposes remote terminal operations as structured tools over JSON-RPC stdio.
Zero dependencies — implements the MCP protocol directly.

Usage:
    octo mcp-server

Configure in .claude/settings.json or .mcp.json:
    {"mcpServers": {"openocto": {"command": "octo", "args": ["mcp-server"]}}}
"""

import base64
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request

from .config import get_relay_config, load_config
from .relay import Relay, RelayError
from .storage import get_storage_client, StorageError
from .daemon import REDIS_TRANSFER_MAX, LAN_PORT

PROTOCOL_VERSION = "2025-03-26"
SERVER_NAME = "openocto"
SERVER_VERSION = "0.1.3"

POLL_INTERVAL = 1  # seconds between polls when waiting for task result
TASK_WAIT_TIMEOUT = 3600  # max seconds to wait for a task result
QUICK_TASK_TIMEOUT = 60  # max seconds for simple ops (cat/glob/grep/edit/clip/inbox/metrics)
PENDING_TIMEOUT = 200  # max seconds to wait for daemon to pick up a task (PENDING phase)
                       # covers worst-case COOL_POLL_MAX (180s) + network margin

_write_lock = threading.Lock()


# ---- Tool Definitions ----

TOOLS = [
    {
        "name": "remote_ls",
        "description": "List all registered remote terminals and their status (online/offline, mode, tags).",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "remote_run",
        "description": "Execute a shell command on a remote terminal. Set nowait to submit and return a task ID immediately. Working directory persists between calls.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "terminal": {
                    "type": "string",
                    "description": "Target terminal name (from remote_ls)",
                },
                "command": {
                    "type": "string",
                    "description": "Shell command to execute",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 3600)",
                },
                "nowait": {
                    "type": "boolean",
                    "description": "Submit and return task ID without waiting (default: false)",
                },
                "no_log": {
                    "type": "boolean",
                    "description": "Don't save output to log file on remote (default: false)",
                },
                "notify": {
                    "type": "string",
                    "description": "Terminal name to notify when command completes (e.g. phone name)",
                },
                "notify_message": {
                    "type": "string",
                    "description": "Custom notification message (default: auto-generated)",
                },
            },
            "required": ["terminal", "command"],
        },
    },
    {
        "name": "remote_read",
        "description": "Read a file on a remote terminal. Returns contents with line numbers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "terminal": {
                    "type": "string",
                    "description": "Target terminal name",
                },
                "path": {
                    "type": "string",
                    "description": "Absolute or relative file path on remote",
                },
                "offset": {
                    "type": "integer",
                    "description": "Start from line N (0-based, default: 0)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max lines to read (default: 2000)",
                },
            },
            "required": ["terminal", "path"],
        },
    },
    {
        "name": "remote_edit",
        "description": "Edit a file on a remote terminal by replacing a string. Fails if old_string is not found or matches multiple times (unless replace_all is true).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "terminal": {
                    "type": "string",
                    "description": "Target terminal name",
                },
                "path": {
                    "type": "string",
                    "description": "File path on remote",
                },
                "old_string": {
                    "type": "string",
                    "description": "Exact string to find",
                },
                "new_string": {
                    "type": "string",
                    "description": "Replacement string",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences (default: false)",
                },
            },
            "required": ["terminal", "path", "old_string", "new_string"],
        },
    },
    {
        "name": "remote_glob",
        "description": "Search for files by glob pattern on a remote terminal. Returns matching file paths sorted by modification time.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "terminal": {
                    "type": "string",
                    "description": "Target terminal name",
                },
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern (e.g. **/*.py)",
                },
                "path": {
                    "type": "string",
                    "description": "Base directory (default: terminal cwd)",
                },
            },
            "required": ["terminal", "pattern"],
        },
    },
    {
        "name": "remote_grep",
        "description": "Search file contents by regex on a remote terminal. Returns file:line:content matches.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "terminal": {
                    "type": "string",
                    "description": "Target terminal name",
                },
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search for",
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search (default: terminal cwd)",
                },
                "glob": {
                    "type": "string",
                    "description": "File glob filter (e.g. *.py)",
                },
            },
            "required": ["terminal", "pattern"],
        },
    },
    {
        "name": "remote_logs",
        "description": "View the output of the current or last task on a remote terminal. If a task is running, returns its current output. If no task is active, reads the last log file from the remote.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "terminal": {
                    "type": "string",
                    "description": "Target terminal name",
                },
                "tail": {
                    "type": "integer",
                    "description": "Show last N lines of log file (default: all)",
                },
                "task_id": {
                    "type": "string",
                    "description": "Read one exact task ID instead of the current task",
                },
            },
            "required": ["terminal"],
        },
    },
    {
        "name": "remote_kill",
        "description": "Kill the currently running command on a remote terminal.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "terminal": {
                    "type": "string",
                    "description": "Target terminal name",
                },
                "task_id": {
                    "type": "string",
                    "description": "Kill one exact task ID instead of the current task",
                },
            },
            "required": ["terminal"],
        },
    },
    {
        "name": "remote_wake",
        "description": "Switch a terminal to fast polling mode. Use before sending tasks to a cooled terminal.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "terminal": {
                    "type": "string",
                    "description": "Target terminal name",
                },
            },
            "required": ["terminal"],
        },
    },
    {
        "name": "remote_cool",
        "description": "Switch a terminal to low-power polling mode (every 60s). Use when done with a terminal to save resources.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "terminal": {
                    "type": "string",
                    "description": "Target terminal name",
                },
            },
            "required": ["terminal"],
        },
    },
    {
        "name": "remote_send",
        "description": "Transfer a file between terminals. Automatically selects the best route: LAN direct (same WiFi), Redis relay (small files), or cloud storage (large files). Use source='local' to send from the machine running the MCP server.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "Source terminal name, or 'local' for the MCP server machine",
                },
                "target": {
                    "type": "string",
                    "description": "Target terminal name",
                },
                "file": {
                    "type": "string",
                    "description": "File path on the source",
                },
                "dest": {
                    "type": "string",
                    "description": "Destination path on target (default: same filename in home dir)",
                },
            },
            "required": ["source", "target", "file"],
        },
    },
    {
        "name": "remote_clip",
        "description": "Read or write the clipboard of a remote terminal. To write: provide text. To read: omit text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "terminal": {
                    "type": "string",
                    "description": "Target terminal name",
                },
                "text": {
                    "type": "string",
                    "description": "Text to write to clipboard. Omit to read clipboard.",
                },
            },
            "required": ["terminal"],
        },
    },
    {
        "name": "remote_inbox",
        "description": "List the inbox of a remote terminal. The inbox receives files sent from other devices (e.g. phone). Files are stored in ~/.octo/inbox/ on the target.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "terminal": {
                    "type": "string",
                    "description": "Target terminal name",
                },
            },
            "required": ["terminal"],
        },
    },
    {
        "name": "remote_metrics",
        "description": "Get GPU status and training metrics from a remote terminal. Shows GPU utilization, VRAM, temperature, and TensorBoard training loss/lr curves. Optionally specify logdir for TensorBoard logs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "terminal": {
                    "type": "string",
                    "description": "Target terminal name",
                },
                "logdir": {
                    "type": "string",
                    "description": "Path to TensorBoard log directory (auto-detected if omitted)",
                },
                "tail": {
                    "type": "integer",
                    "description": "Number of recent data points to show (default: 20)",
                },
            },
            "required": ["terminal"],
        },
    },
]


# ---- Tool Handlers ----

def _get_relay() -> Relay:
    rc = get_relay_config()
    return Relay(rc["redis_url"], rc["redis_token"], rc["workspace"],
                 proxy_url=rc.get("proxy_url", ""))


def _wait_for_task(relay: Relay, target: str, task_id: str = None,
                   timeout: int = None, progress_token=None) -> dict:
    """Poll until task completes or timeout. Returns the final task dict.

    Timeout only counts from when the daemon picks up the task (RUNNING),
    not while it's waiting in the queue (PENDING).  PENDING has its own
    separate ceiling (PENDING_TIMEOUT) so we don't wait forever if the
    daemon is offline.

    progress_token: MCP _meta.progressToken from the client request.  When
    set, incremental stdout is forwarded as notifications/progress messages
    so the MCP client (e.g. Claude Code) can display live output.
    """
    _wake_daemon(relay, target)
    timeout = timeout or TASK_WAIT_TIMEOUT
    submit_time = time.time()
    running_since = None  # set when status first becomes RUNNING
    missing_count = 0
    last_output_len = 0  # bytes already sent as progress
    progress_counter = 0  # monotonically increasing progress value
    while True:
        now = time.time()

        # Timeout logic: PENDING phase uses PENDING_TIMEOUT,
        # RUNNING phase uses the caller-supplied timeout.
        if running_since is not None:
            if now - running_since > timeout:
                return {"output": f"[octo] Timed out waiting for result after {timeout}s.", "exit_code": -1}
        else:
            if now - submit_time > PENDING_TIMEOUT:
                return {
                    "output": f"[octo] Daemon did not pick up task within {PENDING_TIMEOUT}s. "
                              f"Is '{target}' online?",
                    "exit_code": -1,
                }

        # Always prefer per-task key (new path) over legacy single-task key.
        # The daemon writes completion to the per-task key, so that's the
        # authoritative source.
        task = None
        if task_id:
            task = relay.poll_task(target, task_id=task_id)
        if not task:
            # Fallback to legacy key
            task = relay.poll_task(target)
            if task and task_id and task.get("id") != task_id:
                # Legacy key has a different task — ignore it, keep waiting
                # for our per-task key to appear
                _log(f"[poll] per-task=None, legacy id={task.get('id')} != {task_id}, legacy status={task.get('status')}")
                time.sleep(POLL_INTERVAL)
                continue
        if not task:
            missing_count += 1
            _log(f"[poll] both keys returned None, missing_count={missing_count}")
            # Tolerate transient misses (network blip, relay restart, etc.)
            # The key was just SET by submit_task, so it should exist.
            if missing_count >= 5:
                return {"output": "Task disappeared.", "exit_code": 1}
            time.sleep(POLL_INTERVAL)
            continue
        missing_count = 0
        _log(f"[poll] task status={task.get('status')}, id={task.get('id')}, from={'per-task' if task_id and task.get('id') == task_id else 'legacy'}")

        # Stream incremental output via MCP notifications/progress
        if progress_token is not None and task.get("status") == "RUNNING":
            current_output = task.get("output", "") or ""
            if len(current_output) > last_output_len:
                new_text = current_output[last_output_len:]
                last_output_len = len(current_output)
                progress_counter += 1
                _send({
                    "jsonrpc": "2.0",
                    "method": "notifications/progress",
                    "params": {
                        "progressToken": progress_token,
                        "progress": progress_counter,
                        "message": new_text,
                    },
                })

        if task["status"] in ("DONE", "FAILED"):
            # Flush any remaining output not yet sent
            if progress_token is not None:
                final_output = task.get("output", "") or ""
                if len(final_output) > last_output_len:
                    progress_counter += 1
                    _send({
                        "jsonrpc": "2.0",
                        "method": "notifications/progress",
                        "params": {
                            "progressToken": progress_token,
                            "progress": progress_counter,
                            "message": final_output[last_output_len:],
                        },
                    })
            return task
        if task["status"] == "RUNNING" and running_since is None:
            running_since = now
        time.sleep(POLL_INTERVAL)


def _clear_task(relay: Relay, target: str, task_id: str = None):
    """Clean up task keys. Only delete legacy key if it matches our task_id,
    to avoid clobbering a newer task submitted by another handler thread."""
    if task_id:
        relay.clear_task(target, task_id=task_id)
        # Only clear legacy key if it still belongs to this task
        legacy = relay.poll_task(target)
        if legacy and legacy.get("id") == task_id:
            relay.clear_task(target)
    else:
        relay.clear_task(target)


def _wake_daemon(relay: Relay, target: str):
    """Poke the daemon's LAN /wake endpoint to interrupt its poll sleep.
    Best-effort — falls back to normal polling if unreachable."""
    try:
        meta = _get_terminal_meta(relay, target)
        ip = meta.get("lan_ip", "")
        port = meta.get("lan_port", LAN_PORT)
        if not ip:
            return
        url = f"http://{ip}:{port}/wake"
        req = urllib.request.Request(url)
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass


def handle_remote_ls(args: dict) -> str:
    relay = _get_relay()
    terminals = relay.list_terminals()
    if not terminals:
        return "No terminals registered."
    lines = []
    for t in terminals:
        status = "online" if t["online"] else "offline"
        mode = relay.get_mode(t["name"])
        tags = ", ".join(t.get("tags", []))
        ago = t["last_seen_ago"]
        lines.append(f"{t['name']} ({status}, {mode}) [{tags}] last seen {ago}s ago")
    return "\n".join(lines)


def handle_remote_run(args: dict, progress_token=None) -> tuple:
    relay = _get_relay()
    terminal = args["terminal"]
    command = args["command"]
    extra = {}
    if "timeout" in args:
        extra["timeout"] = args["timeout"]
    if args.get("no_log"):
        extra["no_log"] = True
    if args.get("notify"):
        extra["notify"] = args["notify"]
    if args.get("notify_message"):
        extra["notify_message"] = args["notify_message"]
    task_id = relay.submit_task(terminal, task_type="shell", command=command, **extra)
    if args.get("nowait"):
        return json.dumps(
            {
                "status": "SUBMITTED",
                "terminal": terminal,
                "taskId": task_id,
            },
            sort_keys=True,
        ), False
    # Wait timeout = command timeout + buffer for daemon pickup & network latency.
    # Without this, short command timeouts (e.g. 10s) expire before the daemon
    # even picks up the task from the queue.
    cmd_timeout = args.get("timeout", TASK_WAIT_TIMEOUT)
    wait_timeout = cmd_timeout + 30
    task = _wait_for_task(relay, terminal, task_id=task_id, timeout=wait_timeout,
                          progress_token=progress_token)
    output = task.get("output", "")
    exit_code = task.get("exit_code", 0) or 0
    timed_out = exit_code == -1 and "[octo] Timed out waiting" in output
    if timed_out:
        # Don't clear task — it's still running on remote.
        # User can remote_kill or remote_logs to manage.
        return (
            f"{output}\n"
            f"Task is still running on '{terminal}'. "
            f"Use remote_kill to stop it, or remote_logs to check progress."
        ), True
    _clear_task(relay, terminal, task_id)
    is_error = exit_code != 0
    if is_error and output:
        output = f"{output}\n[exit code: {exit_code}]"
    elif is_error:
        output = f"[exit code: {exit_code}]"
    return output, is_error


def handle_remote_read(args: dict) -> tuple:
    relay = _get_relay()
    terminal = args["terminal"]
    kwargs = {"path": args["path"]}
    if "offset" in args:
        kwargs["offset"] = args["offset"]
    if "limit" in args:
        kwargs["limit"] = args["limit"]
    task_id = relay.submit_task(terminal, task_type="cat", **kwargs)
    task = _wait_for_task(relay, terminal, task_id=task_id, timeout=QUICK_TASK_TIMEOUT)
    _clear_task(relay, terminal, task_id)
    output = task.get("output", "")
    is_error = (task.get("exit_code", 0) or 0) != 0
    return output, is_error


def handle_remote_edit(args: dict) -> tuple:
    relay = _get_relay()
    terminal = args["terminal"]
    old_str = args["old_string"]
    new_str = args["new_string"]
    task_id = relay.submit_task(
        terminal,
        task_type="edit",
        path=args["path"],
        old=old_str,
        new=new_str,
        replace_all=args.get("replace_all", False),
    )
    task = _wait_for_task(relay, terminal, task_id=task_id, timeout=QUICK_TASK_TIMEOUT)
    _clear_task(relay, terminal, task_id)
    output = task.get("output", "")
    is_error = (task.get("exit_code", 0) or 0) != 0

    # Append a unified diff so Claude Code renders red/green highlighting
    if not is_error and old_str and new_str:
        import difflib
        file_path = args["path"]
        old_lines = old_str.splitlines(keepends=True)
        new_lines = new_str.splitlines(keepends=True)
        diff = difflib.unified_diff(old_lines, new_lines,
                                     fromfile=f"a/{file_path}",
                                     tofile=f"b/{file_path}")
        diff_str = "".join(diff)
        if diff_str:
            output = f"{output}\n\n```diff\n{diff_str}```"

    return output, is_error


def handle_remote_glob(args: dict) -> tuple:
    relay = _get_relay()
    terminal = args["terminal"]
    kwargs = {"pattern": args["pattern"]}
    if "path" in args:
        kwargs["path"] = args["path"]
    task_id = relay.submit_task(terminal, task_type="glob", **kwargs)
    task = _wait_for_task(relay, terminal, task_id=task_id, timeout=QUICK_TASK_TIMEOUT)
    _clear_task(relay, terminal, task_id)
    output = task.get("output", "")
    is_error = (task.get("exit_code", 0) or 0) != 0
    return output, is_error


def handle_remote_grep(args: dict) -> tuple:
    relay = _get_relay()
    terminal = args["terminal"]
    kwargs = {"pattern": args["pattern"]}
    if "path" in args:
        kwargs["path"] = args["path"]
    if "glob" in args:
        kwargs["glob"] = args["glob"]
    task_id = relay.submit_task(terminal, task_type="grep", **kwargs)
    task = _wait_for_task(relay, terminal, task_id=task_id, timeout=QUICK_TASK_TIMEOUT)
    _clear_task(relay, terminal, task_id)
    output = task.get("output", "")
    is_error = (task.get("exit_code", 0) or 0) != 0
    return output, is_error


def handle_remote_logs(args: dict) -> tuple:
    relay = _get_relay()
    terminal = args["terminal"]

    # Check if there's a current task in Redis
    task_id = args.get("task_id")
    task = relay.poll_task(terminal, task_id=task_id)
    if task:
        output = task.get("output", "")
        status = task.get("status", "UNKNOWN")
        exit_code = task.get("exit_code")
        header = f"[Task {status}]"
        if exit_code is not None:
            header += f" (exit code: {exit_code})"
        return f"{header}\n{output}", False
    if task_id:
        return f"Task not found: {task_id}", True

    # No task in Redis — read last log file from remote
    tail = args.get("tail")
    log_path = f"$HOME/.octo/logs/{terminal}_latest.log"
    if tail:
        cmd = f"tail -n {tail} {log_path}"
    else:
        cmd = f"cat {log_path}"
    task_id = relay.submit_task(terminal, task_type="shell", command=cmd, no_log=True)
    task = _wait_for_task(relay, terminal, task_id=task_id)
    _clear_task(relay, terminal, task_id)
    output = task.get("output", "")
    is_error = (task.get("exit_code", 0) or 0) != 0
    return output, is_error


def handle_remote_kill(args: dict) -> tuple:
    relay = _get_relay()
    terminal = args["terminal"]
    requested_id = args.get("task_id")
    task = relay.poll_task(terminal, task_id=requested_id)
    if not task or task["status"] not in ("PENDING", "RUNNING"):
        suffix = f" with ID {requested_id}" if requested_id else ""
        return f"No active task on '{terminal}'{suffix}.", False
    task_id = requested_id or task.get("id")
    if task_id:
        relay.update_task(terminal, {"status": "KILL"}, task_id=task_id)
    else:
        relay.update_task(terminal, {"status": "KILL"})
    return f"Kill signal sent to '{terminal}' ({task_id or 'current'}).", False


def handle_remote_wake(args: dict) -> tuple:
    relay = _get_relay()
    terminal = args["terminal"]
    relay.set_mode(terminal, "wake")
    return f"'{terminal}' set to WAKE mode.", False


def handle_remote_cool(args: dict) -> tuple:
    relay = _get_relay()
    terminal = args["terminal"]
    relay.set_mode(terminal, "cool")
    return f"'{terminal}' set to COOL mode.", False


def _get_terminal_meta(relay: Relay, name: str) -> dict:
    terminals = relay.list_terminals()
    for t in terminals:
        if t["name"] == name:
            return t.get("meta", {})
    return {}


def _lan_reachable(ip: str, port: int, timeout: float = 1.0) -> bool:
    if not ip:
        return False
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((ip, port))
        s.close()
        return True
    except (OSError, socket.error):
        return False


def handle_remote_send(args: dict) -> tuple:
    relay = _get_relay()
    source = args["source"]
    target = args["target"]
    file_path = args["file"]
    dest = args.get("dest") or os.path.basename(file_path)
    source_is_local = (source == "local")

    target_meta = _get_terminal_meta(relay, target)
    target_lan_ip = target_meta.get("lan_ip", "")
    target_lan_port = target_meta.get("lan_port", LAN_PORT)

    # Determine file size
    if source_is_local:
        if not os.path.isfile(file_path):
            return f"File not found: {file_path}", True
        file_size = os.path.getsize(file_path)
    else:
        source_meta = _get_terminal_meta(relay, source)
        source_is_windows = source_meta.get("platform", "").startswith("win")
        if source_is_windows:
            size_cmd = f"(Get-Item '{file_path}').Length"
        else:
            size_cmd = f"stat -c%s {file_path} 2>/dev/null || stat -f%z {file_path} 2>/dev/null"
        tid = relay.submit_task(source, task_type="shell",
                          command=size_cmd,
                          no_log=True)
        task = _wait_for_task(relay, source, task_id=tid)
        _clear_task(relay, source, tid)
        try:
            file_size = int(task.get("output", "0").strip())
        except ValueError:
            file_size = 0

    route_used = ""

    # Route 1: LAN direct
    if target_lan_ip and _lan_reachable(target_lan_ip, target_lan_port):
        if source_is_local:
            try:
                with open(file_path, "rb") as f:
                    data = f.read()
                dest_encoded = urllib.parse.quote(dest, safe="")
                url = f"http://{target_lan_ip}:{target_lan_port}/receive?dest={dest_encoded}"
                req = urllib.request.Request(url, data=data, method="POST")
                req.add_header("Content-Type", "application/octet-stream")
                req.add_header("Content-Length", str(len(data)))
                with urllib.request.urlopen(req, timeout=300) as resp:
                    resp.read()
                return f"Sent {file_path} → {target}:{dest} ({file_size} bytes, LAN direct)", False
            except Exception:
                pass  # Fall through to other routes
        else:
            # Remote-to-remote LAN: only attempt if source and target
            # are on the same subnet (MCP can reach source, but that
            # doesn't mean target can).
            source_meta = _get_terminal_meta(relay, source)
            source_lan_ip = source_meta.get("lan_ip", "")
            source_lan_port = source_meta.get("lan_port", LAN_PORT)
            target_subnet = ".".join(target_lan_ip.split(".")[:3]) if target_lan_ip else ""
            source_subnet = ".".join(source_lan_ip.split(".")[:3]) if source_lan_ip else ""
            if (source_lan_ip and source_subnet == target_subnet
                    and _lan_reachable(source_lan_ip, source_lan_port)):
                path_encoded = urllib.parse.quote(file_path, safe="")
                lan_url = f"http://{source_lan_ip}:{source_lan_port}/file?path={path_encoded}"
                tid = relay.submit_task(target, task_type="transfer_download",
                                  url=lan_url, dest=dest, timeout=15)
                task = _wait_for_task(relay, target, task_id=tid, timeout=30)
                _clear_task(relay, target, tid)
                output = task.get("output", "")
                is_error = (task.get("exit_code", 0) or 0) != 0
                if not is_error:
                    return f"{output} (LAN direct)", False
                # LAN failed, fall through to Redis relay or cloud storage

    # Route 2: Redis relay (small files)
    if file_size <= REDIS_TRANSFER_MAX:
        transfer_key = f"octo:transfer:{int(time.time())}:{id(file_path) % 10000}"
        if source_is_local:
            with open(file_path, "rb") as f:
                data = f.read()
            b64 = base64.b64encode(data).decode()
            relay._request("SET", transfer_key, b64)
            relay._request("EXPIRE", transfer_key, "600")
        else:
            # Use markers to extract base64 cleanly from shell output,
            # which can be polluted by stderr/warnings/MOTD.
            py_cmd = "python" if source_is_windows else "python3"
            escaped_path = file_path.replace("'", "'\\''")
            suppress_stderr = "" if source_is_windows else " 2>/dev/null"
            tid = relay.submit_task(source, task_type="shell",
                              command=(
                                  f"{py_cmd} -c \""
                                  f"import base64,sys; "
                                  f"data=open('{escaped_path}','rb').read(); "
                                  f"sys.stdout.write('__B64_START__'); "
                                  f"sys.stdout.write(base64.b64encode(data).decode()); "
                                  f"sys.stdout.write('__B64_END__'); "
                                  f"sys.stdout.flush()"
                                  f"\"{suppress_stderr}"
                              ),
                              no_log=True)
            task = _wait_for_task(relay, source, task_id=tid)
            _clear_task(relay, source, tid)
            raw_output = task.get("output", "")
            # Extract base64 between markers
            start_marker = "__B64_START__"
            end_marker = "__B64_END__"
            start_idx = raw_output.find(start_marker)
            end_idx = raw_output.find(end_marker)
            if start_idx == -1 or end_idx == -1:
                return f"Failed to read file from source: {raw_output[:200]}", True
            b64 = raw_output[start_idx + len(start_marker):end_idx]
            if not b64:
                return "Failed to read file from source (empty)", True
            relay._request("SET", transfer_key, b64)
            relay._request("EXPIRE", transfer_key, "600")

        tid = relay.submit_task(target, task_type="transfer_download",
                          redis_key=transfer_key, dest=dest)
        task = _wait_for_task(relay, target, task_id=tid)
        _clear_task(relay, target, tid)
        output = task.get("output", "")
        is_error = (task.get("exit_code", 0) or 0) != 0
        return f"{output} (Redis relay)", is_error

    # Route 3: Cloud storage
    config = load_config()
    s3 = get_storage_client(config)
    if not s3:
        return (f"File too large for Redis relay ({file_size} bytes). "
                f"Configure cloud storage with 'octo config --storage', "
                f"or use LAN direct transfer (connect to same WiFi)."), True

    obj_key = f"transfer/{int(time.time())}_{os.path.basename(file_path)}"
    try:
        if source_is_local:
            s3.upload_file(obj_key, file_path)
        else:
            py_cmd = "python" if source_is_windows else "python3"
            escaped_path = file_path.replace("'", "'\\''")
            suppress_stderr = "" if source_is_windows else " 2>/dev/null"
            tid = relay.submit_task(source, task_type="shell",
                              command=(
                                  f"{py_cmd} -c \""
                                  f"import base64,sys; "
                                  f"data=open('{escaped_path}','rb').read(); "
                                  f"sys.stdout.write('__B64_START__'); "
                                  f"sys.stdout.write(base64.b64encode(data).decode()); "
                                  f"sys.stdout.write('__B64_END__'); "
                                  f"sys.stdout.flush()"
                                  f"\"{suppress_stderr}"
                              ),
                              no_log=True)
            task = _wait_for_task(relay, source, task_id=tid)
            _clear_task(relay, source, tid)
            raw_output = task.get("output", "")
            start_idx = raw_output.find("__B64_START__")
            end_idx = raw_output.find("__B64_END__")
            if start_idx == -1 or end_idx == -1:
                return f"Cloud transfer failed: could not read file from source: {raw_output[:200]}", True
            b64_str = raw_output[start_idx + len("__B64_START__"):end_idx]
            data = base64.b64decode(b64_str)
            s3.upload(obj_key, data)

        download_url = s3.presign_download(obj_key, expires=600)
        tid = relay.submit_task(target, task_type="transfer_download",
                          url=download_url, dest=dest)
        task = _wait_for_task(relay, target, task_id=tid)
        _clear_task(relay, target, tid)
        output = task.get("output", "")
        is_error = (task.get("exit_code", 0) or 0) != 0

        try:
            s3.delete(obj_key)
        except StorageError:
            pass

        return f"{output} (cloud storage)", is_error
    except (StorageError, Exception) as e:
        return f"Cloud transfer failed: {e}", True


def handle_remote_clip(args: dict) -> tuple:
    relay = _get_relay()
    terminal = args["terminal"]
    text = args.get("text")

    if text:
        task_id = relay.submit_task(terminal, task_type="clipboard_write", text=text)
    else:
        task_id = relay.submit_task(terminal, task_type="clipboard_read")

    task = _wait_for_task(relay, terminal, task_id=task_id, timeout=QUICK_TASK_TIMEOUT)
    _clear_task(relay, terminal, task_id)
    output = task.get("output", "")
    is_error = (task.get("exit_code", 0) or 0) != 0
    return output, is_error


def handle_remote_inbox(args: dict) -> tuple:
    relay = _get_relay()
    terminal = args["terminal"]
    task_id = relay.submit_task(terminal, task_type="inbox_list")
    task = _wait_for_task(relay, terminal, task_id=task_id, timeout=QUICK_TASK_TIMEOUT)
    _clear_task(relay, terminal, task_id)
    output = task.get("output", "")
    is_error = (task.get("exit_code", 0) or 0) != 0
    return output, is_error


def handle_remote_metrics(args: dict) -> tuple:
    relay = _get_relay()
    terminal = args["terminal"]
    extra = {}
    if "logdir" in args:
        extra["logdir"] = args["logdir"]
    if "tail" in args:
        extra["tail"] = args["tail"]
    task_id = relay.submit_task(terminal, task_type="metrics", **extra)
    task = _wait_for_task(relay, terminal, task_id=task_id, timeout=QUICK_TASK_TIMEOUT)
    _clear_task(relay, terminal, task_id)
    output = task.get("output", "")
    is_error = (task.get("exit_code", 0) or 0) != 0
    return output, is_error


HANDLERS = {
    "remote_ls": handle_remote_ls,
    "remote_run": handle_remote_run,
    "remote_read": handle_remote_read,
    "remote_edit": handle_remote_edit,
    "remote_glob": handle_remote_glob,
    "remote_grep": handle_remote_grep,
    "remote_logs": handle_remote_logs,
    "remote_kill": handle_remote_kill,
    "remote_wake": handle_remote_wake,
    "remote_cool": handle_remote_cool,
    "remote_send": handle_remote_send,
    "remote_clip": handle_remote_clip,
    "remote_inbox": handle_remote_inbox,
    "remote_metrics": handle_remote_metrics,
}


# ---- JSON-RPC / MCP Protocol ----

_LOG_FILE = os.path.expanduser("~/.octo/mcp_debug.log")  # TODO: gate with OCTO_MCP_DEBUG after bug fix

def _log(msg: str):
    """Log to stderr (stdout is reserved for MCP protocol)."""
    print(f"[octo-mcp] {msg}", file=sys.stderr, flush=True)
    if _LOG_FILE:
        try:
            with open(_LOG_FILE, "a") as f:
                f.write(f"{msg}\n")
        except OSError:
            pass


def _send(msg: dict):
    """Send a JSON-RPC message to stdout (thread-safe)."""
    with _write_lock:
        sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def _response(req_id, result: dict):
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id, code: int, message: str):
    _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def _feed_summary(tool_name, arguments, text, is_error, duration):
    """Create a concise summary for the activity feed."""
    s = {}
    s["tool"] = tool_name
    s["duration"] = round(duration, 1)
    s["ok"] = not is_error
    if "terminal" in arguments:
        s["terminal"] = arguments["terminal"]
    if tool_name == "remote_run":
        cmd = arguments.get("command", "")
        s["command"] = cmd[:80] + ("..." if len(cmd) > 80 else "")
    elif tool_name == "remote_read":
        s["path"] = arguments.get("path", "")
    elif tool_name == "remote_edit":
        s["path"] = arguments.get("path", "")
    elif tool_name == "remote_glob":
        s["pattern"] = arguments.get("pattern", "")
    elif tool_name == "remote_grep":
        s["pattern"] = arguments.get("pattern", "")
    elif tool_name == "remote_send":
        s["source"] = arguments.get("source", "")
        s["file"] = arguments.get("file", "")
    elif tool_name == "remote_clip":
        s["action"] = "write" if "text" in arguments else "read"
    elif tool_name == "remote_inbox":
        pass
    elif tool_name == "remote_ls":
        # Count terminals from output
        lines = text.strip().split("\n") if text.strip() else []
        s["count"] = len(lines)
    return s


def _handle_tool_call(req_id, handler, arguments, tool_name="", progress_token=None):
    """Execute a tool handler and send the response (runs in a thread)."""
    start = time.time()
    try:
        # Pass progress_token to handlers that accept it (remote_run)
        if progress_token is not None and tool_name == "remote_run":
            result = handler(arguments, progress_token=progress_token)
        else:
            result = handler(arguments)
        if isinstance(result, tuple):
            text, is_error = result
        else:
            text, is_error = result, False

        duration = time.time() - start

        # Log to activity feed
        try:
            relay = _get_relay()
            entry = _feed_summary(tool_name, arguments, text, is_error, duration)
            relay.push_feed(entry)
        except Exception:
            pass  # Feed logging is best-effort

        _response(req_id, {
            "content": [{"type": "text", "text": text}],
            "isError": is_error,
        })
    except RelayError as e:
        _response(req_id, {
            "content": [{"type": "text", "text": f"Relay error: {e}"}],
            "isError": True,
        })
    except Exception as e:
        _response(req_id, {
            "content": [{"type": "text", "text": f"Internal error: {e}"}],
            "isError": True,
        })


def handle_message(msg: dict):
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params", {})

    # Notifications (no id) — just acknowledge
    if req_id is None:
        if method == "notifications/initialized":
            _log("Client initialized.")
        return

    if method == "initialize":
        _response(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {
                "tools": {"listChanged": False},
                "notifications": {"progress": True},
            },
            "serverInfo": {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
            },
        })
        _log("Initialized.")

    elif method == "tools/list":
        _response(req_id, {"tools": TOOLS})

    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        progress_token = params.get("_meta", {}).get("progressToken")

        handler = HANDLERS.get(tool_name)
        if not handler:
            _response(req_id, {
                "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                "isError": True,
            })
            return

        # Run tool call in a thread so blocking handlers don't block others
        thread = threading.Thread(
            target=_handle_tool_call,
            args=(req_id, handler, arguments, tool_name, progress_token),
            daemon=True,
        )
        thread.start()

    elif method == "ping":
        _response(req_id, {})

    else:
        _error(req_id, -32601, f"Method not found: {method}")


def run_server():
    """Main MCP server loop — reads JSON-RPC from stdin, writes to stdout."""
    _log("Starting MCP server...")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            _log(f"Invalid JSON: {e}")
            continue

        try:
            handle_message(msg)
        except Exception as e:
            _log(f"Unhandled error: {e}")
            req_id = msg.get("id")
            if req_id is not None:
                _error(req_id, -32603, f"Internal error: {e}")

    _log("Stdin closed. Shutting down.")
