"""octo agent — interactive AI agent with phone sync.

Launches the built-in agent by default, or Claude Code CLI via --backend claude.

Usage:
    octo agent                      # built-in agent (auto-detect API key)
    octo agent --backend claude     # Claude Code CLI in tmux
    octo agent --backend openrouter # built-in with OpenRouter
"""

import os
import shutil
import signal
import subprocess
import sys
import threading
import time

from .relay import Relay, RelayError
from .config import get_relay_config, load_config


TMUX_SESSION = "octo-agent"
SHARE_INTERVAL = 2
CAPTURE_LINES = 80


def _has(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _tmux(*args, capture=False) -> subprocess.CompletedProcess:
    cmd = ["tmux"] + list(args)
    return subprocess.run(cmd, capture_output=capture, text=True)


def _session_exists() -> bool:
    r = _tmux("has-session", "-t", TMUX_SESSION, capture=True)
    return r.returncode == 0


def _create_session(command: str):
    """Create a detached tmux session running command."""
    # Get terminal size for tmux window
    try:
        cols, rows = os.get_terminal_size()
    except OSError:
        cols, rows = 120, 40

    _tmux("new-session", "-d", "-s", TMUX_SESSION,
          "-x", str(cols), "-y", str(rows),
          command)


def _capture_pane() -> str:
    """Capture current tmux pane content."""
    r = _tmux("capture-pane", "-t", TMUX_SESSION, "-p",
              "-S", str(-CAPTURE_LINES), capture=True)
    return r.stdout


def _send_keys(text: str):
    """Inject text into the tmux session."""
    # Use literal flag to avoid tmux key name interpretation
    _tmux("send-keys", "-t", TMUX_SESSION, "-l", text)
    _tmux("send-keys", "-t", TMUX_SESSION, "Enter")


def _share_loop(relay: Relay, session_name: str):
    """Background thread: sync terminal content with phone via relay."""
    output_key = relay._key("session", session_name, "output")
    input_key = relay._key("session", session_name, "input")
    meta_key = relay._key("session", session_name, "meta")

    # Set session metadata + register in global sessions list
    try:
        import json
        meta = json.dumps({
            "status": "active",
            "started": int(time.time()),
            "backend": _current_backend,
        })
        relay._request("SET", meta_key, meta)
        relay._request("EXPIRE", meta_key, "86400")
        relay._request("HSET", relay._key("sessions"), session_name, meta)
    except RelayError:
        pass

    while _sharing:
        try:
            if not _session_exists():
                break

            # Push screen content
            content = _capture_pane()
            if content.strip():
                relay._request("SET", output_key, content)
                relay._request("EXPIRE", output_key, "30")

            # Poll for phone input
            raw = relay._request("RPOP", input_key)
            if raw and isinstance(raw, str) and raw.strip():
                _send_keys(raw.strip())

        except RelayError:
            pass
        except Exception:
            pass

        time.sleep(SHARE_INTERVAL)

    # Mark session ended + remove from global list
    try:
        import json
        meta = json.dumps({"status": "ended", "ended": int(time.time())})
        relay._request("SET", meta_key, meta)
        relay._request("EXPIRE", meta_key, "300")
        relay._request("DEL", output_key)
        relay._request("HDEL", relay._key("sessions"), session_name)
    except Exception:
        pass


# Module-level state for the share thread
_sharing = False
_current_backend = ""


def run_agent(backend: str = "auto", model: str = None, name: str = None,
              extra_args: str = None):
    global _sharing, _current_backend

    # Auto / builtin path — no tmux needed
    if backend == "auto":
        backend = "builtin"
        if _has("claude"):
            print("[octo] Tip: Claude Code CLI detected. Use --backend claude for Claude Code.")

    if backend in ("builtin", "anthropic", "openai", "openrouter",
                    "deepseek", "siliconflow", "qwen", "nvidia", "ollama"):
        from .agent_builtin import run_builtin_agent
        llm_backend = backend if backend != "builtin" else "auto"
        run_builtin_agent(model=model, backend=llm_backend)
        return

    # Claude Code CLI path — needs tmux
    if backend == "claude":
        if not _has("tmux"):
            print("[octo] tmux is required for Claude Code session sharing.")
            print("  Install: brew install tmux  (macOS)")
            print("           apt install tmux   (Linux)")
            print("           pacman -S tmux     (Arch)")
            sys.exit(1)
        if not _has("claude"):
            print("[octo] Claude Code CLI not found.")
            print("  Install: https://claude.ai/code")
            sys.exit(1)
        command = "claude"
        if model:
            command += f" --model {model}"
        if extra_args:
            command += f" {extra_args}"
    else:
        # Treat as arbitrary command (needs tmux)
        if not _has("tmux"):
            print("[octo] tmux is required for custom backend session sharing.")
            sys.exit(1)
        command = backend

    _current_backend = backend

    # Kill stale session if exists
    if _session_exists():
        print(f"[octo] Cleaning up stale session '{TMUX_SESSION}'...")
        _tmux("kill-session", "-t", TMUX_SESSION)
        time.sleep(0.5)

    # Setup relay
    relay = None
    session_name = name
    try:
        rc = get_relay_config()
        relay = Relay(rc["redis_url"], rc["redis_token"], rc["workspace"],
                      proxy_url=rc.get("proxy_url", ""))
        if not session_name:
            config = load_config()
            # Use terminal name or hostname
            session_name = config.get("terminal_name", "")
            if not session_name:
                import platform
                session_name = platform.node().split(".")[0].lower().replace(" ", "-")
    except Exception:
        pass

    # Create tmux session with the command
    print(f"[octo] Starting {backend}...")
    _create_session(command)

    if not _session_exists():
        print("[octo] Failed to create tmux session.")
        sys.exit(1)

    # Start sharing thread
    if relay and session_name:
        _sharing = True
        share_thread = threading.Thread(
            target=_share_loop,
            args=(relay, session_name),
            daemon=True,
        )
        share_thread.start()
        print(f"[octo] Session shared as '{session_name}'. View from phone: Devices → Live")
    else:
        print("[octo] Relay not configured, phone sync disabled.")

    print(f"[octo] Attaching to session (Ctrl+B D to detach, exit to quit)\n")

    # Attach to tmux (blocks until user exits or detaches)
    try:
        os.system(f"tmux attach -t {TMUX_SESSION}")
    except KeyboardInterrupt:
        pass

    # Cleanup
    _sharing = False
    time.sleep(0.5)

    # Kill tmux session if it's still running
    if _session_exists():
        _tmux("kill-session", "-t", TMUX_SESSION)

    print("[octo] Session ended.")
