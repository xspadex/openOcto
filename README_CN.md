<p align="center">
  <img src="assets/octo-bg.png" alt="OpenOcto" width="400" />
</p>

<h1 align="center">OpenOcto</h1>

<p align="center">任意时刻，任意设备，干任何事</p>

> **设备太多，交给Octo就好了。**

<p align="center">
  <img src="assets/Examples_CN.png" alt="OpenOcto" width="400" />
</p>


<p align="center"><a href="README.md">English</a></p>

- [x] 居家办公，远程控制内网GPU服务器。
- [x] 有事出门，手机监控设备状态，更新命令。
- [x] 多设备协作，多设备管理，交给Octo。
- [x] 兼容ClaudeCode、MCP。
- [x] 无视防火墙。


## 工作原理

```
┌──────────────┐         ┌───────────────┐         ┌──────────────┐
│   你的 AI    │  HTTP   │  Redis 中继   │  HTTP   │   远程 GPU   │
│ (Claude Code)│────────>│  (Upstash)    │<────────│ (octo daemon)│
│              │         │   serverless  │         │              │
│  MCP / CLI   │         │    免费额度   │         │   任意机器   │
└──────────────┘         └───────────────┘         └──────────────┘
```

- **代理端**：通过 MCP 工具或 CLI 命令发送任务
- **中继**：内置免费中继（或自建 Upstash Redis 获得完全隐私）
- **工作端**：daemon 轮询任务、执行、流式回传结果

两端都只发起**出站 HTTP 请求**，无需入站端口、SSH 隧道或 VPN。

## 快速开始

### 1. 安装并配置（本地机器）

```bash
pip install openocto
octo setup             # AI 引导配置 — 对话式完成所有设置
```

可选功能按需安装：

```bash
pip install "openocto[qr]"       # `octo token --qr` 的二维码输出
pip install "openocto[nearby]"   # BLE + 近场加密传输
pip install "openocto[metrics]"  # TensorBoard/tfevents 解析增强
```

> `octo setup` 使用免费 LLM，无需任何 API key 即可启动。自动检测环境，通过对话引导完成中继配置和 LLM 设置。
>
> 偏好手动配置？使用 `octo init`。

### 2. 启动工作端（远程机器）

**方式 A** —— 直接在远程机器安装：

```bash
pip install openocto
octo join --token "$(octo token)" --name gpu --tags "gpu,cuda"
```

> Windows 工作端需要 PowerShell 7（`pwsh.exe`）。Python daemon 使用它执行
> shell 任务，并且不会回退到 Windows PowerShell 5.1。
>
> 先在本地机器运行 `octo token` 获取加入令牌，这样远程机器不需要再跑 `octo init`。

**方式 B** —— 远程机器没网？用跳板机 + `--ssh`：

```bash
# 在能 SSH 到 GPU 机器的跳板机上：
octo join --token "..." --name gpu --tags "gpu,cuda" --ssh "user@gpu-内网IP"
```

### 3. 使用

```bash
octo ls                              # 查看所有终端
octo run gpu "nvidia-smi"            # 执行命令
octo run gpu "python train.py"       # 启动训练
octo run gpu "python train.py" --notify phone  # 训练完通知手机
octo cat gpu /work/model.py          # 读取远程文件
octo edit gpu /work/config.py --old "lr=0.001" --new "lr=0.0005"
octo kill gpu                        # 终止运行中的命令
```

### 4. 接入 AI 代理（可选）

在项目目录的 `.mcp.json` 中添加：

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

Claude Code（或任何兼容 MCP 的代理）即可直接控制你的远程终端。

### Agent Skills

跨 Agent 技能以 `.agents/skills/` 作为唯一规范源：

- `openocto-jump-hosts` 用于发现并访问跳板机后的 SSH 目标。
- `openocto-tmux` 用于通过该链路管理持久 tmux 会话。

两个技能均支持 OpenOcto MCP 工具和已认证的 `octo` CLI。
`.cursor/skills/` 等客户端专用目录只保留加载规范技能的适配入口。

## 训练通知

训练跑完或报错，手机自动收到通知：

```bash
octo run gpu "python train.py" --notify phone
octo run gpu "python train.py" --notify phone --notify-message "实验A训练完成"
```

穿透防火墙，通知直达手机锁屏。

## MCP 工具

| 工具 | 说明 |
|---|---|
| `remote_ls` | 列出所有终端及其状态、模式和标签 |
| `remote_run(terminal, command)` | 执行 shell 命令（流式输出） |
| `remote_read(terminal, path)` | 读取文件（带行号） |
| `remote_edit(terminal, path, old, new)` | 字符串替换编辑文件 |
| `remote_glob(terminal, pattern)` | 按 glob 模式搜索文件 |
| `remote_grep(terminal, pattern)` | 按正则搜索文件内容 |
| `remote_kill(terminal)` | 终止运行中的命令 |
| `remote_logs(terminal)` | 查看当前或上一个任务的输出 |
| `remote_send(source, target, file)` | 在终端之间传输文件 |
| `remote_metrics(terminal)` | GPU 状态和训练指标 |
| `remote_wake / remote_cool` | 控制轮询模式 |

## GPU 监控

从 CLI、浏览器或手机监控 GPU 利用率、显存、温度和训练 loss。

```bash
octo metrics gpu                    # 一次性查看 GPU 状态 + 训练指标
octo metrics --dashboard            # 打开 Web 看板（所有 GPU 服务器）
```

daemon 读取 `nvidia-smi` 和 TensorBoard tfevents 文件，训练脚本无需做任何修改。

## 文件传输

```bash
octo send local gpu ~/data/dataset.tar.gz --dest /work/data/dataset.tar.gz
```

自动选择最快路由：

| 路由 | 条件 | 速度 |
|---|---|---|
| **局域网直传** | 同一网络 | 局域网满速 |
| **Redis 中继** | 文件 < 512KB | 即时 |
| **云存储** | 大文件，跨网络 | S3 上传 → 预签名 URL → 下载 |

云存储配置（可选）：`octo config --storage` —— 支持 Cloudflare R2、AWS S3 或任何 S3 兼容服务。

## 配置

### 中继

`octo init` 提供两个选项：

| 选项 | 配置 | 隐私 |
|------|------|------|
| **免费中继**（默认） | 即开即用，无需注册 | 随机 workspace ID 隔离 |
| **自建 Redis** | 在 [upstash.com](https://upstash.com) 免费创建 | 完全隐私，独立实例 |

**添加更多机器** —— 用加入令牌，不需要每台都跑 `octo init`：

```bash
octo token                    # 打印 octo://eyJ...
octo token --qr               # 或显示二维码（手机扫码）
```

### CF Worker 代理（可选）

团队使用自建 Redis 时，部署 Cloudflare Worker 作为代理，成员无需直接获取 Redis 凭据。详见 [`cf-worker/`](cf-worker/)。

### 云存储（可选）

用于跨网络的大文件传输：

```bash
octo config --storage         # 配置 S3 地址、密钥、桶
```

支持 Cloudflare R2（免费 10GB）、AWS S3、MinIO 或任何 S3 兼容服务。

## 权限与安全

**个人模式**（默认）：拥有 Redis 凭据即可完全访问所有终端，适合个人使用。

**公共模式**：适用于共享环境（实验室团队、多用户场景）：

```bash
octo network create mylab --public
octo register alice                       # 生成 Ed25519 密钥对
octo invite gpu --role readwrite          # 分享访问权限
```

角色：`full`（shell + kill + edit）/ `readwrite`（读 + 编辑）/ `readonly`（只读）

每个任务都用发送方的 Ed25519 密钥签名，daemon 执行前验证签名。

## Android App

配套 Android 应用将手机变成 OpenOcto 节点：

- 扫描二维码即可加入网络
- 后台运行守护进程
- 查看 GPU 指标并自动刷新
- 接收训练完成通知
- 中英文界面自动切换

源码：[`android/`](android/)

## 架构

```
src/openocto/
├── relay.py              Upstash Redis REST 客户端（原子 Lua CAS）
├── daemon.py             工作端：轮询、执行、流式回传
├── cli.py                CLI（20+ 子命令）
├── mcp_server.py         MCP 服务器（11 个结构化工具）
├── storage.py            S3 兼容文件传输（并行分片上传）
├── permissions.py        基于角色的 ACL 引擎
├── signing.py            Ed25519 任务签名与验证
├── config.py             配置与加入令牌编码
├── metrics_dashboard.py  GPU 监控 Web 看板
└── agent_md.py           CLAUDE.md 生成器
```

- **基础安装保持精简** —— 二维码、近场传输和训练指标解析通过可选 extras 安装
- **协议**：Redis 中的 JSON 任务，生命周期 `PENDING → RUNNING → DONE`
- **安全**：Ed25519 签名任务、基于角色的 ACL、路径校验、原子 CAS 更新

## CLI 参考

```
设置:
  octo setup                             AI 引导配置向导（推荐）
  octo init                              手动配置中继
  octo token [--qr]                      生成加入令牌
  octo join --name NAME [选项]           启动 daemon
       --tags T                          逗号分隔的标签
       --ssh user@host                   通过 SSH 转发
       --daemon                          后台运行

远程操作:
  octo run TARGET "COMMAND"              执行命令
       --notify TERMINAL                 完成后通知指定终端
       --notify-message MSG              自定义通知内容
  octo cat TARGET /path                  读取文件
  octo edit TARGET /path --old X --new Y 编辑文件
  octo glob TARGET "**/*.py"             按模式搜索文件
  octo grep TARGET "pattern"             搜索文件内容
  octo logs TARGET [-f] [--tail N]       查看任务输出
  octo kill TARGET                       终止命令
  octo send SOURCE TARGET FILE           传输文件
  octo metrics TARGET                    GPU + 训练指标
  octo metrics --dashboard               打开 Web 看板

终端管理:
  octo ls                                列出所有终端
  octo wake TARGET                       快速轮询
  octo cool TARGET                       低功耗轮询

网络与权限:
  octo network create NAME [--public]    创建网络
  octo register NAME                     注册设备身份
  octo invite TARGET --role ROLE         生成邀请
  octo acl TARGET [--default R]          管理访问权限

代理:
  octo agent-md                          生成 CLAUDE.md
  octo agent --backend claude            启动 Claude Code 并同步到手机
  octo agent --backend codex             启动 Codex 并同步到手机
  octo agent-serve --backend codex-cli   启动 Codex agent daemon
  octo mcp-server                        启动 MCP 服务器
```

## 故障排查

| 问题 | 解决方案 |
|---|---|
| "OpenOcto not configured" | 运行 `octo init` |
| 终端显示 "offline" | daemon 未运行，检查 `~/.octo/daemon.log` |
| 命令卡住 | `octo kill TARGET` 或 Ctrl+C |
| 首次响应慢 | 终端在 cool 模式，收到任务后自动唤醒（最多 60 秒） |
| `pkill -f` 误杀 wrapper | 用括号技巧：`pkill -f "[t]rain_script"` |
| Windows 找不到 `python3` | daemon 在 Windows 上自动检测 `python` |
| Windows / 公司网络下出现 `CERTIFICATE_VERIFY_FAILED` | 你的 Python 环境可能不信任系统证书或公司 HTTPS 代理证书。可先执行 `pip install pip-system-certs`，重开终端后重试。这在 Conda / Miniforge 环境下较常见。 |
| 注册中继失败：`Connection failed (direct + proxy)` / `Tunnel connection failed: 403` | 终端里的 `HTTP(S)_PROXY` 指向了 IDE 内部代理，或公司安全网关拦截了中继域名。改用浏览器实际使用的公司代理启动 daemon，见下节。 |

### 公司网络 / 代理环境下的启动

在受管控的公司网络中，daemon 注册中继可能失败（`~/.octo/daemon.log` 报
`Connection failed (direct + proxy): Tunnel connection failed: 403`）。常见原因：

1. **`HTTP(S)_PROXY` 指向 IDE 内部代理**：部分 AI 编码工具（如 opencode）会在其终端里注入
   `HTTPS_PROXY=http://localhost:PORT`。这类端口只放行自家 API，不是通用代理，
   CONNECT 其他域名一律返回 403。
2. **公司安全网关（SWG）拦截中继域名**：请求被 302 跳转到网关警告页
   （"您访问的网站可能存在安全风险"），Python 请求拿到的是 HTML 而非 JSON。
3. **直连被防火墙拦截**：不走代理直接超时。

解决步骤：

1. **找到浏览器实际使用的代理**。浏览器能正常上网说明该代理可用：
   - Windows：注册表 `HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings`
     中的 `ProxyServer` 值（或"设置 → 网络 → 代理"），例如 `proxy.example.com:8080`。
2. **在浏览器中打开中继地址**（`~/.octo/config.json` 里的 `redis_url`）。
   若弹出安全网关风险警告页，点击"接受风险并继续"——网关会按用户身份放行该域名，
   之后同一账号经代理发出的请求不再被拦。
3. **用浏览器同款代理并带上认证启动 daemon**（把 `USER:PASS` 换成实际账号密码；
   密码中的特殊字符需 URL 编码，例如 `@` 写作 `%40`）：

   ```powershell
   # PowerShell
   $env:HTTP_PROXY  = "http://USER:PASS@proxy.example.com:8080"
   $env:HTTPS_PROXY = "http://USER:PASS@proxy.example.com:8080"
   octo join --name NAME --daemon
   ```

   ```bash
   # bash
   HTTP_PROXY=http://USER:PASS@proxy.example.com:8080 \
     HTTPS_PROXY=http://USER:PASS@proxy.example.com:8080 \
     octo join --name NAME --daemon
   ```

4. **验证**：`octo ls` 应显示该终端 online。

> 注意：
> - 后台 daemon 会继承启动时的环境变量。如果网络环境变化导致中继失联，先结束旧
>   daemon 进程，再带正确代理重新启动。
> - 从源码运行时还需设置 `PYTHONPATH` 指向仓库的 `src/` 目录。
> - 不要把带密码的代理地址提交到仓库或写入会被同步的配置文件。

## 许可证

Apache 2.0
