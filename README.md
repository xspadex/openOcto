<p align="center">
  <img src="assets/octo-bg.png" alt="openOcto" width="400" />
</p>

<h1 align="center">openOcto</h1>

<p align="center">Let your AI agent control any terminal, anywhere. Zero config, firewall-proof.</p>

openOcto connects AI agents (Claude Code, etc.) to remote machines — through firewalls, NATs, and restricted networks. All communication goes through a Redis relay (outbound HTTP only), so no ports need to be opened.

Built for researchers who need AI to manage GPU servers they can't directly access.

## How It Works

```
┌──────────────┐         ┌───────────────┐         ┌──────────────┐
│  Your Agent  │  HTTP   │  Redis Relay  │  HTTP   │  Remote GPU  │
│ (Claude Code)│────────>│  (Upstash)    │<────────│ (octo daemon)│
│              │         │  serverless   │         │              │
│ MCP / CLI    │         │  free tier    │         │  any machine │
└──────────────┘         └───────────────┘         └──────────────┘
```

- **Agent side**: sends tasks via MCP tools or CLI commands
- **Relay**: your own Upstash Redis instance (free tier works fine)
- **Worker side**: daemon polls for tasks, executes, streams results back

Both sides only make **outbound HTTP requests**. No inbound ports, no SSH tunnels, no VPN.

## Quick Start

### 1. Create a Redis relay (one-time, 30 seconds)

Go to [upstash.com](https://upstash.com) → sign up (free) → create a Redis database. Copy the **REST URL** and **REST Token**.

### 2. Install & configure (local machine)

```bash
pip install openocto
octo init              # Paste your Redis URL and Token
```

### 3. Start a worker (remote machine)

**Option A** — Install directly on the remote:

```bash
pip install openocto
octo join --token "$(octo token)" --name gpu --tags "gpu,cuda"
```

> Run `octo token` on your local machine first to get a join token. This avoids repeating `octo init` on every machine.

**Option B** — Remote has no internet? Use a jump server with `--ssh`:

```bash
# On a jump server that can SSH into the GPU machine:
octo join --token "..." --name gpu --tags "gpu,cuda" --ssh "user@gpu-internal-ip"
```

### 4. Use it

```bash
octo ls                              # See all terminals
octo run gpu "nvidia-smi"            # Run a command
octo run gpu "python train.py"       # Start training
octo cat gpu /work/model.py          # Read a remote file
octo edit gpu /work/config.py --old "lr=0.001" --new "lr=0.0005"
octo kill gpu                        # Kill running command
```

### 5. Connect your AI agent (optional)

Add to `.mcp.json` in your project:

```json
{
  "mcpServers": {
    "openocto": {
      "command": "octo",
      "args": ["mcp-server"]
    }
  }
}
```

Claude Code (or any MCP-compatible agent) can now control your remote terminals directly.

## MCP Tools

| Tool | Description |
|---|---|
| `remote_ls` | List all terminals with status, mode, and tags |
| `remote_run(terminal, command)` | Execute a shell command (streaming output) |
| `remote_read(terminal, path)` | Read a file with line numbers |
| `remote_edit(terminal, path, old, new)` | Edit a file by string replacement |
| `remote_glob(terminal, pattern)` | Search files by glob pattern |
| `remote_grep(terminal, pattern)` | Search file contents by regex |
| `remote_kill(terminal)` | Kill running command |
| `remote_logs(terminal)` | View output of current or last task |
| `remote_send(source, target, file)` | Transfer files between terminals |
| `remote_metrics(terminal)` | GPU status and training metrics |
| `remote_wake / remote_cool` | Control polling mode |

## GPU Metrics

Monitor GPU utilization, VRAM, temperature, and training loss — from CLI, web browser, or phone.

```bash
octo metrics gpu                    # One-shot: print GPU status + training metrics
octo metrics --dashboard            # Open web dashboard (all GPU servers)
```

The daemon reads `nvidia-smi` and TensorBoard tfevents files. No code changes needed in your training scripts.

## File Transfer

```bash
octo send local gpu ~/data/dataset.tar.gz --dest /work/data/dataset.tar.gz
```

Smart routing picks the fastest path:

| Route | When | Speed |
|---|---|---|
| **LAN direct** | Same network | Full LAN speed |
| **Redis relay** | File < 512KB | Instant |
| **Cloud storage** | Large files, different networks | S3 upload → presigned URL → download |

Cloud storage setup (optional): `octo config --storage` — supports Cloudflare R2, AWS S3, or any S3-compatible service.

## Configuration

### Redis Relay (required)

| Config | What it is | Where to get it |
|--------|-----------|-----------------|
| Redis URL | Upstash REST API endpoint | [upstash.com](https://upstash.com) → your database → REST URL |
| Redis Token | Bearer token | Upstash dashboard → REST Token |
| Workspace | Namespace for isolation | Your choice, default `default` |

**Adding more machines** — use a join token instead of repeating `octo init`:

```bash
octo token                    # prints octo://eyJ...
octo token --qr               # or show QR code (for phone)
```

### CF Worker Proxy (optional)

If you don't want to share Redis credentials directly (e.g. in a team), deploy the included Cloudflare Worker as a proxy. See [`cf-worker/`](cf-worker/).

### Cloud Storage (optional)

For large file transfers between terminals on different networks:

```bash
octo config --storage         # S3 endpoint, keys, bucket
```

Works with Cloudflare R2 (free 10GB), AWS S3, MinIO, or any S3-compatible service.

## Permissions & Security

**Personal mode** (default): anyone with the Redis credentials has full access. Fine for solo use.

**Public mode**: for shared environments (lab teams, multi-user setups):

```bash
octo network create mylab --public
octo register alice                       # Generate Ed25519 keypair
octo invite gpu --role readwrite          # Share access
```

Roles: `full` (shell + kill + edit) / `readwrite` (read + edit) / `readonly` (read only)

Every task is signed with the sender's Ed25519 key and verified by the daemon before execution.

## Android App

The companion app turns your phone into an openOcto node:

- Scan QR code to join a network
- Run as a background daemon
- View GPU metrics with auto-refresh
- Receive and execute tasks

Source: [`android/`](android/)

## Architecture

```
src/openocto/
├── relay.py              Upstash Redis REST client (atomic Lua CAS)
├── daemon.py             Worker: poll, execute, stream results
├── cli.py                CLI (20+ subcommands)
├── mcp_server.py         MCP server (11 structured tools)
├── storage.py            S3-compatible file transfer (parallel multipart)
├── permissions.py        Role-based ACL engine
├── signing.py            Ed25519 task signing & verification
├── config.py             Config & join token encoding
├── metrics_dashboard.py  GPU metrics web dashboard
└── agent_md.py           CLAUDE.md generator
```

- **Zero external dependencies** — Python standard library only
- **Protocol**: JSON tasks in Redis, lifecycle `PENDING → RUNNING → DONE`
- **Security**: Ed25519 signed tasks, role-based ACL, path validation, atomic CAS updates

## CLI Reference

```
Setup:
  octo init                              Configure Redis relay
  octo token [--qr]                      Generate join token
  octo join --name NAME [options]        Start daemon
       --tags T                          Comma-separated tags
       --ssh user@host                   Forward via SSH
       --daemon                          Run in background

Remote Operations:
  octo run TARGET "COMMAND"              Execute command
  octo cat TARGET /path                  Read file
  octo edit TARGET /path --old X --new Y Edit file
  octo glob TARGET "**/*.py"             Search files by pattern
  octo grep TARGET "pattern"             Search file contents
  octo logs TARGET [-f] [--tail N]       View task output
  octo kill TARGET                       Kill running command
  octo send SOURCE TARGET FILE           Transfer file
  octo metrics TARGET                    GPU + training metrics
  octo metrics --dashboard               Open web dashboard

Terminal Management:
  octo ls                                List all terminals
  octo wake TARGET                       Fast polling
  octo cool TARGET                       Low-power polling

Network & Permissions:
  octo network create NAME [--public]    Create network
  octo register NAME                     Register device identity
  octo invite TARGET --role ROLE         Generate invite
  octo acl TARGET [--default R]          Manage access

Agent:
  octo agent-md                          Generate CLAUDE.md
  octo mcp-server                        Start MCP server
```

## Troubleshooting

| Problem | Solution |
|---|---|
| "openOcto not configured" | Run `octo init` |
| Terminal shows "offline" | Daemon not running, check `~/.octo/daemon.log` |
| Command hangs | `octo kill TARGET` or Ctrl+C |
| Slow first response | Terminal in cool mode, auto-wakes on task (up to 60s) |
| `pkill -f` kills the wrapper | Use bracket trick: `pkill -f "[t]rain_script"` |
| Windows: `python3` not found | Daemon auto-detects `python` on Windows |

## License

Apache 2.0
