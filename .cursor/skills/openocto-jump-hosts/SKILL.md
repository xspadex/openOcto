---
name: openocto-jump-hosts
description: Discovers SSH targets behind OpenOcto jump terminals and executes non-interactive commands through them. Use when the user asks which servers an OpenOcto terminal can access, wants to inspect a jump terminal's SSH config, selects an SSH target, or runs a command through the OpenOcto-to-SSH chain.
---

# OpenOcto jump hosts

Use OpenOcto terminals as jump machines and resolve final SSH targets from each
jump machine's own OpenSSH configuration.

## Handoff values

Resolve and retain these values for downstream operations:

- `OCTO_JUMP_TERMINAL`: online terminal returned by `remote_ls`
- `SSH_ALIAS`: exact alias discovered from the jump terminal's SSH config
- `SSH_OPTIONS`: `-o BatchMode=yes -o ConnectTimeout=15`

Pass these values to workflows such as the companion `openocto-tmux` skill.
Honor explicit values supplied by the user.

## Discover jump terminals and SSH targets

1. Call OpenOcto MCP `remote_ls`.
2. Select the terminal named by the user. If several terminals are plausible
   and none was named, ask which one to use.
3. On that terminal, inspect only SSH host declarations from
   `$HOME/.ssh/config` (`%USERPROFILE%\.ssh\config` on Windows).
4. Follow `Include` directives when present.
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
apply the same exclusions.

Treat discovered entries as "configured SSH targets", not proven reachable
servers. SSH config presence does not prove current network access or valid
authentication.

## Resolve and use an SSH alias

Use the exact alias rather than duplicating its hostname or username. Resolve
only non-secret fields when useful:

```powershell
ssh -G SSH_ALIAS |
    Select-String -Pattern '^(hostname|user|port) '
```

Execute through OpenOcto MCP `remote_run`:

- `terminal`: `OCTO_JUMP_TERMINAL`
- `command`: `ssh -o BatchMode=yes -o ConnectTimeout=15 SSH_ALIAS "<command>"`
- Use a timeout appropriate for the requested command.
- Set `no_log` for short diagnostic operations that need no durable log.

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

- The OpenOcto jump terminal used.
- The configured SSH alias selected.
- The non-sensitive command executed.
- The relevant output and exit status.
- Whether connectivity was proven or the target was only discovered in config.
