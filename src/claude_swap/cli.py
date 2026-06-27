"""Deprecated entry point — use ``cshift`` instead of ``cswap``.

``cswap`` is kept for backward compatibility. All functionality has been
consolidated into ``cshift`` (claude_swap.autoswitch).
"""

from __future__ import annotations

import sys


def main() -> None:
    print(
        "cswap is deprecated; use 'cshift' instead. "
        "All cswap flags are supported by cshift.",
        file=sys.stderr,
    )
    from claude_swap.autoswitch import main as cshift_main  # noqa: PLC0415
    cshift_main()


if __name__ == "__main__":
    main()
