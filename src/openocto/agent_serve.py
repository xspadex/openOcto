"""openOcto Agent Serve — AI agent daemon that processes natural language requests.

Registers as a special terminal with ai_agent=true, polls for ai_request tasks,
calls AI backend (Claude CLI, Anthropic API, OpenAI-compatible API, Ollama),
executes octo commands via tool use, and streams results back.

Usage:
    octo agent-serve                    # auto-detect backend
    octo agent-serve --backend anthropic-api --model claude-sonnet-4-20250514
    octo agent-serve --name my_agent
"""

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
from typing import Any, Optional

from .relay import Relay, RelayError


# ---- Constants ----

MAX_OUTPUT = 200_000
MAX_TOOL_ROUNDS = 8
TOOL_TIMEOUT = 120  # seconds per tool call
POLL_COOL = 5  # seconds between polls when idle
HEARTBEAT_INTERVAL = 30


# ---- Backend Detection ----

def detect_backends() -> list:
    """Auto-detect available AI backends on this machine."""
    backends = []

    if shutil.which("claude"):
        backends.append({"type": "claude-cli", "label": "Claude Code CLI"})

    if os.environ.get("ANTHROPIC_API_KEY"):
        backends.append({"type": "anthropic-api", "label": "Anthropic API"})

    if os.environ.get("OPENAI_API_KEY"):
        backends.append({"type": "openai-api", "label": "OpenAI API"})

    if os.environ.get("GEMINI_API_KEY"):
        backends.append({"type": "openai-api", "label": "Gemini API (OpenAI compat)"})

    # Check Ollama
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(("localhost", 11434))
        s.close()
        backends.append({"type": "ollama", "label": "Ollama (local)"})
    except (OSError, socket.error):
        pass

    return backends


# ---- Tool Definitions ----

TOOLS = [
    {
        "name": "list_terminals",
        "description": "List all registered terminals with their online/offline status, platform, shell, and tags.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "run_command",
        "description": "Execute a shell command on a remote terminal. Use bash for Linux/Mac, powershell for Windows.",
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Terminal name"},
                "command": {"type": "string", "description": "Shell command"},
            },
            "required": ["target", "command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file on a remote terminal with line numbers.",
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Terminal name"},
                "path": {"type": "string", "description": "File path"},
            },
            "required": ["target", "path"],
        },
    },
    {
        "name": "edit_file",
        "description": "Edit a file on a remote terminal by replacing text.",
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Terminal name"},
                "path": {"type": "string", "description": "File path"},
                "old_text": {"type": "string", "description": "Text to find"},
                "new_text": {"type": "string", "description": "Replacement text"},
            },
            "required": ["target", "path", "old_text", "new_text"],
        },
    },
    {
        "name": "search_files",
        "description": "Search for files by glob pattern on a remote terminal.",
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Terminal name"},
                "pattern": {"type": "string", "description": "Glob pattern (e.g. **/*.py)"},
                "path": {"type": "string", "description": "Base directory (optional)"},
            },
            "required": ["target", "pattern"],
        },
    },
    {
        "name": "search_content",
        "description": "Search file contents by regex on a remote terminal.",
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Terminal name"},
                "pattern": {"type": "string", "description": "Regex pattern"},
                "path": {"type": "string", "description": "Directory to search (optional)"},
            },
            "required": ["target", "pattern"],
        },
    },
    {
        "name": "clipboard_read",
        "description": "Read clipboard content from a remote terminal.",
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Terminal name"},
            },
            "required": ["target"],
        },
    },
    {
        "name": "clipboard_write",
        "description": "Write text to clipboard on a remote terminal.",
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Terminal name"},
                "text": {"type": "string", "description": "Text to write"},
            },
            "required": ["target", "text"],
        },
    },
]


def _claude_tools():
    """Convert tools to Claude API format."""
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "input_schema": t["parameters"],
        }
        for t in TOOLS
    ]


def _openai_tools():
    """Convert tools to OpenAI API format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
        for t in TOOLS
    ]


# ---- System Prompt ----

def build_system_prompt(relay: Relay, self_name: str) -> str:
    terminals = relay.list_terminals()
    terminal_info = []
    for t in terminals:
        name = t.get("name", "?")
        if name == self_name:
            continue  # don't list self
        online = "online" if t.get("online") else "offline"
        meta = t.get("meta", {})
        plat = meta.get("platform", "")
        shell = meta.get("shell", "")
        tags = ", ".join(t.get("tags", []))
        terminal_info.append(f"- {name} ({online}, {plat}, {shell}) [{tags}]")

    terminals_str = "\n".join(terminal_info) if terminal_info else "No terminals available."

    return f"""You are an AI assistant integrated into openOcto, a cross-device control system.
You can execute commands and manage files on any registered terminal via tool calls.

Available terminals:
{terminals_str}

Guidelines:
- Use the correct shell syntax: bash for Linux/Mac, powershell for Windows.
- For file operations, prefer read_file/edit_file over running cat/sed commands.
- Be concise. Summarize command output rather than showing it raw.
- If a terminal is offline, tell the user.
- Respond in the same language as the user's message."""


# ---- Tool Execution ----

def execute_tool(relay: Relay, self_name: str, name: str, args: dict) -> str:
    """Execute an octo tool and return the result as a string."""
    try:
        if name == "list_terminals":
            terminals = relay.list_terminals()
            lines = []
            for t in terminals:
                n = t.get("name", "?")
                if n == self_name:
                    continue
                online = "online" if t.get("online") else "offline"
                meta = t.get("meta", {})
                lines.append(f"{n} [{online}] platform={meta.get('platform','')} shell={meta.get('shell','')} tags={','.join(t.get('tags',[]))}")
            return "\n".join(lines) or "No terminals."

        elif name == "run_command":
            return _submit_and_wait(relay, args["target"], "shell", {"command": args["command"]})

        elif name == "read_file":
            return _submit_and_wait(relay, args["target"], "cat", {"path": args["path"]})

        elif name == "edit_file":
            return _submit_and_wait(relay, args["target"], "edit", {
                "path": args["path"], "old": args["old_text"], "new": args["new_text"]
            })

        elif name == "search_files":
            params = {"pattern": args["pattern"]}
            if args.get("path"):
                params["path"] = args["path"]
            return _submit_and_wait(relay, args["target"], "glob", params)

        elif name == "search_content":
            params = {"pattern": args["pattern"]}
            if args.get("path"):
                params["path"] = args["path"]
            return _submit_and_wait(relay, args["target"], "grep", params)

        elif name == "clipboard_read":
            return _submit_and_wait(relay, args["target"], "clipboard_read", {})

        elif name == "clipboard_write":
            return _submit_and_wait(relay, args["target"], "clipboard_write", {"text": args["text"]})

        else:
            return f"Unknown tool: {name}"

    except Exception as e:
        return f"Error: {e}"


def _submit_and_wait(relay: Relay, target: str, task_type: str, kwargs: dict) -> str:
    """Submit a task to a terminal and wait for completion."""
    relay.submit_task(target, task_type=task_type, **kwargs)
    elapsed = 0
    while elapsed < TOOL_TIMEOUT:
        time.sleep(1)
        elapsed += 1
        task = relay.poll_task(target)
        if not task:
            return "[Task disappeared]"
        if task.get("status") in ("DONE", "FAILED"):
            output = task.get("output", "")
            exit_code = task.get("exit_code", -1)
            relay.clear_task(target)
            # Truncate long output for AI context
            if len(output) > 10000:
                output = output[:5000] + "\n...[truncated]...\n" + output[-3000:]
            if exit_code != 0 and not output:
                return f"[Failed with exit code {exit_code}]"
            return output
    return f"[Timeout after {TOOL_TIMEOUT}s]"


# ---- AI Backends ----

def _api_request(url: str, headers: dict, body: dict) -> dict:
    """Make a JSON POST request using stdlib."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    req.add_header("Content-Type", "application/json")

    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


def process_anthropic(relay: Relay, self_name: str, prompt: str,
                      model: str = None, on_output=None) -> str:
    """Process request via Anthropic Messages API with tool use loop."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    model = model or "claude-sonnet-4-20250514"
    system = build_system_prompt(relay, self_name)
    messages = [{"role": "user", "content": prompt}]

    for round_num in range(MAX_TOOL_ROUNDS):
        body = {
            "model": model,
            "max_tokens": 4096,
            "system": system,
            "tools": _claude_tools(),
            "messages": messages,
        }

        resp = _api_request(
            "https://api.anthropic.com/v1/messages",
            {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            body,
        )

        content = resp.get("content", [])
        stop_reason = resp.get("stop_reason", "end_turn")

        # Collect text and tool_use blocks
        text_parts = []
        tool_uses = []
        for block in content:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_uses.append(block)

        # Stream partial output
        if text_parts and on_output:
            on_output("\n".join(text_parts))

        if not tool_uses or stop_reason == "end_turn":
            return "\n".join(text_parts)

        # Add assistant message
        messages.append({"role": "assistant", "content": content})

        # Execute tools
        results = []
        for tu in tool_uses:
            tool_name = tu["name"]
            tool_input = tu.get("input", {})
            if on_output:
                on_output(f"\n[Tool: {tool_name}({json.dumps(tool_input, ensure_ascii=False)[:80]})]")
            result = execute_tool(relay, self_name, tool_name, tool_input)
            results.append({
                "type": "tool_result",
                "tool_use_id": tu["id"],
                "content": result,
            })

        messages.append({"role": "user", "content": results})

    return "[Max tool rounds exceeded]"


def process_openai(relay: Relay, self_name: str, prompt: str,
                   model: str = None, base_url: str = None, api_key: str = None,
                   on_output=None) -> str:
    """Process request via OpenAI-compatible API with tool use loop."""
    api_key = api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
    base_url = base_url or "https://api.openai.com/v1"

    # Auto-detect Gemini
    if os.environ.get("GEMINI_API_KEY") and not os.environ.get("OPENAI_API_KEY"):
        base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
        model = model or "gemini-2.0-flash"
    else:
        model = model or "gpt-4o-mini"

    system = build_system_prompt(relay, self_name)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]

    for round_num in range(MAX_TOOL_ROUNDS):
        body = {
            "model": model,
            "messages": messages,
            "tools": _openai_tools(),
        }

        resp = _api_request(
            f"{base_url.rstrip('/')}/chat/completions",
            {"Authorization": f"Bearer {api_key}"},
            body,
        )

        choice = resp.get("choices", [{}])[0]
        msg = choice.get("message", {})
        tool_calls = msg.get("tool_calls")

        if not tool_calls:
            text = msg.get("content", "")
            if on_output:
                on_output(text)
            return text

        # Add assistant message
        messages.append(msg)

        # Execute tool calls
        for tc in tool_calls:
            fn = tc.get("function", {})
            tool_name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {}
            call_id = tc.get("id", "")

            if on_output:
                on_output(f"\n[Tool: {tool_name}({json.dumps(args, ensure_ascii=False)[:80]})]")

            result = execute_tool(relay, self_name, tool_name, args)
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": result,
            })

    return "[Max tool rounds exceeded]"


def process_claude_cli(relay: Relay, self_name: str, prompt: str,
                       on_output=None) -> str:
    """Process request via claude CLI -p mode (single-turn, no tool use)."""
    try:
        result = subprocess.run(
            ["claude", "-p"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = result.stdout.strip()
        if on_output:
            on_output(output)
        return output
    except subprocess.TimeoutExpired:
        return "[claude CLI timed out]"
    except Exception as e:
        return f"[claude CLI error: {e}]"


def process_ollama(relay: Relay, self_name: str, prompt: str,
                   model: str = None, on_output=None) -> str:
    """Process request via Ollama (OpenAI-compatible mode, no tool use)."""
    model = model or "llama3"
    system = build_system_prompt(relay, self_name)

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }

    resp = _api_request(
        "http://localhost:11434/v1/chat/completions",
        {},
        body,
    )

    text = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
    if on_output:
        on_output(text)
    return text


# ---- Main Agent Daemon ----

def run_agent_serve(relay: Relay = None, name: str = None,
                    backend: str = None, model: str = None):
    """Main entry point for the agent-serve daemon."""
    from .config import get_relay_config

    if not relay:
        rc = get_relay_config()
        relay = Relay(rc["redis_url"], rc["redis_token"], rc["workspace"],
                      proxy_url=rc.get("proxy_url", ""))

    if not name:
        name = platform.node().split(".")[0].lower().replace(" ", "_") + "_agent"

    # Detect backends
    backends = detect_backends()
    if backend:
        backends = [b for b in backends if b["type"] == backend]
    if not backends:
        print(f"[agent] No AI backend available.", file=sys.stderr)
        if not backend:
            print(f"[agent] Install claude CLI, set ANTHROPIC_API_KEY, OPENAI_API_KEY, or run Ollama.", file=sys.stderr)
        sys.exit(1)

    active_backend = backends[0]
    print(f"[agent] Starting as '{name}'")
    print(f"[agent] Backend: {active_backend['label']} ({active_backend['type']})")
    if model:
        print(f"[agent] Model: {model}")

    # Register as AI agent terminal
    relay.register(name, tags=["ai"], meta={
        "platform": sys.platform,
        "ai_agent": True,
        "ai_backend": active_backend["type"],
        "ai_label": active_backend["label"],
    })
    relay.set_mode(name, "wake")
    print(f"[agent] Registered. Waiting for ai_request tasks...")

    # Heartbeat thread
    import threading
    stop_event = threading.Event()

    def heartbeat_loop():
        while not stop_event.is_set():
            try:
                relay.heartbeat(name)
            except Exception:
                pass
            stop_event.wait(HEARTBEAT_INTERVAL)

    hb = threading.Thread(target=heartbeat_loop, daemon=True)
    hb.start()

    # Poll loop
    try:
        while True:
            try:
                task = relay.poll_task(name)
                if task and task.get("status") == "PENDING":
                    task_type = task.get("type", "")

                    if task_type == "ai_request":
                        prompt = task.get("prompt", task.get("message", ""))
                        if not prompt:
                            relay.complete_task(name, "No prompt provided.", 1)
                            continue

                        relay.update_task(name, {"status": "RUNNING"})
                        print(f"[agent] Processing: {prompt[:80]}...")

                        output_parts = []

                        def on_output(text):
                            output_parts.append(text)
                            combined = "\n".join(output_parts)
                            if len(combined) > MAX_OUTPUT:
                                combined = combined[-MAX_OUTPUT:]
                            try:
                                relay.update_task(name, {"output": combined})
                            except Exception:
                                pass

                        try:
                            bt = active_backend["type"]
                            if bt == "anthropic-api":
                                result = process_anthropic(relay, name, prompt, model=model, on_output=on_output)
                            elif bt == "openai-api":
                                result = process_openai(relay, name, prompt, model=model, on_output=on_output)
                            elif bt == "claude-cli":
                                result = process_claude_cli(relay, name, prompt, on_output=on_output)
                            elif bt == "ollama":
                                result = process_ollama(relay, name, prompt, model=model, on_output=on_output)
                            else:
                                result = f"Unknown backend: {bt}"

                            relay.complete_task(name, result, 0)
                            print(f"[agent] Done ({len(result)} chars)")
                        except Exception as e:
                            relay.complete_task(name, f"Agent error: {e}", 1)
                            print(f"[agent] Error: {e}", file=sys.stderr)
                    else:
                        # Not an ai_request, ignore
                        relay.complete_task(name, f"Agent only handles ai_request tasks, got: {task_type}", 1)

                else:
                    time.sleep(POLL_COOL)

            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"[agent] Poll error: {e}", file=sys.stderr)
                time.sleep(5)

    except KeyboardInterrupt:
        print("\n[agent] Stopping...")
    finally:
        stop_event.set()
        try:
            relay.unregister(name)
        except Exception:
            pass
        print("[agent] Unregistered.")
