"""chronx feature plugins.

Each module here defines ``register(main)`` and adds click command(s) to the
main CLI group. They are auto-discovered at import time by ``cli._load_plugins``
and loaded defensively — a broken plugin is skipped, never fatal.
"""
