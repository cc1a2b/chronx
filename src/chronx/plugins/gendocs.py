"""chronx gen-docs — generate a Markdown command reference for the whole CLI.

This large CLI (60+ commands, most of them auto-discovered plugins) is a pain to
document by hand and drifts the moment a new command lands. Instead we walk the
live click command tree at run time and emit a complete, deterministic Markdown
reference — headings, one-line help, and per-command argument/option tables —
ready to drop into the README or a docs site.

Pure CLI introspection: it reads the click objects only, never the chronx store,
so it works with no `chronx init` and can't race the recorder.
"""

from __future__ import annotations

import inspect
import sys
from typing import Iterator

from chronx import pluginlib as X

# A command node in the walk: its full path parts (e.g. ["chronx", "daemon",
# "start"]) and the click Command/Group object itself.
_Node = tuple[list[str], "X.click.Command"]


def _walk(group: "X.click.Group", prefix: list[str]) -> Iterator[_Node]:
    """Yield every (path_parts, command) under ``group`` in a stable order.

    Recurses into sub-groups so a group appears immediately before its own
    subcommands. Names are sorted for deterministic output. Hidden commands
    (``cmd.hidden``) are skipped entirely, together with their descendants.
    """
    for name in sorted(group.commands):
        cmd = group.commands[name]
        if getattr(cmd, "hidden", False):
            continue
        parts = prefix + [name]
        yield parts, cmd
        # A click.Group carries its own ``.commands`` dict — recurse into it so
        # subcommands (daemon start, note add, stash pop, roots forget, ...)
        # are documented nested under their parent.
        if isinstance(cmd, X.click.Group):
            yield from _walk(cmd, parts)


def _anchor(text: str) -> str:
    """GitHub-style heading anchor for a command path like 'chronx daemon start'.

    Lowercase, drop anything that isn't a word char / space / hyphen, then turn
    spaces into hyphens — matching how GitHub slugifies Markdown headings.
    """
    slug = "".join(c for c in text.lower() if c.isalnum() or c in " -_")
    return slug.strip().replace(" ", "-")


def _cell(text: object) -> str:
    """Make a value safe to place inside a Markdown table cell (one line)."""
    s = "" if text is None else str(text)
    return s.replace("|", "\\|").replace("\n", " ").strip()


def _short_help(cmd: "X.click.Command") -> str:
    """The command's one-line summary, un-truncated where possible."""
    try:
        # A generous limit avoids click's default 45-char ellipsis truncation.
        summary = cmd.get_short_help_str(limit=1000)
    except Exception:
        summary = ""
    if summary:
        return summary.strip()
    # Fall back to the first non-empty line of the full help text.
    for line in (cmd.help or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _full_help(cmd: "X.click.Command") -> str:
    """The complete help text, cleaned of indentation and click's `\\b` markers."""
    raw = cmd.help
    if not raw:
        return ""
    # inspect.cleandoc normalizes the leading indentation of docstrings; click
    # uses a literal backspace to mark "don't rewrap" paragraphs — strip those.
    return inspect.cleandoc(raw).replace("\b", "").strip()


def _opt_metavar(opt: "X.click.Option") -> str:
    """A human type hint for a value option (flags are handled separately)."""
    # A Choice reads far better as its literal alternatives than as "choice".
    if isinstance(opt.type, X.click.Choice):
        return "[" + "|".join(str(c) for c in opt.type.choices) + "]"
    # Prefer an explicit metavar, else the type's own name (TEXT, INTEGER, ...).
    if opt.metavar:
        return str(opt.metavar)
    name = getattr(opt.type, "name", "") or "text"
    return name.upper()


def _is_help_option(param: "X.click.Parameter") -> bool:
    """True for the auto-added ``--help`` flag (defensive; usually not in params)."""
    return "--help" in getattr(param, "opts", ())


def _render_params(cmd: "X.click.Command") -> list[str]:
    """Markdown lines documenting a command's arguments and options."""
    args: list["X.click.Argument"] = []
    opts: list["X.click.Option"] = []
    for param in cmd.params:
        if _is_help_option(param) or getattr(param, "hidden", False):
            continue
        if isinstance(param, X.click.Argument):
            args.append(param)
        elif isinstance(param, X.click.Option):
            opts.append(param)
        # Any other exotic Parameter subclass is intentionally ignored.

    lines: list[str] = []

    if args:
        lines.append("**Arguments:**")
        lines.append("")
        for arg in args:
            # click renders arguments in uppercase; variadic (nargs == -1)
            # arguments take a trailing ellipsis.
            name = str(arg.name).upper()
            if arg.nargs == -1:
                name += "..."
            note = "required" if arg.required else "optional"
            lines.append(f"- `{name}` — {note}")
        lines.append("")

    if opts:
        lines.append("**Options:**")
        lines.append("")
        lines.append("| Option | Type | Required | Default | Description |")
        lines.append("| --- | --- | --- | --- | --- |")
        for opt in opts:
            # Primary spellings plus the "off" switch of a boolean flag
            # (secondary_opts, e.g. --no-cache) so on/off pairs read clearly.
            names = list(opt.opts) + list(opt.secondary_opts)
            option = ", ".join(f"`{n}`" for n in names)

            if opt.is_flag:
                kind = "flag"
            elif getattr(opt, "count", False):
                kind = "count"
            else:
                kind = _opt_metavar(opt)
            if getattr(opt, "multiple", False):
                kind += " (repeatable)"

            required = "yes" if opt.required else ""

            default = opt.default
            # Skip uninformative defaults: None, a bare False flag, or a
            # callable/dynamic default we can't render meaningfully.
            if default is None or callable(default) or (opt.is_flag and default is False):
                default_str = ""
            else:
                default_str = f"`{_cell(default)}`"

            lines.append(
                f"| {_cell(option)} | {_cell(kind)} | {required} | "
                f"{default_str} | {_cell(opt.help)} |"
            )
        lines.append("")

    if not args and not opts:
        lines.append("_No arguments or options._")
        lines.append("")

    return lines


def _build_doc(main: "X.click.Group", title: str) -> str:
    """Assemble the full Markdown reference document as a single string."""
    nodes = list(_walk(main, ["chronx"]))

    out: list[str] = []
    out.append(f"# {title}")
    out.append("")
    out.append(
        "Auto-generated command reference for the `chronx` CLI, produced by "
        "`chronx gen-docs` by walking the live command tree."
    )
    out.append("")
    out.append(f"**{len(nodes)}** commands and subcommands are documented below.")
    out.append("")

    # ---- Table of contents (indented to mirror group nesting) -------------
    out.append("## Table of contents")
    out.append("")
    for parts, _cmd in nodes:
        path = " ".join(parts)
        depth = len(parts) - 2  # parts[0] == "chronx"; top-level commands => 0
        indent = "  " * max(depth, 0)
        out.append(f"{indent}- [`{path}`](#{_anchor(path)})")
    out.append("")

    # ---- One section per command ------------------------------------------
    out.append("## Commands")
    out.append("")
    for parts, cmd in nodes:
        path = " ".join(parts)
        out.append(f"### `{path}`")
        out.append("")

        # Prefer the full help (its first line is the one-line summary) so the
        # reference is self-contained; only prepend the curated short help when
        # it genuinely differs from the help's opening line, avoiding a
        # duplicated first sentence.
        summary = _short_help(cmd)
        full = _full_help(cmd)
        if full:
            first_line = full.splitlines()[0].strip()
            if summary and summary != first_line:
                out.append(summary)
                out.append("")
            out.append(full)
            out.append("")
        elif summary:
            out.append(summary)
            out.append("")

        if isinstance(cmd, X.click.Group):
            subs = sorted(
                n for n, c in cmd.commands.items()
                if not getattr(c, "hidden", False)
            )
            if subs:
                joined = ", ".join(f"`{path} {s}`" for s in subs)
                out.append(f"Subcommands: {joined}")
                out.append("")

        out.extend(_render_params(cmd))

    return "\n".join(out).rstrip("\n")


def register(main: "X.click.Group") -> None:  # type: ignore[valid-type]
    @main.command("gen-docs")
    @X.click.option(
        "-o", "--output", "output",
        type=X.click.Path(dir_okay=False, path_type=X.Path),
        default=None,
        help="Write the Markdown to FILE instead of standard output.",
    )
    @X.click.option(
        "--title", "title",
        default="chronx — command reference",
        show_default=True,
        help="Top-level document title (the leading `# ` heading).",
    )
    def gen_docs(output, title: str) -> None:  # type: ignore[valid-type]
        """Generate a Markdown command reference for the whole CLI.

        Walks the live click command tree — every command, group, and
        subcommand, plugins included — and renders a deterministic Markdown
        document: a title, a table of contents, and one section per command
        with its help text and an arguments/options table.

        Pure introspection: no chronx store is read, so this works even before
        `chronx init`. With -o the document is written to a file (a confirmation
        goes to stderr); otherwise it is printed to stdout.
        """
        # ``main`` is captured from the closure and, by the time this runs, has
        # every command and plugin registered — so the reference is complete.
        doc = _build_doc(main, title)
        count = doc.count("\n### ") + (1 if doc.startswith("### ") else 0)

        if output is None:
            X.click.echo(doc)
            return

        out = output
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(doc + "\n", encoding="utf-8")
        X.click.secho(
            f"wrote command reference ({count} commands) to {out}",
            fg="green",
            err=True,
        )
