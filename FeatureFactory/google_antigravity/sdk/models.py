"""Lightweight data models for the local Antigravity SDK shim.

NOTE: This is a *local interface shim*. There is no public, pip-installable
"Google Antigravity" trading-agent SDK with Decide/Transform lifecycle hooks at
the time of writing, so the project models the lifecycle locally. The shapes
here mirror the lifecycle contract described in the project brief, so swapping in
a real vendor SDK later is a matter of re-pointing the import.
"""

from typing import Any, Dict, Optional


class TurnContext:
    def __init__(self, turn_id: str = "turn_123", token_usage: int = 150, latency_ms: float = 120.0):
        self.turn_id = turn_id
        self.token_usage = token_usage
        self.latency_ms = latency_ms


class ToolCall:
    def __init__(self, name: str, arguments: Optional[Dict[str, Any]] = None):
        self.name = name
        self.arguments = arguments or {}


class Decision:
    """Result of a blocking ``decide`` gateway: ALLOW lets the action proceed,
    BLOCK halts it before any side effect occurs."""

    ALLOW = "ALLOW"
    BLOCK = "BLOCK"

    def __init__(self, action: str, reason: str = ""):
        self.action = action
        self.reason = reason

    @classmethod
    def allow(cls, reason: str = "") -> "Decision":
        return cls(cls.ALLOW, reason)

    @classmethod
    def block(cls, reason: str = "") -> "Decision":
        return cls(cls.BLOCK, reason)

    @property
    def allowed(self) -> bool:
        return self.action == self.ALLOW

    def __repr__(self) -> str:
        return f"Decision({self.action}, reason={self.reason!r})"


# Backwards-compatible aliases used by older call sites.
turn_context = TurnContext
tool_call = ToolCall
