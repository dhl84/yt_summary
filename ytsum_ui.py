#!/usr/bin/env python3
"""A local web page for ytsum: paste YouTube links, read the briefs.

    python ytsum_ui.py            # serves http://127.0.0.1:8765 and opens the browser

It runs ytsum.py once per link, one link at a time, because the local model
uses the whole GPU. The page lists the briefs in out/ and shows each one.
Standard library only. It listens on 127.0.0.1, so no other machine can reach it.
"""
from collections import deque
import itertools
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ytsum

HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("YTSUM_PORT", 8765))
VENV_BIN = HERE / ".venv" / ("Scripts" if os.name == "nt" else "bin")
URL = re.compile(r"https?://(?:www\.|m\.)?(?:youtube\.com|youtu\.be)/[^\s<>\"']+")
VIDEO_ID = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/live/)([\w-]{11})")

jobs = {}                 # id -> job dict, in the order they arrive
queue = deque()
lock = threading.Lock()
wake = threading.Event()
next_id = itertools.count(1)


def child_env():
    env = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1")
    if VENV_BIN.exists():  # the yt-dlp that the venv holds, before any older copy on PATH
        env["PATH"] = str(VENV_BIN) + os.pathsep + env.get("PATH", "")
    return env


def worker():
    while True:
        wake.wait()
        with lock:
            if not queue:
                wake.clear()
                continue
            job = jobs[queue.popleft()]
            job.update(status="running", started=time.time())
        cmd = [sys.executable, str(HERE / "ytsum.py"), job["url"], "--engine", job["engine"],
               "--model", job["model"], "--comments", str(job["comments"])]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = subprocess.Popen(cmd, cwd=HERE, env=child_env(), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace", creationflags=flags)
        job["proc"] = proc
        if job["status"] == "cancelled":  # Stop pressed while the process started
            proc.kill()
        last = ""
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                job["log"].append(line)
                last = line
        proc.wait()
        job["finished"] = time.time()
        if job["status"] == "cancelled":
            continue
        # ytsum prints the path of the brief as its last line when it succeeds.
        if proc.returncode == 0 and last.endswith(".md"):
            job.update(status="done", brief=Path(last).name)
        else:
            job["status"] = "failed"


def add_jobs(text, engine, model, comments):
    # Key by video id, so a watch link and a youtu.be link to one video run once.
    found = {}
    for url in (u.rstrip(".,;)") for u in URL.findall(text)):
        match = VIDEO_ID.search(url)
        found.setdefault(match[1] if match else url, url)
    urls = list(found.values())
    with lock:
        for url in urls:
            job_id = next(next_id)
            jobs[job_id] = {"id": job_id, "url": url, "engine": engine, "model": model,
                            "comments": comments, "status": "queued", "log": [],
                            "added": time.time(), "started": None, "finished": None, "brief": None}
            queue.append(job_id)
    wake.set()
    return len(urls)


def cancel(job_id):
    with lock:
        job = jobs.get(job_id)
        if not job or job["status"] not in ("queued", "running"):
            return
        if job["status"] == "queued":
            queue.remove(job_id)
        job["status"] = "cancelled"
        job["finished"] = time.time()
    if job.get("proc") and job["proc"].poll() is None:
        job["proc"].kill()


def briefs():
    out = []
    for path in sorted(ytsum.OUT.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        with path.open(encoding="utf-8", errors="replace") as f:
            title = f.readline().lstrip("# ").strip() or path.stem
        out.append({"name": path.name, "title": title, "mtime": path.stat().st_mtime})
    return out


def models():
    try:
        with urllib.request.urlopen(ytsum.OLLAMA.replace("/api/generate", "/api/tags"), timeout=2) as r:
            names = [m["name"] for m in json.load(r).get("models", [])]
    except OSError:
        return []
    return [n for n in names if "embed" not in n]


def state():
    with lock:
        listed = [{k: v for k, v in j.items() if k != "proc"} for j in jobs.values()]
    return {"jobs": listed[-50:], "briefs": briefs(), "models": models(), "default_model": ytsum.MODEL,
            "out": str(ytsum.OUT), "now": time.time()}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # pythonw has no stderr
        pass

    def send(self, code, body, kind="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def local(self):
        # Refuse a request whose Host is some other name: that stops DNS rebinding.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self.local():
            return self.send(403, "{}")
        path, _, query = self.path.partition("?")
        if path == "/":
            return self.send(200, PAGE, "text/html; charset=utf-8")
        if path == "/api/state":
            return self.send(200, json.dumps(state()))
        if path == "/api/brief":
            name = urllib.parse.unquote(query.removeprefix("name="))
            target = (ytsum.OUT / name).resolve()
            if target.parent != ytsum.OUT.resolve() or target.suffix != ".md" or not target.exists():
                return self.send(404, "{}")
            return self.send(200, target.read_text(encoding="utf-8", errors="replace"),
                             "text/markdown; charset=utf-8")
        self.send(404, "{}")

    def do_POST(self):
        # A JSON body forces a CORS preflight, which this server never answers,
        # so another web page in the browser cannot start a run here.
        if not self.local() or "application/json" not in (self.headers.get("Content-Type") or ""):
            return self.send(403, "{}")
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/api/run":
            engine = body.get("engine") if body.get("engine") in ("ollama", "claude") else "ollama"
            count = add_jobs(str(body.get("text", "")), engine,
                             str(body.get("model") or ytsum.MODEL), max(0, int(body.get("comments", 60))))
            return self.send(200, json.dumps({"added": count}))
        if self.path == "/api/cancel":
            cancel(int(body.get("id", 0)))
            return self.send(200, "{}")
        if self.path == "/api/clear":
            with lock:
                for job_id in [i for i, j in jobs.items() if j["status"] not in ("queued", "running")]:
                    del jobs[job_id]
            return self.send(200, "{}")
        if self.path == "/api/open-folder":
            ytsum.OUT.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(ytsum.OUT)
            else:
                try:
                    subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(ytsum.OUT)])
                except OSError:  # a Linux box with no desktop has no xdg-open
                    return self.send(501, "{}")
            return self.send(200, "{}")
        self.send(404, "{}")


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>YouTube Briefs</title>
<style>
:root {
  --bg: #f6f5f2; --panel: #ffffff; --ink: #1d1d1b; --muted: #6b6a66; --line: #e2e0da;
  --accent: #b3261e; --accent-ink: #ffffff; --ok: #2e7d32; --warn: #a15c00; --bad: #b3261e;
  --chip: #efede8; --code: #f1efea;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #161615; --panel: #1f1f1d; --ink: #ecebe7; --muted: #9c9a94; --line: #34332f;
    --accent: #ff6b5e; --accent-ink: #1a0b09; --ok: #7fcf83; --warn: #e5a54b; --bad: #ff6b5e;
    --chip: #2a2a27; --code: #2a2a27;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.5 "Segoe UI", system-ui, -apple-system, sans-serif; }
header { display: flex; align-items: baseline; gap: 12px; padding: 18px 24px 0; }
header h1 { font-size: 20px; margin: 0; letter-spacing: -0.01em; }
header span { color: var(--muted); font-size: 13px; }
main { display: grid; grid-template-columns: minmax(320px, 420px) 1fr; gap: 20px; padding: 16px 24px 24px;
  height: calc(100vh - 50px); }
.col { display: flex; flex-direction: column; gap: 16px; min-height: 0; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 14px; }
textarea { width: 100%; min-height: 120px; resize: vertical; padding: 10px; border-radius: 8px;
  border: 1px solid var(--line); background: var(--bg); color: var(--ink); font: 13px/1.45 ui-monospace, Consolas, monospace; }
textarea:focus, select:focus, input:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
.opts { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 10px; align-items: end; }
label { display: flex; flex-direction: column; font-size: 12px; color: var(--muted); gap: 3px; }
select, input[type=number] { padding: 6px 8px; border-radius: 6px; border: 1px solid var(--line);
  background: var(--bg); color: var(--ink); font: inherit; font-size: 13px; }
input[type=number] { width: 70px; }
#model { max-width: 190px; }
.opts .primary { margin-left: auto; }
button { font: inherit; font-size: 13px; border-radius: 6px; border: 1px solid var(--line); background: var(--chip);
  color: var(--ink); padding: 6px 12px; cursor: pointer; }
button.primary { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); font-weight: 600; padding: 8px 16px; }
button:disabled { opacity: .5; cursor: default; }
.hint { font-size: 12px; color: var(--muted); margin-top: 8px; min-height: 18px; }
h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 0 0 8px;
  display: flex; justify-content: space-between; align-items: center; }
h2 button { text-transform: none; letter-spacing: 0; padding: 2px 8px; font-size: 12px; }
.jobs { max-height: 34vh; overflow: auto; }
.job { border-top: 1px solid var(--line); padding: 8px 0; font-size: 13px; }
.job:first-child { border-top: 0; }
.job .row { display: flex; gap: 8px; align-items: center; }
.job .url { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.job a { color: inherit; }
.st { font-size: 11px; font-weight: 600; padding: 1px 7px; border-radius: 99px; background: var(--chip); }
.st.running { color: var(--warn); } .st.done { color: var(--ok); } .st.failed, .st.cancelled { color: var(--bad); }
.job pre { margin: 6px 0 0; font-size: 11.5px; white-space: pre-wrap; color: var(--muted); max-height: 160px; overflow: auto;
  background: var(--code); padding: 6px 8px; border-radius: 6px; }
.job .x { padding: 0 6px; font-size: 12px; }
.list { flex: 1; overflow: auto; min-height: 120px; }
.item { display: block; width: 100%; text-align: left; border: 0; border-radius: 6px; background: none; padding: 7px 8px; }
.item:hover { background: var(--chip); }
.item.sel { background: var(--chip); box-shadow: inset 3px 0 0 var(--accent); }
.item small { display: block; color: var(--muted); font-size: 11.5px; }
.empty { color: var(--muted); font-size: 13px; padding: 6px 2px; }
article { overflow: auto; padding: 24px 32px; min-height: 0; }
article h1 { font-size: 22px; line-height: 1.3; margin: 0 0 6px; }
article h2 { font-size: 16px; text-transform: none; letter-spacing: 0; color: var(--ink); margin: 22px 0 6px;
  padding-bottom: 4px; border-bottom: 1px solid var(--line); display: block; }
article ul { padding-left: 20px; margin: 6px 0; } article li { margin: 4px 0; }
article a { color: var(--accent); }
article hr { border: 0; border-top: 1px solid var(--line); margin: 14px 0; }
article p { margin: 6px 0; }
article .meta { color: var(--muted); font-size: 13px; }
code { background: var(--code); padding: 0 4px; border-radius: 4px; font-size: 90%; }
.ts { font-family: ui-monospace, Consolas, monospace; font-size: 12px; color: var(--muted); }
.tag { font-size: 11px; font-weight: 600; padding: 0 6px; border-radius: 4px; background: var(--chip); }
@media (max-width: 860px) {
  main { grid-template-columns: 1fr; height: auto; padding: 12px 16px; }
  header { padding: 14px 16px 0; }
  article { padding: 18px 16px; }
}
</style>
</head>
<body>
<header><h1>YouTube Briefs</h1><span id="where"></span></header>
<main>
  <div class="col">
    <section class="panel">
      <textarea id="urls" placeholder="Paste one or more YouTube links here. Any text around them is fine." autofocus></textarea>
      <div class="opts">
        <label>Engine<select id="engine"><option value="ollama">Ollama (local)</option><option value="claude">Claude CLI</option></select></label>
        <label>Model<select id="model"></select></label>
        <label>Comments<input id="comments" type="number" min="0" step="20" value="60"></label>
        <button class="primary" id="go">Summarise</button>
      </div>
      <div class="hint" id="hint"></div>
    </section>
    <section class="panel">
      <h2>Runs <button id="clear">Clear finished</button></h2>
      <div class="jobs" id="jobs"><div class="empty">No runs yet.</div></div>
    </section>
    <section class="panel col" style="flex:1">
      <h2>Briefs <button id="folder">Open folder</button></h2>
      <div class="list" id="briefs"></div>
    </section>
  </div>
  <article class="panel" id="view"><div class="empty">Choose a brief, or paste a link to make one.</div></article>
</main>
<script>
const $ = id => document.getElementById(id);
let selected = null, open = new Set(), seenDone = new Set(), firstLoad = true, modelsSet = false;

const esc = s => s.replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function inline(s) {
  s = esc(s);
  s = s.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  s = s.replace(/^\[(\d{1,2}:\d\d(?::\d\d)?)\]/, '<span class="ts">$1</span>');
  s = s.replace(/^\[(Supports|Adds|Disputes)\]/, '<span class="tag">$1</span>');
  return s;
}
function md(text) {
  let html = "", list = false, para = [];
  const flush = () => { if (para.length) { html += "<p>" + inline(para.join(" ")) + "</p>"; para = []; }
                        if (list) { html += "</ul>"; list = false; } };
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    let m;
    if (!line) { flush(); continue; }
    if ((m = line.match(/^(#{1,3})\s+(.*)/))) { flush(); html += `<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`; continue; }
    if (/^-{3,}$/.test(line)) { flush(); html += "<hr>"; continue; }
    if ((m = line.match(/^[-*]\s+(.*)/))) {
      if (para.length) { html += "<p>" + inline(para.join(" ")) + "</p>"; para = []; }
      if (!list) { html += "<ul>"; list = true; }
      html += "<li>" + inline(m[1]) + "</li>"; continue;
    }
    if (list) { html += "</ul>"; list = false; }
    para.push(line);
  }
  flush();
  return html;
}

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} :
    {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  return path === "/api/state" || body !== undefined ? r.json() : r.text();
}

async function show(name) {
  selected = name;
  const text = await api("/api/brief?name=" + encodeURIComponent(name));
  const view = $("view");
  view.innerHTML = md(text);
  const meta = view.querySelectorAll("p");  // the two lines under the title
  for (const p of [...meta].slice(0, 2)) p.classList.add("meta");
  view.scrollTop = 0;
  history.replaceState(null, "", "#" + encodeURIComponent(name));
  document.querySelectorAll(".item").forEach(b => b.classList.toggle("sel", b.dataset.name === name));
}

const ago = t => { const s = Math.max(0, Date.now() / 1000 - t);
  return s < 60 ? "just now" : s < 3600 ? Math.floor(s / 60) + " min ago" :
         s < 86400 ? Math.floor(s / 3600) + " h ago" : new Date(t * 1000).toLocaleDateString(); };
const secs = (a, b) => a ? Math.round((b || Date.now() / 1000) - a) + "s" : "";

function renderJobs(jobs, now) {
  const box = $("jobs");
  if (!jobs.length) { box.innerHTML = '<div class="empty">No runs yet.</div>'; return; }
  const scroll = {};
  box.querySelectorAll("pre[data-id]").forEach(p => scroll[p.dataset.id] = p.scrollTop);
  box.innerHTML = jobs.slice().reverse().map(j => {
    const live = j.status === "running" || j.status === "queued";
    const step = j.status === "running" && j.log.length ? " · " + esc(j.log[j.log.length - 1].trim()).slice(0, 60) : "";
    return `<div class="job">
      <div class="row"><span class="st ${j.status}">${j.status}</span>
        <span class="url"><a href="${esc(j.url)}" target="_blank" rel="noopener">${esc(j.url.replace(/^https?:\/\/(www\.)?/, ""))}</a></span>
        <span class="ts">${secs(j.started, j.finished)}</span>
        ${j.brief ? `<button class="x" data-show="${esc(j.brief)}">Read</button>` : ""}
        <button class="x" data-log="${j.id}">${open.has(j.id) ? "Hide" : "Log"}</button>
        ${live ? `<button class="x" data-cancel="${j.id}" title="Stop this run">Stop</button>` : ""}</div>
      ${step ? `<div class="ts" style="margin-top:3px">${step}</div>` : ""}
      ${open.has(j.id) || j.status === "failed" ? `<pre data-id="${j.id}">${esc(j.log.join("\n") || "(no output yet)")}</pre>` : ""}
    </div>`;
  }).join("");
  box.querySelectorAll("pre[data-id]").forEach(p => {
    p.scrollTop = p.dataset.id in scroll ? scroll[p.dataset.id] : p.scrollHeight; });
}

function renderBriefs(list) {
  $("briefs").innerHTML = list.length ? list.map(b =>
    `<button class="item${b.name === selected ? " sel" : ""}" data-name="${esc(b.name)}">${esc(b.title)}
       <small>${esc(b.name.slice(0, 10))} · added ${ago(b.mtime)}</small></button>`).join("")
    : '<div class="empty">No briefs yet.</div>';
}

async function refresh() {
  let s;
  try { s = await api("/api/state"); } catch { $("hint").textContent = "The server stopped. Start YouTube Briefs again."; return; }
  $("where").textContent = "Briefs go to " + s.out;
  if (!modelsSet) {
    const models = s.models.length ? s.models : [s.default_model];
    $("model").innerHTML = models.map(m => `<option${m === s.default_model ? " selected" : ""}>${esc(m)}</option>`).join("");
    if (!s.models.length) $("hint").textContent = "Ollama does not answer. Start Ollama, or choose the Claude CLI.";
    modelsSet = true;
  }
  renderJobs(s.jobs, s.now);
  renderBriefs(s.briefs);
  for (const j of s.jobs) {
    if (j.status === "done" && !seenDone.has(j.id)) {
      seenDone.add(j.id);
      if (!firstLoad) show(j.brief);   // open each new brief as it lands
    }
  }
  firstLoad = false;
}

async function go() {
  const text = $("urls").value;
  if (!text.trim()) return;
  const r = await api("/api/run", {text, engine: $("engine").value, model: $("model").value,
                                   comments: +$("comments").value || 0});
  $("hint").textContent = r.added ? `Added ${r.added} link${r.added > 1 ? "s" : ""}. They run one at a time.`
                                  : "No YouTube link found in that text.";
  if (r.added) $("urls").value = "";
  refresh();
}

$("go").onclick = go;
$("urls").addEventListener("keydown", e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); go(); } });
$("engine").onchange = () => { $("model").disabled = $("engine").value === "claude"; };
$("clear").onclick = () => api("/api/clear", {}).then(refresh);
$("folder").onclick = () => api("/api/open-folder", {});
$("briefs").onclick = e => { const b = e.target.closest(".item"); if (b) show(b.dataset.name); };
$("jobs").onclick = e => {
  const t = e.target;
  if (t.dataset.show) show(t.dataset.show);
  if (t.dataset.log) { const id = +t.dataset.log; open.has(id) ? open.delete(id) : open.add(id); refresh(); }
  if (t.dataset.cancel) api("/api/cancel", {id: +t.dataset.cancel}).then(refresh);
};
$("hint").textContent = (/Mac/.test(navigator.platform) ? "Cmd" : "Ctrl") + "+Enter also starts the run.";
refresh();
setInterval(refresh, 1500);
if (location.hash.length > 1) show(decodeURIComponent(location.hash.slice(1)));
</script>
</body>
</html>
"""


class Server(ThreadingHTTPServer):
    # On Windows, SO_REUSEADDR lets a second server take a port that another server
    # still listens on. Without it, a second start fails to bind and opens the page instead.
    allow_reuse_address = os.name != "nt"


def main():
    address = f"http://127.0.0.1:{PORT}/"
    try:
        server = Server(("127.0.0.1", PORT), Handler)
    except OSError:
        # Already running: a second click on the shortcut just opens the page.
        webbrowser.open(address)
        return
    threading.Thread(target=worker, daemon=True).start()
    if "--no-browser" not in sys.argv:
        webbrowser.open(address)
    if sys.stdout:
        print(f"ytsum UI on {address}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
