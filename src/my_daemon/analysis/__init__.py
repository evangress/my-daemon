"""Pure-Python analysis layer over a snapshot bundle (M3).

The analyzer reads from a frozen snapshot — never the live state — and writes
its reports to ``data/consolidation/<snapshot_id>/``. The M4 observer LLM
reads those reports back to write the user-facing letter.
"""

from my_daemon.analysis.structural import (
    compute_report,
    persist_reports,
    simulate_evolution,
)

__all__ = ["compute_report", "simulate_evolution", "persist_reports"]
