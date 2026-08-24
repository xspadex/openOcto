---
name: tmux-over-openocto
description: Discovers SSH targets from an OpenOcto jump terminal and operates persistent tmux sessions on those targets. Use when the user asks which servers a jump terminal can reach; asks to create, list, inspect, continue, interrupt, or stop tmux sessions; sends commands or answers to a session; captures terminal output; or performs stateful pseudo-interactive terminal workflows through OpenOcto.
---

# tmux over OpenOcto

Use OpenOcto as the transport to a jump machine, SSH from that machine to the
Linux host, and use tmux to preserve terminal state between calls.

## Topology placeholders

- OpenOcto jump terminal: `OCTO_JUMP_TERMINAL` (discover with `remote_ls`)
- SSH alias on that terminal: `SSH_ALIAS` (discover from its SSH config)
- tmux session: `SESSION` (use the name supplied by the user)
- SSH options: `-o BatchMode=yes -o ConnectTimeout=15`

Honor a different terminal, SSH host, or session when the user specifies one.
Do not store or send passwords, private keys, access tokens, or MFA answers.

## Discover SSH targets on a jump terminal

Treat OpenOcto terminals as jump terminals unless the user says otherwise. The
final SSH targets are defined on each jump terminal in the OpenSSH config file
at `$HOME/.ssh/config` (normally `%USERPROFILE%\.ssh\config` on Windows).
`.ssh/config` is a file, not a directory.

At the beginning of a task:

1. Use `remote_ls` to identify online OpenOcto terminals.
2. Select the terminal named by the user. If none is named and there is more
   than one plausible terminal, ask which one to use.
3. Discover configured SSH aliases from that terminal's own `.ssh/config`.
   Cache the terminal-to-alias mapping for the current conversation.
4. Exclude wildcard and negated patterns such as `Host *`, `Host *.example`,
   and `Host !blocked`.
5. If the user names an alias, use that exact config alias rather than copying
   its IP address or username into commands.
6. Describe aliases as "configured SSH targets" until a connection succeeds;
   presence in SSH config does not prove current reachability or authentication.

On a Windows jump terminal, list aliases without printing the complete config:

```powershell
$config = Join-Path $HOME '.ssh\config'
if (Test-Path -LiteralPath $config) {
    Get-Content -LiteralPath $config |
        ForEach-Object {
            if ($_ -match '^\s*Host\s+(.+?)\s*$') {
                $Matches[1] -split '\s+'
            }
        } |
        Where-Object { $_ -and $_ -notmatch '[*!?]' } |
        Sort-Object -Unique
}
```

On a Linux or macOS jump terminal, parse only `Host` declarations from
`~/.ssh/config` and apply the same wildcard exclusions. Follow `Include`
directives when present, but do not read private keys.

For a selected alias, optionally resolve only its non-secret connection fields:

```powershell
ssh -G SSH_ALIAS |
    Select-String -Pattern '^(hostname|user|port) '
```

Do not dump the complete SSH config unless the user explicitly requests it.
Never read files referenced by `IdentityFile`. Test an actual connection only
when the requested task requires one.

## Transport

Perform operations with the OpenOcto MCP `remote_run` tool:

- `terminal`: `OCTO_JUMP_TERMINAL`
- `command`: `ssh -o BatchMode=yes -o ConnectTimeout=15 SSH_ALIAS "<remote command>"`
- Use a short timeout for tmux control operations.
- Set `no_log` when the control command does not need a durable OpenOcto log.

Do not run `tmux attach` through OpenOcto. OpenOcto does not allocate a PTY, so
attachment will not provide a usable interactive terminal and may hang.

## Session safety

1. Accept session names containing only letters, digits, `_`, `-`, and `.`.
2. List sessions before acting when the requested session is ambiguous.
3. Never replace or kill an existing session unless the user explicitly asks.
4. Use `tmux send-keys -l` for literal text, then send `Enter` separately.
5. Capture output after sending input and verify the expected command or prompt
   appears. Submitting input alone is not proof that it was processed.
6. Never send passwords or confirmation answers to an unexpected prompt.
7. Keep one writer per session. Avoid simultaneous `send-keys` calls.

## Core operations

### List sessions

Run:

```bash
tmux list-sessions -F '#{session_name} #{session_attached} #{session_windows}'
```

If tmux reports that no server is running, report that there are no sessions.

### Create a session

First check whether it exists:

```bash
tmux has-session -t SESSION 2>/dev/null
```

When it does not exist:

```bash
tmux new-session -d -s SESSION
```

To start in a specific directory:

```bash
tmux new-session -d -s SESSION -c /absolute/path
```

### Send a command or answer

For ordinary single-line input:

```bash
tmux send-keys -t SESSION -l 'TEXT'
tmux send-keys -t SESSION Enter
```

Shell-quote `TEXT` correctly. For multiline text or text containing complicated
quotes, encode its UTF-8 bytes as base64 and paste it through a tmux buffer:

```bash
printf '%s' 'BASE64' | base64 -d | tmux load-buffer -
tmux paste-buffer -t SESSION
tmux send-keys -t SESSION Enter
```

### Capture output

Capture recent plain text and join wrapped lines:

```bash
tmux capture-pane -p -J -t SESSION -S -100
```

After sending input, normally wait one second in the same SSH command and then
capture the pane. For a long-running operation, perform bounded follow-up
captures only when progress is expected or requested.

### Interrupt the foreground program

```bash
tmux send-keys -t SESSION C-c
sleep 1
tmux capture-pane -p -J -t SESSION -S -50
```

### Inspect and close a session

Inspect the current pane before closing:

```bash
tmux list-panes -t SESSION -F '#{pane_pid} #{pane_current_command} #{pane_dead}'
```

Only after explicit user confirmation:

```bash
tmux kill-session -t SESSION
```

## Pseudo-interactive workflow

For each user turn:

1. Resolve the OpenOcto jump terminal, discover or validate the SSH alias, and
   resolve the tmux session.
2. Confirm the session exists.
3. Send the user's text literally and send `Enter`.
4. Wait briefly, then capture the recent pane.
5. Return only the new relevant output when it can be identified; otherwise
   return the recent pane and say that it includes prior context.
6. State whether a shell/application prompt returned or the process still
   appears to be running.

This workflow is suitable for shells, REPLs, and simple question-and-answer
programs. It is not a true interactive PTY. Treat full-screen TUIs, editors,
password prompts, MFA, mouse input, and terminal resize-dependent programs as
unsupported; recommend SSH or remote desktop for those cases.

## Error handling

- SSH authentication or host-key failure: report the exact error. Do not weaken
  host-key checking or fall back to password automation.
- Missing SSH config or alias: report the config path checked and list any
  aliases that were discovered; do not guess a hostname.
- Missing tmux: report it and provide the installation command, but do not use
  `sudo` or install packages without user authorization.
- Missing session: list available sessions. Create one only if requested.
- Unexpected prompt: capture and report it instead of guessing an answer.
- Timeout: capture the pane once to distinguish a running command from a failed
  control operation; do not repeatedly resend the same input.

## Result reporting

Always report:

- The OpenOcto jump terminal, SSH alias, and tmux session used.
- The command or non-sensitive input submitted.
- The relevant captured output.
- Whether the operation completed, is still running, or needs user input.
