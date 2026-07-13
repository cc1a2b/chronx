"""Allow `python -m chronx` (used by the daemon spawner)."""

from __future__ import annotations

from chronx.cli import main

if __name__ == "__main__":
    main()
