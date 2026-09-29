---
name: openocto-jump-hosts
description: Discovers SSH targets behind OpenOcto jump terminals and executes non-interactive commands through them. Use when the user asks which servers an OpenOcto terminal can access, wants to inspect a jump terminal's SSH config, selects an SSH target, or runs a command through the OpenOcto-to-SSH chain.
compatibility: Requires OpenOcto MCP tools or an authenticated octo CLI. Final SSH connections require OpenSSH on the jump terminal.
---

# OpenOcto jump hosts

Use an OpenOcto terminal as a jump machine, then resolve final SSH targets from
that machine's own OpenSSH configuration. Keep the workflow independent of any
specific AI client.

## Select an OpenOcto transport

Use the first available transport and retain it as `OCTO_TRANSPORT`:

1. **MCP**: use OpenOcto tools with the capabilities `remote_ls` and
   `remote_run`. Tool namespaces may differ between clients; match tools by
   capability and schema, not by a hard-coded namespace.
2. **CLI**: use an authenticated `octo` executable:
   - List terminals: `octo ls`
   - Run a simple command:
     `octo run OCTO_JUMP_TERMINAL "COMMAND" --timeout 30 --no-log`
   - For commands with complex quoting, pass the command over stdin with
     `octo run OCTO_JUMP_TERMINAL - --timeout 30 --no-log`.

For MCP execution, call `remote_run` with:

- `terminal`: `OCTO_JUMP_TERMINAL`
- `command`: the exact command to run on the jump terminal
- `timeout`: a bounded timeout appropriate for the operation
- `no_log`: `true` for short diagnostics that need no durable log

Treat the selected MCP or CLI mechanism as `RUN_ON_JUMP(COMMAND)` throughout
this skill.

If neither transport is available, stop and explain that OpenOcto must first be
configured through MCP or the CLI. Do not substitute unrelated remote-access
tools without the user's approval.

## Handoff values

Resolve and retain these values for downstream operations:

- `OCTO_TRANSPORT`: `mcp` or `cli`
- `OCTO_JUMP_TERMINAL`: online terminal returned by the selected transport
- `SSH_ALIAS`: exact alias discovered from the jump terminal's SSH config
- `SSH_OPTIONS`: `-o BatchMode=yes -o ConnectTimeout=15`

Pass these values to workflows such as the companion `openocto-tmux` skill.
Honor explicit values supplied by the user.

## Discover jump terminals and SSH targets

1. List OpenOcto terminals with the selected transport.
2. Select the terminal named by the user. If several terminals are plausible
   and none was named, ask which one to use.
3. On that terminal, inspect only `Host` and `Include` declarations from
   `$HOME/.ssh/config` (`%USERPROFILE%\.ssh\config` on Windows).
4. Follow `Include` declarations while keeping all reads limited to SSH config
   files. Do not read private keys.
5. Exclude wildcard and negated entries such as `Host *`, `Host *.example`,
   and `Host !blocked`.
6. Cache the terminal-to-alias mapping for the current conversation.

On a Windows jump terminal, extract aliases without printing the full config:

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

On Linux or macOS, parse only `Host` declarations from `~/.ssh/config` and
included config files, applying the same exclusions.

Treat discovered entries as configured SSH targets, not proven reachable
servers. SSH config presence does not prove current network access or valid
authentication.

## Resolve and use an SSH alias

Use the exact alias rather than duplicating its hostname or username. Resolve
only non-secret fields when useful:

```text
ssh -G SSH_ALIAS
```

Filter the result to `hostname`, `user`, and `port`; do not report identity-file
paths unless they are directly relevant and the user requests them.

Test or execute through the jump terminal:

```text
ssh -o BatchMode=yes -o ConnectTimeout=15 SSH_ALIAS "COMMAND"
```

Send that complete command through `RUN_ON_JUMP` using the selected OpenOcto
transport. Use a timeout appropriate for `COMMAND`.

Do not test every configured target automatically. Connect only when needed for
the user's requested task.

## Security and failure handling

- Never read `IdentityFile` contents or transmit passwords, private keys,
  access tokens, or MFA answers.
- Do not print the complete SSH config unless the user explicitly requests it.
- Do not weaken host-key verification or automate password authentication.
- Validate user-selected aliases against discovered aliases. Do not guess a
  hostname when the alias is absent.
- On SSH authentication, host-key, DNS, or timeout failure, report the exact
  error and stop instead of retrying with weaker options.
- A raw hostname or IP may be used only when the user explicitly supplies it.

## Result reporting

Report:

- The OpenOcto transport and jump terminal used.
- The configured SSH alias selected.
- The non-sensitive command executed.
- The relevant output and exit status.
- Whether connectivity was proven or the target was only discovered in config.
