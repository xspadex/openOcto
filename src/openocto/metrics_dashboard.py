"""GPU Metrics Dashboard — all GPU servers at a glance.

`octo metrics --dashboard` opens a browser page showing all GPU terminals.
"""

import json
import os
import socket
import sys
import threading
import time
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from .config import get_relay_config
from .relay import Relay


def _get_relay() -> Relay:
    rc = get_relay_config()
    return Relay(rc["redis_url"], rc["redis_token"], rc["workspace"],
                 proxy_url=rc.get("proxy_url", ""))


def _fetch_metrics(relay: Relay, terminal: str, logdir: str = "", tail: int = 20) -> dict:
    """Submit metrics task and wait for result."""
    extra = {"tail": tail}
    if logdir:
        extra["logdir"] = logdir
    task_id = relay.submit_task(terminal, task_type="metrics", **extra)
    try:
        relay.set_mode(terminal, "wake")
    except Exception:
        pass

    deadline = time.time() + 60
    while time.time() < deadline:
        task = relay.poll_task(terminal, task_id=task_id)
        if not task:
            task = relay.poll_task(terminal)
            if task and task.get("id") != task_id:
                time.sleep(2)
                continue
        if task and task.get("status") in ("DONE", "FAILED"):
            output = task.get("output", "")
            exit_code = task.get("exit_code", 0) or 0
            relay.clear_task(terminal, task_id=task_id)
            return {"ok": exit_code == 0, "data": output, "ts": int(time.time())}
        time.sleep(2)
    return {"ok": False, "data": "Timeout", "ts": int(time.time())}


def _list_gpu_terminals(relay: Relay) -> list:
    """List all GPU-tagged online terminals (fast, no metrics fetch)."""
    terminals = relay.list_terminals()
    return [
        t["name"] for t in terminals
        if t.get("online") and any(
            "gpu" in tag.lower() or "cuda" in tag.lower()
            for tag in t.get("tags", [])
        )
    ]


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GPU Dashboard</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'SF Pro', system-ui, sans-serif;
    background: #0a0a0f;
    color: #e0e0e0;
    min-height: 100vh;
    padding: 20px;
    max-width: 900px;
    margin: 0 auto;
}

/* Header */
.header {
    display: flex; justify-content: space-between; align-items: center;
    padding-bottom: 16px; border-bottom: 1px solid #1a1a2e; margin-bottom: 20px;
}
.header h1 { font-size: 20px; font-weight: 700; color: #7c7cff; }
.header .status { font-size: 12px; color: #666; }
.header .status.live { color: #4ade80; }

/* Controls */
.controls {
    display: flex; gap: 6px; margin-bottom: 16px; align-items: center;
}
.controls .label { font-size: 12px; color: #666; margin-right: 4px; }
.btn {
    background: #1a1a2e; color: #888; border: 1px solid #2a2a4e;
    border-radius: 6px; padding: 6px 14px; font-size: 13px; cursor: pointer;
    transition: all .15s;
}
.btn:hover { border-color: #7c7cff; color: #ccc; }
.btn.active { background: #7c7cff; color: #fff; border-color: #7c7cff; }
.btn-refresh { margin-left: auto; }

/* Progress bar */
.refresh-bar { height: 2px; background: #1a1a2e; border-radius: 1px; margin-bottom: 16px; overflow: hidden; }
.refresh-bar .fill { height: 100%; background: #7c7cff; width: 0%; transition: width linear; }

/* Server strip */
.server {
    background: #12121e; border: 1px solid #1a1a2e; border-radius: 10px;
    margin-bottom: 8px; overflow: hidden; transition: border-color .2s;
}
.server:hover { border-color: #2a2a4e; }
.server-strip {
    display: flex; align-items: center; padding: 12px 16px; cursor: pointer;
    gap: 12px;
}
.dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.dot.active { background: #4ade80; }
.dot.idle { background: #fbbf24; }
.dot.off { background: #444; }
.dot.err { background: #f87171; }
.sname {
    font-size: 14px; font-weight: 600; color: #e0e0e0;
    min-width: 140px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.sbar {
    width: 80px; height: 6px; background: #1a1a2e; border-radius: 3px;
    overflow: hidden; flex-shrink: 0;
}
.sbar-fill { height: 100%; border-radius: 3px; transition: width .5s; }
.spct { font-size: 14px; font-weight: 700; min-width: 40px; text-align: right; }
.smeta { font-size: 12px; color: #666; white-space: nowrap; }
.sloading { font-size: 12px; color: #555; font-style: italic; }

/* Expanded detail */
.detail { display: none; padding: 0 16px 16px; }
.detail.open { display: block; }
.gpu-row {
    display: flex; gap: 8px; margin-bottom: 8px;
    background: #0a0a15; border-radius: 8px; padding: 10px;
}
.gpu-row .gpu-label { font-size: 11px; color: #888; margin-bottom: 6px; }
.gpu-row .col { flex: 1; text-align: center; }
.gpu-row .val { font-size: 20px; font-weight: 700; }
.gpu-row .sub { font-size: 10px; color: #666; margin-top: 2px; }
.gauge { width: 100%; height: 4px; background: #1a1a2e; border-radius: 2px; margin-top: 4px; }
.gauge-fill { height: 100%; border-radius: 2px; }

.procs { font-size: 11px; color: #777; font-family: monospace; margin-top: 8px; line-height: 1.6; }
.training { font-size: 12px; color: #aaa; margin-top: 10px; padding-top: 10px; border-top: 1px solid #1a1a2e; }
.training .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.chip { background: #0a0a15; padding: 4px 10px; border-radius: 4px; font-size: 12px; }
.chip .k { color: #888; }
.chip .v { color: #fff; font-weight: 600; }

.empty { text-align: center; padding: 60px 20px; color: #444; }
</style>
</head>
<body>

<div class="header">
    <h1>GPU Dashboard</h1>
    <div class="status" id="status">Loading...</div>
</div>

<div class="controls">
    <span class="label">Refresh:</span>
    <button class="btn" data-iv="0">Off</button>
    <button class="btn active" data-iv="30">30s</button>
    <button class="btn" data-iv="60">1m</button>
    <button class="btn" data-iv="300">5m</button>
    <button class="btn btn-refresh" onclick="fetchAll()">Refresh Now</button>
</div>

<div class="refresh-bar"><div class="fill" id="bar"></div></div>
<div id="content"><div class="empty">Loading GPU servers...</div></div>

<script>
let interval = 30;
let timer = null;
let expanded = {};
let cache = {};

// Interval buttons
document.querySelectorAll('.btn[data-iv]').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.btn[data-iv]').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        interval = parseInt(btn.dataset.iv);
        scheduleNext();
    });
});

function scheduleNext() {
    clearTimeout(timer);
    const bar = document.getElementById('bar');
    bar.style.width = '0%';
    bar.style.transition = 'none';
    if (interval > 0) {
        requestAnimationFrame(() => {
            bar.style.transition = `width ${interval}s linear`;
            bar.style.width = '100%';
        });
        timer = setTimeout(fetchAll, interval * 1000);
    }
}

// fetchAll defined at bottom (async version)

function pctColor(p) {
    if (p >= 90) return '#f87171';
    if (p >= 70) return '#fbbf24';
    return '#4ade80';
}

function fmtMiB(s) {
    const v = parseFloat(s);
    if (isNaN(v)) return s;
    return v >= 1024 ? (v/1024).toFixed(1)+'G' : Math.round(v)+'M';
}

const gpuRe = /GPU (\d+): (.+?) \| (\d+)°C \| util (\d+)% \| VRAM ([\d.]+)\/([\d.]+) MiB \((\d+)%\) \| ([\d.]+)W/g;
const procRe = /GPU (\d+) \| PID (\d+) \| (\d+) MiB \| (.+)/g;

function parseGpus(raw) {
    const gpus = [];
    let m;
    gpuRe.lastIndex = 0;
    while ((m = gpuRe.exec(raw)) !== null) {
        gpus.push({idx:m[1], name:m[2], temp:+m[3], util:+m[4],
            vUsed:m[5], vTotal:m[6], vPct:+m[7], power:m[8]});
    }
    return gpus;
}

function parseProcs(raw) {
    const procs = [];
    let m;
    procRe.lastIndex = 0;
    while ((m = procRe.exec(raw)) !== null) {
        procs.push({gpu:m[1], pid:m[2], mem:m[3], name:m[4]});
    }
    return procs;
}

function parseTraining(raw) {
    const chips = [];
    for (const line of raw.split('\n')) {
        if (line.startsWith('Latest step:')) chips.push(['step', line.split(':')[1].trim()]);
        if (line.includes(': ') && line.includes(' | ') && !line.startsWith('#')) {
            line.split(' | ').forEach(p => {
                const kv = p.split(': ');
                if (kv.length === 2) chips.push([kv[0].trim(), kv[1].trim()]);
            });
        }
    }
    return chips;
}

function render(terminals, results) {
    const el = document.getElementById('content');
    if (!terminals.length) {
        el.innerHTML = '<div class="empty">No GPU terminals online</div>';
        return;
    }

    let html = '';
    for (const name of terminals) {
        const r = results[name];
        const raw = r ? r.data || '' : '';
        const ok = r && r.ok;
        const gpus = parseGpus(raw);
        const isOpen = expanded[name];

        // Summary: aggregate across GPUs
        let maxUtil = 0, sumVUsed = 0, sumVTotal = 0, maxTemp = 0;
        for (const g of gpus) {
            if (g.util > maxUtil) maxUtil = g.util;
            sumVUsed += parseFloat(g.vUsed);
            sumVTotal += parseFloat(g.vTotal);
            if (g.temp > maxTemp) maxTemp = g.temp;
        }

        const dotClass = !ok ? 'err' : maxUtil > 10 ? 'active' : gpus.length ? 'idle' : 'off';
        const barW = gpus.length ? maxUtil : 0;
        const barCol = pctColor(barW);

        html += `<div class="server">`;
        html += `<div class="server-strip" onclick="toggle('${name}')">`;
        html += `<div class="dot ${dotClass}"></div>`;
        html += `<div class="sname">${name}</div>`;

        if (gpus.length) {
            html += `<div class="sbar"><div class="sbar-fill" style="width:${barW}%;background:${barCol}"></div></div>`;
            html += `<div class="spct" style="color:${barCol}">${maxUtil}%</div>`;
            html += `<span class="smeta">${fmtMiB(''+sumVUsed)}/${fmtMiB(''+sumVTotal)}</span>`;
            html += `<span class="smeta">${maxTemp}°</span>`;
            if (gpus.length > 1) html += `<span class="smeta">${gpus.length}x GPU</span>`;
        } else if (!ok) {
            html += `<span class="sloading">${raw.substring(0,30) || 'Error'}</span>`;
        } else {
            html += `<span class="sloading">No GPU</span>`;
        }

        html += `</div>`; // strip

        // Detail
        html += `<div class="detail ${isOpen ? 'open' : ''}">`;
        for (const g of gpus) {
            html += `<div class="gpu-row">`;
            html += `<div class="col"><div class="gpu-label">GPU ${g.idx}: ${g.name}</div></div>`;
            html += `</div>`;
            html += `<div class="gpu-row">`;
            // Util
            html += `<div class="col">
                <div class="val" style="color:${pctColor(g.util)}">${g.util}%</div>
                <div class="sub">util</div>
                <div class="gauge"><div class="gauge-fill" style="width:${g.util}%;background:${pctColor(g.util)}"></div></div>
            </div>`;
            // VRAM
            html += `<div class="col">
                <div class="val" style="color:${pctColor(g.vPct)}">${g.vPct}%</div>
                <div class="sub">${fmtMiB(g.vUsed)}/${fmtMiB(g.vTotal)}</div>
                <div class="gauge"><div class="gauge-fill" style="width:${g.vPct}%;background:${pctColor(g.vPct)}"></div></div>
            </div>`;
            // Temp
            const tc = g.temp > 80 ? '#f87171' : g.temp > 65 ? '#fbbf24' : '#4ade80';
            html += `<div class="col">
                <div class="val" style="color:${tc}">${g.temp}°</div>
                <div class="sub">temp</div>
            </div>`;
            // Power
            html += `<div class="col">
                <div class="val">${g.power}W</div>
                <div class="sub">power</div>
            </div>`;
            html += `</div>`;
        }

        // Processes
        const procs = parseProcs(raw);
        if (procs.length) {
            html += '<div class="procs">';
            for (const p of procs) {
                html += `GPU ${p.gpu} | PID ${p.pid} | ${p.mem} MiB | ${p.name}<br>`;
            }
            html += '</div>';
        }

        // Training
        const sections = raw.split('\n\n');
        for (const sec of sections) {
            if (!sec.startsWith('## Training')) continue;
            const chips = parseTraining(sec);
            if (chips.length) {
                html += '<div class="training">Training<div class="chips">';
                for (const [k,v] of chips) {
                    html += `<div class="chip"><span class="k">${k}: </span><span class="v">${v}</span></div>`;
                }
                html += '</div></div>';
            }
        }

        html += `</div>`; // detail
        html += `</div>`; // server
    }

    el.innerHTML = html;
}

function toggle(name) {
    expanded[name] = !expanded[name];
    // Re-render with cache
    const terminals = Object.keys(cache);
    render(terminals, cache);
}

// Fetch terminal list (fast), then fetch each in parallel
async function fetchAll() {
    clearTimeout(timer);
    document.getElementById('status').textContent = 'Fetching...';
    document.getElementById('status').className = 'status';

    try {
        const resp = await fetch('/api/gpu_terminals');
        const names = await resp.json();

        if (!names.length) {
            document.getElementById('content').innerHTML = '<div class="empty">No GPU terminals online</div>';
            document.getElementById('status').textContent = 'No GPU terminals';
            scheduleNext();
            return;
        }

        // Show placeholders immediately
        for (const name of names) {
            if (!cache[name]) cache[name] = {ok: false, data: 'Loading...'};
        }
        render(names, cache);

        // Fetch each terminal in parallel
        const promises = names.map(name =>
            fetch(`/api/metrics?terminal=${encodeURIComponent(name)}`)
                .then(r => r.json())
                .then(data => {
                    cache[name] = data;
                    render(Object.keys(cache), cache);
                })
                .catch(e => {
                    cache[name] = {ok: false, data: e.message};
                    render(Object.keys(cache), cache);
                })
        );

        await Promise.all(promises);
        document.getElementById('status').textContent = 'Updated ' + new Date().toLocaleTimeString();
        document.getElementById('status').className = 'status live';
    } catch(e) {
        document.getElementById('status').textContent = 'Error: ' + e.message;
    }
    scheduleNext();
}

// Auto-start
fetchAll();
</script>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    relay = None
    logdir = ""
    tail = 20

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode())

        elif self.path == "/api/terminals":
            terminals = self.relay.list_terminals()
            result = [{"name": t["name"], "online": t["online"],
                        "tags": t.get("tags", [])} for t in terminals]
            self._json(result)

        elif self.path.startswith("/api/metrics"):
            qs = parse_qs(urlparse(self.path).query)
            terminal = qs.get("terminal", [""])[0]
            if not terminal:
                self._json({"ok": False, "data": "No terminal specified"})
                return
            result = _fetch_metrics(self.relay, terminal, self.logdir, self.tail)
            self._json(result)

        elif self.path.startswith("/api/gpu_terminals"):
            names = _list_gpu_terminals(self.relay)
            self._json(names)

        else:
            self.send_response(404)
            self.end_headers()

    def _json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


def run_dashboard(port: int = 9530, logdir: str = "", tail: int = 20):
    """Start the metrics dashboard and open browser."""
    relay = _get_relay()
    DashboardHandler.relay = relay
    DashboardHandler.logdir = logdir
    DashboardHandler.tail = tail

    server = HTTPServer(("0.0.0.0", port), DashboardHandler)
    url = f"http://127.0.0.1:{port}"
    print(f"[octo] GPU Dashboard: {url}")

    # Open browser
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    print("[octo] Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[octo] Dashboard stopped.")
        server.server_close()
