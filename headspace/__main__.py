"""Entry point for ``python -m headspace``."""

from __future__ import annotations

import sys

from headspace.cli import main

if __name__ == "__main__":
    sys.exit(main())
