#!/usr/bin/env python3
"""Local dashboard server for DaVinci AAC Support.

Replaces zenity dialogs with a real web page (opened in the default
browser) for both the install flow and the standalone live-activity
monitor. Stdlib only -- no new dependency for something meant to work on
any Linux desktop, regardless of which GUI toolkit (if any) is installed.

Two entry points, both backed by this same server:
  --mode install   used by install.sh: shows install progress live, or
                    (if already installed) a status/manage view instead.
  --mode monitor   used by the standalone `davinci-aac-support-monitor`
                    command: just the live status/activity view.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BIN_DIR = os.path.expanduser("~/.local/bin")
SERVICE_DIR = os.path.expanduser("~/.config/systemd/user")
DAEMON_PATH = os.path.join(BIN_DIR, "davinci_aac_support_watch.py")
UI_PATH = os.path.join(BIN_DIR, "davinci_aac_support_ui.py")
MONITOR_PATH = os.path.join(BIN_DIR, "davinci-aac-support-monitor")
SERVICE_NAME = "davinci-aac-support.service"
SERVICE_PATH = os.path.join(SERVICE_DIR, SERVICE_NAME)
STATE_DIR = os.path.expanduser("~/.cache/davinci-aac-support")
STATUS_FILE = os.path.join(STATE_DIR, "status.json")
INSTALL_LOG = os.path.join(STATE_DIR, "install-log.jsonl")
EVENTS_FILE = os.path.join(STATE_DIR, "events.jsonl")
PORT_FILE = os.path.join(STATE_DIR, "ui-port.txt")


def is_installed():
    return os.path.isfile(SERVICE_PATH)


def is_active():
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", SERVICE_NAME],
            timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def read_json_file(path):
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def tail_lines(path, from_start=True):
    """Yields new lines appended to path, forever. Starts from the
    beginning if from_start, otherwise from wherever the file currently
    ends (for a monitor attaching mid-stream)."""
    while not os.path.exists(path):
        time.sleep(0.2)
    with open(path) as f:
        if not from_start:
            f.seek(0, os.SEEK_END)
        while True:
            line = f.readline()
            if line:
                yield line.rstrip("\n")
            else:
                time.sleep(0.3)


# ---------------------------------------------------------------- HTML --

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DaVinci AAC Support</title>
<style>
  :root {
    --bg: #0b0d12; --card: #161a24; --border: #262b38; --text: #e7e9ee;
    --muted: #9aa3b5; --accent: #5fd0a3; --accent-dim: #2f5c49;
    --danger: #e0685f; --danger-dim: #5c2f2c; --amber: #e0b95f;
    --mono: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
    --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text); font-family: var(--sans);
    line-height: 1.55; min-height: 100vh; display: flex; flex-direction: column;
  }
  .wrap { max-width: 720px; margin: 0 auto; padding: 40px 24px 64px; width: 100%; }
  header {
    padding: 40px 0 28px;
    background: radial-gradient(500px 220px at 20% 0%, rgba(95,208,163,0.14), transparent 60%);
    border-bottom: 1px solid var(--border);
  }
  h1 { font-size: 1.7rem; margin: 0 0 6px; letter-spacing: -0.01em; }
  .sub { color: var(--muted); margin: 0; font-size: 0.95rem; }
  .card {
    background: var(--card); border: 1px solid var(--border); border-radius: 14px;
    padding: 22px 24px; margin-top: 20px;
  }
  .row { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
  .pill {
    display: inline-flex; align-items: center; gap: 6px; font-size: 0.8rem;
    padding: 4px 10px; border-radius: 999px; font-weight: 600;
  }
  .pill.ok { background: rgba(95,208,163,0.12); color: var(--accent); border: 1px solid var(--accent-dim); }
  .pill.bad { background: rgba(224,104,95,0.12); color: var(--danger); border: 1px solid var(--danger-dim); }
  .pill.wait { background: rgba(224,185,95,0.12); color: var(--amber); border: 1px solid #5c4d2f; }
  .kv { margin-top: 14px; font-size: 0.92rem; }
  .kv div { display: flex; justify-content: space-between; padding: 5px 0; border-bottom: 1px dashed var(--border); }
  .kv div:last-child { border-bottom: none; }
  .kv span:first-child { color: var(--muted); }
  .btn {
    display: inline-flex; align-items: center; gap: 8px; padding: 10px 18px; border-radius: 9px;
    font-weight: 600; font-size: 0.9rem; border: 1px solid var(--border); background: transparent;
    color: var(--text); cursor: pointer; font-family: inherit;
  }
  .btn:hover { border-color: var(--accent-dim); }
  .btn.primary { background: var(--accent); color: #0b1712; border-color: var(--accent); }
  .btn.danger { color: var(--danger); border-color: var(--danger-dim); }
  .btn:disabled { opacity: 0.5; cursor: default; }
  .btns { display: flex; gap: 10px; margin-top: 18px; flex-wrap: wrap; }
  .log {
    font-family: var(--mono); font-size: 0.85rem; background: #0d1017; border: 1px solid var(--border);
    border-radius: 10px; padding: 14px 16px; margin-top: 20px; max-height: 340px; overflow-y: auto;
  }
  .log .line { padding: 2px 0; white-space: pre-wrap; }
  .log .step { color: var(--muted); }
  .log .ok::before { content: "✔ "; color: var(--accent); }
  .log .ok { color: var(--text); }
  .log .fail::before { content: "✘ "; }
  .log .fail { color: var(--danger); }
  .log .event { color: var(--text); }
  .log .event.detected::before { content: "● "; color: var(--amber); }
  .log .event.converting::before { content: "◐ "; color: var(--amber); }
  .log .event.fixed::before { content: "✔ "; color: var(--accent); }
  .log .event.failed::before { content: "✘ "; color: var(--danger); }
  .ask { background: rgba(224,185,95,0.08); border: 1px solid #5c4d2f; border-radius: 10px; padding: 16px; margin-top: 18px; }
  .ask p { margin: 0 0 12px; white-space: pre-wrap; }
  .hidden { display: none !important; }
  .spinner {
    width: 14px; height: 14px; border-radius: 50%; border: 2px solid var(--border);
    border-top-color: var(--accent); animation: spin 0.7s linear infinite; display: inline-block;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  footer { margin-top: auto; padding: 20px 0; text-align: center; color: var(--muted); font-size: 0.8rem; }
  a { color: var(--accent); }
</style>
</head>
<body>
<header><div class="wrap">
  <h1>DaVinci AAC Support</h1>
  <p class="sub" id="subtitle">Checking status&hellip;</p>
</div></header>
<div class="wrap">

  <div class="card" id="status-card">
    <div class="row">
      <strong id="status-title">&nbsp;</strong>
      <span class="pill wait" id="status-pill"><span class="spinner"></span> checking</span>
    </div>
    <div class="kv" id="status-kv"></div>
    <div class="btns" id="status-btns"></div>
  </div>

  <div class="ask hidden" id="ask-card">
    <p id="ask-text"></p>
    <div class="btns">
      <button class="btn primary" onclick="answer(true)">Yes, proceed</button>
      <button class="btn" onclick="answer(false)">No, cancel</button>
    </div>
  </div>

  <div class="log hidden" id="log"></div>
</div>
<footer>davinci-aac-support &middot; <a href="https://github.com/broskisworld/davinci-aac-support" target="_blank">GitHub</a></footer>

<script>
const MODE = %%MODE%%;
let askId = null;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function logLine(cls, text) {
  const log = document.getElementById('log');
  log.classList.remove('hidden');
  const line = el('div', 'line ' + cls, text);
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}

function answer(yes) {
  document.getElementById('ask-card').classList.add('hidden');
  fetch('/api/answer', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id: askId, answer: yes ? 'yes' : 'no'})});
}

function renderStatus(s) {
  const title = document.getElementById('status-title');
  const pill = document.getElementById('status-pill');
  const kv = document.getElementById('status-kv');
  const btns = document.getElementById('status-btns');
  kv.innerHTML = '';
  btns.innerHTML = '';

  if (!s.installed) {
    title.textContent = 'Not installed';
    pill.className = 'pill bad'; pill.innerHTML = 'not installed';
    return;
  }

  title.textContent = s.active ? 'Watcher running' : 'Watcher installed, not running';
  pill.className = 'pill ' + (s.active ? 'ok' : 'bad');
  pill.innerHTML = s.active ? 'active' : 'stopped';

  const rows = [];
  if (s.status) {
    rows.push(['Connected to Resolve', s.status.connected ? 'yes' : 'no']);
    if (s.status.product) rows.push(['Resolve', s.status.product + ' ' + (s.status.version || '')]);
    if (s.status.project) rows.push(['Project', s.status.project]);
    rows.push(['Clips fixed', s.status.fixed_count || 0]);
    if (s.status.last_fixed) rows.push(['Last fixed', s.status.last_fixed]);
  }
  for (const [k, v] of rows) {
    const row = el('div');
    row.appendChild(el('span', null, k));
    row.appendChild(el('span', null, String(v)));
    kv.appendChild(row);
  }

  if (MODE === 'monitor' || s.active) {
    const b = el('button', 'btn danger', 'Uninstall');
    b.onclick = () => { if (confirm('Remove the watcher and installed files?')) doAction('uninstall'); };
    btns.appendChild(b);
    const r = el('button', 'btn', 'Restart');
    r.onclick = () => doAction('restart');
    btns.appendChild(r);
  }
}

function doAction(action) {
  fetch('/api/action', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action})}).then(r => r.json()).then(() => refreshStatus());
}

function refreshStatus() {
  fetch('/api/status').then(r => r.json()).then(s => {
    document.getElementById('subtitle').textContent =
      MODE === 'monitor' ? 'Live status' : (s.installed ? 'Already installed' : 'Installing');
    renderStatus(s);
  });
}

function streamEvents() {
  const es = new EventSource('/api/events?stream=' + MODE);
  es.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'step') logLine('step', msg.text);
    else if (msg.type === 'ok') logLine('ok', msg.text);
    else if (msg.type === 'fail') logLine('fail', msg.text);
    else if (msg.type === 'ask') {
      askId = msg.id;
      document.getElementById('ask-text').textContent = msg.text;
      document.getElementById('ask-card').classList.remove('hidden');
    } else if (msg.type === 'event') {
      logLine('event ' + msg.kind, msg.text);
      refreshStatus();
    } else if (msg.type === 'done') {
      refreshStatus();
    }
  };
}

refreshStatus();
streamEvents();
setInterval(refreshStatus, 4000);
</script>
</body>
</html>
"""


# ------------------------------------------------------------- server --

class Handler(BaseHTTPRequestHandler):
    server_version = "davinci-aac-support-ui/1"

    def log_message(self, fmt, *args):
        pass  # keep stdout clean; install.sh doesn't parse this process's own logs

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            body = PAGE.replace("%%MODE%%", json.dumps(self.server.mode)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/status":
            installed = is_installed()
            self._json({
                "installed": installed,
                "active": is_active() if installed else False,
                "status": read_json_file(STATUS_FILE),
            })
        elif path == "/api/events":
            self._stream_events()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            body = {}

        if self.path == "/api/answer":
            answer_id = body.get("id")
            answer = body.get("answer")
            if answer_id:
                with open(os.path.join(STATE_DIR, f"answer-{answer_id}.txt"), "w") as f:
                    f.write(answer or "no")
            self._json({"ok": True})
        elif self.path == "/api/action":
            action = body.get("action")
            result = self._do_action(action)
            self._json(result)
        else:
            self.send_response(404)
            self.end_headers()

    def _do_action(self, action):
        if action == "restart":
            subprocess.run(["systemctl", "--user", "restart", SERVICE_NAME], timeout=15)
            return {"ok": True}
        if action == "uninstall":
            subprocess.run(["systemctl", "--user", "disable", "--now", SERVICE_NAME], timeout=15)
            for p in (SERVICE_PATH, DAEMON_PATH, UI_PATH, MONITOR_PATH):
                try:
                    os.remove(p)
                except FileNotFoundError:
                    pass
            subprocess.run(["systemctl", "--user", "daemon-reload"], timeout=15)
            # Deliberately not removing STATE_DIR here, unlike install.sh's
            # CLI uninstall -- this server process (and the page's own
            # ongoing status polling) lives under STATE_DIR/ui-port.txt, so
            # pulling the directory out from under itself while still
            # serving the confirmation is asking for trouble. Nothing left
            # behind is sensitive (status.json becomes correctly stale).
            return {"ok": True}
        return {"ok": False, "error": "unknown action"}

    def _stream_events(self):
        source = INSTALL_LOG if self.server.mode == "install" else EVENTS_FILE
        # Monitor mode: show recent history first (tail from start of an
        # already-populated file), then keep streaming. Install mode: the
        # log is fresh for this run, always read from the start.
        from_start = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            for line in tail_lines(source, from_start=from_start):
                if not line.strip():
                    continue
                self.wfile.write(f"data: {line}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["install", "monitor"], required=True)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()

    os.makedirs(STATE_DIR, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.mode = args.mode
    port = server.server_address[1]
    with open(PORT_FILE, "w") as f:
        f.write(str(port))

    url = f"http://127.0.0.1:{port}/"
    print(f"SERVER_URL={url}", flush=True)

    def open_browser():
        time.sleep(0.3)
        try:
            subprocess.run(["xdg-open", url], check=False,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            pass

    threading.Thread(target=open_browser, daemon=True).start()

    if args.mode == "install":
        # This process outlives install.sh (started detached, so the page
        # keeps working with no terminal attached) -- bound its lifetime so
        # it doesn't linger forever once the user's done looking at it.
        # Monitor mode has no timeout: that one's explicitly launched to
        # watch something ongoing, for as long as that takes.
        def self_destruct():
            time.sleep(30 * 60)
            os._exit(0)
        threading.Thread(target=self_destruct, daemon=True).start()

    server.serve_forever()


if __name__ == "__main__":
    main()
