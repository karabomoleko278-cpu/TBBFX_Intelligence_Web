"""Offline check of the OpenBB governance agent + immutable risk gateways.

Run: python -m tests.verify_agent_flow  (from the FeatureFactory directory)
Network-free: the GEX/SVI telemetry fetch is stubbed.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import core.governance_agent as g
from core.config import settings


def _stub_analyze(symbol: str, persist: bool = False):
    return {
        "ticker": symbol,
        "underlying_price": 525.0,
        "gamma_flip": 523.4,
        "net_gex": -8.2e8,
        "regime": "NEGATIVE",
        "dex": 0.35,
        "vex": 0.20,
        "chex": -0.05,
    }


g._telemetry_engine.analyze = _stub_analyze


class _StubPortfolioRiskEngine:
    def calculate_symbol_var(self, symbol: str):
        return {
            "symbol": symbol,
            "status": "insufficient_data",
            "var_99_zar": 0.0,
            "var_99_fraction": 0.0,
            "exceeds_max_variance": False,
            "warnings": [],
        }


g._portfolio_risk_engine = _StubPortfolioRiskEngine()


async def _no_mcp_payload(symbol: str):
    return None


g._fetch_mcp_market_payload = _no_mcp_payload


async def main() -> int:
    failures = []

    allowed = g.risk_decide(g.RiskGatewayRequest(symbol="XAUUSD", proposed_risk_pct=0.15, target_r=4.0))
    print(f"XAUUSD/0.15 decide.allowed={allowed.allowed}")
    if not allowed.allowed:
        failures.append("XAUUSD at immutable 15% risk should be allowed")

    percent_notation = g.risk_decide(g.RiskGatewayRequest(symbol="EURUSD", proposed_risk_pct=15))
    print(f"EURUSD/15 decide.allowed={percent_notation.allowed}")
    if not percent_notation.allowed:
        failures.append("EURUSD should accept 15 as percent notation for immutable 15%")

    blocked_risk = g.risk_decide(g.RiskGatewayRequest(symbol="XAUUSD", proposed_risk_pct=0.20))
    print(f"XAUUSD/0.20 decide.allowed={blocked_risk.allowed} reason={blocked_risk.reason!r}")
    if blocked_risk.allowed:
        failures.append("XAUUSD attempted 20% override should be blocked")

    blocked_symbol = g.risk_decide(g.RiskGatewayRequest(symbol="GME", proposed_risk_pct=0.15))
    print(f"GME decide.allowed={blocked_symbol.allowed} reason={blocked_symbol.reason!r}")
    if blocked_symbol.allowed:
        failures.append("GME should be blocked because it is not in the sanctioned watchlist")

    transformed = g.risk_transform(g.RiskGatewayRequest(symbol="GBPUSD", proposed_risk_pct=12, quantity=2500))
    print(f"GBPUSD transform.allowed={transformed.allowed} normalized_quantity={transformed.normalized_quantity}")
    if transformed.normalized_quantity != int(settings.RISK_MAX_ORDER_SIZE):
        failures.append(f"transform should clamp 2500 -> {settings.RISK_MAX_ORDER_SIZE}")

    tools = g.governance_tool_definitions()
    print(f"OpenBB tool definitions={len(tools)}")
    if len(tools) != 8:
        failures.append("expected Decide/Transform plus six read-only MCP tool definitions")

    assessment = await g.execute_agent_assessment("XAUUSD")
    print(
        "assessment ticker={} regime={} stance={} momentum={}".format(
            assessment.ticker,
            assessment.volatility_regime,
            assessment.recommended_execution_stance,
            assessment.composite_momentum_score,
        )
    )
    if assessment.volatility_regime != "NEGATIVE":
        failures.append("stubbed net_gex<0 should produce NEGATIVE regime")

    if failures:
        print("\nFAILURES:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
