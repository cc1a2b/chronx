"""chronx cast — export a self-contained, shareable HTML "replay" of a session.

Unlike ``chronx serve`` (a live local server), ``cast`` writes ONE static
``.html`` file that anyone can open in a browser with no server and no chronx
install: a dark, terminal-styled timeline of the recorded events, each an
expandable card showing the command, its exit code, and colorized unified
diffs of every file it touched.

Scoped to the tracked root containing the cwd and its active timeline (branch),
this reads the store READ-ONLY and only ever writes the requested output file.

Everything (CSS + JS) is inlined; there are NO external references, so the page
is fully offline. ALL recorded content (commands and file contents are
untrusted) is escaped with :func:`html.escape`, so a recorded ``<script>`` can
never break out of the page or inject script.

Imports only ``chronx.pluginlib`` (as X) plus stdlib ``html``, so it stays
decoupled from ``cli.py`` and is auto-discovered from the filesystem.
"""

from __future__ import annotations

import html

from chronx import pluginlib as X

# Per-file diff line cap; keeps a huge change from bloating the page. Oversize /
# binary / unavailable blobs already self-truncate to a single ``@@ ... @@``
# marker inside ``render_delta``.
_MAX_DIFF_LINES = 500

# Human labels for the single-letter change kinds stored on a delta.
_CHANGE_WORD = {"A": "added", "M": "modified", "D": "deleted"}


def _h(value: object) -> str:
    """HTML-escape any value (quote=True), safe in both text and attributes.

    ``None`` renders as empty. This is the single choke point that neutralizes
    untrusted recorded content (commands, paths, diff bodies).
    """
    return html.escape("" if value is None else str(value), quote=True)


def _line_class(line: str) -> str:
    """Map a unified-diff line to a CSS class by its leading marker."""
    if line.startswith(("+++", "---")):
        return "head"  # file headers -> bold
    if line.startswith("@@"):
        return "hunk"  # hunk / binary / truncation markers -> cyan
    head = line[:1]
    if head == "+":
        return "add"  # insertion -> green
    if head == "-":
        return "del"  # deletion -> red
    return "ctx"  # context -> dim


def _diff_html(lines: list[str]) -> str:
    """Render diff lines as a ``<pre>`` block, one escaped span per line."""
    rows: list[str] = []
    for line in lines:
        # ``or " "`` keeps blank lines from collapsing to zero height.
        rows.append(f'<div class="dl {_line_class(line)}">{_h(line) or " "}</div>')
    return '<pre class="diff">' + "".join(rows) + "</pre>"


def _file_detail(delta: "X.dbm.Delta") -> str:
    """A compact size summary for one delta, e.g. ``312 -> 340 B``."""
    if delta.change == "A":
        return f"+{delta.after_size or 0} B"
    if delta.change == "D":
        return f"-{delta.before_size or 0} B"
    before = delta.before_size or 0
    after = delta.after_size if delta.after_size is not None else "?"
    return f"{before} → {after} B"


def _exit_chip(code: object) -> str:
    """A colored exit-code chip: green ok, red failed, dim when unrecorded."""
    if code is None:
        return '<span class="exit na" title="no exit code">exit —</span>'
    cls = "ok" if code == 0 else "bad"
    return f'<span class="exit {cls}">exit {_h(code)}</span>'


def _badges(counts: dict[str, int]) -> str:
    """Per-event add/modify/delete badges (or a muted 'no file changes')."""
    a, m, d = counts["A"], counts["M"], counts["D"]
    if not (a or m or d):
        return '<span class="badges"><span class="b none">no file changes</span></span>'
    parts: list[str] = []
    if a:
        parts.append(f'<span class="b A">+{a} added</span>')
    if m:
        parts.append(f'<span class="b M">~{m} modified</span>')
    if d:
        parts.append(f'<span class="b D">-{d} deleted</span>')
    return '<span class="badges">' + "".join(parts) + "</span>"


def _file_block(delta: "X.dbm.Delta", diff_lines: list[str]) -> str:
    """One file's header (change kind + path + size) followed by its diff."""
    word = _CHANGE_WORD.get(delta.change, delta.change)
    return (
        '<div class="file">'
        '<div class="fhead">'
        f'<span class="k {_h(delta.change)}" title="{_h(word)}">{_h(delta.change)}</span>'
        f'<span class="fpath">{_h(delta.path)}</span>'
        f'<span class="fstat">{_h(_file_detail(delta))}</span>'
        "</div>"
        f"{_diff_html(diff_lines)}"
        "</div>"
    )


def _card(
    *,
    ev_id: int,
    time_str: str,
    exit_code: object,
    command: str,
    external: bool,
    counts: dict[str, int],
    files_html: str,
    failed: bool,
) -> str:
    """A single expandable ``<details>`` event card."""
    cls = "event"
    if failed:
        cls += " failed"
    elif files_html:
        cls += " changed"

    if external:
        cmd_html = f'<span class="cmd ext">{_h(command)}</span>'
    else:
        cmd_html = (
            f'<span class="cmd"><span class="dollar">$ </span>{_h(command)}</span>'
        )

    summary = (
        "<summary>"
        f'<span class="eid">#{_h(ev_id)}</span>'
        f'<span class="etime">{_h(time_str)}</span>'
        f"{_exit_chip(exit_code)}"
        f"{_badges(counts)}"
        f"{cmd_html}"
        "</summary>"
    )
    if files_html:
        body = f'<div class="body">{files_html}</div>'
    else:
        body = (
            '<div class="body"><div class="empty">'
            "No filesystem changes recorded for this command."
            "</div></div>"
        )
    # data-cmd (lowercased, escaped) powers the client-side command filter.
    return (
        f'<details class="{cls}" data-cmd="{_h(command.lower())}">'
        f"{summary}{body}</details>"
    )


# --- inline assets (no external references; fully offline) -------------------

_CSS = """
:root{
  --bg:#0f1115;--panel:#161a22;--panel2:#1b2029;--border:#252c38;--fg:#d6dae3;
  --dim:#7c8698;--yellow:#e5c07b;--green:#8fd07b;--red:#e06c75;--cyan:#56b6c2;
  --mag:#c678dd;--accent:#61afef;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{background:var(--bg);color:var(--fg);font:14px/1.55 var(--mono);
  -webkit-font-smoothing:antialiased;padding:0 0 60px}
.wrap{max-width:960px;margin:0 auto;padding:22px 18px}
header.top{border-bottom:1px solid var(--border);padding-bottom:16px}
header.top h1{margin:0;font-size:20px;letter-spacing:.5px;font-weight:600}
header.top h1 b{color:var(--accent)}
.sub{color:var(--dim);font-size:13px;margin-top:6px;word-break:break-all}
.pills{margin-top:12px;display:flex;flex-wrap:wrap;gap:6px}
.pill{background:var(--panel2);border:1px solid var(--border);border-radius:20px;
  padding:3px 11px;font-size:12px;color:var(--fg)}
.pill b{color:var(--accent);font-weight:600}
.toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:16px 0 8px;
  position:sticky;top:0;background:var(--bg);padding:10px 0;z-index:5}
.toolbar input{flex:1;min-width:160px;background:var(--panel2);color:var(--fg);
  border:1px solid var(--border);border-radius:6px;padding:7px 10px;font:inherit}
.toolbar button{background:var(--panel2);color:var(--fg);border:1px solid var(--border);
  border-radius:6px;padding:7px 11px;font:inherit;cursor:pointer}
.toolbar button:hover{border-color:var(--accent);color:var(--accent)}
.toolbar .count{color:var(--dim);font-size:12px;white-space:nowrap}
.timeline{border-left:2px solid var(--border);margin-left:8px;margin-top:6px}
details.event{position:relative;margin:0 0 10px 18px;background:var(--panel);
  border:1px solid var(--border);border-radius:8px;overflow:hidden}
details.event::before{content:"";position:absolute;left:-27px;top:15px;width:10px;
  height:10px;border-radius:50%;background:var(--dim);border:2px solid var(--bg)}
details.event.changed::before{background:var(--accent)}
details.event.failed::before{background:var(--red)}
details.event[open]{border-color:var(--accent)}
summary{list-style:none;cursor:pointer;padding:11px 14px;display:flex;gap:10px;
  align-items:baseline;flex-wrap:wrap}
summary::-webkit-details-marker{display:none}
summary:hover{background:var(--panel2)}
.eid{color:var(--dim);font-size:12px;min-width:44px}
.etime{color:var(--dim);font-size:12px}
.exit{font-size:12px;padding:0 6px;border-radius:4px;border:1px solid var(--border)}
.exit.ok{color:var(--green)}.exit.bad{color:var(--red)}.exit.na{color:var(--dim)}
.badges{display:flex;gap:5px;flex-wrap:wrap}
.b{font-size:11px;padding:1px 6px;border-radius:4px}
.b.A{background:#173026;color:var(--green)}
.b.M{background:#302a15;color:var(--yellow)}
.b.D{background:#301a1c;color:var(--red)}
.b.none{background:var(--panel2);color:var(--dim)}
.cmd{flex:1 1 100%;font-size:13.5px;color:var(--yellow);white-space:pre-wrap;
  word-break:break-word;margin-top:4px}
.cmd .dollar{color:var(--green);user-select:none}
.cmd.ext{color:var(--dim);font-style:italic}
.body{border-top:1px solid var(--border);padding:6px 14px 12px}
.file{margin-top:12px}
.file:first-child{margin-top:6px}
.fhead{display:flex;gap:8px;align-items:baseline;margin-bottom:5px;flex-wrap:wrap}
.k{display:inline-block;min-width:16px;text-align:center;border-radius:3px;
  font-size:11px;padding:0 4px}
.k.A{background:#173026;color:var(--green)}
.k.M{background:#302a15;color:var(--yellow)}
.k.D{background:#301a1c;color:var(--red)}
.fpath{color:var(--fg);word-break:break-all}
.fstat{color:var(--dim);font-size:12px}
pre.diff{margin:0;background:var(--panel2);border:1px solid var(--border);
  border-radius:6px;overflow-x:auto;font:12.5px/1.5 var(--mono);padding:6px 0}
pre.diff .dl{padding:0 12px;white-space:pre;display:block}
.dl.add{background:rgba(143,208,123,.09);color:var(--green)}
.dl.del{background:rgba(224,108,117,.09);color:var(--red)}
.dl.hunk{color:var(--cyan)}
.dl.head{color:var(--fg);font-weight:600}
.dl.ctx{color:var(--dim)}
.empty{color:var(--dim);padding:40px 10px;text-align:center}
footer{color:var(--dim);font-size:12px;text-align:center;margin-top:30px;
  border-top:1px solid var(--border);padding-top:14px}
"""

# Progressive-enhancement only: cards expand natively via <details>. This adds a
# command filter and expand/collapse-all. It reads attributes and toggles
# state; it never inserts recorded content, so it cannot be an injection vector.
_JS = """
(function(){
  var box=document.getElementById('filter');
  var cards=[].slice.call(document.querySelectorAll('details.event'));
  var shown=document.getElementById('shown');
  var nomatch=document.getElementById('nomatch');
  function apply(){
    var q=(box&&box.value||'').trim().toLowerCase();
    var n=0;
    for(var i=0;i<cards.length;i++){
      var c=cards[i];
      var hit=!q||(c.getAttribute('data-cmd')||'').indexOf(q)!==-1;
      c.style.display=hit?'':'none';
      if(hit)n++;
    }
    if(shown)shown.textContent=n;
    if(nomatch)nomatch.style.display=(n||!cards.length)?'none':'';
  }
  if(box)box.addEventListener('input',apply);
  var ex=document.getElementById('expandAll');
  var co=document.getElementById('collapseAll');
  if(ex)ex.addEventListener('click',function(){
    cards.forEach(function(c){if(c.style.display!=='none')c.open=true;});});
  if(co)co.addEventListener('click',function(){
    cards.forEach(function(c){c.open=false;});});
})();
"""


def register(main: "X.click.Group") -> None:
    @main.command()
    @X.click.option(
        "--output", "-o",
        type=X.click.Path(path_type=X.Path),
        required=True,
        help="Destination .html file.",
    )
    @X.click.option(
        "--limit", "-n", default=200, show_default=True,
        help="Most recent events to include.",
    )
    @X.click.option(
        "--since", default=None,
        help="Only events at/after this moment (mark, #id, 'now', or a time spec).",
    )
    def cast(output: "X.Path", limit: int, since: str | None) -> None:
        """Export a self-contained HTML replay of the recorded session."""
        conn = X.open_db()
        store = X.ObjectStore(X.paths().objects)
        try:
            # Scope to the cwd's tracked root; raises a clean ClickException if
            # the cwd isn't inside any tracked directory.
            root = X.root_for_cwd(conn)
            root_id = int(root["id"])
            root_path = str(root["path"])
            active = X.active_branch_id(conn, root_id)

            branch_row = X.dbm.get_branch(conn, active) if active is not None else None
            branch_name = branch_row["name"] if branch_row is not None else "main"

            # Newest `limit` events, oldest-first (timeline order).
            events = X.dbm.recent_events(
                conn, root_id=root_id, limit=limit, branch_id=active
            )
            if since is not None:
                since_ts = X.moment_ts(conn, since)  # ClickException on bad spec
                events = [e for e in events if e["started_at"] >= since_ts]

            cards: list[str] = []
            total_deltas = 0
            n_failed = 0
            for ev in events:
                ev_id = int(ev["id"])
                exit_code = ev["exit_code"]
                external = ev["command"] is None
                failed = exit_code not in (None, 0)
                if failed:
                    n_failed += 1

                # Read deltas + diffs defensively: never crash on a bad row,
                # missing/binary/corrupt blob (render_delta emits a marker).
                try:
                    deltas = X.dbm.deltas_for(conn, ev_id)
                except Exception:
                    deltas = []
                counts = {"A": 0, "M": 0, "D": 0}
                blocks: list[str] = []
                for d in deltas:
                    if d.change in counts:
                        counts[d.change] += 1
                    try:
                        diff_lines = X.render_delta(store, d, max_lines=_MAX_DIFF_LINES)
                    except Exception:
                        diff_lines = [f"@@ could not render diff for {d.path} @@"]
                    blocks.append(_file_block(d, diff_lines))
                total_deltas += len(deltas)

                cards.append(
                    _card(
                        ev_id=ev_id,
                        time_str=X.fmt_ts(ev["started_at"]),
                        exit_code=exit_code,
                        command=X.describe_command(ev),
                        external=external,
                        counts=counts,
                        files_html="".join(blocks),
                        failed=failed,
                    )
                )

            n_events = len(events)
            if events:
                span = (
                    f"{_h(X.fmt_ts(events[0]['started_at']))}"
                    f"  →  {_h(X.fmt_ts(events[-1]['started_at']))}"
                )
            else:
                span = "—"

            # ---- assemble the document (concatenated; CSS/JS are raw) --------
            header = (
                '<header class="top">'
                "<h1><b>chronx</b> cast</h1>"
                f'<div class="sub">{_h(root_path)}</div>'
                '<div class="pills">'
                f'<span class="pill"><b>{n_events}</b> event(s)</span>'
                f'<span class="pill"><b>{total_deltas}</b> file change(s)</span>'
                f'<span class="pill"><b>{n_failed}</b> failed</span>'
                f'<span class="pill">timeline <b>{_h(branch_name)}</b></span>'
                f'<span class="pill">{span}</span>'
                "</div></header>"
            )
            toolbar = (
                '<div class="toolbar">'
                '<input id="filter" type="text" placeholder="filter commands…" '
                'autocomplete="off" spellcheck="false">'
                '<button id="expandAll" type="button">expand all</button>'
                '<button id="collapseAll" type="button">collapse all</button>'
                f'<span class="count"><span id="shown">{n_events}</span> / '
                f"{n_events} shown</span>"
                "</div>"
            )
            if cards:
                timeline = (
                    '<div class="timeline">' + "".join(cards) + "</div>"
                    '<div class="empty" id="nomatch" style="display:none">'
                    "No commands match your filter.</div>"
                )
            else:
                timeline = (
                    '<div class="empty">No recorded events on this timeline yet.</div>'
                )

            doc = (
                "<!doctype html>\n"
                '<html lang="en">\n<head>\n'
                '<meta charset="utf-8">\n'
                '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
                f"<title>chronx cast — {_h(root_path)}</title>\n"
                "<style>" + _CSS + "</style>\n"
                "</head>\n<body>\n"
                '<div class="wrap">'
                + header
                + toolbar
                + timeline
                + '<footer>generated by <b>chronx cast</b> · '
                "self-contained replay · no server required</footer>"
                "</div>\n"
                "<script>" + _JS + "</script>\n"
                "</body>\n</html>\n"
            )

            data = doc.encode("utf-8")
            X.write_atomic(output, data)
            X.click.secho(
                f"wrote replay → {output}  "
                f"({X.human_bytes(len(data))}, {n_events} event(s))",
                fg="green",
            )
            X.click.secho(
                "open it in any browser — no server or chronx needed.", dim=True
            )
        finally:
            conn.close()
