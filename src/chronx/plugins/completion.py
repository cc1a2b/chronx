"""chronx completion — emit a shell tab-completion script for chronx.

This 60+-command CLI benefits from tab completion. Rather than hand-writing
per-shell scripts, we reuse click's *native* completion machinery: click can
introspect the whole command group at run time and generate a completion
script for bash, zsh, or fish that shells out back to ``chronx`` (via the
``_CHRONX_COMPLETE`` env var) to compute candidates. That means completion
always stays in sync with the live CLI — including every plugin command.

Pure CLI introspection: no chronx store is touched.
"""

from __future__ import annotations

import sys

from chronx import pluginlib as X
from click.shell_completion import get_completion_class

# Shell -> one-line install hint, printed to STDERR so that piping the script
# (``eval "$(chronx completion bash)"``) keeps STDOUT free of noise.
_INSTALL_HINT: dict[str, str] = {
    "bash": '# add to ~/.bashrc: eval "$(chronx completion bash)"',
    "zsh": '# add to ~/.zshrc: eval "$(chronx completion zsh)"',
    "fish": "# chronx completion fish > ~/.config/fish/completions/chronx.fish",
}


def register(main: "X.click.Group") -> None:  # type: ignore[valid-type]
    @main.command()
    @X.click.argument("shell", type=X.click.Choice(["bash", "zsh", "fish"]))
    def completion(shell: str) -> None:
        """Emit a tab-completion script for SHELL (bash, zsh, or fish).

        Write it to STDOUT and source it, e.g. for bash:

            eval "$(chronx completion bash)"

        A one-line install hint is printed to STDERR, so the STDOUT stream
        stays a pure, evaluatable completion script.
        """
        # Look up click's shell-specific completion generator. Guard against an
        # unknown shell even though Choice already constrains the argument, so a
        # future/edge shell yields a clean ClickException instead of a crash.
        comp_cls = get_completion_class(shell)
        if comp_cls is None:
            raise X.click.ClickException(
                f"unsupported shell {shell!r} (try bash, zsh, fish)"
            )

        # ``main`` (captured from the closure) is the full top-level group, so
        # the generated script completes every chronx command, plugins included.
        comp = comp_cls(
            cli=main,
            ctx_args={},
            prog_name="chronx",
            complete_var="_CHRONX_COMPLETE",
        )

        # Install hint -> STDERR; the completion script itself -> STDOUT.
        X.click.echo(_INSTALL_HINT[shell], file=sys.stderr)
        X.click.echo(comp.source())
