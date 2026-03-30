"""Generate CLAUDE.md content based on registered terminals."""

import os
from string import Template
from .relay import Relay


SYNC_PRESETS = {
    "direct": """\
## Code Sync: Direct (Remote-Only)
- Read and edit code directly on the remote terminal using `remote_read`/`remote_edit` (or `octo cat`/`octo edit`).
- Do NOT use `sed` for file editing — always use `remote_edit` / `octo edit`.
- All code changes happen on the remote. No local-remote sync needed.""",

    "git": """\
## Code Sync: Git
- Edit code locally using your native tools (Read, Edit, Grep) — they are more powerful than remote equivalents.
- When ready to run on remote, commit and push first:
  ```
  git add <files> && git commit -m "message" && git push
  ```
- Then sync and run on remote:
  ```
  remote_run(terminal, "git pull && <your command>")
  ```
- Do NOT edit files on remote directly — keep the git repo as single source of truth.
- Always push before remote_run. Never skip the push step.""",

    "rsync": """\
## Code Sync: Rsync
- Edit code locally using your native tools (Read, Edit, Grep).
- Before running on remote, sync files with rsync:
  ```
  rsync -avz --exclude '.git' ./ user@remote:/path/to/project/
  ```
- Then run on remote:
  ```
  remote_run(terminal, "<your command>")
  ```
- Do NOT edit files on remote directly — local is the source of truth.""",

    "none": "",
}

TEMPLATE = """\
# openOcto - Remote Terminals

## Commands
- `octo ls` — list all terminals and their status
- `octo run <name> "<command>"` — execute shell command (streaming output)
- `octo cat <name> /path/to/file` — read a remote file (with line numbers)
- `octo edit <name> /path --old "X" --new "Y"` — edit a remote file
- `octo glob <name> "**/*.py" --path /dir` — search files by pattern
- `octo grep <name> "regex" --path /dir` — search file contents
- `octo kill <name>` — kill a running command
- `octo logs <name>` — view task output or last log file

## Available Terminals
$terminals

$sync_rules

## Rules
- Always run `octo ls` first to verify the target is online.
- Use the correct shell syntax for each terminal (check the shell type above).
  - **bash/zsh**: `echo hello && whoami`, `cd /path`
  - **powershell**: `echo hello; whoami`, `Set-Location C:\\path`
- Shell commands maintain working directory between calls (cd persists).
- Output streams in real-time. Ctrl+C sends kill signal to remote process.
- For file operations, prefer `octo cat`/`octo edit` over `octo run cat`/`octo run sed`.
$custom_rules
"""


def generate_agent_md(relay: Relay, custom_rules: str = "", sync_mode: str = "direct",
                      sync_rules_custom: str = "") -> str:
    terminals = relay.list_terminals()

    if not terminals:
        terminal_lines = "No terminals registered yet. Run `octo join` on remote machines."
    else:
        lines = []
        for t in terminals:
            status = "online" if t["online"] else "offline"
            tags = ", ".join(t.get("tags", []))
            tag_str = f" [{tags}]" if tags else ""
            meta = t.get("meta", {})
            shell = meta.get("shell", "")
            shell_str = f", shell: {shell}" if shell else ""
            ssh = meta.get("ssh", "")
            ssh_str = f", via SSH" if ssh else ""
            desc = meta.get("description", "")
            desc_str = f" - {desc}" if desc else ""
            lines.append(f"- **{t['name']}**{tag_str} ({status}{shell_str}{ssh_str}){desc_str}")
        terminal_lines = "\n".join(lines)

    # Resolve sync rules
    if sync_mode == "custom":
        sync_rules = f"## Code Sync: Custom\n{sync_rules_custom}"
    else:
        sync_rules = SYNC_PRESETS.get(sync_mode, SYNC_PRESETS["direct"])

    return Template(TEMPLATE).substitute(
        terminals=terminal_lines,
        sync_rules=sync_rules,
        custom_rules=custom_rules,
    )


def write_agent_md(relay: Relay, path: str = "CLAUDE.md", custom_rules: str = "",
                   sync_mode: str = "direct", sync_rules_custom: str = "") -> str:
    content = generate_agent_md(relay, custom_rules, sync_mode=sync_mode,
                                sync_rules_custom=sync_rules_custom)

    # Standalone file (e.g. .claude/rules/octo.md): overwrite entirely
    if os.path.basename(path) != "CLAUDE.md":
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    # CLAUDE.md: merge — replace only the openOcto section, keep the rest
    if os.path.exists(path):
        with open(path, "r") as f:
            existing = f.read()

        marker = "# openOcto - Remote Terminals"
        if marker in existing:
            start = existing.index(marker)
            rest = existing[start + len(marker):]
            end_idx = -1
            for i, line in enumerate(rest.split("\n")):
                if i > 0 and line.startswith("# ") and "openOcto" not in line:
                    end_idx = start + len(marker) + rest.index(line)
                    break
            if end_idx > 0:
                content = existing[:start] + content + "\n" + existing[end_idx:]
            else:
                content = existing[:start] + content
        else:
            content = existing.rstrip() + "\n\n" + content

    with open(path, "w") as f:
        f.write(content)
    return path
