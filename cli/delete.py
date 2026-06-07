"""Command to completely remove codexproxy from the system."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


def main() -> None:
    """Remove all codexproxy user data and suggest package removal."""
    home = Path.home()
    codex_dir = home / ".codex"
    codexproxy_dir = home / ".codexproxy"

    removed = []
    for directory in (codex_dir, codexproxy_dir):
        if directory.exists():
            try:
                shutil.rmtree(directory)
                removed.append(str(directory))
            except Exception as exc:
                print(f"Warning: failed to remove {directory}: {exc}", file=sys.stderr)

    if removed:
        print("Removed codexproxy directories:")
        for path in removed:
            print(f"  - {path}")
    else:
        print("No codexproxy directories found to remove.")

    print("\nTo remove the codexproxy tool itself, run:")
    print("  uv tool uninstall codexproxy")
    print(
        "\nNote: This does not affect any global Codex CLI or Desktop App installations."
    )


if __name__ == "__main__":
    main()
