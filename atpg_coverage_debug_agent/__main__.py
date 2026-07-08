"""Allow ``python -m atpg_coverage_debug_agent`` to launch the GUI.

Use ``python -m atpg_coverage_debug_agent.cli ...`` for the command line.
"""

from __future__ import annotations

import sys


def main() -> int:
    try:
        from .gui import launch
    except ImportError as exc:  # PySide6 not installed
        print(
            "GUI dependencies are missing (PySide6). Install them with "
            "'pip install -r requirements.txt', or use the CLI:\n"
            "  python -m atpg_coverage_debug_agent.cli --help\n"
            f"Import error: {exc}",
            file=sys.stderr,
        )
        return 1
    return launch()


if __name__ == "__main__":
    raise SystemExit(main())
