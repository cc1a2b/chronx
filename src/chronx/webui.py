"""`chronx serve` — a local web UI over the recorded history.

An embedded, dependency-free HTTP server (stdlib only) that reads the chronx
store READ-ONLY and serves:

  GET /                     the single-page app (inline HTML/CSS/JS)
  GET /api/summary          roots + totals, and the default root to show
  GET /api/events           timeline (filter by root/query/changes, poll ?after)
  GET /api/event/<id>       one command with its per-file unified diffs
  GET /api/blame?root=&path led its history for a file
  GET /api/search           command grep or -S content pickaxe
  GET /api/stats?root=      hottest files / noisiest commands / store size

Every request opens its own read-only connection, so the threaded server never
shares a sqlite handle and never writes. Bind stays on localhost by default.
"""

from __future__ import annotations

import json
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__
from . import db as dbm
from .config import Paths
from .diffview import render_delta, stat_line
from .ops import describe_command
from .store import ObjectStore
from .when import fmt_ts


def _counts(conn: sqlite3.Connection, event_id: int) -> dict[str, int]:
    c = dbm.delta_counts(conn, event_id)
    c["total"] = c["A"] + c["M"] + c["D"]
    return c


def _event_brief(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    c = _counts(conn, int(row["id"]))
    return {
        "id": int(row["id"]),
        "ts": row["started_at"],
        "time": fmt_ts(row["started_at"]),
        "command": describe_command(row),
        "external": row["command"] is None,
        "exit": row["exit_code"],
        "adds": c["A"], "mods": c["M"], "dels": c["D"], "total": c["total"],
        "session": row["session"],
    }


def api_summary(conn: sqlite3.Connection, default_root: int | None) -> dict:
    roots = [
        {"id": int(r["id"]), "path": r["path"], "events": r["events"], "files": r["files"]}
        for r in dbm.root_summaries(conn)
    ]
    total = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    if default_root is None and roots:
        default_root = roots[0]["id"]
    return {
        "version": __version__,
        "roots": roots,
        "active_root": default_root,
        "total_events": total,
    }


def api_events(
    conn: sqlite3.Connection, *, root_id: int | None, limit: int,
    q: str | None, changes: bool, after: int | None,
) -> dict:
    if after is not None:
        rows = dbm.events_after(conn, after, root_id=root_id, changes_only=changes)
    else:
        rows = dbm.recent_events(
            conn, root_id=root_id, limit=limit, changes_only=changes
        )
    if q:
        needle = q.lower()
        rows = [r for r in rows if needle in describe_command(r).lower()]
    events = [_event_brief(conn, r) for r in rows]
    return {"events": events, "max_id": dbm.max_event_id(conn)}


def api_event(conn: sqlite3.Connection, store: ObjectStore, event_id: int) -> dict:
    row = dbm.event_by_id(conn, event_id)
    if row is None:
        return {"error": f"no event #{event_id}"}
    files = []
    for d in dbm.deltas_for(conn, event_id):
        files.append({
            "path": d.path,
            "change": d.change,
            "stat": stat_line(d),
            "before_size": d.before_size,
            "after_size": d.after_size,
            "diff": render_delta(store, d),
        })
    brief = _event_brief(conn, row)
    brief.update({"cwd": row["cwd"], "files": files})
    return brief


def api_blame(conn: sqlite3.Connection, root_id: int, rel: str) -> dict:
    entries = [
        {
            "id": int(r["id"]),
            "time": fmt_ts(r["started_at"]),
            "change": r["change"],
            "command": describe_command(r),
            "exit": r["exit_code"],
        }
        for r in dbm.events_touching(conn, root_id, rel)
    ]
    return {"path": rel, "entries": entries}


def api_search(
    conn: sqlite3.Connection, store: ObjectStore, *,
    root_id: int | None, q: str, mode: str, limit: int,
) -> dict:
    if mode == "content":
        try:
            rx = re.compile(q)
        except re.error as exc:
            return {"error": f"bad regex: {exc}"}
        hits = []
        for event in reversed(
            dbm.recent_events(conn, root_id=root_id, limit=limit, changes_only=True)
        ):
            matched = []
            for d in dbm.deltas_for(conn, int(event["id"])):
                lines = [
                    ln for ln in render_delta(store, d, max_lines=2000)
                    if ln[:1] in "+-" and not ln.startswith(("+++", "---"))
                    and rx.search(ln[1:])
                ]
                if lines:
                    matched.append({"path": d.path, "lines": lines[:6]})
            if matched:
                hits.append({**_event_brief(conn, event), "matches": matched})
        return {"mode": "content", "hits": hits}
    rows = dbm.search_commands(conn, q, root_id=root_id, limit=limit)
    return {"mode": "command", "hits": [_event_brief(conn, r) for r in rows]}


def api_graph(conn: sqlite3.Connection, root_id: int, limit: int) -> dict:
    from .graphview import _NODE, build_graph

    g = build_graph(conn, root_id, limit=limit)
    rows = []
    for r in g.rows:
        cells = [
            (_NODE[r.kind] if col == r.node_lane else ("│" if live else " "))
            for col, live in enumerate(r.lanes)
        ]
        rows.append({
            "graph": " ".join(cells),
            "node_lane": r.node_lane,
            "kind": r.kind,
            "id": int(r.event["id"]),
            "time": fmt_ts(r.event["started_at"]).split(" ")[1],
            "command": describe_command(r.event),
            "annot": r.annot,
        })
    return {"branches": list(g.branch_names), "rows": rows}


def api_stats(conn: sqlite3.Connection, paths: Paths, root_id: int | None) -> dict:
    scope, params = "", []
    if root_id is not None:
        scope, params = " AND e.root_id = ?", [root_id]
    tot = conn.execute(
        f"SELECT COUNT(*) AS n, SUM(command IS NULL) AS ext,"
        f" SUM(exit_code IS NOT NULL AND exit_code != 0) AS failed,"
        f" MIN(started_at) AS first, MAX(started_at) AS last"
        f" FROM events e WHERE 1=1{scope}", params,
    ).fetchone()
    changed = conn.execute(
        f"SELECT COUNT(DISTINCT e.id) AS n FROM events e"
        f" JOIN deltas d ON d.event_id = e.id WHERE 1=1{scope}", params,
    ).fetchone()["n"]
    hot = [
        {"path": r["path"], "n": r["n"]}
        for r in conn.execute(
            f"SELECT d.path, COUNT(*) AS n FROM deltas d JOIN events e ON e.id=d.event_id"
            f" WHERE 1=1{scope} GROUP BY d.path ORDER BY n DESC, d.path LIMIT 12", params)
    ]
    noisy = [
        {"command": r["command"], "n": r["n"]}
        for r in conn.execute(
            f"SELECT e.command, COUNT(*) AS n FROM deltas d JOIN events e ON e.id=d.event_id"
            f" WHERE e.command IS NOT NULL{scope} GROUP BY e.command ORDER BY n DESC LIMIT 12",
            params)
    ]
    blobs, stored = ObjectStore(paths.objects).disk_usage()
    return {
        "events": tot["n"] or 0, "changed": changed,
        "external": tot["ext"] or 0, "failed": tot["failed"] or 0,
        "first": fmt_ts(tot["first"]) if tot["first"] else "-",
        "last": fmt_ts(tot["last"]) if tot["last"] else "-",
        "hot_files": hot, "noisy": noisy,
        "blobs": blobs, "bytes": stored,
    }


class _Handler(BaseHTTPRequestHandler):
    paths: Paths
    default_root: int | None = None

    def log_message(self, *args) -> None:  # keep the terminal quiet
        pass

    def _db(self) -> sqlite3.Connection:
        return dbm.connect(self.paths.db, readonly=True)

    def _send(self, body: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._send(json.dumps(obj).encode("utf-8"), "application/json", status)

    def _serve_bundle(self, root_id: int | None) -> None:
        """Stream a .chronx export archive for a root (read-only)."""
        import tempfile
        from .transfer import export_root

        conn = self._db()
        try:
            if root_id is not None:
                row = conn.execute(
                    "SELECT * FROM roots WHERE id = ?", (root_id,)
                ).fetchone()
            else:
                roots = dbm.get_roots(conn)
                row = roots[0] if roots else None
            if row is None:
                self._json({"error": "no such root"}, 404)
                return
            with tempfile.NamedTemporaryFile(
                suffix=".chronx", delete=False, dir=str(self.paths.home)
            ) as tf:
                tmp = Path(tf.name)
            try:
                export_root(conn, ObjectStore(self.paths.objects), row, tmp)
                data = tmp.read_bytes()
            finally:
                tmp.unlink(missing_ok=True)
        finally:
            conn.close()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header(
            "Content-Disposition", 'attachment; filename="chronx-history.chronx"')
        self.send_header("X-Chronx-Root", str(row["path"]))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)

        def qi(name, default=None):
            if name in q and q[name]:
                try:
                    return int(q[name][0])
                except ValueError:
                    return default
            return default

        def qs(name, default=None):
            return q[name][0] if name in q and q[name] else default

        try:
            if path in ("/", "/index.html"):
                self._send(INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/bundle":
                self._serve_bundle(qi("root"))
                return
            if not path.startswith("/api/"):
                self._json({"error": "not found"}, 404)
                return

            conn = self._db()
            try:
                store = ObjectStore(self.paths.objects)
                if path == "/api/summary":
                    self._json(api_summary(conn, self.default_root))
                elif path == "/api/events":
                    self._json(api_events(
                        conn, root_id=qi("root"), limit=qi("limit", 300),
                        q=qs("q"), changes=qs("changes") == "1", after=qi("after")))
                elif path.startswith("/api/event/"):
                    self._json(api_event(conn, store, int(path.rsplit("/", 1)[1])))
                elif path == "/api/blame":
                    self._json(api_blame(conn, qi("root"), qs("path", "")))
                elif path == "/api/search":
                    self._json(api_search(
                        conn, store, root_id=qi("root"), q=qs("q", ""),
                        mode=qs("mode", "command"), limit=qi("limit", 200)))
                elif path == "/api/stats":
                    self._json(api_stats(conn, self.paths, qi("root")))
                elif path == "/api/graph":
                    root = qi("root")
                    if root is None:
                        self._json({"branches": [], "rows": []})
                    else:
                        self._json(api_graph(conn, root, qi("limit", 200)))
                else:
                    self._json({"error": "not found"}, 404)
            finally:
                conn.close()
        except Exception as exc:  # never crash the server on a bad request
            self._json({"error": str(exc)}, 500)


def make_server(
    paths: Paths, host: str, port: int, default_root: int | None
) -> ThreadingHTTPServer:
    handler = type("ChronxHandler", (_Handler,), {
        "paths": paths, "default_root": default_root,
        "server_version": f"chronx/{__version__}",
    })
    return ThreadingHTTPServer((host, port), handler)


INDEX_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>chronx</title>
<style>
:root{--bg:#0f1115;--panel:#161a22;--panel2:#1b2029;--border:#252c38;--fg:#d6dae3;
--dim:#7c8698;--yellow:#e5c07b;--green:#8fd07b;--red:#e06c75;--cyan:#56b6c2;
--mag:#c678dd;--accent:#61afef}
*{box-sizing:border-box}
body{margin:0;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:var(--bg);color:var(--fg)}
header{display:flex;gap:12px;align-items:center;padding:10px 14px;background:var(--panel);
border-bottom:1px solid var(--border);position:sticky;top:0;z-index:5}
header h1{font-size:16px;margin:0;letter-spacing:.5px}
header h1 b{color:var(--accent)}
select,input,button{background:var(--panel2);color:var(--fg);border:1px solid var(--border);
border-radius:6px;padding:6px 9px;font:inherit}
input#q{flex:1;min-width:120px}
button{cursor:pointer}
button.on{border-color:var(--accent);color:var(--accent)}
#live{width:9px;height:9px;border-radius:50%;background:var(--green);box-shadow:0 0 6px var(--green)}
#wrap{display:flex;height:calc(100vh - 53px)}
#list{width:44%;min-width:320px;overflow:auto;border-right:1px solid var(--border)}
#detail{flex:1;overflow:auto;padding:14px}
.ev{padding:8px 12px;border-bottom:1px solid var(--border);cursor:pointer;display:flex;
gap:10px;align-items:baseline}
.ev:hover{background:var(--panel)}
.ev.sel{background:var(--panel2);box-shadow:inset 3px 0 0 var(--accent)}
.ev .id{color:var(--dim);font-family:ui-monospace,monospace;font-size:12px;min-width:42px}
.ev .cmd{flex:1;font-family:ui-monospace,monospace;white-space:nowrap;overflow:hidden;
text-overflow:ellipsis;color:var(--yellow)}
.ev.ext .cmd{color:var(--dim);font-style:italic}
.ev .badge{font-family:ui-monospace,monospace;font-size:12px;color:var(--mag)}
.ev .x{font-family:ui-monospace,monospace;font-size:12px}
.ev .t{color:var(--dim);font-size:11px;font-family:ui-monospace,monospace}
.meta{color:var(--dim);font-size:12px;margin-bottom:2px}
.cmdline{font-family:ui-monospace,monospace;color:var(--yellow);font-size:15px;
background:var(--panel);padding:8px 10px;border-radius:6px;border:1px solid var(--border);
white-space:pre-wrap;word-break:break-all}
.file{margin-top:14px}
.file h3{margin:0 0 4px;font:13px ui-monospace,monospace;font-weight:600}
.file h3 .k{display:inline-block;width:16px;text-align:center;border-radius:3px;margin-right:6px;
font-size:11px}
.k.A{background:#1d3b24;color:var(--green)}.k.M{background:#3a341c;color:var(--yellow)}
.k.D{background:#3a2224;color:var(--red)}
.file h3 .blame{float:right;font-weight:400;color:var(--accent);cursor:pointer;font-size:12px}
pre.diff{margin:0;background:var(--panel);border:1px solid var(--border);border-radius:6px;
overflow:auto;font:12.5px/1.45 ui-monospace,monospace;padding:6px 0}
pre.diff .ln{padding:0 10px;white-space:pre}
.dl.add{background:rgba(143,208,123,.08);color:var(--green)}
.dl.del{background:rgba(224,108,117,.08);color:var(--red)}
.dl.hunk{color:var(--cyan)}.dl.head{color:var(--fg);font-weight:600}
.hint{color:var(--dim);padding:30px;text-align:center}
.stats table{border-collapse:collapse;width:100%;margin:6px 0 18px}
.stats td{padding:3px 8px;border-bottom:1px solid var(--border);font-family:ui-monospace,monospace}
.stats td.n{color:var(--mag);text-align:right;width:60px}
.stats h2{font-size:14px;margin:16px 0 4px}
.pill{display:inline-block;background:var(--panel2);border:1px solid var(--border);border-radius:20px;
padding:2px 10px;margin:2px;font-size:12px;font-family:ui-monospace,monospace}
.tag{color:var(--dim);font-size:12px}
</style></head><body>
<header>
  <h1><b>chron</b>x</h1>
  <select id="root"></select>
  <input id="q" placeholder="filter commands…  (prefix S: for content pickaxe)">
  <button id="changes" title="only commands that changed files">± only</button>
  <button id="graphBtn" title="timeline graph">graph</button>
  <button id="statsBtn" title="statistics">stats</button>
  <span class="tag" id="count"></span>
  <span id="live" title="live"></span>
</header>
<div id="wrap">
  <div id="list"></div>
  <div id="detail"><div class="hint">Select a command to see what it changed.</div></div>
</div>
<script>
const $=s=>document.querySelector(s), listEl=$('#list'), detailEl=$('#detail');
let root=null, maxId=0, sel=null, changes=false, timer=null, q='';

function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
async function j(u){const r=await fetch(u);return r.json();}

function evRow(e){
  const d=document.createElement('div');
  d.className='ev'+(e.external?' ext':'')+(e.id===sel?' sel':'');
  d.dataset.id=e.id;
  const badge=e.total?`<span class="badge">±${e.total}</span>`:'<span class="badge" style="color:var(--dim)">·</span>';
  const x=e.exit===null?'<span class="x" style="color:var(--dim)">–</span>'
        :`<span class="x" style="color:${e.exit===0?'var(--green)':'var(--red)'}">${e.exit}</span>`;
  d.innerHTML=`<span class="id">#${e.id}</span>${badge}${x}`+
    `<span class="cmd" title="${esc(e.command)}">${esc(e.command)}</span>`+
    `<span class="t">${e.time.slice(11)}</span>`;
  d.onclick=()=>{sel=e.id;render();openEvent(e.id);};
  return d;
}
function render(){document.querySelectorAll('.ev').forEach(n=>
  n.classList.toggle('sel',+n.dataset.id===sel));}

async function loadRoots(){
  const s=await j('/api/summary'); root=s.active_root;
  const sel=$('#root'); sel.innerHTML='';
  s.roots.forEach(r=>{const o=document.createElement('option');o.value=r.id;
    o.textContent=r.path.replace(/^.*\//,'…/'+r.path.split('/').slice(-2).join('/')==r.path?'':'')||r.path;
    o.textContent=r.path;o.title=r.path;if(r.id===root)o.selected=true;sel.appendChild(o);});
  sel.onchange=()=>{root=+sel.value;maxId=0;listEl.innerHTML='';loadEvents(true);};
}
async function loadEvents(reset){
  if(reset){maxId=0;listEl.innerHTML='';}
  const p=new URLSearchParams({limit:300}); if(root!=null)p.set('root',root);
  if(changes)p.set('changes','1'); if(q)p.set('q',q);
  const d=await j('/api/events?'+p);
  listEl.innerHTML=''; d.events.forEach(e=>listEl.appendChild(evRow(e)));
  maxId=d.max_id; $('#count').textContent=d.events.length+' events';
}
async function poll(){
  if(q||changes) return; // live only in default view
  const p=new URLSearchParams({after:maxId}); if(root!=null)p.set('root',root);
  const d=await j('/api/events?'+p);
  if(d.events.length){d.events.forEach(e=>listEl.insertBefore(evRow(e),listEl.firstChild));
    maxId=d.max_id; $('#count').textContent=listEl.children.length+' events';}
}
function diffHtml(lines){
  return '<pre class="diff">'+lines.map(l=>{
    let c='';const h=l[0];
    if(l.startsWith('+++')||l.startsWith('---'))c='head';
    else if(h==='@')c='hunk';else if(h==='+')c='add';else if(h==='-')c='del';
    return `<div class="ln dl ${c}">${esc(l)||' '}</div>`;}).join('')+'</pre>';
}
async function openEvent(id){
  const e=await j('/api/event/'+id);
  if(e.error){detailEl.innerHTML=`<div class="hint">${esc(e.error)}</div>`;return;}
  let h=`<div class="meta">event #${e.id} · ${e.time} · exit `+
    (e.exit===null?'–':e.exit)+` · <span class="tag">${esc(e.cwd||'')}</span></div>`+
    `<div class="cmdline">$ ${esc(e.command)}</div>`;
  if(!e.files.length)h+='<div class="hint">No filesystem changes.</div>';
  e.files.forEach(f=>{h+=`<div class="file"><h3><span class="k ${f.change}">${f.change}</span>`+
    `${esc(f.path)}<span class="blame" onclick="blame('${encodeURIComponent(f.path)}')">blame ↗</span></h3>`+
    diffHtml(f.diff)+`</div>`;});
  detailEl.innerHTML=h; detailEl.scrollTop=0;
}
async function blame(p){
  const d=await j('/api/blame?root='+root+'&path='+p);
  let h=`<div class="meta">blame</div><div class="cmdline">${esc(decodeURIComponent(p))}</div>`;
  h+='<div class="stats"><table>';
  d.entries.forEach(e=>{h+=`<tr><td class="n">#${e.id}</td><td>${e.time.slice(5)}</td>`+
    `<td>${e.change}</td><td style="color:var(--yellow);cursor:pointer" onclick="sel=${e.id};render();openEvent(${e.id})">${esc(e.command)}</td></tr>`;});
  h+='</table></div>'; if(!d.entries.length)h+='<div class="hint">No recorded changes.</div>';
  detailEl.innerHTML=h; detailEl.scrollTop=0;
}
async function showStats(){
  const p=new URLSearchParams(); if(root!=null)p.set('root',root);
  const s=await j('/api/stats?'+p);
  let h=`<div class="stats"><div class="meta">statistics</div>`+
    `<p><span class="pill">${s.events} events</span><span class="pill">${s.changed} changed files</span>`+
    `<span class="pill">${s.external} external</span><span class="pill">${s.failed} failed</span>`+
    `<span class="pill">${s.blobs} blobs</span><span class="pill">${(s.bytes/1024).toFixed(1)} KiB</span></p>`+
    `<div class="tag">${s.first} → ${s.last}</div>`+
    `<h2>hottest files</h2><table>`;
  s.hot_files.forEach(r=>h+=`<tr><td class="n">${r.n}</td><td>${esc(r.path)}</td></tr>`);
  h+='</table><h2>noisiest commands</h2><table>';
  s.noisy.forEach(r=>h+=`<tr><td class="n">${r.n}</td><td style="color:var(--yellow)">${esc(r.command)}</td></tr>`);
  h+='</table></div>'; detailEl.innerHTML=h; detailEl.scrollTop=0;
}
async function runSearch(){
  const raw=$('#q').value.trim();
  if(raw.startsWith('S:')){
    const d=await j('/api/search?mode=content&root='+root+'&q='+encodeURIComponent(raw.slice(2).trim()));
    let h='<div class="meta">content pickaxe</div>';
    if(d.error)h+=`<div class="hint">${esc(d.error)}</div>`;
    (d.hits||[]).forEach(e=>{h+=`<div class="file"><h3>#${e.id} `+
      `<span style="color:var(--yellow)">${esc(e.command)}</span></h3>`;
      e.matches.forEach(m=>{h+=`<div class="tag">${esc(m.path)}</div>`+diffHtml(m.lines);});h+='</div>';});
    if(!(d.hits||[]).length&&!d.error)h+='<div class="hint">No matching added/removed lines.</div>';
    detailEl.innerHTML=h; q=''; return;
  }
  q=raw; loadEvents(true);
}
$('#q').addEventListener('keydown',e=>{if(e.key==='Enter')runSearch();
  else if(e.target.value===''){q='';}});
$('#q').addEventListener('input',e=>{if(e.target.value===''&&q){q='';loadEvents(true);}});
const LANE_COLORS=['#56b6c2','#8fd07b','#e5c07b','#c678dd','#61afef','#e06c75','#4dd0e1'];
async function showGraph(){
  const p=new URLSearchParams({limit:200}); if(root!=null)p.set('root',root);
  const d=await j('/api/graph?'+p);
  let legend=d.branches.map((n,i)=>`<span class="pill" style="color:${LANE_COLORS[i%LANE_COLORS.length]}">●${esc(n)}</span>`).join(' ');
  let h=`<div class="meta">timeline graph</div><p>${legend}</p><pre class="diff" style="padding:8px 10px">`;
  d.rows.forEach(r=>{
    let g='';
    for(let c=0;c<r.graph.length;c++){const ch=r.graph[c];
      const lane=Math.floor(c/2);const col=LANE_COLORS[lane%LANE_COLORS.length];
      g+=ch===' '?' ':`<span style="color:${col}">${ch}</span>`;}
    const cc=r.kind==='cmd'?'var(--yellow)':(r.kind==='merge'?'var(--mag)':'var(--dim)');
    const annot=r.annot?`<span class="tag"> ← ${esc(r.annot)}</span>`:'';
    h+=`<div class="ln">${g}  <span class="tag">#${r.id} ${r.time}</span> `+
       `<span style="color:${cc}">${esc(r.command)}</span>${annot}</div>`;
  });
  h+='</pre>'; if(!d.rows.length)h+='<div class="hint">No events yet.</div>';
  detailEl.innerHTML=h; detailEl.scrollTop=0;
}
$('#changes').onclick=()=>{changes=!changes;$('#changes').classList.toggle('on',changes);loadEvents(true);};
$('#graphBtn').onclick=showGraph;
$('#statsBtn').onclick=showStats;
(async()=>{await loadRoots();await loadEvents(true);timer=setInterval(poll,2500);})();
</script></body></html>
"""
