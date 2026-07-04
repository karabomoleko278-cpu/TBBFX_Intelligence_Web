"""
Agentic governance layer.

Deploys an autonomous market-microstructure agent (via the local Antigravity SDK
shim) that reads options Greeks and recommends an execution stance. All proposed
orders pass through **blocking Decide and Transform risk gateways** that enforce
strict risk limits before anything could ever reach a broker:

* **Decide**  : hard ALLOW/BLOCK — denies unauthorized symbols and grossly
                oversized orders (prevents unauthorized trades).
* **Transform**: clamps order size to the configured cap and normalises it
                (prevents fat-finger / execution errors).
"""

from google_antigravity.sdk import Agent, Tool, LifecycleHook
from google_antigravity.sdk.models import TurnContext, ToolCall, Decision
from pydantic import BaseModel, Field

from core.options_exposure_engine import OptionsExposureEngine
from core.config import settings


# Structured output schema for the agent's risk assessment.
class MicrostructureAssessment(BaseModel):
    ticker: str = Field(description="The equity symbol being analyzed")
    volatility_regime: str = Field(description="GEX regime: POSITIVE (dampening) or NEGATIVE (amplifying)")
    composite_momentum_score: float = Field(description="Composite momentum score from 0 to 100")
    recommended_execution_stance: str = Field(
        description="CONSERVATIVE_SCALP, BREAKOUT_LONG, DEFENSIVE_CASH, or LIQUIDITY_PROVISION"
    )
    target_price_level: float = Field(description="Near-term institutional support / magnet level")


# Shared engine instance for the agent's telemetry tool (proxy + OI/volume aware).
_telemetry_engine = OptionsExposureEngine()


# Custom Tool: local Greeks ingestion (free, no paid surface).
def fetch_options_greeks_telemetry(symbol: str) -> str:
    """Query local net GEX, the Gamma Flip point and the volatility regime.

    Spot FX/metals/indices are routed to their ETF options proxy automatically.
    """
    try:
        data = _telemetry_engine.analyze(symbol, persist=False)
        regime = data["regime"]
        flip = data["gamma_flip"] if data["gamma_flip"] is not None else data["underlying_price"]
        proxy_note = ""
        if data.get("options_proxy") and data["options_proxy"] != symbol.upper():
            proxy_note = f" (GEX via {data['options_proxy']} proxy, weight={data['weight_source']})"
        return (
            f"Symbol: {symbol}{proxy_note}. "
            f"Underlying price: {data['underlying_price']:.2f}. "
            f"Gamma flip strike: {flip:.2f}. "
            f"Net GEX regime: {regime}. "
            f"Net GEX value: {data['net_gex']:.2f}."
        )
    except Exception as e:  # noqa: BLE001
        return f"Tool Execution Failure: {str(e)}"


class TradingRiskGatekeeper(LifecycleHook):
    """Blocking risk gateways enforcing the firm's execution policy."""

    # Absolute hard ceiling: anything this far above the cap is treated as a
    # configuration/agent error and is denied outright rather than clamped.
    HARD_CEILING_MULTIPLE = 10

    async def pre_tool_call(self, turn_context: TurnContext, tool_call: ToolCall) -> bool:
        # Read-only telemetry tools are always permitted.
        return True

    async def decide(self, turn_context: TurnContext, tool_call: ToolCall) -> Decision:
        if tool_call.name != "submit_execution_order":
            return Decision.allow()

        symbol = str(tool_call.arguments.get("symbol", "")).upper()
        qty = float(tool_call.arguments.get("quantity", 0))

        # 1. Block unauthorized symbols (not on the sanctioned watchlist).
        #    Accept broker-suffixed forms too (e.g. 'XAUUSDm' == 'XAUUSD').
        suffix = settings.MT5_SYMBOL_SUFFIX.upper()
        authorized = set()
        for s in settings.WATCHLIST:
            u = s.upper()
            authorized.add(u)
            authorized.add(u + suffix)  # allow the broker-suffixed variant
        if symbol not in authorized:
            return Decision.block(f"Unauthorized symbol '{symbol}' is not on the sanctioned watchlist.")

        # 2. Block grossly oversized orders (likely an error, not a trade).
        hard_ceiling = settings.RISK_MAX_ORDER_SIZE * self.HARD_CEILING_MULTIPLE
        if qty > hard_ceiling:
            return Decision.block(
                f"Order size {qty:.0f} exceeds hard ceiling {hard_ceiling} ({self.HARD_CEILING_MULTIPLE}x cap)."
            )

        # 3. Block non-positive sizes.
        if qty <= 0:
            return Decision.block(f"Non-positive order size {qty:.0f} rejected.")

        return Decision.allow("Within policy.")

    async def transform(self, turn_context: TurnContext, tool_call: ToolCall) -> ToolCall:
        if tool_call.name != "submit_execution_order":
            return tool_call

        qty = float(tool_call.arguments.get("quantity", 0))
        capped = min(qty, float(settings.RISK_MAX_ORDER_SIZE))
        if capped != qty:
            print(
                f"[RiskGatekeeper] TRANSFORM clamped order size {qty:.0f} -> {capped:.0f} "
                f"(cap {settings.RISK_MAX_ORDER_SIZE})."
            )
        # Normalise to a whole number of contracts/shares.
        tool_call.arguments["quantity"] = int(capped)
        return tool_call


def create_market_intelligence_agent() -> Agent:
    """Configures and returns the market intelligence agent with risk gateways."""
    greeks_tool = Tool.from_function(
        fetch_options_greeks_telemetry,
        name="fetch_options_greeks_telemetry",
        description="Query options market-maker GEX, the flip level and the baseline volatility regime.",
    )
    return Agent(
        instructions=(
            "You are an institutional market-microstructure research agent. "
            "Analyze conditions using options Greeks and order flow. "
            "In positive gamma regimes prioritize scalp/range strategies; "
            "in negative gamma regimes focus on breakout/momentum strategies."
        ),
        tools=[greeks_tool],
        lifecycle_hooks=[TradingRiskGatekeeper()],
        structured_output_schema=MicrostructureAssessment,
    )


async def execute_agent_assessment(ticker: str) -> MicrostructureAssessment:
    agent = create_market_intelligence_agent()
    prompt = f"Perform a microstructure risk assessment and recommend an execution stance for {ticker}."
    response = await agent.run(prompt, symbol=ticker.upper())
    return response.structured_output


if __name__ == "__main__":
    import asyncio

    async def test_run():
        print("Running governance agent test...")
        assessment = await execute_agent_assessment("SPY")
        print(f"\nAssessment for {assessment.ticker}:")
        print(f"  Volatility Regime: {assessment.volatility_regime}")
        print(f"  Momentum Score:    {assessment.composite_momentum_score:.1f}")
        print(f"  Execution Stance:  {assessment.recommended_execution_stance}")
        print(f"  Target Price:      ${assessment.target_price_level:.2f}")

    asyncio.run(test_run())
