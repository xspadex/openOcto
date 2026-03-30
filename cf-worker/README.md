# openOcto Relay Proxy (Cloudflare Worker)

将 Upstash Redis 请求通过 Cloudflare Worker 代理转发，解决：
- 国内直连 Upstash 被墙/不稳定
- 公司网络拦截 HTTPS（Worker 支持 HTTP 访问）
- 隐藏 Redis token（token 存在 Worker 端，不暴露给客户端）

## 部署步骤

### 1. 安装 Wrangler CLI

```bash
npm install -g wrangler
```

### 2. 登录 Cloudflare

```bash
wrangler login
```

浏览器会弹出授权页面，点击允许。

### 3. 配置 Upstash 密钥

```bash
cd cf-worker

# 设置你的 Upstash Redis URL（不含尾部斜杠）
wrangler secret put UPSTASH_URL
# 输入: https://your-redis-xxx.upstash.io

# 设置你的 Upstash Redis token
wrangler secret put UPSTASH_TOKEN
# 输入: AXxxxxxxxxxxxxxxxxxxxx
```

### 4. 部署

```bash
wrangler deploy
```

部署成功后会输出 Worker URL，类似：
```
https://openocto-relay.your-account.workers.dev
```

### 5. 测试

```bash
# 测试 Worker 是否正常
curl http://openocto-relay.your-account.workers.dev/health

# 测试 Redis 连通性
curl http://openocto-relay.your-account.workers.dev/PING
# 应返回: {"result":"PONG"}
```

### 6. 在 openOcto 中配置

```bash
octo init
# Redis URL: （填你的 Upstash URL，用于直连）
# Redis Token: （填你的 Upstash Token）
# Workspace: default
# Proxy URL: http://openocto-relay.your-account.workers.dev
```

或者只用 Proxy（不配 Upstash 直连）：
```bash
octo init
# Redis URL: （留空或随便填）
# Redis Token: （留空或随便填）
# Proxy URL: http://openocto-relay.your-account.workers.dev
```

## 工作原理

```
客户端 (octo CLI/daemon)
  │
  ├─ 优先：直连 Upstash (HTTPS)
  │   ↓ 失败
  └─ 降级：CF Worker 代理 (HTTP)
       │
       └─→ Upstash Redis (HTTPS, Worker 服务器端发出)
```

## 自定义域名（可选）

编辑 `wrangler.toml`，取消注释 routes 部分，改成你的域名：

```toml
routes = [
  { pattern = "relay.yourdomain.com", custom_domain = true }
]
```

然后重新部署：`wrangler deploy`

## 免费额度

Cloudflare Workers 免费版：
- 每天 100,000 次请求
- 对于个人/小团队完全够用
