"""Offline end-to-end check of the agent loop + Decide/Transform gateways.

Run: python -m tests.verify_agent_flow  (from the FeatureFactory directory)
Network-free: the yfinance-backed data fetch is stubbed.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import core.governance_agent as g
from core.config import settings
from google_antigravity.sdk.models import ToolCall, TurnContext

# Deterministic, offline market data (negative-gamma scenario).
g.fetch_local_gex_data = lambda s: {"underlying_price": 525.0, "gamma_flip": 523.4, "net_gex": -8.2e8}


async def main() -> int:
    gk = g.TradingRiskGatekeeper()
    cap = settings.RISK_MAX_ORDER_SIZE
    ceiling = cap * gk.HARD_CEILING_MULTIPLE
    print("cap={}  hard_ceiling={}".format(cap, ceiling))

    failures = []

    # Decide: authorized + in-range size is allowed.
    d = await gk.decide(TurnContext(), ToolCall("submit_execution_order", {"symbol": "SPY", "quantity": 500}))
    print("SPY/500   decide.allowed={}".format(d.allowed))
    if not d.allowed:
        failures.append("SPY/500 should be allowed")

    # Decide: unauthorized symbol is blocked.
    d = await gk.decide(TurnContext(), ToolCall("submit_execution_order", {"symbol": "GME", "quantity": 100}))
    print("GME/100   decide.allowed={}  reason={!r}".format(d.allowed, d.reason))
    if d.allowed:
        failures.append("GME should be blocked (unauthorized)")

    # Decide: grossly oversized is blocked.
    d = await gk.decide(TurnContext(), ToolCall("submit_execution_order", {"symbol": "SPY", "quantity": ceiling + 1}))
    print("SPY oversized decide.allowed={}  reason={!r}".format(d.allowed, d.reason))
    if d.allowed:
        failures.append("oversized should be blocked")

    # Transform: clamps to the cap.
    t = await gk.transform(TurnContext(), ToolCall("submit_execution_order", {"symbol": "SPY", "quantity": 2500}))
    print("SPY/2500  transform -> {}".format(t.arguments["quantity"]))
    if t.arguments["quantity"] != cap:
        failures.append("transform should clamp 2500 -> {}".format(cap))

    # Full agent turn: read tool -> Decide -> Transform -> structured output.
    resp = await g.create_market_intelligence_agent().run("assess SPY", symbol="SPY")
    a = resp.structured_output
    executed = resp.executed_order.arguments["quantity"] if resp.executed_order else None
    print("FULL TURN: {} regime={} stance={} blocked={} executed={}".format(
        a.ticker, a.volatility_regime, a.recommended_execution_stance, resp.blocked, executed))
    if a.volatility_regime != "NEGATIVE":
        failures.append("regime should be NEGATIVE for net_gex<0")
    if resp.blocked:
        failures.append("SPY/2500 full turn should clear Decide and be clamped, not blocked")
    if executed != cap:
        failures.append("executed order should be clamped to cap")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
