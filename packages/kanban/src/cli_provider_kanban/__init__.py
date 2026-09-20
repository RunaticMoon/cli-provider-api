"""cli_provider_kanban — Hermes kanban card contract + Jev rules classifier.

Read-only shadow integration: this package never mutates the card graph,
never schedules work, and never imports the Hermes tree.
"""

from .board import BoardTask, card_fingerprint, list_scope_tasks, open_readonly_board
from .cache import CacheEntry, DecisionCache
from .classifier import classify
from .errors import CacheError, ShadowError
from .models import (
    EffortHint,
    JevDecision,
    RecommendedAction,
    RiskFlag,
    Role,
    TaskSpec,
    Tier,
)
from .policy import Policy, available_candidates, load_policy, route_key
from .shadow import run_shadow
from .spec import SpecResult, resolve_spec

__all__ = [
    "BoardTask",
    "CacheEntry",
    "CacheError",
    "DecisionCache",
    "EffortHint",
    "JevDecision",
    "Policy",
    "RecommendedAction",
    "RiskFlag",
    "Role",
    "ShadowError",
    "SpecResult",
    "TaskSpec",
    "Tier",
    "available_candidates",
    "card_fingerprint",
    "classify",
    "list_scope_tasks",
    "load_policy",
    "open_readonly_board",
    "resolve_spec",
    "route_key",
    "run_shadow",
]
