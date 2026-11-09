"""Background pre-computation so heavy pages read from cache instead of computing.

Runs while `ties serve` is up: whenever the vault has changed, it re-runs the inference
rules and recomputes duplicate candidates, introduction matches and network analytics.
Each result is cached until the next change (Vault.cached), so a page that needs it
returns immediately once the warm-up has finished.
"""

from __future__ import annotations

from datetime import date

from .db import Vault


def warm(vault: Vault, today: date | None = None) -> bool:
    from . import graph, inference, resolution, strategy

    state = vault.__dict__.setdefault("_warm_state", {})
    if state.get("version") == vault.version:
        return False
    today = today or date.today()
    inference.run_if_changed(vault)          # may write suggestions, so it goes first
    resolution.candidates(vault)
    strategy.intro_suggestions(vault)
    graph.analytics(vault, today)
    graph._load(vault, True, True, today)    # what the graph page asks for first
    state["version"] = vault.version
    return True
