"""OpenOcto Built-in Agent — interactive AI agent with tool use, streaming, and context compaction.

Architecture modeled after Claude Code's query loop:
- Async-style conversation loop with multi-turn tool use
- Streaming API output with real-time display
- Concurrent read-only tools, serial write tools
- Auto-compaction when context exceeds token budget
- Multiple LLM backend support (Anthropic, OpenAI-compat, Ollama)

Usage:
    octo agent                                  # auto-detect API key
    octo agent --backend anthropic              # use Anthropic API
    octo agent --backend openrouter --model anthropic/claude-sonnet-4
"""

import json
import os
import readline  # noqa: F401 — enables input() line editing
import shlex
import shutil
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

from .relay import Relay, RelayError
from .config import get_relay_config, load_config


# ---- Constants ----

MAX_TOOL_ROUNDS = 15
MAX_CONCURRENT_TOOLS = 8
STREAM_IDLE_TIMEOUT = 90  # seconds
DEFAULT_MAX_TOKENS = 16384
COMPACT_MAX_OUTPUT_TOKENS = 16384
COMPACT_BUFFER_TOKENS = 10000
CHARS_PER_TOKEN = 4  # rough estimate

# ---- LLM Providers ----
# Two protocol types: "anthropic" (Messages API) and "openai_compat" (chat/completions).
# Every provider maps to one of these. To add a new provider, just add an entry here.

PROVIDERS = {
    "anthropic": {
        "type": "anthropic",
        "key_env": "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-4-20250514",
    },
    "openai": {
        "type": "openai_compat",
        "base_url": "https://api.openai.com/v1",
        "key_env": "OPENAI_API_KEY",
        "default_model": "gpt-4o",
    },
    "openrouter": {
        "type": "openai_compat",
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
        "default_model": "anthropic/claude-sonnet-4",
    },
    "deepseek": {
        "type": "openai_compat",
        "base_url": "https://api.deepseek.com/v1",
        "key_env": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-chat",
    },
    "siliconflow": {
        "type": "openai_compat",
        "base_url": "https://api.siliconflow.cn/v1",
        "key_env": "SILICONFLOW_API_KEY",
        "default_model": "Qwen/Qwen2.5-72B-Instruct",
    },
    "qwen": {
        "type": "openai_compat",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "key_env": "DASHSCOPE_API_KEY",
        "default_model": "qwen-plus",
    },
    "nvidia": {
        "type": "openai_compat",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "key_env": "NVIDIA_API_KEY",
        "default_model": "moonshotai/kimi-k2-instruct",
    },
    "ollama": {
        "type": "openai_compat",
        "base_url": "http://localhost:11434/v1",
        "key_env": None,  # no key needed
        "default_model": "qwen2.5:14b",
    },
}

# Community free tier: LLM requests proxied through the public CF Worker.
# NVIDIA API key is stored server-side in the Worker's secrets,
# never exposed to clients.
_COMMUNITY_LLM_URL = "https://openocto-relay.openocto.workers.dev"

# Auto-detection priority order
_AUTO_DETECT_ORDER = [
    "anthropic", "openai", "openrouter", "deepseek",
    "siliconflow", "qwen", "ollama",
]


def resolve_provider(backend: str = "auto", model: str = None) -> dict:
    """Resolve LLM provider config. Returns dict with keys:
    type, base_url (if openai_compat), api_key, model, provider_name.
    """
    config = load_config()
    llm_config = config.get("llm", {})

    def _try_provider(name: str) -> dict | None:
        prov = PROVIDERS.get(name)
        if not prov:
            return None
        key_env = prov.get("key_env")
        # env var > config > skip
        api_key = ""
        if key_env:
            api_key = os.environ.get(key_env, "")
            if not api_key and llm_config.get("provider") == name:
                api_key = llm_config.get("api_key", "")
            if not api_key and name != "ollama":
                return None
        result = {
            "type": prov["type"],
            "api_key": api_key,
            "model": model or prov["default_model"],
            "provider_name": name,
        }
        if prov.get("base_url"):
            result["base_url"] = prov["base_url"]
        # OPENAI_BASE_URL override for openai provider
        if name == "openai" and os.environ.get("OPENAI_BASE_URL"):
            result["base_url"] = os.environ["OPENAI_BASE_URL"]
        return result

    if backend != "auto":
        result = _try_provider(backend)
        if result:
            return result
        raise RuntimeError(
            f"Provider '{backend}' not available. "
            f"Set {PROVIDERS[backend]['key_env']} or configure via: octo init --llm"
            if backend in PROVIDERS else f"Unknown provider: {backend}"
        )

    # Auto-detect: config preference first
    if llm_config.get("provider"):
        result = _try_provider(llm_config["provider"])
        if result:
            return result

    # Then try each provider in priority order
    for name in _AUTO_DETECT_ORDER:
        if name == "ollama":
            # Quick probe — don't hang if ollama isn't running
            import socket
            try:
                s = socket.create_connection(("127.0.0.1", 11434), timeout=0.5)
                s.close()
            except (OSError, ConnectionRefusedError):
                continue
        result = _try_provider(name)
        if result:
            return result

    # Last resort: community free tier (proxied through public CF Worker)
    print(f"{C_YELLOW}[octo] No API key found. Using free community LLM.{C_RESET}")
    print(f"{C_DIM}  Shared quota — set your own API key for unlimited use.{C_RESET}")
    return {
        "type": "openai_compat",
        "base_url": _COMMUNITY_LLM_URL,
        "api_key": "community",  # placeholder, Worker injects real key
        "model": model or "moonshotai/Kimi-K2-Instruct",
        "provider_name": "community",
        "chat_endpoint": "/llm/infer",
    }

    raise RuntimeError(
        "No LLM API key found. Set one of:\n"
        "  ANTHROPIC_API_KEY    — Anthropic (Claude)\n"
        "  OPENAI_API_KEY       — OpenAI (GPT)\n"
        "  OPENROUTER_API_KEY   — OpenRouter (multi-model)\n"
        "  DEEPSEEK_API_KEY     — DeepSeek\n"
        "  SILICONFLOW_API_KEY  — SiliconFlow (硅基流动)\n"
        "  DASHSCOPE_API_KEY    — Qwen (通义千问)\n"
        "  NVIDIA_API_KEY       — NVIDIA Build (Kimi K2.5)\n"
        "  Or start Ollama locally: ollama serve"
    )


# ANSI colors
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_CYAN = "\033[36m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_RED = "\033[31m"
C_MAGENTA = "\033[35m"
C_BLUE = "\033[34m"
C_DARK_GREY = "\033[90m"
# Code block: dark grey bg (#303030) + light grey fg (#d0d0d0) so text is readable
_CODE_BG   = "\033[48;5;236m"
_CODE_FG   = "\033[38;5;252m"
_CODE_RESET = "\033[0m"
_ERASE_LINE = "\r\033[K"      # carriage-return + erase to end of line
# User message bubble: visible medium grey (clearly distinct from black terminal bg)
_USER_BG   = "\033[48;5;241m"  # #626262 — clearly grey in any dark terminal
_USER_FG   = "\033[38;5;255m"  # near-white text

# Octopus ASCII art — brand mark shown in the welcome header
_OCTO_ART = (
    r"      ___      ",
    r"   __/ o \__   ",
    r"  /   ___   \  ",
    r"  |  (   )  |  ",
    r"   \_______/   ",
    r"  /|/|/|/|/|\  ",
)


def _terminal_width() -> int:
    return shutil.get_terminal_size((80, 24)).columns


def _separator():
    """Print a full-width horizontal rule (dim), matching Claude Code's input border."""
    print(f"{C_DIM}{'─' * _terminal_width()}{C_RESET}", flush=True)


# ---- Spinner (Claude Code style) ----
# Uses a pulsing ⏺ dot that alternates bright↔dim every 400 ms.
# Color differs by state: green = thinking, yellow = tool running.

class Spinner:
    """Pulsing ⏺ dot spinner with configurable color."""

    DOT = "⏺"

    def __init__(self, label: str, color: str = C_GREEN):
        self._label = label
        self._color = color
        self._phase = 0
        self._stop_evt = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def update(self, label: str):
        self._label = label

    def _dot(self) -> str:
        """Alternate between bright and dim versions of the dot."""
        if self._phase % 2 == 0:
            return f"{self._color}{self.DOT}{C_RESET}"
        return f"\033[2m{self._color}{self.DOT}{C_RESET}"

    def _run(self):
        # Draw immediately so user sees the dot without waiting 400ms
        sys.stdout.write(f"{_ERASE_LINE}{self._dot()} {self._label}")
        sys.stdout.flush()
        while not self._stop_evt.wait(0.4):
            self._phase += 1
            sys.stdout.write(f"{_ERASE_LINE}{self._dot()} {self._label}")
            sys.stdout.flush()

    def _join(self):
        self._stop_evt.set()
        self._thread.join(timeout=0.5)
        sys.stdout.write(_ERASE_LINE)
        sys.stdout.flush()

    def finish(self, label: str = None):
        """Stop and show solid green ⏺ (done)."""
        self._join()
        print(f"{C_GREEN}{self.DOT}{C_RESET} {label or self._label}")
        sys.stdout.flush()

    def fail(self, label: str = None):
        """Stop and show solid red ⏺ (error)."""
        self._join()
        print(f"{C_RED}{self.DOT}{C_RESET} {label or self._label}")
        sys.stdout.flush()

    def erase(self):
        """Stop and clear the line (caller will print next)."""
        self._join()


# ---- Data Types ----

@dataclass
class Message:
    role: str  # "user", "assistant", "system"
    content: Any  # str or list of content blocks
    tool_use_id: str = ""
    is_error: bool = False
    is_compact_boundary: bool = False
    token_estimate: int = 0


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class ConversationState:
    messages: list = field(default_factory=list)
    turn_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    has_compacted: bool = False


# ---- Tool Definitions ----

TOOLS = [
    {
        "name": "list_terminals",
        "description": "List all registered terminals with status, platform, shell, and tags.",
        "parameters": {"type": "object", "properties": {}, "required": []},
        "read_only": True,
    },
    {
        "name": "run_command",
        "description": "Execute a shell command on a remote terminal. Use correct shell syntax (bash for Linux/Mac, powershell for Windows).",
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Terminal name"},
                "command": {"type": "string", "description": "Shell command"},
            },
            "required": ["target", "command"],
        },
        "read_only": False,
    },
    {
        "name": "read_file",
        "description": "Read a file on a remote terminal with line numbers.",
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Terminal name"},
                "path": {"type": "string", "description": "File path"},
                "offset": {"type": "integer", "description": "Start line (0-indexed)"},
                "limit": {"type": "integer", "description": "Max lines to read"},
            },
            "required": ["target", "path"],
        },
        "read_only": True,
    },
    {
        "name": "edit_file",
        "description": "Edit a file by replacing text on a remote terminal.",
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
        "read_only": False,
    },
    {
        "name": "search_files",
        "description": "Search for files by glob pattern on a remote terminal.",
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Terminal name"},
                "pattern": {"type": "string", "description": "Glob pattern"},
                "path": {"type": "string", "description": "Base directory"},
            },
            "required": ["target", "pattern"],
        },
        "read_only": True,
    },
    {
        "name": "search_content",
        "description": "Search file contents by regex on a remote terminal.",
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Terminal name"},
                "pattern": {"type": "string", "description": "Regex pattern"},
                "path": {"type": "string", "description": "Directory to search"},
                "glob": {"type": "string", "description": "File glob filter"},
            },
            "required": ["target", "pattern"],
        },
        "read_only": True,
    },
    {
        "name": "send_notification",
        "description": "Send a notification to a terminal (e.g. phone).",
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Terminal to notify"},
                "title": {"type": "string", "description": "Notification title"},
                "body": {"type": "string", "description": "Notification body"},
            },
            "required": ["target", "title", "body"],
        },
        "read_only": False,
    },
]


def _tool_schemas_anthropic():
    return [
        {"name": t["name"], "description": t["description"],
         "input_schema": t["parameters"]}
        for t in TOOLS
    ]


def _tool_schemas_openai():
    return [
        {"type": "function", "function": {"name": t["name"],
         "description": t["description"], "parameters": t["parameters"]}}
        for t in TOOLS
    ]


# ---- Tool Execution ----

def _submit_and_wait(relay: Relay, target: str, task_type: str, params: dict,
                     timeout: int = 120, progress_cb=None) -> str:
    """Submit task to remote terminal and wait for result.

    progress_cb(new_text): called with incremental stdout as the daemon writes it.
    """
    task_id = relay.submit_task(target, task_type=task_type, **params)
    relay.set_mode(target, "wake")
    deadline = time.time() + timeout
    last_output_len = 0
    while time.time() < deadline:
        task = relay.poll_task(target, task_id=task_id)
        if not task:
            task = relay.poll_task(target)
            if task and task.get("id") != task_id:
                time.sleep(1)
                continue
        if task:
            current_output = task.get("output", "") or ""
            if progress_cb and task.get("status") == "RUNNING":
                if len(current_output) > last_output_len:
                    progress_cb(current_output[last_output_len:])
                    last_output_len = len(current_output)
            if task.get("status") in ("DONE", "FAILED"):
                # Flush any remaining output not yet streamed
                if progress_cb and len(current_output) > last_output_len:
                    progress_cb(current_output[last_output_len:])
                relay.clear_task(target, task_id=task_id)
                exit_code = task.get("exit_code", 0) or 0
                if exit_code != 0:
                    return f"{current_output}\n[exit code: {exit_code}]"
                return current_output
        time.sleep(1.5)
    return "[timeout waiting for result]"


def execute_tool(relay: Relay, name: str, args: dict, progress_cb=None) -> str:
    """Execute a tool and return the result string."""
    try:
        if name == "list_terminals":
            terminals = relay.list_terminals()
            lines = []
            for t in terminals:
                n = t.get("name", "?")
                online = "online" if t.get("online") else "offline"
                meta = t.get("meta", {})
                tags = ",".join(t.get("tags", []))
                lines.append(f"{n} [{online}] {meta.get('platform','')} {meta.get('shell','')} [{tags}]")
            return "\n".join(lines) or "No terminals."

        elif name == "run_command":
            # run_command gets streaming output via progress_cb
            return _submit_and_wait(relay, args["target"], "shell",
                                    {"command": args["command"]},
                                    progress_cb=progress_cb)

        elif name == "read_file":
            params = {"path": args["path"]}
            if "offset" in args:
                params["offset"] = args["offset"]
            if "limit" in args:
                params["limit"] = args["limit"]
            return _submit_and_wait(relay, args["target"], "cat", params)

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
            if args.get("glob"):
                params["glob"] = args["glob"]
            return _submit_and_wait(relay, args["target"], "grep", params)

        elif name == "send_notification":
            relay.push_notification(args["target"], args["title"],
                                    args.get("body", ""))
            return f"Notification sent to {args['target']}"

        else:
            return f"Unknown tool: {name}"
    except (RelayError, Exception) as e:
        return f"Error: {e}"


def partition_tool_calls(tool_calls: list) -> list:
    """Partition tool calls into batches: consecutive read-only together, write tools alone."""
    tool_map = {t["name"]: t for t in TOOLS}
    batches = []
    for tc in tool_calls:
        is_ro = tool_map.get(tc.name, {}).get("read_only", False)
        if is_ro and batches and batches[-1]["read_only"]:
            batches[-1]["calls"].append(tc)
        else:
            batches.append({"read_only": is_ro, "calls": [tc]})
    return batches


def execute_tool_batch(relay: Relay, batch: dict, on_start=None,
                       on_progress=None, on_result=None) -> list:
    """Execute a batch of tool calls. Concurrent for read-only, serial for writes.

    on_start(tc)              — called immediately before tool execution starts
    on_progress(tc, new_text) — called with incremental output (run_command only, serial)
    on_result(tc, output)     — called after tool execution completes
    """
    results = []
    if batch["read_only"] and len(batch["calls"]) > 1:
        # Parallel read-only: announce all starts, then execute concurrently
        from concurrent.futures import ThreadPoolExecutor, as_completed
        for tc in batch["calls"]:
            if on_start:
                on_start(tc)
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_TOOLS) as ex:
            futures = {
                ex.submit(execute_tool, relay, tc.name, tc.input): tc
                for tc in batch["calls"]
            }
            for fut in as_completed(futures):
                tc = futures[fut]
                output = fut.result()
                results.append((tc, output))
                if on_result:
                    on_result(tc, output)
    else:
        for tc in batch["calls"]:
            if on_start:
                on_start(tc)
            if on_progress:
                def _pcb(text, _tc=tc):
                    on_progress(_tc, text)
                output = execute_tool(relay, tc.name, tc.input, progress_cb=_pcb)
            else:
                output = execute_tool(relay, tc.name, tc.input)
            results.append((tc, output))
            if on_result:
                on_result(tc, output)
    return results


# ---- System Prompt ----

def build_system_prompt(relay: Relay) -> str:
    terminals = relay.list_terminals()
    terminal_lines = []
    for t in terminals:
        name = t.get("name", "?")
        online = "online" if t.get("online") else "offline"
        meta = t.get("meta", {})
        tags = ", ".join(t.get("tags", []))
        terminal_lines.append(
            f"- {name} ({online}, {meta.get('platform','')}, {meta.get('shell','')}) [{tags}]")

    terminals_str = "\n".join(terminal_lines) if terminal_lines else "No terminals available."

    return f"""You are Octo, an AI assistant integrated into OpenOcto — a cross-device control system.
You can execute commands and manage files on any registered terminal via tool calls.

Available terminals:
{terminals_str}

Guidelines:
- Use the correct shell syntax for each terminal (bash for Linux/Mac, powershell for Windows).
- For file operations, prefer read_file/edit_file over running cat/sed commands.
- Be concise. Summarize command output rather than showing it raw.
- If a terminal is offline, tell the user.
- When a task is done, offer to notify the user's phone if one is available.
- Respond in the same language as the user's message."""


# ---- Context Compaction ----

COMPACT_PROMPT = """CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

Summarize the conversation above into a structured summary. Include ALL of the following sections:

<analysis>
Chronologically analyze each section of the conversation:
- User's explicit requests and intents
- Your approach to addressing them
- Key decisions, technical concepts, code patterns
- Specific details: file names, code snippets, function signatures, terminal names
- Errors encountered and how they were resolved
- User feedback received
</analysis>

<summary>
1. Primary Request and Intent: [Detailed description of what the user wanted]
2. Key Technical Concepts: [List all technical details discussed]
3. Files and Code: [All file paths, code changes, configurations mentioned]
4. Errors and Fixes: [Problems encountered and solutions applied]
5. Terminal Operations: [Which terminals were used, what was done on each]
6. Pending Tasks: [Anything not yet completed]
7. Current State: [Precise description of where things stand]
8. Next Step: [What should happen next based on recent context]
</summary>"""


def estimate_tokens(messages: list) -> int:
    """Rough token estimation from message content."""
    total = 0
    for msg in messages:
        if isinstance(msg.content, str):
            total += len(msg.content) // CHARS_PER_TOKEN
        elif isinstance(msg.content, list):
            for block in msg.content:
                if isinstance(block, dict):
                    total += len(json.dumps(block)) // CHARS_PER_TOKEN
                elif isinstance(block, str):
                    total += len(block) // CHARS_PER_TOKEN
        total += 10  # overhead per message
    return total


def should_compact(messages: list, context_window: int) -> bool:
    """Check if conversation needs compaction."""
    threshold = context_window - COMPACT_BUFFER_TOKENS
    return estimate_tokens(messages) > threshold


def _sliding_window_truncate(messages: list, context_window: int) -> list:
    """Fallback for very small context windows: keep only recent messages that fit.
    Used when LLM-based compaction would itself exceed the context budget."""
    target = context_window - COMPACT_BUFFER_TOKENS
    result = []
    total = 0
    # Walk backwards, keep as many recent messages as fit
    for msg in reversed(messages):
        est = estimate_tokens([msg])
        if total + est > target:
            break
        result.insert(0, msg)
        total += est
    if not result and messages:
        result = [messages[-1]]  # Always keep at least the last message
    return result


def compact_conversation(messages: list, call_llm_fn, context_window: int) -> list:
    """Compact conversation history by summarizing with LLM.

    For large context models (>64K): LLM-based structured summary.
    For small context models (<64K): LLM summary with aggressive keep ratio.
    Fallback: sliding window truncation if LLM summary would itself be too long.
    """
    token_est = estimate_tokens(messages)

    # For very small contexts, LLM summary might not fit — use sliding window
    if context_window < 16000:
        print(f"{C_DIM}  Small context model — using sliding window{C_RESET}")
        return _sliding_window_truncate(messages, context_window)

    # Adjust keep ratio based on context window
    # Large context: keep 30% (more room for summary)
    # Medium context: keep 50% (less room, keep more recent)
    if context_window >= 100000:
        keep_ratio = 0.30
    elif context_window >= 32000:
        keep_ratio = 0.40
    else:
        keep_ratio = 0.50

    keep_count = max(2, int(len(messages) * keep_ratio))
    messages_to_summarize = messages[:-keep_count]
    messages_to_keep = messages[-keep_count:]

    if not messages_to_summarize:
        return messages  # Nothing to summarize

    # Build summarization request
    summary_messages = []
    for msg in messages_to_summarize:
        if isinstance(msg.content, str):
            summary_messages.append({"role": msg.role, "content": msg.content})
        elif isinstance(msg.content, list):
            # Flatten content blocks to text
            text_parts = []
            for block in msg.content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block["text"])
                    elif block.get("type") == "tool_use":
                        text_parts.append(f"[Tool: {block['name']}({json.dumps(block.get('input', {}))})]")
                    elif block.get("type") == "tool_result":
                        content = block.get("content", "")
                        if isinstance(content, list):
                            content = " ".join(
                                b.get("text", "") for b in content if isinstance(b, dict))
                        text_parts.append(f"[Result: {str(content)[:500]}]")
                elif isinstance(block, str):
                    text_parts.append(block)
            summary_messages.append({"role": msg.role, "content": "\n".join(text_parts)})

    summary_messages.append({"role": "user", "content": COMPACT_PROMPT})

    # Call LLM for summary (no tools)
    try:
        summary_text = call_llm_fn(
            summary_messages,
            system_prompt="You are a conversation summarizer. Respond with text only.",
            tools=None,
            max_tokens=COMPACT_MAX_OUTPUT_TOKENS,
        )
    except Exception as e:
        # If summarization fails, fall back to sliding window
        print(f"{C_YELLOW}  Compaction LLM call failed: {e}, falling back to sliding window{C_RESET}")
        return _sliding_window_truncate(messages, context_window)

    # Format summary
    summary_text = _format_compact_summary(summary_text)

    # Build new message list
    boundary = Message(role="system", content="[Context compacted]",
                       is_compact_boundary=True)
    summary_msg = Message(role="user",
                          content=f"[Conversation summary]\n{summary_text}")

    new_messages = [boundary, summary_msg] + messages_to_keep
    new_est = estimate_tokens(new_messages)
    print(f"{C_DIM}  Compacted: {token_est} → {new_est} tokens "
          f"({len(messages)} → {len(new_messages)} messages){C_RESET}")
    return new_messages


def _format_compact_summary(text: str) -> str:
    """Strip analysis section, extract summary."""
    import re
    # Remove analysis block
    text = re.sub(r'<analysis>[\s\S]*?</analysis>', '', text)
    # Extract summary content
    m = re.search(r'<summary>([\s\S]*?)</summary>', text)
    if m:
        text = f"Summary:\n{m.group(1).strip()}"
    # Clean up
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ---- LLM Backends ----

def call_anthropic(messages: list, system_prompt: str, tools: list = None,
                   max_tokens: int = DEFAULT_MAX_TOKENS,
                   model: str = "claude-sonnet-4-20250514",
                   stream_callback=None) -> tuple:
    """Call Anthropic API. Returns (text, tool_calls, usage)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": messages,
        "stream": True,
    }
    if tools:
        body["tools"] = tools

    data = json.dumps(body).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=data,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )

    text_parts = []
    tool_calls = []
    usage = {"input_tokens": 0, "output_tokens": 0}
    current_block = None
    current_tool_input = ""

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            buffer = ""
            for chunk in iter(lambda: resp.read(1024), b""):
                buffer += chunk.decode("utf-8", errors="replace")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    if payload == "[DONE]":
                        break
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue

                    etype = event.get("type", "")

                    if etype == "content_block_start":
                        block = event.get("content_block", {})
                        if block.get("type") == "tool_use":
                            current_block = {"type": "tool_use",
                                             "id": block["id"],
                                             "name": block["name"]}
                            current_tool_input = ""
                        else:
                            current_block = {"type": "text"}

                    elif etype == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            text = delta["text"]
                            text_parts.append(text)
                            if stream_callback:
                                stream_callback(text)
                        elif delta.get("type") == "input_json_delta":
                            current_tool_input += delta.get("partial_json", "")

                    elif etype == "content_block_stop":
                        if current_block and current_block.get("type") == "tool_use":
                            try:
                                inp = json.loads(current_tool_input) if current_tool_input else {}
                            except json.JSONDecodeError:
                                inp = {}
                            tool_calls.append(ToolCall(
                                id=current_block["id"],
                                name=current_block["name"],
                                input=inp,
                            ))
                        current_block = None

                    elif etype == "message_delta":
                        u = event.get("usage", {})
                        usage["output_tokens"] = u.get("output_tokens",
                                                        usage["output_tokens"])

                    elif etype == "message_start":
                        u = event.get("message", {}).get("usage", {})
                        usage["input_tokens"] = u.get("input_tokens", 0)

    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        raise RuntimeError(f"Anthropic API error ({e.code}): {body[:500]}")

    return "".join(text_parts), tool_calls, usage


def _to_openai_messages(messages: list) -> list:
    """Convert Anthropic-style message history to OpenAI format.

    Anthropic stores tool calls as content blocks inside assistant messages,
    and tool results as content blocks in user messages.
    OpenAI expects tool calls in a top-level `tool_calls` field on assistant
    messages, and tool results as separate messages with role="tool".
    """
    result = []
    for msg in messages:
        if not isinstance(msg.get("content"), list):
            result.append(msg)
            continue
        role = msg["role"]
        blocks = msg["content"]

        if role == "assistant":
            text_parts = []
            tool_calls = []
            for block in blocks:
                if not isinstance(block, dict):
                    text_parts.append(str(block))
                elif block.get("type") == "text":
                    text_parts.append(block["text"])
                elif block.get("type") == "tool_use":
                    tool_calls.append({
                        "id": block["id"],
                        "type": "function",
                        "function": {
                            "name": block["name"],
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    })
            out = {"role": "assistant", "content": " ".join(text_parts) or None}
            if tool_calls:
                out["tool_calls"] = tool_calls
            result.append(out)

        elif role == "user":
            # May contain tool_result blocks (one per tool call)
            pending_text = []
            for block in blocks:
                if not isinstance(block, dict):
                    pending_text.append(str(block))
                elif block.get("type") == "tool_result":
                    # Flush any preceding text as a user message
                    if pending_text:
                        result.append({"role": "user", "content": " ".join(pending_text)})
                        pending_text = []
                    content = block.get("content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            b.get("text", "") for b in content if isinstance(b, dict))
                    result.append({
                        "role": "tool",
                        "tool_call_id": block["tool_use_id"],
                        "content": str(content),
                    })
                elif block.get("type") == "text":
                    pending_text.append(block["text"])
                else:
                    pending_text.append(str(block))
            if pending_text:
                result.append({"role": "user", "content": " ".join(pending_text)})
        else:
            result.append(msg)
    return result


def call_openai_compat(messages: list, system_prompt: str, tools: list = None,
                       max_tokens: int = DEFAULT_MAX_TOKENS,
                       model: str = "gpt-4o",
                       base_url: str = "https://api.openai.com/v1",
                       api_key: str = "",
                       stream_callback=None,
                       chat_endpoint: str = "/chat/completions") -> tuple:
    """Call OpenAI-compatible API. Returns (text, tool_calls, usage)."""
    if not api_key:
        api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("API key not set")

    api_messages = [{"role": "system", "content": system_prompt}] + messages
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": api_messages,
        "stream": True,
    }
    if tools:
        body["tools"] = tools

    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}{chat_endpoint}",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "openocto/0.1",
        },
    )

    text_parts = []
    tool_calls_map = {}  # index → {id, name, arguments_str}
    usage = {"input_tokens": 0, "output_tokens": 0}

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            buffer = ""
            try:
                for chunk in iter(lambda: resp.read(1024), b""):
                    buffer += chunk.decode("utf-8", errors="replace")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if not line.startswith("data: "):
                            continue
                        payload = line[6:]
                        if payload == "[DONE]":
                            break
                        try:
                            event = json.loads(payload)
                        except json.JSONDecodeError:
                            continue

                        choices = event.get("choices", [])
                        if not choices:
                            # Usage chunk
                            u = event.get("usage", {})
                            if u:
                                usage["input_tokens"] = u.get("prompt_tokens", 0)
                                usage["output_tokens"] = u.get("completion_tokens", 0)
                            continue

                        delta = choices[0].get("delta", {})

                        # Text
                        if delta.get("content"):
                            text_parts.append(delta["content"])
                            if stream_callback:
                                stream_callback(delta["content"])

                        # Tool calls
                        for tc_delta in delta.get("tool_calls", []):
                            idx = tc_delta.get("index", 0)
                            if idx not in tool_calls_map:
                                tool_calls_map[idx] = {
                                    "id": tc_delta.get("id", ""),
                                    "name": tc_delta.get("function", {}).get("name", ""),
                                    "arguments": "",
                                }
                            if tc_delta.get("id"):
                                tool_calls_map[idx]["id"] = tc_delta["id"]
                            fn = tc_delta.get("function", {})
                            if fn.get("name"):
                                tool_calls_map[idx]["name"] = fn["name"]
                            if fn.get("arguments"):
                                tool_calls_map[idx]["arguments"] += fn["arguments"]
            except (ConnectionError, OSError):
                pass  # Server closed connection after stream end — normal for SSE

    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        raise RuntimeError(f"API error ({e.code}): {body[:500]}")
    except urllib.error.URLError as e:
        # SSL EOF / connection reset after streaming — treat as normal close if we got content
        if text_parts or tool_calls_map:
            pass  # partial or complete response, return what we have
        else:
            raise RuntimeError(str(e))

    # Parse tool calls
    tool_calls = []
    for idx in sorted(tool_calls_map.keys()):
        tc = tool_calls_map[idx]
        try:
            inp = json.loads(tc["arguments"]) if tc["arguments"] else {}
        except json.JSONDecodeError:
            inp = {}
        tool_calls.append(ToolCall(id=tc["id"], name=tc["name"], input=inp))

    return "".join(text_parts), tool_calls, usage


# ---- Display Helpers ----

# Verb labels for tool spinner messages (matches Claude Code's tool display)
_TOOL_VERBS = {
    "run_command":       "Bash",
    "read_file":         "Read",
    "edit_file":         "Edit",
    "search_files":      "Glob",
    "search_content":    "Grep",
    "list_terminals":    "ListTerminals",
    "send_notification": "Notify",
    # Setup guide tools
    "get_state":         "Checking config",
    "get_platform":      "Checking platform",
    "configure_relay":   "Saving relay config",
    "test_relay":        "Testing relay",
    "configure_llm":     "Saving LLM config",
    "test_llm_key":      "Testing API key",
}

REPL_PROMPT = "› "  # U+203A, matches Claude Code's LineEditor prompt


def print_streaming(text: str):
    """Write streaming LLM text directly to stdout (no prefix)."""
    sys.stdout.write(text)
    sys.stdout.flush()


def _spinner_tool_label(tc: ToolCall) -> str:
    """Label shown in the spinner while a tool is executing."""
    verb = _TOOL_VERBS.get(tc.name, tc.name)
    args_json = json.dumps(tc.input, ensure_ascii=False)
    if len(args_json) > 80:
        args_json = args_json[:77] + "..."
    return f"Running tool `{verb}` with {args_json}"


def _tool_output_block(tool_name: str, output: str):
    """Render tool output as a fenced code block (Claude Code style).

    Format:
        ### Tool `Verb`

        ╭─ text
          line1              ← dark bg + light grey fg so text is visible
          line2
        ╰─
    """
    verb = _TOOL_VERBS.get(tool_name, tool_name)
    print(f"\n{C_BOLD}### Tool `{verb}`{C_RESET}\n")
    lines = output.strip().splitlines() if output.strip() else []
    MAX_LINES = 30
    print(f"{C_DARK_GREY}{C_BOLD}╭─ text{C_RESET}")
    shown = lines[:MAX_LINES]
    for line in shown:
        # Explicit light-grey fg on dark-grey bg so text is always readable
        print(f"{_CODE_BG}{_CODE_FG}  {line}{_CODE_RESET}")
    if len(lines) > MAX_LINES:
        print(f"{_CODE_BG}{_CODE_FG}  … ({len(lines) - MAX_LINES} more lines){_CODE_RESET}")
    print(f"{C_DARK_GREY}{C_BOLD}╰─{C_RESET}\n")


# ---- Main Conversation Loop ----

def run_builtin_agent(model: str = None, backend: str = "auto"):
    """Run the interactive agent REPL."""

    # Setup relay
    try:
        rc = get_relay_config()
        relay = Relay(rc["redis_url"], rc["redis_token"], rc["workspace"],
                      proxy_url=rc.get("proxy_url", ""))
    except Exception as e:
        print(f"{C_RED}[octo] Relay not configured: {e}{C_RESET}")
        print(f"  Run: octo init")
        return

    # Resolve LLM provider
    try:
        provider = resolve_provider(backend, model)
    except RuntimeError as e:
        print(f"{C_RED}[octo] {e}{C_RESET}")
        return
    backend = provider["type"]  # "anthropic" or "openai_compat"
    model = provider["model"]
    provider_name = provider["provider_name"]

    # Context window sizes
    context_windows = {
        "claude-sonnet-4-20250514": 200000,
        "claude-opus-4-20250514": 200000,
        "claude-haiku-4-5-20251001": 200000,
        "gpt-4o": 128000,
        "gpt-4o-mini": 128000,
        "gpt-4.1": 1000000,
        "deepseek-chat": 64000,
        "deepseek-reasoner": 64000,
        "qwen-plus": 128000,
        "qwen-turbo": 128000,
    }
    # For Ollama / unknown models, default to conservative 32K
    context_window = context_windows.get(model, 32000)

    # Build system prompt
    system_prompt = build_system_prompt(relay)

    # State
    state = ConversationState()
    session_file = os.path.expanduser(f"~/.octo/sessions/session_{int(time.time())}.json")

    # Session name for phone sync
    config = load_config()
    session_name = config.get("terminal_name", "")
    if not session_name:
        import platform
        session_name = platform.node().split(".")[0].lower().replace(" ", "-")

    # Register session as active (both per-session key and global sessions list)
    try:
        meta_json = json.dumps({"status": "active", "started": int(time.time()),
                                "backend": provider_name, "model": model, "type": "builtin"})
        relay._request("SET", relay._key("session", session_name, "meta"), meta_json)
        relay._request("EXPIRE", relay._key("session", session_name, "meta"), "86400")
        # Register in global sessions hash so phone can discover it
        relay._request("HSET", relay._key("sessions"), session_name, meta_json)
    except Exception:
        pass

    # LLM call wrapper — dispatches to anthropic or openai_compat based on resolved provider
    _prov_base_url = provider.get("base_url", "")
    _prov_api_key = provider.get("api_key", "")
    _prov_chat_endpoint = provider.get("chat_endpoint", "/chat/completions")

    def call_llm(messages, system_prompt=system_prompt, tools=None,
                 max_tokens=DEFAULT_MAX_TOKENS, stream_cb=None):
        if backend == "anthropic":
            text, _, _ = call_anthropic(
                messages, system_prompt,
                tools=_tool_schemas_anthropic() if tools is not False else None,
                max_tokens=max_tokens, model=model, stream_callback=stream_cb)
            return text
        else:
            text, _, _ = call_openai_compat(
                messages, system_prompt,
                tools=_tool_schemas_openai() if tools is not False else None,
                max_tokens=max_tokens, model=model,
                base_url=_prov_base_url,
                api_key=_prov_api_key,
                stream_callback=stream_cb,
                chat_endpoint=_prov_chat_endpoint)
            return text

    def call_llm_full(api_messages, stream_cb=None):
        """Full call returning (text, tool_calls, usage)."""
        if backend == "anthropic":
            return call_anthropic(
                api_messages, system_prompt,
                tools=_tool_schemas_anthropic(),
                max_tokens=DEFAULT_MAX_TOKENS, model=model,
                stream_callback=stream_cb)
        else:
            return call_openai_compat(
                _to_openai_messages(api_messages), system_prompt,
                tools=_tool_schemas_openai(),
                max_tokens=DEFAULT_MAX_TOKENS, model=model,
                base_url=_prov_base_url,
                api_key=_prov_api_key,
                chat_endpoint=_prov_chat_endpoint,
                stream_callback=stream_cb)

    # Header — octopus brand mark + session info
    print()
    for line in _OCTO_ART:
        print(f"{C_CYAN}{C_BOLD}{line}{C_RESET}")
    print()
    print(f"{C_BOLD}OpenOcto Agent{C_RESET}  {C_DIM}v0.1.1 · {provider_name}: {model}{C_RESET}")
    terminals = relay.list_terminals()
    online = sum(1 for t in terminals if t.get("online"))
    print(f"{C_DIM}{online} terminal{'s' if online != 1 else ''} online  ·  /help for commands{C_RESET}\n")

    # REPL loop
    while True:
        phone_input = _check_phone_input(relay, session_name)
        if phone_input:
            user_input = phone_input
            # Phone input shown as a full-width grey bubble (no overwrite needed)
            sys.stdout.write(f"{_USER_BG}{_USER_FG}  \U0001f4f1  {user_input}  \033[K{C_RESET}\n")
            sys.stdout.flush()
        else:
            try:
                user_input = input(REPL_PROMPT).strip()
            except (EOFError, KeyboardInterrupt):
                print(f"\n{C_DIM}Goodbye.{C_RESET}")
                break
            if user_input:
                # Overwrite the input line with a grey bubble.
                # \033[1A  — cursor up one line (back to the › prompt line)
                # \r\033[K — erase that line (with default bg, so no colour bleed)
                # then set bg/fg, print text, \033[K fills remainder of line with grey bg
                sys.stdout.write(
                    f"\033[1A\r\033[K"
                    f"{_USER_BG}{_USER_FG}  {user_input}  \033[K{C_RESET}\n"
                )
                sys.stdout.flush()

        if not user_input:
            continue

        # Slash commands
        if user_input in ("/quit", "/exit"):
            print(f"{C_DIM}Goodbye.{C_RESET}")
            break
        if user_input == "/compact":
            print(f"{C_DIM}  Compacting conversation...{C_RESET}")
            state.messages = compact_conversation(state.messages, call_llm, context_window)
            state.has_compacted = True
            continue
        if user_input == "/status":
            tokens = estimate_tokens(state.messages)
            print(f"{C_DIM}status: turns={state.turn_count} "
                  f"tokens(est)={tokens}/{context_window} "
                  f"in={state.total_input_tokens} out={state.total_output_tokens}{C_RESET}")
            continue
        if user_input == "/help":
            print(f"{C_DIM}  /compact   compress conversation context\n"
                  f"  /status    show token usage and turn count\n"
                  f"  /quit      exit{C_RESET}")
            continue

        # Add user message
        state.messages.append(Message(role="user", content=user_input))

        # Auto-compact check
        if should_compact(state.messages, context_window):
            print(f"{C_YELLOW}  Context approaching limit, auto-compacting...{C_RESET}")
            state.messages = compact_conversation(state.messages, call_llm, context_window)
            state.has_compacted = True

        # Prepare API messages
        api_messages = []
        for msg in state.messages:
            if msg.is_compact_boundary:
                continue
            if isinstance(msg.content, (str, list)):
                api_messages.append({"role": msg.role, "content": msg.content})

        # Conversation loop — tool use may cause multiple LLM rounds
        for _round in range(MAX_TOOL_ROUNDS):
            state.turn_count += 1

            # ── Spinner before LLM responds ──────────────────────────────────
            stream_spinner = Spinner("Opening conversation stream").start()
            saw_text = [False]

            def _stream_cb(chunk, _spinner=stream_spinner, _saw=saw_text):
                if not _saw[0]:
                    _spinner.finish("Streaming response")
                    _saw[0] = True
                sys.stdout.write(chunk)
                sys.stdout.flush()

            try:
                text, tool_calls, usage = call_llm_full(api_messages, stream_cb=_stream_cb)
            except Exception as e:
                stream_spinner.fail("Streaming response failed")
                print(f"\n{C_RED}  Error: {e}{C_RESET}")
                state.messages.append(Message(role="assistant", content=f"[Error: {e}]"))
                break

            # If no text was streamed at all, still close the spinner
            if not saw_text[0]:
                stream_spinner.finish("Streaming response")

            state.total_input_tokens += usage.get("input_tokens", 0)
            state.total_output_tokens += usage.get("output_tokens", 0)

            if text:
                print()  # newline after streamed text

            # Token usage line (matches Claude Code's format)
            print(f"\n{C_DIM}Token usage: {usage.get('input_tokens',0)} input"
                  f" / {usage.get('output_tokens',0)} output{C_RESET}")

            # Build assistant message
            assistant_content = []
            if text:
                assistant_content.append({"type": "text", "text": text})
            for tc in tool_calls:
                assistant_content.append({
                    "type": "tool_use", "id": tc.id,
                    "name": tc.name, "input": tc.input,
                })
            state.messages.append(Message(role="assistant",
                                          content=assistant_content or text))

            if not tool_calls:
                break  # no tools → turn complete

            # ── Execute tools ─────────────────────────────────────────────────
            batches = partition_tool_calls(tool_calls)
            tool_result_blocks = []

            for batch in batches:
                _start_times: dict = {}
                _tool_spinners: dict = {}   # tc.id → Spinner
                _streamed_tcs: set = set()
                _partial_buf: dict = {}

                def on_start(tc,
                             _st=_start_times, _ts=_tool_spinners):
                    _st[tc.id] = time.time()
                    label = _spinner_tool_label(tc)
                    # Yellow dot while tool is executing
                    _ts[tc.id] = Spinner(label, color=C_YELLOW).start()

                def on_progress(tc, new_text,
                                _ts=_tool_spinners, _str=_streamed_tcs,
                                _buf=_partial_buf, _st=_start_times):
                    # First progress chunk: stop spinner, print solid header
                    if tc.id not in _str:
                        s = _ts.get(tc.id)
                        if s:
                            s.erase()
                        _str.add(tc.id)
                        verb = _TOOL_VERBS.get(tc.name, tc.name)
                        args_json = json.dumps(tc.input, ensure_ascii=False)
                        if len(args_json) > 80:
                            args_json = args_json[:77] + "..."
                        print(f"{C_BOLD}⏺ Running tool `{verb}` with {args_json}{C_RESET}")
                    # Buffer lines; print complete lines with readable colors
                    buf = _buf.get(tc.id, "") + new_text
                    *lines, remainder = buf.split("\n")
                    _buf[tc.id] = remainder
                    for line in lines:
                        print(f"{_CODE_BG}{_CODE_FG}  {line}{_CODE_RESET}")
                    sys.stdout.flush()

                def on_result(tc, output,
                              _ts=_tool_spinners, _st=_start_times,
                              _str=_streamed_tcs, _buf=_partial_buf):
                    # Flush partial line buffer
                    leftover = _buf.pop(tc.id, "")
                    if leftover:
                        print(f"{_CODE_BG}{_CODE_FG}  {leftover}{_CODE_RESET}")

                    elapsed = time.time() - _st.get(tc.id, time.time())
                    s = _ts.pop(tc.id, None)

                    if tc.id in _str:
                        # Streaming path: output already shown, just footer
                        print(f"{C_DIM}  ⎿  [{elapsed:.1f}s]{C_RESET}\n")
                    else:
                        # Non-streaming: finish spinner then render code block
                        done_label = f"Tool `{_TOOL_VERBS.get(tc.name, tc.name)}` completed"
                        if s:
                            s.finish(done_label)
                        _tool_output_block(tc.name, output)

                results = execute_tool_batch(relay, batch,
                                             on_start=on_start,
                                             on_progress=on_progress,
                                             on_result=on_result)

                for tc, output in results:
                    if len(output) > 50000:
                        output = output[:25000] + "\n...[truncated]...\n" + output[-25000:]
                    tool_result_blocks.append({
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "content": output,
                    })

            # Add tool results and loop for next LLM round
            state.messages.append(Message(role="user", content=tool_result_blocks))
            api_messages.append({"role": "assistant", "content": assistant_content})
            api_messages.append({"role": "user", "content": tool_result_blocks})

        # Save session + sync to Redis for phone
        _save_session(session_file, state)
        _sync_session_to_redis(relay, session_name, state)

    # Mark session ended + remove from global list
    try:
        meta_end = json.dumps({"status": "ended", "ended": int(time.time())})
        relay._request("SET", relay._key("session", session_name, "meta"), meta_end)
        relay._request("EXPIRE", relay._key("session", session_name, "meta"), "300")
        relay._request("DEL", relay._key("session", session_name, "output"))
        relay._request("HDEL", relay._key("sessions"), session_name)
    except Exception:
        pass


def _sync_session_to_redis(relay: Relay, session_name: str, state: ConversationState):
    """Push conversation display to Redis for phone viewing."""
    try:
        # Build a readable text view of recent conversation
        lines = []
        for msg in state.messages[-20:]:  # Last 20 messages
            if msg.is_compact_boundary:
                lines.append("[--- context compacted ---]")
                continue
            role_label = {"user": "You", "assistant": "Octo", "system": "System"}.get(
                msg.role, msg.role)
            if isinstance(msg.content, str):
                lines.append(f"{role_label}: {msg.content}")
            elif isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            lines.append(f"{role_label}: {block['text']}")
                        elif block.get("type") == "tool_use":
                            lines.append(f"  ┌ {block['name']}({json.dumps(block.get('input', {}))[:80]})")
                        elif block.get("type") == "tool_result":
                            content = block.get("content", "")
                            if isinstance(content, str):
                                preview = content[:200]
                            else:
                                preview = str(content)[:200]
                            lines.append(f"  │ {preview}")
                            lines.append(f"  └ done")

        output = "\n".join(lines)
        output_key = relay._key("session", session_name, "output")
        relay._request("SET", output_key, output)
        relay._request("EXPIRE", output_key, "60")
    except Exception:
        pass


def _check_phone_input(relay: Relay, session_name: str) -> str:
    """Check if phone sent any input via Redis."""
    try:
        input_key = relay._key("session", session_name, "input")
        raw = relay._request("RPOP", input_key)
        if raw and isinstance(raw, str) and raw.strip():
            return raw.strip()
    except Exception:
        pass
    return ""


def _save_session(path: str, state: ConversationState):
    """Persist session to disk."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = []
        for msg in state.messages:
            data.append({
                "role": msg.role,
                "content": msg.content if isinstance(msg.content, str) else msg.content,
                "is_compact_boundary": msg.is_compact_boundary,
            })
        with open(path, "w") as f:
            json.dump({"messages": data, "turn_count": state.turn_count}, f)
    except Exception:
        pass


# ── LLM-driven Setup Guide ────────────────────────────────────────────────────
#
# `octo setup` replaces the old numbered-menu `octo init` with a
# conversational LLM guide that:
#   1. Detects what is already configured
#   2. Walks through Relay → LLM key in natural conversation
#   3. Calls tools to actually write config and verify each step
#   4. Uses the community free LLM — zero prerequisites for new users
#
# Tool execution is local-only (no shell, no subprocess) so there are no
# security concerns; tools read/write ~/.octo/config.json and test network
# connectivity using the same code paths as the rest of the CLI.

_SETUP_SYSTEM_PROMPT = """\
You are Octo's interactive setup guide. Your only job is to get the user \
fully configured through friendly conversation.

First call get_state AND get_platform together to understand the situation. \
Then guide the user through:
  1. Relay configuration  (required to use any octo command)
  2. LLM API key          (optional — needed for `octo agent`)

Rules:
- Always call a test tool after configuring something. Fix failures before moving on.
- If something is already configured, confirm it's working, then skip or ask if \
they want to reconfigure.
- Be concise. Don't over-explain. One question at a time.
- Respond in the same language the user uses.
- When all steps are done, output a short "what to do next" block with the \
exact commands to run, then say goodbye and stop asking questions.
"""

_SETUP_TOOLS_ANTHROPIC = [
    {
        "name": "get_state",
        "description": "Read current octo configuration and detect what is missing.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_platform",
        "description": "Get OS, Python version, and machine info.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "configure_relay",
        "description": "Save relay configuration. mode='free' uses the public relay; mode='custom' needs redis_url and redis_token.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mode":        {"type": "string", "enum": ["free", "custom"]},
                "redis_url":   {"type": "string"},
                "redis_token": {"type": "string"},
                "workspace":   {"type": "string", "description": "Optional. Auto-generated if omitted."},
            },
            "required": ["mode"],
        },
    },
    {
        "name": "test_relay",
        "description": "Test the currently saved relay connection.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "configure_llm",
        "description": "Save LLM provider and API key to config.",
        "input_schema": {
            "type": "object",
            "properties": {
                "provider": {"type": "string",
                             "description": "One of: anthropic, openai, openrouter, deepseek, siliconflow, qwen, nvidia, ollama"},
                "api_key":  {"type": "string"},
                "model":    {"type": "string", "description": "Optional model override."},
            },
            "required": ["provider", "api_key"],
        },
    },
    {
        "name": "test_llm_key",
        "description": "Test an API key for a given provider by sending a minimal request.",
        "input_schema": {
            "type": "object",
            "properties": {
                "provider": {"type": "string"},
                "api_key":  {"type": "string"},
            },
            "required": ["provider", "api_key"],
        },
    },
]

# OpenAI-compat format (same info, different schema wrapper)
_SETUP_TOOLS_OPENAI = [
    {
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t["description"],
            "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
        },
    }
    for t in _SETUP_TOOLS_ANTHROPIC
]


def _setup_exec_tool(name: str, args: dict) -> str:
    """Dispatch a setup tool call and return a JSON string result."""
    from .config import load_config, save_config, CONFIG_FILE
    from .relay import Relay, RelayError

    if name == "get_state":
        config = load_config()
        has_relay = bool(config.get("proxy_url") or config.get("redis_url"))
        relay_type = (
            "free" if config.get("proxy_url") and not config.get("redis_url")
            else "custom" if config.get("redis_url")
            else "none"
        )
        llm_cfg = config.get("llm", {})
        found_keys = [
            prov for prov, info in PROVIDERS.items()
            if info.get("key_env") and os.environ.get(info["key_env"])
        ]
        return json.dumps({
            "relay_configured": has_relay,
            "relay_type": relay_type,
            "workspace": config.get("workspace", ""),
            "llm_configured": bool(llm_cfg.get("api_key")),
            "llm_provider": llm_cfg.get("provider", ""),
            "env_keys_found": found_keys,
            "config_file": str(CONFIG_FILE),
        }, ensure_ascii=False)

    if name == "get_platform":
        import platform
        return json.dumps({
            "os": platform.system(),
            "os_version": platform.release(),
            "python": sys.version.split()[0],
            "machine": platform.machine(),
            "hostname": platform.node(),
        })

    if name == "configure_relay":
        import uuid
        mode = args.get("mode", "free")
        if mode == "free":
            config = load_config()
            ws = config.get("workspace", "")
            if not ws or ws == "default" or len(ws) < 10:
                ws = f"ws-{uuid.uuid4().hex[:24]}"
            new_cfg = {
                "redis_url": "",
                "redis_token": "",
                "workspace": ws,
                "proxy_url": "https://openocto-relay.openocto.workers.dev",
            }
        else:
            redis_url = args.get("redis_url", "")
            redis_token = args.get("redis_token", "")
            if not redis_url or not redis_token:
                return json.dumps({"success": False,
                                   "error": "redis_url and redis_token are required for custom mode"})
            ws = args.get("workspace", "") or f"ws-{uuid.uuid4().hex[:16]}"
            new_cfg = {
                "redis_url": redis_url,
                "redis_token": redis_token,
                "workspace": ws,
            }
        save_config(new_cfg)
        return json.dumps({"success": True, "workspace": new_cfg["workspace"],
                           "config_file": str(CONFIG_FILE)}, ensure_ascii=False)

    if name == "test_relay":
        try:
            from .config import get_relay_config
            rc = get_relay_config()
            relay = Relay(rc["redis_url"], rc["redis_token"], rc["workspace"],
                          proxy_url=rc.get("proxy_url", ""))
            terminals = relay.list_terminals()
            online = sum(1 for t in terminals if t.get("online"))
            return json.dumps({"success": True, "terminals_online": online,
                               "total_terminals": len(terminals)})
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)[:300]})

    if name == "configure_llm":
        provider = args.get("provider", "")
        api_key = args.get("api_key", "")
        if provider not in PROVIDERS:
            return json.dumps({"success": False,
                               "error": f"Unknown provider '{provider}'. Valid: {list(PROVIDERS)}"})
        config = load_config()
        llm_cfg: dict = {"provider": provider, "api_key": api_key}
        if args.get("model"):
            llm_cfg["model"] = args["model"]
        config["llm"] = llm_cfg
        save_config(config)
        return json.dumps({"success": True, "provider": provider,
                           "config_file": str(CONFIG_FILE)})

    if name == "test_llm_key":
        provider = args.get("provider", "")
        api_key = args.get("api_key", "")
        prov = PROVIDERS.get(provider)
        if not prov:
            return json.dumps({"success": False, "error": f"Unknown provider: {provider}"})
        try:
            msgs = [{"role": "user", "content": "hi"}]
            if prov["type"] == "openai_compat":
                text, _, _ = call_openai_compat(
                    msgs, "Reply with just 'ok'.", tools=None,
                    max_tokens=10, model=prov["default_model"],
                    base_url=prov["base_url"], api_key=api_key,
                )
            else:
                # Anthropic: direct HTTP call (avoids env-var dependency)
                body = json.dumps({
                    "model": prov["default_model"], "max_tokens": 10,
                    "system": "Reply with just 'ok'.",
                    "messages": msgs, "stream": False,
                }).encode()
                req = urllib.request.Request(
                    "https://api.anthropic.com/v1/messages", data=body,
                    headers={"Content-Type": "application/json",
                             "x-api-key": api_key,
                             "anthropic-version": "2023-06-01"},
                )
                with urllib.request.urlopen(req, timeout=20) as resp:
                    r = json.loads(resp.read())
                text = r.get("content", [{}])[0].get("text", "ok")
            return json.dumps({"success": True, "response_preview": (text or "ok")[:40]})
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)[:300]})

    return json.dumps({"error": f"Unknown setup tool: {name}"})


def run_setup_guide():
    """LLM-driven interactive setup wizard — `octo setup`."""

    # Always use the community LLM so there are zero prerequisites
    provider = {
        "type": "openai_compat",
        "base_url": _COMMUNITY_LLM_URL,
        "api_key": "community",
        "model": "moonshotai/Kimi-K2-Instruct",
        "provider_name": "community",
        "chat_endpoint": "/llm/infer",
    }

    # ── Header ────────────────────────────────────────────────────────────────
    print()
    for line in _OCTO_ART:
        print(f"{C_CYAN}{C_BOLD}{line}{C_RESET}")
    print()
    print(f"{C_BOLD}OpenOcto Setup Guide{C_RESET}  "
          f"{C_DIM}AI-assisted · type /skip to skip a step · /quit to exit{C_RESET}\n")

    messages: list = []
    # Prime the conversation: ask the LLM to greet + check state immediately
    messages.append({"role": "user",
                     "content": "Hello! Please check my current configuration and "
                                "start the setup guide."})

    def _call(msgs, stream_cb=None) -> tuple:
        return call_openai_compat(
            msgs, _SETUP_SYSTEM_PROMPT,
            tools=_SETUP_TOOLS_OPENAI,
            max_tokens=1024,
            model=provider["model"],
            base_url=provider["base_url"],
            api_key=provider["api_key"],
            chat_endpoint=provider["chat_endpoint"],
            stream_callback=stream_cb,
        )

    # ── Conversation loop ─────────────────────────────────────────────────────
    while True:
        # ── LLM turn ─────────────────────────────────────────────────────────
        for _round in range(10):   # max tool-use rounds per LLM turn
            spin = Spinner("Thinking…").start()
            saw_text = [False]

            def _stream_cb(chunk, _s=spin, _f=saw_text):
                if not _f[0]:
                    _s.finish("Octo")
                    _f[0] = True
                sys.stdout.write(chunk)
                sys.stdout.flush()

            try:
                text, tool_calls, _ = _call(messages, stream_cb=_stream_cb)
            except Exception as e:
                spin.fail("Error")
                print(f"\n{C_RED}  {e}{C_RESET}")
                return

            if not saw_text[0]:
                spin.finish("Octo")
            if text:
                print()   # newline after streamed text

            # Build assistant message
            assistant_content: list = []
            if text:
                assistant_content.append({"type": "text", "text": text})
            for tc in tool_calls:
                assistant_content.append({"type": "tool_use", "id": tc.id,
                                          "name": tc.name, "input": tc.input})
            messages.append({"role": "assistant",
                              "content": assistant_content or text or ""})

            if not tool_calls:
                break   # no more tools — show response and wait for user

            # ── Execute tools (all setup tools are fast & local) ─────────────
            tool_result_blocks = []
            for tc in tool_calls:
                ts = Spinner(f"{_TOOL_VERBS.get(tc.name, tc.name)}…",
                             color=C_YELLOW).start()
                result = _setup_exec_tool(tc.name, tc.input)
                ts.finish(f"{_TOOL_VERBS.get(tc.name, tc.name)}")
                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": result,
                })

            # OpenAI compat: flatten tool results into the messages list
            oai = _to_openai_messages(
                [{"role": "assistant", "content": assistant_content},
                 {"role": "user",     "content": tool_result_blocks}]
            )
            # Replace the last assistant message (already added above) and
            # append the tool results
            messages[-1] = oai[0]
            messages.extend(oai[1:])

        # ── User input ───────────────────────────────────────────────────────
        try:
            user_input = input(REPL_PROMPT).strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{C_DIM}Setup exited.{C_RESET}")
            break

        if not user_input:
            continue
        if user_input in ("/quit", "/exit"):
            print(f"{C_DIM}Setup exited.{C_RESET}")
            break

        # Grey bubble display
        sys.stdout.write(
            f"\033[1A\r\033[K"
            f"{_USER_BG}{_USER_FG}  {user_input}  \033[K{C_RESET}\n"
        )
        sys.stdout.flush()

        messages.append({"role": "user", "content": user_input})
