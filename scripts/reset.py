# SPDX-License-Identifier: Apache-2.0
"""Dev convenience: ``python scripts/reset.py`` == ``daemon reset``.

This script used to hold its own copy of the wipe — a recursive delete of a
hardcoded state directory, with no config, no prompt and no guards — which made
it strictly more dangerous than the command it shadowed, and guaranteed the two
would drift. It is now a delegation and nothing else: same config-resolved
targets, same size listing, same confirmation prompt, same `--models` / `--all`
tiers.
"""

from __future__ import annotations

from my_daemon.cli import app


def main() -> None:
    app(["reset"])


if __name__ == "__main__":
    main()
