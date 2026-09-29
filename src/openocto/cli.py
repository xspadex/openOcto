"""CLI entry point for openOcto.

Usage:
    octo init                                 Configure relay connection
    octo token                                Generate a join token
    octo join --name NAME [--tags T]          Register as worker and start daemon
    octo ls                                   List all terminals
    octo run TARGET "COMMAND"                 Execute command on a remote terminal
    octo cat TARGET /path/to/file             Read a remote file
    octo edit TARGET /path --old X --new Y    Edit a remote file
    octo glob TARGET "**/*.py"                Search remote files by pattern
    octo grep TARGET "pattern"                Search remote file contents
    octo logs TARGET [-f] [--tail N]          View task output or log file
    octo kill TARGET                          Kill running command on terminal
    octo agent-md                             Generate/update CLAUDE.md

Network & Permissions:
    octo network create NAME [--public]       Create a network
    octo network info                         Show network info
    octo register NAME                        Register device identity
    octo unregister NAME                      Unregister device identity
    octo up [--tags T] [--ssh HOST]           Start daemon (go online)
    octo down                                 Stop daemon (go offline)
    octo invite TERMINAL --to DEVICE --role R Direct invite
    octo invite TERMINAL --role R [--expires] Generate invite code
    octo accept CODE                          Accept an invite code
    octo acl TERMINAL [--default R] [--revoke D]  Manage ACL

Nearby P2P Transfer:
    octo nearby scan                             Scan for nearby Octo devices via BLE
    octo nearby send FILE                        Send a file (no internet needed)
    octo nearby receive HOST                     Receive a file from sender IP
    octo nearby receive --ble ADDRESS            Receive via full BLE flow
"""

import argparse
import os
import signal
import sys
import time

import base64
import json

from .config import (
    load_config, save_config, get_relay_config,
    encode_token, decode_token, CONFIG_FILE,
    get_storage_config, set_storage_config,
    get_identity, set_identity, clear_identity,
    get_keypair, set_keypair,
)
from .relay import Relay, RelayError
from .daemon import Daemon, REDIS_TRANSFER_MAX, LAN_PORT
from .storage import get_storage_client, StorageError
from .agent_md import write_agent_md
from .mcp_server import run_server as mcp_run_server


POLL_INTERVAL = 1  # seconds


# ---- Auth Helpers ----

def _get_auth_kwargs(relay, task_id: str, task_type: str = "shell") -> dict:
    """Build requester + signature kwargs for submit_task.

    task_id must be the actual ID that will be used in submit_task
    (generate it first with Relay.generate_task_id).

    Returns empty dict for personal networks (no overhead).
    Only loads signing module for public networks.
    """
    identity = get_identity()
    if not identity:
        return {}

    # Check if network is public
    try:
        meta = relay.get_network_meta()
        if not meta or meta.get("mode") != "public":
            return {"requester": identity}
    except Exception:
        return {"requester": identity}

    # Public network: sign the task with the actual task_id
    from .signing import sign_message, build_sign_payload
    payload = build_sign_payload(task_id, task_type, identity)
    sig = sign_message(payload, identity)
    return {"requester": identity, "signature": sig}


# ---- Helpers ----

def _mask(s: str) -> str:
    if not s:
        return ""
    if len(s) <= 8:
        return "****"
    return s[:4] + "****" + s[-4:]


def _get_relay() -> Relay:
    rc = get_relay_config()
    return Relay(rc["redis_url"], rc["redis_token"], rc["workspace"],
                 proxy_url=rc.get("proxy_url", ""))


def _check_target(relay: Relay, target: str) -> None:
    """Verify target terminal exists, warn if offline."""
    terminals = relay.list_terminals()
    names = [t["name"] for t in terminals]
    if target not in names:
        avail = ", ".join(names) or "none"
        print(f"[octo] Terminal '{target}' not found. Available: {avail}", file=sys.stderr)
        sys.exit(1)
    t = next(t for t in terminals if t["name"] == target)
    if not t["online"]:
        print(f"[octo] Warning: '{target}' appears offline ({t['last_seen_ago']}s ago).", file=sys.stderr)


def _poll_streaming(relay: Relay, target: str, task_id: str = None) -> int:
    """Poll task with streaming output display. Returns exit code."""
    printed_len = 0
    try:
        while True:
            task = relay.poll_task(target, task_id=task_id)
            if not task:
                print("[octo] Task disappeared.", file=sys.stderr)
                return 1

            # Check task ID matches — another submit may have replaced ours
            if task_id and task.get("id") != task_id:
                print(f"[octo] Task was replaced (expected {task_id}).", file=sys.stderr)
                return 1

            # Stream new output
            output = task.get("output", "")
            if len(output) > printed_len:
                sys.stdout.write(output[printed_len:])
                sys.stdout.flush()
                printed_len = len(output)

            if task["status"] in ("DONE", "FAILED"):
                break

            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        # Send kill signal
        print("\n[octo] Sending kill signal...", file=sys.stderr)
        relay.update_task(target, {"status": "KILL"}, task_id=task_id)
        try:
            for _ in range(5):
                time.sleep(1)
                task = relay.poll_task(target, task_id=task_id)
                if task and task["status"] in ("DONE", "FAILED"):
                    output = task.get("output", "")
                    if len(output) > printed_len:
                        sys.stdout.write(output[printed_len:])
                        sys.stdout.flush()
                    break
        except KeyboardInterrupt:
            print("\n[octo] Force stop.", file=sys.stderr)
        finally:
            relay.clear_task(target, task_id=task_id)
        return 130

    exit_code = task.get("exit_code", 0) or 0
    relay.clear_task(target, task_id=task_id)
    return exit_code


# ---- Commands ----

FREE_RELAY_URL = "https://openocto-relay.openocto.workers.dev"

def cmd_init(args):
    print("OpenOcto Setup")
    print("=" * 40)

    existing = load_config()

    print("\nRelay options:")
    print("  1. Free relay  — Quick start, no setup needed (recommended)")
    print("  2. Own Redis   — Full privacy, deploy your own relay")
    choice = input("\nChoice [1/2] (default: 1): ").strip() or "1"

    if choice == "1":
        import uuid
        proxy_url = FREE_RELAY_URL
        redis_url = ""
        redis_token = ""
        # Each user gets a cryptographically random workspace ID
        # (24 hex chars = 96 bits of entropy, brute-force infeasible)
        default_ws = existing.get("workspace", "")
        if not default_ws or default_ws == "default" or len(default_ws) < 10:
            default_ws = f"ws-{uuid.uuid4().hex[:24]}"
        workspace = default_ws
        print(f"\n  Workspace: {workspace}")
        print(f"  Relay:     {proxy_url}")
        print()
        print("  Security: each workspace is isolated by a random ID.")
        print("  Share access via 'octo token' — don't share the workspace ID directly.")
        print("  For full privacy, choose option 2 (own Redis).")
    else:
        redis_url = input(
            f"\nRedis URL [{existing.get('redis_url', '')}]: "
        ).strip()
        if not redis_url:
            redis_url = existing.get("redis_url", "")
        if not redis_url:
            print("Create one free at: https://upstash.com/")
            redis_url = input("Redis URL: ").strip()
            if not redis_url:
                print("Aborted.")
                return

        redis_token = input(
            f"Redis Token [{_mask(existing.get('redis_token', ''))}]: "
        ).strip()
        if not redis_token:
            redis_token = existing.get("redis_token", "")
        if not redis_token:
            redis_token = input("Redis Token: ").strip()
            if not redis_token:
                print("Aborted.")
                return

        workspace = input(
            f"Workspace [{existing.get('workspace', 'default')}]: "
        ).strip() or existing.get("workspace", "default")

        print("\n  Proxy URL is optional. If you want a fallback relay,")
        print("  deploy your own CF Worker (see cf-worker/). Do NOT use the free relay URL.")
        proxy_url = input(
            f"Proxy URL (optional) [{existing.get('proxy_url', '')}]: "
        ).strip()
        if not proxy_url:
            proxy_url = existing.get("proxy_url", "")

    config = {
        "redis_url": redis_url,
        "redis_token": redis_token,
        "workspace": workspace,
    }
    if proxy_url:
        config["proxy_url"] = proxy_url
    save_config(config)
    print(f"\nConfig saved to {CONFIG_FILE}")

    print("Testing connection...", end=" ", flush=True)
    try:
        Relay(redis_url, redis_token, workspace, proxy_url=proxy_url).list_terminals()
        print("OK")
    except RelayError as e:
        print(f"FAILED\n  {e}")


def cmd_token(args):
    rc = get_relay_config()
    token = encode_token(rc["redis_url"], rc["redis_token"], rc["workspace"],
                         proxy_url=rc.get("proxy_url", ""))
    if args.qr:
        try:
            import segno
            qr = segno.make(token)
            # Save as PNG image
            import tempfile, os
            img_path = os.path.join(tempfile.gettempdir(), "octo_qr.png")
            qr.save(img_path, scale=10, border=4)
            # Terminal display
            qr.terminal(compact=True)
            print()
            print(token)
            print(f"\nQR image saved to: {img_path}")
        except ImportError:
            print("[octo] Install 'segno' for QR codes: pip install segno")
            print(f"\nToken (paste manually): {token}")
    else:
        print(token)


def cmd_join(args):
    if args.token:
        try:
            decoded = decode_token(args.token)
            cfg = {
                "redis_url": decoded["redis_url"],
                "redis_token": decoded["redis_token"],
                "workspace": decoded["workspace"],
            }
            if decoded.get("proxy_url"):
                cfg["proxy_url"] = decoded["proxy_url"]
            save_config(cfg)
            print(f"[octo] Config imported from token.")
        except Exception as e:
            print(f"[octo] Invalid token: {e}", file=sys.stderr)
            sys.exit(1)

    rc = get_relay_config()
    relay = Relay(rc["redis_url"], rc["redis_token"], rc["workspace"],
                  proxy_url=rc.get("proxy_url", ""))
    name = args.name
    tags = [t.strip() for t in args.tags.split(",")] if args.tags else []

    try:
        existing = next((t for t in relay.list_terminals() if t["name"] == name), None)
        if existing:
            status = "online" if existing.get("online") else "offline"
            print(
                f"[octo] Warning: terminal name '{name}' already exists in this workspace "
                f"({status}, last seen {existing.get('last_seen_ago', '?')}s ago). "
                f"Registering again will replace the existing entry shown by 'octo ls'.",
                file=sys.stderr,
            )
    except Exception:
        pass

    if args.daemon:
        import subprocess as sp
        cmd = [sys.executable, "-m", "openocto.cli", "join", "--name", name]
        if args.tags:
            cmd += ["--tags", args.tags]
        if args.ssh:
            cmd += ["--ssh", args.ssh]
        if args.log_min_seconds is not None:
            cmd += ["--log-min-seconds", str(args.log_min_seconds)]
        if args.log_max_files is not None:
            cmd += ["--log-max-files", str(args.log_max_files)]
        if getattr(args, 'cool_max_interval', None) is not None:
            cmd += ["--cool-max-interval", str(args.cool_max_interval)]
        if getattr(args, 'cool_idle_timeout', None) is not None:
            cmd += ["--cool-idle-timeout", str(args.cool_idle_timeout)]
        log = os.path.expanduser("~/.octo/daemon.log")
        log_fh = open(log, "a")
        proc = sp.Popen(
            cmd,
            stdout=log_fh,
            stderr=sp.STDOUT,
            start_new_session=True,
        )
        log_fh.close()  # Popen has duplicated the fd
        print(f"[octo] Daemon started (PID: {proc.pid}), log: {log}")
        return

    daemon = Daemon(relay, name, tags, ssh=args.ssh,
                    log_min_seconds=args.log_min_seconds,
                    log_max_files=args.log_max_files,
                    verbose=getattr(args, 'verbose', False),
                    owner_identity=get_identity(),
                    cool_max_interval=getattr(args, 'cool_max_interval', None),
                    cool_idle_timeout=getattr(args, 'cool_idle_timeout', None))
    daemon.start()


def cmd_ls(args):
    relay = _get_relay()
    terminals = relay.list_terminals()
    if not terminals:
        print("No terminals registered.")
        return

    print(f"{'NAME':<20} {'STATUS':<10} {'MODE':<8} {'TAGS':<20} {'LAST SEEN'}")
    print("-" * 75)
    for t in terminals:
        status = "online" if t["online"] else "offline"
        mode = relay.get_mode(t["name"])
        tags = ", ".join(t.get("tags", []))
        ago = t["last_seen_ago"]
        if ago < 60:
            seen = f"{ago}s ago"
        elif ago < 3600:
            seen = f"{ago // 60}m ago"
        else:
            seen = f"{ago // 3600}h ago"
        print(f"{t['name']:<20} {status:<10} {mode:<8} {tags:<20} {seen}")


def cmd_run(args):
    relay = _get_relay()
    target = args.target
    _check_target(relay, target)

    # Read command from argument or stdin
    command = args.command
    if command == "-" or (command is None and not sys.stdin.isatty()):
        command = sys.stdin.read().strip()
    if not command:
        print("[octo] No command provided.", file=sys.stderr)
        sys.exit(1)

    extra = {}
    if args.timeout:
        extra["timeout"] = args.timeout
    if args.no_log:
        extra["no_log"] = True
    if args.notify:
        extra["notify"] = args.notify
        if args.notify_message:
            extra["notify_message"] = args.notify_message
    task_id = Relay.generate_task_id(target)
    extra.update(_get_auth_kwargs(relay, task_id=task_id, task_type="shell"))
    task_id = relay.submit_task(target, task_type="shell", command=command,
                                task_id=task_id, **extra)
    _wake_daemon(relay, target)
    print(f"[octo] Executing on '{target}': {command[:80]}{'...' if len(command) > 80 else ''}", file=sys.stderr)

    if args.nowait:
        print(f"[octo] Task submitted (nowait): {task_id}", file=sys.stderr)
        return

    sys.exit(_poll_streaming(relay, target, task_id=task_id))


def cmd_cat(args):
    relay = _get_relay()
    target = args.target
    _check_target(relay, target)

    kwargs = {"path": args.path}
    if args.offset:
        kwargs["offset"] = args.offset
    if args.limit:
        kwargs["limit"] = args.limit

    tid = Relay.generate_task_id(target)
    kwargs.update(_get_auth_kwargs(relay, task_id=tid, task_type="cat"))
    relay.submit_task(target, task_type="cat", task_id=tid, **kwargs)
    sys.exit(_poll_streaming(relay, target, task_id=tid))


def cmd_edit(args):
    relay = _get_relay()
    target = args.target
    _check_target(relay, target)

    tid = Relay.generate_task_id(target)
    auth = _get_auth_kwargs(relay, task_id=tid, task_type="edit")
    relay.submit_task(
        target,
        task_type="edit",
        task_id=tid,
        path=args.path,
        old=args.old,
        new=args.new,
        replace_all=args.replace_all,
        **auth,
    )
    sys.exit(_poll_streaming(relay, target, task_id=tid))


def cmd_glob(args):
    relay = _get_relay()
    target = args.target
    _check_target(relay, target)

    kwargs = {"pattern": args.pattern}
    if args.path:
        kwargs["path"] = args.path

    tid = Relay.generate_task_id(target)
    kwargs.update(_get_auth_kwargs(relay, task_id=tid, task_type="glob"))
    relay.submit_task(target, task_type="glob", task_id=tid, **kwargs)
    sys.exit(_poll_streaming(relay, target, task_id=tid))


def cmd_grep(args):
    relay = _get_relay()
    target = args.target
    _check_target(relay, target)

    kwargs = {"pattern": args.pattern}
    if args.path:
        kwargs["path"] = args.path
    if args.file_glob:
        kwargs["glob"] = args.file_glob

    tid = Relay.generate_task_id(target)
    kwargs.update(_get_auth_kwargs(relay, task_id=tid, task_type="grep"))
    relay.submit_task(target, task_type="grep", task_id=tid, **kwargs)
    sys.exit(_poll_streaming(relay, target, task_id=tid))


def cmd_kill(args):
    relay = _get_relay()
    target = args.target
    task_id = getattr(args, "task_id", None)

    task = relay.poll_task(target, task_id=task_id)
    if not task or task["status"] not in ("PENDING", "RUNNING"):
        suffix = f" with ID {task_id}" if task_id else ""
        print(f"[octo] No active task on '{target}'{suffix}.", file=sys.stderr)
        return

    resolved_id = task_id or task.get("id")
    if resolved_id:
        relay.update_task(target, {"status": "KILL"}, task_id=resolved_id)
    else:
        relay.update_task(target, {"status": "KILL"})
    print(f"[octo] Kill signal sent to '{target}' ({resolved_id or 'current'}).")


def cmd_logs(args):
    relay = _get_relay()
    target = args.target
    task_id = getattr(args, "task_id", None)

    # First check if there's a task in Redis
    task = relay.poll_task(target, task_id=task_id)
    if task:
        output = task.get("output", "")
        status = task.get("status", "UNKNOWN")
        if output:
            print(output)
        if status in ("RUNNING", "PENDING"):
            if args.follow:
                # Stream like _poll_streaming but don't clear
                printed_len = len(output)
                try:
                    while True:
                        task = relay.poll_task(target)
                        if not task:
                            break
                        out = task.get("output", "")
                        if len(out) > printed_len:
                            sys.stdout.write(out[printed_len:])
                            sys.stdout.flush()
                            printed_len = len(out)
                        if task["status"] in ("DONE", "FAILED"):
                            break
                        time.sleep(POLL_INTERVAL)
                except KeyboardInterrupt:
                    pass
            else:
                print(f"\n[octo] Task is {status}. Use -f to follow.", file=sys.stderr)
        else:
            exit_code = task.get("exit_code", 0) or 0
            print(f"\n[octo] Task {status} (exit code: {exit_code})", file=sys.stderr)
        return
    if task_id:
        print(f"[octo] Task not found: {task_id}", file=sys.stderr)
        return

    # No task in Redis — read the log file from remote
    _check_target(relay, target)
    log_path = f"$HOME/.octo/logs/{target}_latest.log"
    if args.tail:
        cmd = f"tail -n {args.tail} {log_path}"
    else:
        cmd = f"cat {log_path}"
    relay.submit_task(target, task_type="shell", command=cmd, no_log=True)
    sys.exit(_poll_streaming(relay, target))


def cmd_wake(args):
    relay = _get_relay()
    target = args.target
    relay.set_mode(target, "wake")
    print(f"[octo] '{target}' set to WAKE mode (fast polling).")


def cmd_cool(args):
    relay = _get_relay()
    target = args.target
    relay.set_mode(target, "cool")
    print(f"[octo] '{target}' set to COOL mode (polling every 60s).")


def cmd_rm(args):
    relay = _get_relay()
    for target in args.targets:
        relay.unregister(target)
        print(f"[octo] '{target}' removed.")


AGENT_PRESETS = {
    "claude":   ".claude/rules/octo.md",
    "cursor":   ".cursorrules",
    "windsurf": ".windsurfrules",
    "copilot":  ".github/copilot-instructions.md",
}


def _pick_agent_path(agent: str = None) -> str:
    """Resolve output path: explicit agent, -o override, or interactive."""
    if agent and agent in AGENT_PRESETS:
        path = AGENT_PRESETS[agent]
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        return path

    # Interactive selection
    print("Select target agent:")
    keys = list(AGENT_PRESETS.keys())
    for i, name in enumerate(keys, 1):
        print(f"  {i}) {name:<12} → {AGENT_PRESETS[name]}")
    print(f"  {len(keys) + 1}) other        → enter custom path")

    try:
        choice = input("Choice [1]: ").strip() or "1"
        idx = int(choice) - 1
    except (ValueError, EOFError):
        idx = 0

    if 0 <= idx < len(keys):
        path = AGENT_PRESETS[keys[idx]]
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        return path
    else:
        custom = input("Output path: ").strip()
        if not custom:
            print("Aborted.")
            sys.exit(1)
        os.makedirs(os.path.dirname(custom) or ".", exist_ok=True)
        return custom


def cmd_agent_md(args):
    from .agent_md import SYNC_PRESETS

    relay = _get_relay()

    # --show: print current effective MD and exit
    if args.show:
        path = args.output
        if not path:
            path = _pick_agent_path(args.agent) if args.agent else None
            if not path:
                # Try all presets to find existing file
                for p in AGENT_PRESETS.values():
                    if os.path.exists(p):
                        path = p
                        break
        if not path or not os.path.exists(path):
            print("[octo] No agent-md file found. Run `octo agent-md` to generate one.", file=sys.stderr)
            sys.exit(1)
        with open(path, "r", encoding="utf-8") as f:
            print(f.read())
        return

    if args.output:
        path = args.output
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    else:
        path = _pick_agent_path(args.agent)

    custom = ""
    if args.rules:
        custom = "\n".join(f"- {r}" for r in args.rules)
    if args.rules_file:
        with open(args.rules_file, "r", encoding="utf-8") as f:
            custom = (custom + "\n" + f.read().strip()) if custom else f.read().strip()

    # Sync mode
    sync_mode = args.sync or "direct"
    sync_rules_custom = ""
    if sync_mode == "custom":
        if args.sync_rules:
            sync_rules_custom = args.sync_rules
        elif args.sync_rules_file:
            with open(args.sync_rules_file, "r", encoding="utf-8") as f:
                sync_rules_custom = f.read().strip()
        else:
            print("Sync mode 'custom' requires --sync-rules or --sync-rules-file.", file=sys.stderr)
            print("\nTip: Describe the sync workflow you want the agent to follow. Example:", file=sys.stderr)
            print('  --sync-rules "1. Edit code locally. 2. Run make deploy to sync. 3. Use remote_run to execute."', file=sys.stderr)
            print(f"\nAvailable presets: {', '.join(SYNC_PRESETS.keys())}", file=sys.stderr)
            sys.exit(1)

    written = write_agent_md(relay, path, custom_rules=custom,
                             sync_mode=sync_mode, sync_rules_custom=sync_rules_custom)
    print(f"[octo] Written to {written}")


def _get_terminal_meta(relay: Relay, name: str) -> dict:
    """Get terminal metadata including LAN info."""
    terminals = relay.list_terminals()
    for t in terminals:
        if t["name"] == name:
            return t.get("meta", {})
    return {}


def _wake_daemon(relay: Relay, target: str) -> None:
    """Best-effort LAN wake to interrupt a sleeping daemon poll."""
    import urllib.request

    try:
        meta = _get_terminal_meta(relay, target)
        ip = meta.get("lan_ip", "")
        port = meta.get("lan_port", LAN_PORT)
        if not ip:
            return
        url = f"http://{ip}:{port}/wake"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=2):
            pass
    except Exception:
        # The relay remains the source of truth; LAN wake is only an
        # acceleration when the CLI can reach the target directly.
        pass


def _lan_reachable(ip: str, port: int, timeout: float = 1.0) -> bool:
    """Check if a LAN IP:port is reachable."""
    if not ip:
        return False
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((ip, port))
        s.close()
        return True
    except (OSError, socket.error):
        return False


def _transfer_file(relay: Relay, source: str, target: str, file_path: str,
                    dest: str, source_is_local: bool = False) -> int:
    """Transfer a file from source to target terminal.

    Routing: LAN direct → Redis (small) → Cloud storage (large).
    """
    import urllib.request

    # Get target terminal info
    target_meta = _get_terminal_meta(relay, target)
    target_lan_ip = target_meta.get("lan_ip", "")
    target_lan_port = target_meta.get("lan_port", LAN_PORT)

    # Get source terminal info
    if not source_is_local:
        source_meta = _get_terminal_meta(relay, source)
        source_lan_ip = source_meta.get("lan_ip", "")
        source_lan_port = source_meta.get("lan_port", LAN_PORT)
        source_is_windows = source_meta.get("platform", "").startswith("win")

    # Determine file size
    if source_is_local:
        if not os.path.isfile(file_path):
            print(f"[octo] File not found: {file_path}", file=sys.stderr)
            return 1
        file_size = os.path.getsize(file_path)
    else:
        # Ask source for file size via shell
        if source_is_windows:
            size_cmd = f"(Get-Item '{file_path}').Length"
        else:
            size_cmd = f"stat -c%s {file_path} 2>/dev/null || stat -f%z {file_path} 2>/dev/null"
        relay.submit_task(source, task_type="shell",
                          command=size_cmd,
                          no_log=True)
        task = _wait_result(relay, source)
        try:
            file_size = int(task.get("output", "0").strip())
        except ValueError:
            file_size = 0

    print(f"[octo] File: {file_path} ({file_size} bytes)", file=sys.stderr)

    # Route 1: LAN direct transfer
    if target_lan_ip and _lan_reachable(target_lan_ip, target_lan_port):
        if source_is_local:
            # Push directly from local to target's LAN HTTP server
            print(f"[octo] Route: LAN direct → {target_lan_ip}:{target_lan_port}", file=sys.stderr)
            try:
                with open(file_path, "rb") as f:
                    data = f.read()
                dest_encoded = urllib.parse.quote(dest, safe="")
                url = f"http://{target_lan_ip}:{target_lan_port}/receive?dest={dest_encoded}"
                req = urllib.request.Request(url, data=data, method="POST")
                req.add_header("Content-Type", "application/octet-stream")
                req.add_header("Content-Length", str(len(data)))
                with urllib.request.urlopen(req, timeout=300) as resp:
                    result = resp.read().decode()
                print(f"[octo] Transfer complete: {result}", file=sys.stderr)
                return 0
            except Exception as e:
                print(f"[octo] LAN transfer failed: {e}, trying fallback...", file=sys.stderr)
        else:
            # Source is remote - tell target to download from source's LAN
            if not source_is_local:
                source_lan_ip = source_meta.get("lan_ip", "")
                source_lan_port = source_meta.get("lan_port", LAN_PORT)
                if source_lan_ip and _lan_reachable(source_lan_ip, source_lan_port):
                    path_encoded = urllib.parse.quote(file_path, safe="")
                    lan_url = f"http://{source_lan_ip}:{source_lan_port}/file?path={path_encoded}"
                    print(f"[octo] Route: LAN direct (source serves)", file=sys.stderr)
                    relay.submit_task(target, task_type="transfer_download",
                                      url=lan_url, dest=dest)
                    return _poll_streaming(relay, target)

    # Route 2: Small file via Redis
    if file_size <= REDIS_TRANSFER_MAX:
        print(f"[octo] Route: Redis relay ({file_size} bytes)", file=sys.stderr)
        transfer_key = f"octo:transfer:{int(time.time())}:{id(file_path) % 10000}"

        if source_is_local:
            with open(file_path, "rb") as f:
                data = f.read()
            b64 = base64.b64encode(data).decode()
            relay._request("SET", transfer_key, b64)
            relay._request("EXPIRE", transfer_key, "600")
        else:
            # Tell source to read and upload to Redis
            py_cmd = "python" if source_is_windows else "python3"
            relay.submit_task(source, task_type="shell",
                              command=f"{py_cmd} -c \""
                              f"import base64,sys; "
                              f"data=open('{file_path}','rb').read(); "
                              f"print(base64.b64encode(data).decode())\"",
                              no_log=True)
            task = _wait_result(relay, source)
            b64 = task.get("output", "").strip()
            if not b64:
                print("[octo] Failed to read file from source.", file=sys.stderr)
                return 1
            relay._request("SET", transfer_key, b64)
            relay._request("EXPIRE", transfer_key, "600")

        relay.submit_task(target, task_type="transfer_download",
                          redis_key=transfer_key, dest=dest)
        return _poll_streaming(relay, target)

    # Route 3: Large file via cloud storage
    config = load_config()
    s3 = get_storage_client(config)
    if not s3:
        print(f"[octo] File too large for Redis relay ({file_size} bytes > {REDIS_TRANSFER_MAX}).", file=sys.stderr)
        print(f"[octo] Configure cloud storage: octo config --storage", file=sys.stderr)
        print(f"[octo] Or transfer on the same WiFi for LAN direct transfer.", file=sys.stderr)
        return 1

    print(f"[octo] Route: Cloud storage ({file_size} bytes)", file=sys.stderr)
    obj_key = f"transfer/{int(time.time())}_{os.path.basename(file_path)}"

    try:
        if source_is_local:
            s3.upload_file(obj_key, file_path)
        else:
            # Tell source to read the file and we'll relay it
            py_cmd = "python" if source_is_windows else "python3"
            relay.submit_task(source, task_type="shell",
                              command=(
                                  f"{py_cmd} -c \""
                                  f"import base64,sys; "
                                  f"data=open('{file_path}','rb').read(); "
                                  f"print(base64.b64encode(data).decode())\""
                              ),
                              no_log=True)
            task = _wait_result(relay, source)
            b64 = task.get("output", "").strip()
            data = base64.b64decode(b64)
            s3.upload(obj_key, data)

        download_url = s3.presign_download(obj_key, expires=600)
        relay.submit_task(target, task_type="transfer_download",
                          url=download_url, dest=dest)
        exit_code = _poll_streaming(relay, target)

        # Clean up cloud storage
        try:
            s3.delete(obj_key)
        except StorageError:
            pass

        return exit_code
    except (StorageError, Exception) as e:
        print(f"[octo] Cloud transfer failed: {e}", file=sys.stderr)
        return 1


def _wait_result(relay: Relay, target: str) -> dict:
    """Wait for task to complete and return result. Does NOT clear."""
    while True:
        task = relay.poll_task(target)
        if not task:
            return {"output": "", "exit_code": 1}
        if task["status"] in ("DONE", "FAILED"):
            relay.clear_task(target)
            return task
        time.sleep(1)


def cmd_send(args):
    relay = _get_relay()
    source = args.source
    target = args.target
    file_path = args.file

    # Determine dest path
    dest = args.dest or os.path.basename(file_path)

    source_is_local = (source == "local")
    if not source_is_local:
        _check_target(relay, source)
    _check_target(relay, target)

    sys.exit(_transfer_file(relay, source, target, file_path, dest,
                             source_is_local=source_is_local))


def _write_local_clipboard(text: str) -> bool:
    """Write text to local clipboard. Returns True on success."""
    import subprocess as _sp
    if sys.platform == "darwin":
        proc = _sp.run(["pbcopy"], input=text.encode(), capture_output=True)
        return proc.returncode == 0
    elif sys.platform == "win32":
        proc = _sp.run(["clip"], input=text.encode(), capture_output=True)
        return proc.returncode == 0
    else:
        for cmd in [["xclip", "-selection", "clipboard"],
                    ["xsel", "--clipboard", "--input"],
                    ["wl-copy"]]:
            try:
                proc = _sp.run(cmd, input=text.encode(), capture_output=True, timeout=5)
                if proc.returncode == 0:
                    return True
            except FileNotFoundError:
                continue
        return False


def cmd_clip(args):
    relay = _get_relay()
    target = args.target
    _check_target(relay, target)

    if args.text:
        # Write to target clipboard
        tid = Relay.generate_task_id(target)
        auth = _get_auth_kwargs(relay, task_id=tid, task_type="clipboard_write")
        relay.submit_task(target, task_type="clipboard_write", text=args.text,
                          task_id=tid, **auth)
        sys.exit(_poll_streaming(relay, target, task_id=tid))
    else:
        # Read from target clipboard
        tid = Relay.generate_task_id(target)
        auth = _get_auth_kwargs(relay, task_id=tid, task_type="clipboard_read")
        relay.submit_task(target, task_type="clipboard_read", task_id=tid, **auth)

        # Poll for result
        printed_len = [0]
        task = None
        try:
            while True:
                task = relay.poll_task(target)
                if not task:
                    print("[octo] Task disappeared.", file=sys.stderr)
                    sys.exit(1)
                if tid and task.get("id") != tid:
                    print("[octo] Task was replaced.", file=sys.stderr)
                    sys.exit(1)
                if task["status"] in ("DONE", "FAILED"):
                    break
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            relay.clear_task(target)
            sys.exit(130)

        output = task.get("output", "")
        exit_code = task.get("exit_code", 0) or 0
        relay.clear_task(target)

        if exit_code != 0:
            print(output, file=sys.stderr)
            sys.exit(exit_code)

        if args.paste:
            # Write remote clipboard content to local clipboard
            if _write_local_clipboard(output):
                print(f"[octo] Copied to local clipboard ({len(output)} chars).", file=sys.stderr)
            else:
                print("[octo] Failed to write to local clipboard. Content:", file=sys.stderr)
                print(output)
                sys.exit(1)
        else:
            # Just print
            print(output)


def cmd_config(args):
    if args.storage:
        print("Cloud Storage Configuration (S3-compatible)")
        print("=" * 40)
        existing = get_storage_config()

        endpoint = input(f"Endpoint [{existing.get('endpoint', '')}]: ").strip()
        if not endpoint:
            endpoint = existing.get("endpoint", "")

        bucket = input(f"Bucket [{existing.get('bucket', '')}]: ").strip()
        if not bucket:
            bucket = existing.get("bucket", "")

        access_key = input(f"Access Key ID [{_mask(existing.get('access_key', ''))}]: ").strip()
        if not access_key:
            access_key = existing.get("access_key", "")

        secret_key = input(f"Secret Key [{_mask(existing.get('secret_key', ''))}]: ").strip()
        if not secret_key:
            secret_key = existing.get("secret_key", "")

        region = input(f"Region [{existing.get('region', 'auto')}]: ").strip()
        if not region:
            region = existing.get("region", "auto")

        set_storage_config({
            "endpoint": endpoint,
            "bucket": bucket,
            "access_key": access_key,
            "secret_key": secret_key,
            "region": region,
        })
        print(f"\nStorage config saved to {CONFIG_FILE}")

    elif args.show:
        config = load_config()
        # Mask sensitive fields
        display = {}
        for k, v in config.items():
            if k == "redis_token":
                display[k] = _mask(v)
            elif k == "storage" and isinstance(v, dict):
                display[k] = {
                    sk: _mask(sv) if sk in ("access_key", "secret_key") else sv
                    for sk, sv in v.items()
                }
            else:
                display[k] = v
        print(json.dumps(display, indent=2))
    else:
        print("Use --storage to configure cloud storage, or --show to display config.")


def cmd_inbox(args):
    relay = _get_relay()
    target = args.target
    _check_target(relay, target)
    relay.submit_task(target, task_type="inbox_list")
    sys.exit(_poll_streaming(relay, target))


def cmd_metrics(args):
    if args.dashboard:
        from .metrics_dashboard import run_dashboard
        run_dashboard(port=args.port, logdir=args.logdir, tail=args.tail)
        return
    if not args.target:
        print("[octo] Usage: octo metrics <terminal> or octo metrics --dashboard", file=sys.stderr)
        return 1
    relay = _get_relay()
    target = args.target
    _check_target(relay, target)
    extra = {}
    if args.logdir:
        extra["logdir"] = args.logdir
    if args.tail != 20:
        extra["tail"] = args.tail
    relay.submit_task(target, task_type="metrics", **extra)
    sys.exit(_poll_streaming(relay, target))


def cmd_companion(args):
    from .companion import run_companion
    run_companion(name=args.name)


def cmd_setup(args):
    from .agent_builtin import run_setup_guide
    run_setup_guide()


def cmd_agent(args):
    from .agent_repl import run_agent
    extra = " ".join(args.args) if args.args else None
    run_agent(backend=args.backend, model=args.model, name=args.name,
              extra_args=extra)


def cmd_agent_serve(args):
    from .agent_serve import run_agent_serve
    run_agent_serve(name=args.name, backend=args.backend, model=args.model)


def cmd_agent_ls(args):
    relay = _get_relay()
    terminals = relay.list_terminals()
    agents = [t for t in terminals if t.get("meta", {}).get("ai_agent")]

    if not agents:
        print("No AI agents online. Start one with: octo agent-serve")
        return

    # Load preferred agent
    config = load_config()
    preferred = config.get("preferred_agent", "")

    print(f"{'NAME':<20} {'STATUS':<10} {'BACKEND':<25} {'PREFERRED'}")
    print("-" * 65)
    for a in agents:
        name = a["name"]
        status = "online" if a.get("online") else "offline"
        meta = a.get("meta", {})
        label = meta.get("ai_label", meta.get("ai_backend", "?"))
        pref = "*" if name == preferred else ""
        print(f"{name:<20} {status:<10} {label:<25} {pref}")


def cmd_agent_prefer(args):
    config = load_config()
    config["preferred_agent"] = args.name
    save_config(config)
    print(f"[octo] Preferred agent set to '{args.name}'")


def cmd_mcp_server(args):
    mcp_run_server()


# ---- Network & Permission Commands ----

def cmd_network(args):
    relay = _get_relay()
    action = args.action

    if action == "create":
        name = args.name
        mode = "public" if args.public else "personal"
        # Update workspace in config
        config = load_config()
        config["workspace"] = name
        save_config(config)
        # Re-create relay with new workspace
        relay = _get_relay()
        meta = relay.get_network_meta()
        if meta:
            print(f"[octo] Network '{name}' already exists (mode: {meta.get('mode')}).")
            return
        relay.set_network_meta({
            "mode": mode,
            "created_by": get_identity() or "unknown",
            "created_at": int(time.time()),
        })
        print(f"[octo] Network '{name}' created (mode: {mode}).")

    elif action == "info":
        meta = relay.get_network_meta()
        if not meta:
            print(f"[octo] No network metadata (legacy/personal mode).")
            return
        print(f"Network: {relay.workspace}")
        print(f"Mode: {meta.get('mode', 'personal')}")
        print(f"Created by: {meta.get('created_by', '?')}")
        created = meta.get("created_at", 0)
        if created:
            import datetime
            dt = datetime.datetime.fromtimestamp(created)
            print(f"Created at: {dt.isoformat()}")

    elif action == "set-mode":
        mode = args.mode
        meta = relay.get_network_meta() or {}
        meta["mode"] = mode
        relay.set_network_meta(meta)
        print(f"[octo] Network mode set to: {mode}")


def cmd_register(args):
    relay = _get_relay()
    name = args.name

    # Check if already registered locally
    current = get_identity()
    if current == name:
        print(f"[octo] Already registered as '{name}'.")
        return

    # Check network mode for keypair generation
    meta = relay.get_network_meta()
    is_public = meta and meta.get("mode") == "public"

    # Build identity info
    info = {
        "registered_at": int(time.time()),
        "platform": sys.platform,
    }

    # Generate keypair for public networks
    if is_public:
        from .signing import generate_keypair
        private_b64, public_b64 = generate_keypair(name)
        set_keypair(private_b64, public_b64)
        info["public_key"] = public_b64

    # Try to register (atomic, fails if name taken)
    if not relay.register_identity(name, info):
        print(f"[octo] Name '{name}' is already taken.", file=sys.stderr)
        sys.exit(1)

    set_identity(name)
    print(f"[octo] Registered as '{name}'.")
    if is_public:
        print(f"[octo] Ed25519 keypair generated for signing.")


def cmd_unregister(args):
    relay = _get_relay()
    name = args.name

    current = get_identity()
    if current != name:
        print(f"[octo] You are not registered as '{name}' (current: '{current or 'none'}').",
              file=sys.stderr)
        sys.exit(1)

    relay.remove_identity(name)
    clear_identity()
    print(f"[octo] Unregistered '{name}'. Name released.")


def cmd_up(args):
    """Start daemon with current identity."""
    identity = get_identity()
    if not identity:
        print("[octo] Not registered. Run 'octo register <name>' first.", file=sys.stderr)
        sys.exit(1)

    rc = get_relay_config()
    relay = Relay(rc["redis_url"], rc["redis_token"], rc["workspace"],
                  proxy_url=rc.get("proxy_url", ""))
    tags = [t.strip() for t in args.tags.split(",")] if args.tags else []

    if args.daemon:
        import subprocess as sp
        cmd = [sys.executable, "-m", "openocto.cli", "up"]
        if args.tags:
            cmd += ["--tags", args.tags]
        if args.ssh:
            cmd += ["--ssh", args.ssh]
        if getattr(args, 'cool_max_interval', None) is not None:
            cmd += ["--cool-max-interval", str(args.cool_max_interval)]
        if getattr(args, 'cool_idle_timeout', None) is not None:
            cmd += ["--cool-idle-timeout", str(args.cool_idle_timeout)]
        log = os.path.expanduser("~/.octo/daemon.log")
        log_fh = open(log, "a")
        proc = sp.Popen(cmd, stdout=log_fh, stderr=sp.STDOUT,
                         start_new_session=True)
        log_fh.close()
        print(f"[octo] Daemon started (PID: {proc.pid}), log: {log}")
        return

    daemon = Daemon(relay, identity, tags, ssh=args.ssh,
                    cool_max_interval=getattr(args, 'cool_max_interval', None),
                    cool_idle_timeout=getattr(args, 'cool_idle_timeout', None))
    daemon.start()


def cmd_down(args):
    """Stop the daemon (identity preserved)."""
    # Find and kill the daemon process
    import subprocess as sp
    identity = get_identity()
    if not identity:
        print("[octo] Not registered.", file=sys.stderr)
        sys.exit(1)
    # Best-effort: look for the daemon process and send SIGTERM
    try:
        result = sp.run(
            ["pgrep", "-f", f"openocto.*join.*{identity}|openocto.*up"],
            capture_output=True, text=True,
        )
        pids = result.stdout.strip().split("\n")
        for pid in pids:
            if pid.strip():
                os.kill(int(pid.strip()), signal.SIGTERM)
                print(f"[octo] Sent SIGTERM to PID {pid.strip()}")
    except Exception:
        pass
    print(f"[octo] '{identity}' going offline (identity preserved).")


def cmd_invite(args):
    relay = _get_relay()
    terminal = args.terminal
    identity = get_identity()

    # Verify ownership
    id_info = relay.get_identity(identity)
    if not id_info:
        print("[octo] You are not registered.", file=sys.stderr)
        sys.exit(1)

    # Check that the terminal is registered under this identity
    # (For now, we trust the user — they need to own the daemon)

    if args.to:
        # Direct invite: grant role to a known device
        target_id = relay.get_identity(args.to)
        if not target_id:
            print(f"[octo] Device '{args.to}' is not registered.", file=sys.stderr)
            sys.exit(1)

        acl = relay.get_acl(terminal) or {}
        entry = {"role": args.role}
        if args.allow:
            entry["allow"] = [x.strip() for x in args.allow.split(",")]
        if args.deny:
            entry["deny"] = [x.strip() for x in args.deny.split(",")]
        acl[args.to] = entry
        relay.set_acl(terminal, acl)
        print(f"[octo] Granted '{args.to}' role '{args.role}' on '{terminal}'.")

    else:
        # Generate invite code for unknown users
        import secrets
        code = secrets.token_hex(6)  # 12-char hex code
        ttl = _parse_duration(args.expires) if args.expires else 604800  # default 7 days
        relay.create_invite(code, {
            "terminal": terminal,
            "role": args.role,
            "invited_by": identity,
            "created_at": int(time.time()),
        }, ttl=ttl)
        print(f"[octo] Invite code: {code}")
        print(f"[octo] Role: {args.role} on '{terminal}'")
        print(f"[octo] Expires in: {args.expires or '7d'}")
        print(f"[octo] Share this code. Recipient runs: octo accept {code}")


def cmd_accept(args):
    relay = _get_relay()
    code = args.code
    identity = get_identity()

    if not identity:
        print("[octo] Register first: octo register <name>", file=sys.stderr)
        sys.exit(1)

    # Atomically consume invite (prevents two people accepting the same code)
    invite = relay.consume_invite(code)
    if not invite:
        print(f"[octo] Invite code '{code}' not found or expired.", file=sys.stderr)
        sys.exit(1)

    terminal = invite["terminal"]
    role = invite["role"]

    # Add to ACL
    acl = relay.get_acl(terminal) or {}
    acl[identity] = {"role": role}
    relay.set_acl(terminal, acl)

    print(f"[octo] Accepted! You have '{role}' access to '{terminal}'.")


def cmd_acl(args):
    relay = _get_relay()
    terminal = args.terminal

    if args.default:
        acl = relay.get_acl(terminal) or {}
        acl["default"] = args.default
        relay.set_acl(terminal, acl)
        print(f"[octo] Default role for '{terminal}' set to: {args.default}")

    elif args.revoke:
        acl = relay.get_acl(terminal) or {}
        if args.revoke in acl:
            del acl[args.revoke]
            relay.set_acl(terminal, acl)
            print(f"[octo] Revoked '{args.revoke}' from '{terminal}'.")
        else:
            print(f"[octo] '{args.revoke}' not in ACL for '{terminal}'.")

    else:
        # Show ACL
        acl = relay.get_acl(terminal)
        if not acl:
            print(f"No ACL configured for '{terminal}' (all access allowed).")
            return
        import json as _json
        print(f"ACL for '{terminal}':")
        print(_json.dumps(acl, indent=2))


def _parse_duration(s: str) -> int:
    """Parse duration string like '7d', '24h', '30m' to seconds."""
    s = s.strip().lower()
    if s.endswith("d"):
        return int(s[:-1]) * 86400
    elif s.endswith("h"):
        return int(s[:-1]) * 3600
    elif s.endswith("m"):
        return int(s[:-1]) * 60
    return int(s)


def cmd_nearby(args):
    """Nearby P2P file transfer."""
    import asyncio
    from .nearby import cmd_scan, cmd_send, cmd_receive

    if args.action == "scan":
        asyncio.run(cmd_scan(args.timeout))
    elif args.action == "send":
        if not args.target:
            print("Usage: octo nearby send FILE")
            sys.exit(1)
        asyncio.run(cmd_send(args.target, args.port))
    elif args.action == "receive":
        if args.ble:
            asyncio.run(cmd_receive("", args.port, args.dir,
                                    use_ble=True, ble_address=args.ble))
        elif args.target:
            asyncio.run(cmd_receive(args.target, args.port, args.dir))
        else:
            print("Usage: octo nearby receive HOST")
            print("       octo nearby receive --ble ADDRESS")
            sys.exit(1)


# ---- CLI Parser ----

def main():
    parser = argparse.ArgumentParser(
        prog="octo",
        description="openOcto - Remote terminal control for AI agents",
    )
    sub = parser.add_subparsers(dest="subcmd")

    # setup — LLM-guided onboarding (replaces the old init wizard)
    sub.add_parser("setup", help="AI-guided setup wizard (recommended for new users)")

    # init
    sub.add_parser("init", help="Configure relay connection (manual)")

    # token
    p = sub.add_parser("token", help="Generate join token")
    p.add_argument("--qr", action="store_true", help="Display as QR code for mobile scanning")

    # join
    p = sub.add_parser("join", help="Register terminal and start daemon")
    p.add_argument("--name", "-n", required=True, help="Terminal name")
    p.add_argument("--tags", "-t", default="", help="Comma-separated tags")
    p.add_argument("--token", default=None, help="Join token")
    p.add_argument("--daemon", "-d", action="store_true", help="Run in background")
    p.add_argument("--ssh", default=None, help="SSH target (e.g. user@gpu-server)")
    p.add_argument("--log-min-seconds", type=int, default=None,
                   help="Auto-delete log if command finishes under N seconds (default: 10)")
    p.add_argument("--log-max-files", type=int, default=None,
                   help="Keep only N most recent log files per terminal (default: 20)")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Print wrapped commands and execution details")
    p.add_argument("--cool-max-interval", type=int, default=None,
                   help="Max polling interval in cool mode, seconds (default: 180)")
    p.add_argument("--cool-idle-timeout", type=int, default=None,
                   help="Idle time before entering cool mode, seconds (default: 300)")

    # ls
    sub.add_parser("ls", help="List all terminals")

    # run
    p = sub.add_parser("run", help="Execute command on remote terminal")
    p.add_argument("target", help="Target terminal name")
    p.add_argument("command", nargs="?", default=None, help="Command (or - for stdin)")
    p.add_argument("--nowait", action="store_true", help="Don't wait for result")
    p.add_argument("--timeout", type=int, default=None, help="Timeout in seconds (default: 3600)")
    p.add_argument("--no-log", action="store_true", help="Don't save output to log file on remote")
    p.add_argument("--notify", metavar="TERMINAL", default=None,
                   help="Send notification to TERMINAL when done (e.g. phone name)")
    p.add_argument("--notify-message", metavar="MSG", default="",
                   help="Custom notification message")

    # cat
    p = sub.add_parser("cat", help="Read a remote file")
    p.add_argument("target", help="Target terminal name")
    p.add_argument("path", help="File path on remote")
    p.add_argument("--offset", type=int, default=0, help="Start from line N")
    p.add_argument("--limit", type=int, default=2000, help="Max lines to read")

    # edit
    p = sub.add_parser("edit", help="Edit a remote file")
    p.add_argument("target", help="Target terminal name")
    p.add_argument("path", help="File path on remote")
    p.add_argument("--old", required=True, help="String to find")
    p.add_argument("--new", required=True, help="Replacement string")
    p.add_argument("--replace-all", action="store_true", help="Replace all occurrences")

    # glob
    p = sub.add_parser("glob", help="Search remote files by pattern")
    p.add_argument("target", help="Target terminal name")
    p.add_argument("pattern", help="Glob pattern (e.g. **/*.py)")
    p.add_argument("--path", default=None, help="Base directory")

    # grep
    p = sub.add_parser("grep", help="Search remote file contents")
    p.add_argument("target", help="Target terminal name")
    p.add_argument("pattern", help="Regex pattern")
    p.add_argument("--path", default=None, help="Directory to search")
    p.add_argument("--glob", dest="file_glob", default=None, help="File glob filter")

    # logs
    p = sub.add_parser("logs", help="View task output or log file from remote")
    p.add_argument("target", help="Target terminal name")
    p.add_argument("--task-id", default=None, help="Query one exact task ID")
    p.add_argument("-f", "--follow", action="store_true", help="Follow output of running task")
    p.add_argument("--tail", type=int, default=None, help="Show last N lines of log file")

    # kill
    p = sub.add_parser("kill", help="Kill running command on a terminal")
    p.add_argument("target", help="Target terminal name")
    p.add_argument("--task-id", default=None, help="Kill one exact task ID")

    # wake
    p = sub.add_parser("wake", help="Switch terminal to fast polling mode")
    p.add_argument("target", help="Target terminal name")

    # cool
    p = sub.add_parser("cool", help="Switch terminal to low-power polling mode")
    p.add_argument("target", help="Target terminal name")

    # rm
    p = sub.add_parser("rm", help="Remove terminal registration")
    p.add_argument("targets", nargs="+", help="Terminal name(s) to remove")

    # agent-md
    p = sub.add_parser("agent-md", help="Generate agent instructions file")
    p.add_argument("--agent", "-a", default=None,
                   choices=list(AGENT_PRESETS.keys()),
                   help="Target agent (claude/cursor/windsurf/copilot)")
    p.add_argument("--output", "-o", default=None, help="Custom output path (overrides --agent)")
    p.add_argument("--rule", "-r", dest="rules", action="append", default=[], help="Add custom rule (repeatable)")
    p.add_argument("--rules-file", "-f", default=None, help="File containing custom rules")
    p.add_argument("--sync", default=None,
                   choices=["direct", "git", "rsync", "custom", "none"],
                   help="Code sync strategy: direct (default), git, rsync, custom, none")
    p.add_argument("--sync-rules", default=None,
                   help="Custom sync rules text (use with --sync custom)")
    p.add_argument("--sync-rules-file", default=None,
                   help="File containing custom sync rules (use with --sync custom)")
    p.add_argument("--show", action="store_true",
                   help="Print current effective agent-md content without writing")

    # send
    p = sub.add_parser("send", help="Transfer a file between terminals")
    p.add_argument("source", help="Source terminal name (or 'local')")
    p.add_argument("target", help="Target terminal name")
    p.add_argument("file", help="File path on source")
    p.add_argument("--dest", default=None, help="Destination path on target")

    # clip
    p = sub.add_parser("clip", help="Clipboard sync between terminals")
    p.add_argument("target", help="Target terminal name")
    p.add_argument("text", nargs="?", default=None, help="Text to write (omit to read)")
    p.add_argument("--paste", "-p", action="store_true",
                   help="Read remote clipboard and write to local clipboard")

    # inbox
    p = sub.add_parser("inbox", help="View inbox of a terminal")
    p.add_argument("target", help="Target terminal name")

    # metrics
    p = sub.add_parser("metrics", help="Show GPU status and training metrics")
    p.add_argument("target", nargs="?", default=None, help="Target terminal (omit for dashboard)")
    p.add_argument("--logdir", default="", help="TensorBoard log directory (auto-detected if omitted)")
    p.add_argument("--tail", type=int, default=20, help="Number of recent data points (default: 20)")
    p.add_argument("--dashboard", "-d", action="store_true", help="Open web dashboard")
    p.add_argument("--port", type=int, default=9530, help="Dashboard port (default: 9530)")

    # config
    p = sub.add_parser("config", help="View or update configuration")
    p.add_argument("--storage", action="store_true", help="Configure cloud storage")
    p.add_argument("--show", action="store_true", help="Show current config")

    # agent (interactive)
    p = sub.add_parser("agent", help="Start interactive AI agent with phone sync")
    p.add_argument("--backend", "-b", default="auto",
                   help="Backend: auto, claude, codex, anthropic, openai, openrouter, deepseek, siliconflow, qwen, nvidia, ollama")
    p.add_argument("--model", "-m", default=None, help="Model name override")
    p.add_argument("--name", "-n", default=None, help="Session name for phone sync")
    p.add_argument("args", nargs="*", default=[], help="Extra arguments passed to backend (e.g. claude flags)")

    # agent-serve
    p = sub.add_parser("agent-serve", help="Start AI agent daemon")
    p.add_argument("--name", "-n", default=None, help="Agent name (default: hostname_agent)")
    p.add_argument("--backend", default=None,
                   choices=["claude-cli", "codex-cli", "anthropic-api", "openai-api", "ollama"],
                   help="AI backend to use (default: auto-detect)")
    p.add_argument("--model", default=None, help="Model name override")

    # agent-ls
    sub.add_parser("agent-ls", help="List online AI agent nodes")

    # agent-prefer
    p = sub.add_parser("agent-prefer", help="Set preferred AI agent")
    p.add_argument("name", help="Agent name to prefer")

    # companion
    p = sub.add_parser("companion", help="Start desktop companion (wake input agent)")
    p.add_argument("--name", "-n", default=None, help="Terminal name (default: hostname)")

    # mcp-server
    sub.add_parser("mcp-server", help="Start MCP server (for AI agent integration)")

    # nearby
    p = sub.add_parser("nearby", help="Nearby P2P file transfer (no internet needed)")
    p.add_argument("action", choices=["scan", "send", "receive"],
                   help="scan: find devices | send: send file | receive: receive file")
    p.add_argument("target", nargs="?", default="",
                   help="File path (send) or sender IP (receive)")
    p.add_argument("-p", "--port", type=int, default=9528, help="TCP port")
    p.add_argument("-d", "--dir", default=".", help="Save directory (receive)")
    p.add_argument("-t", "--timeout", type=float, default=10.0, help="BLE scan timeout")
    p.add_argument("--ble", default="", help="BLE address for full BLE flow (receive)")

    # network
    p = sub.add_parser("network", help="Manage octo networks")
    p.add_argument("action", choices=["create", "info", "set-mode"],
                   help="Action: create, info, set-mode")
    p.add_argument("name", nargs="?", default=None, help="Network name (for create)")
    p.add_argument("--public", action="store_true", help="Create as public network (permissions enforced)")
    p.add_argument("--mode", choices=["personal", "public"], help="New mode (for set-mode)")

    # register
    p = sub.add_parser("register", help="Register device identity on the network")
    p.add_argument("name", help="Unique device name")

    # unregister
    p = sub.add_parser("unregister", help="Unregister device identity (release name)")
    p.add_argument("name", help="Device name to unregister")

    # up
    p = sub.add_parser("up", help="Start daemon and go online")
    p.add_argument("--tags", "-t", default="", help="Comma-separated tags")
    p.add_argument("--daemon", "-d", action="store_true", help="Run in background")
    p.add_argument("--ssh", default=None, help="SSH target")
    p.add_argument("--cool-max-interval", type=int, default=None,
                   help="Max polling interval in cool mode, seconds (default: 180)")
    p.add_argument("--cool-idle-timeout", type=int, default=None,
                   help="Idle time before entering cool mode, seconds (default: 300)")

    # down
    sub.add_parser("down", help="Stop daemon and go offline (identity preserved)")

    # invite
    p = sub.add_parser("invite", help="Invite a device to access your terminal")
    p.add_argument("terminal", help="Your terminal name to grant access to")
    p.add_argument("--to", default=None, help="Target device name (for direct invite)")
    p.add_argument("--role", default="readonly",
                   choices=["full", "readwrite", "readonly"],
                   help="Permission level (default: readonly)")
    p.add_argument("--allow", default=None, help="Extra ops to allow (comma-separated)")
    p.add_argument("--deny", default=None, help="Ops to deny (comma-separated)")
    p.add_argument("--expires", default=None, help="Expiry duration, e.g. 7d, 24h (for invite codes)")

    # accept
    p = sub.add_parser("accept", help="Accept an invite code")
    p.add_argument("code", help="Invite code")

    # acl
    p = sub.add_parser("acl", help="View or manage terminal ACL")
    p.add_argument("terminal", help="Terminal name")
    p.add_argument("--default", default=None,
                   choices=["full", "readwrite", "readonly", "none"],
                   help="Set default role for all devices")
    p.add_argument("--revoke", default=None, help="Revoke a device's access")

    args = parser.parse_args()

    if not args.subcmd:
        parser.print_help()
        sys.exit(1)

    cmds = {
        "setup": cmd_setup,
        "init": cmd_init,
        "token": cmd_token,
        "join": cmd_join,
        "ls": cmd_ls,
        "run": cmd_run,
        "cat": cmd_cat,
        "edit": cmd_edit,
        "glob": cmd_glob,
        "grep": cmd_grep,
        "logs": cmd_logs,
        "kill": cmd_kill,
        "wake": cmd_wake,
        "cool": cmd_cool,
        "rm": cmd_rm,
        "send": cmd_send,
        "clip": cmd_clip,
        "inbox": cmd_inbox,
        "metrics": cmd_metrics,
        "config": cmd_config,
        "agent-md": cmd_agent_md,
        "agent": cmd_agent,
        "agent-serve": cmd_agent_serve,
        "agent-ls": cmd_agent_ls,
        "agent-prefer": cmd_agent_prefer,
        "companion": cmd_companion,
        "mcp-server": cmd_mcp_server,
        "network": cmd_network,
        "register": cmd_register,
        "unregister": cmd_unregister,
        "up": cmd_up,
        "down": cmd_down,
        "invite": cmd_invite,
        "accept": cmd_accept,
        "acl": cmd_acl,
        "nearby": cmd_nearby,
    }
    cmds[args.subcmd](args)


if __name__ == "__main__":
    main()
