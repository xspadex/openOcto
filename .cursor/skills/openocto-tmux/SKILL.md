---
name: openocto-tmux
description: Operates persistent tmux sessions on SSH targets reached through OpenOcto jump terminals. Use when the user asks to create, list, inspect, continue, interrupt, or close tmux sessions; send commands or answers; capture terminal output; or conduct a stateful pseudo-interactive workflow through OpenOcto.
---

# OpenOcto tmux sessions

Manage persistent tmux sessions after an OpenOcto jump terminal and final SSH
alias have been resolved.

## Required routing context

Use:

- `OCTO_JUMP_TERMINAL`: selected online OpenOcto terminal
- `SSH_ALIAS`: selected SSH config alias on that jump terminal
- `SESSION`: tmux session named by the user
- `SSH_OPTIONS`: `-o BatchMode=yes -o ConnectTimeout=15`

If the jump terminal or SSH alias is unknown, apply the companion
`openocto-jump-hosts` skill first. Do not guess routing values.

Run tmux control operations with OpenOcto MCP `remote_run`:

- `terminal`: `OCTO_JUMP_TERMINAL`
- `command`: `ssh SSH_OPTIONS SSH_ALIAS "<tmux command>"`
- Use a short timeout and `no_log` for control operations.

Do not run `tmux attach` through OpenOcto. OpenOcto does not allocate a PTY, so
attachment will not produce a usable interactive terminal and may hang.

## Session safety

1. Accept session names containing only letters, digits, `_`, `-`, and `.`.
2. List sessions when the requested session is ambiguous.
3. Never replace or kill a session unless the user explicitly asks.
4. Keep one writer per session and avoid concurrent `send-keys` calls.
5. Send literal input first and `Enter` separately.
6. Capture output after input and verify that it was processed.
7. Never send passwords, MFA answers, or responses to unexpected prompts.

## Core operations

### List sessions

```bash
tmux list-sessions -F '#{session_name} #{session_attached} #{session_windows}'
```

If tmux reports that no server is running, report that there are no sessions.

### Create a session

Check first:

```bash
tmux has-session -t SESSION 2>/dev/null
```

Create only when absent:

```bash
tmux new-session -d -s SESSION
```

Create in a specific directory:

```bash
tmux new-session -d -s SESSION -c /absolute/path
```

### Send a command or answer

For ordinary single-line input:

```bash
tmux send-keys -t SESSION -l 'TEXT'
tmux send-keys -t SESSION Enter
```

Shell-quote `TEXT` correctly. For multiline text or complicated quoting,
base64-encode the UTF-8 text and paste it through a tmux buffer:

```bash
printf '%s' 'BASE64' | base64 -d | tmux load-buffer -
tmux paste-buffer -t SESSION
tmux send-keys -t SESSION Enter
```

### Capture output

```bash
tmux capture-pane -p -J -t SESSION -S -100
```

After sending input, normally wait one second in the same SSH command and
capture the pane. For long-running operations, make bounded follow-up captures
only when progress is expected or requested.

### Interrupt the foreground process

```bash
tmux send-keys -t SESSION C-c
sleep 1
tmux capture-pane -p -J -t SESSION -S -50
```

### Inspect and close a session

Inspect the pane before closing:

```bash
tmux list-panes -t SESSION -F '#{pane_pid} #{pane_current_command} #{pane_dead}'
```

Only after explicit authorization:

```bash
tmux kill-session -t SESSION
```

## Pseudo-interactive workflow

For each user turn:

1. Resolve routing context and confirm the session exists.
2. Send the user's text literally, followed by `Enter`.
3. Wait briefly and capture the recent pane.
4. Return only new output when it can be identified. Otherwise state that the
   capture includes previous context.
5. State whether a prompt returned, the process is still running, or user input
   is required.

This is store-and-forward pseudo-interaction, not a true PTY. It is suitable for
shells, REPLs, and simple question-and-answer programs. Treat full-screen TUIs,
interactive editors, password prompts, MFA, mouse input, and terminal
resize-dependent applications as unsupported; recommend SSH or remote desktop.

## Failure handling

- Missing tmux: report it and provide the installation command, but do not use
  `sudo` or install packages without authorization.
- Missing session: list available sessions and create one only when requested.
- Unexpected prompt: capture and report it instead of guessing an answer.
- Timeout: capture the pane once to distinguish a running command from a failed
  control operation. Do not resend the same input blindly.
- SSH routing failure: report it and return to the `openocto-jump-hosts`
  workflow rather than changing authentication or host-key settings.

## Result reporting

Report:

- The OpenOcto jump terminal, SSH alias, and tmux session.
- The command or non-sensitive input submitted.
- The relevant captured output.
- Whether the operation completed, is running, or requires input.
