"""
Local Antigravity SDK shim
==========================

Models the autonomous-agent lifecycle described in the project brief — including
the **blocking Decide and Transform risk gateways** — without depending on an
external vendor package. The agent "reasoning" step is simulated locally; the
deterministic parts (tool execution, the Decide/Transform gateways, structured
output) are real so the risk controls are genuinely exercised and unit-testable.

Lifecycle per turn:
    on_turn_start
      -> pre_tool_call  (blocking)   : may veto a *read* tool
      -> [tool executes]
      -> post_tool_call
      -> decide         (blocking)   : ALLOW / BLOCK a proposed execution order
      -> transform      (mutating)   : clamp / rewrite the order before it fires
    on_turn_end
"""

import re
from typing import List, Callable, Any, Type, Optional
from pydantic import BaseModel

from google_antigravity.sdk.models import TurnContext, ToolCall, Decision


class Tool:
    def __init__(self, function: Callable, name: str, description: str):
        self.function = function
        self.name = name
        self.description = description

    @classmethod
    def from_function(cls, function: Callable, name: str, description: str):
        return cls(function, name, description)


class LifecycleHook:
    """Base hook. Override the gateways you care about; defaults are pass-through."""

    async def on_turn_start(self, turn_context: TurnContext) -> None:
        pass

    async def pre_tool_call(self, turn_context: TurnContext, tool_call: ToolCall) -> bool:
        """Blocking gateway for *read* tools. Return False to veto."""
        return True

    async def post_tool_call(self, turn_context: TurnContext, tool_call: ToolCall, result: Any) -> None:
        pass

    async def decide(self, turn_context: TurnContext, tool_call: ToolCall) -> Decision:
        """Blocking risk gateway. Return Decision.block(...) to deny an order."""
        return Decision.allow()

    async def transform(self, turn_context: TurnContext, tool_call: ToolCall) -> ToolCall:
        """Mutating risk gateway. Return a (possibly clamped) ToolCall."""
        return tool_call

    async def on_turn_end(self, turn_context: TurnContext) -> None:
        pass


class AgentResponse:
    def __init__(self, structured_output: Any, blocked: bool = False, executed_order: Optional[ToolCall] = None):
        self.structured_output = structured_output
        self.blocked = blocked
        self.executed_order = executed_order


class Agent:
    def __init__(
        self,
        instructions: str,
        tools: Optional[List[Tool]] = None,
        lifecycle_hooks: Optional[List[LifecycleHook]] = None,
        structured_output_schema: Optional[Type[BaseModel]] = None,
    ):
        self.instructions = instructions
        self.tools = tools or []
        self.lifecycle_hooks = lifecycle_hooks or []
        self.structured_output_schema = structured_output_schema

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_greeks(text: str) -> dict:
        """Extract the numeric fields produced by the greeks telemetry tool."""
        def grab(pattern, cast=float, default=None):
            m = re.search(pattern, text)
            return cast(m.group(1)) if m else default

        # Number patterns stop after the decimals so a trailing sentence period
        # (e.g. "525.00.") is not captured into the float.
        num = r"(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
        return {
            "underlying_price": grab(r"Underlying price:\s*" + num),
            "gamma_flip": grab(r"Gamma flip strike:\s*" + num),
            "regime": grab(r"Net GEX regime:\s*(\w+)", cast=str, default="POSITIVE"),
            "net_gex": grab(r"Net GEX value:\s*" + num),
        }

    async def run(self, prompt: str, symbol: str = "SPY") -> AgentResponse:
        print(f"[Agent] Running prompt: '{prompt}'")
        ctx = TurnContext()
        for hook in self.lifecycle_hooks:
            await hook.on_turn_start(ctx)

        # --- Step 1: read market structure via the greeks tool -----------
        greeks = {"regime": "POSITIVE", "underlying_price": 0.0, "gamma_flip": 0.0, "net_gex": 0.0}
        greeks_tool = next((t for t in self.tools if t.name == "fetch_options_greeks_telemetry"), None)
        if greeks_tool:
            tc = ToolCall(name=greeks_tool.name, arguments={"symbol": symbol})
            allowed = True
            for hook in self.lifecycle_hooks:
                allowed = await hook.pre_tool_call(ctx, tc)
                if not allowed:
                    print(f"[Agent] Read tool {tc.name} vetoed by {type(hook).__name__}.")
                    break
            if allowed:
                result_text = greeks_tool.function(symbol)
                print(f"[Agent] Tool result: {result_text}")
                for hook in self.lifecycle_hooks:
                    await hook.post_tool_call(ctx, tc, result_text)
                parsed = self._parse_greeks(result_text)
                greeks.update({k: v for k, v in parsed.items() if v is not None})

        regime = (greeks.get("regime") or "POSITIVE").upper()
        spot = greeks.get("underlying_price") or 0.0
        flip = greeks.get("gamma_flip") or spot

        # --- Step 2: propose an execution order and run risk gateways ----
        # The simulated reasoning sizes an order; the Decide/Transform gateways
        # then enforce the firm's risk policy on it before anything "executes".
        proposed_qty = 2500  # intentionally above the default cap to exercise Transform
        side = "buy"
        proposed = ToolCall(
            name="submit_execution_order",
            arguments={"symbol": symbol, "side": side, "quantity": proposed_qty, "price": flip},
        )

        decision = Decision.allow()
        for hook in self.lifecycle_hooks:
            decision = await hook.decide(ctx, proposed)
            if not decision.allowed:
                print(f"[Agent] DECIDE gateway BLOCKED order: {decision.reason}")
                break

        executed_order: Optional[ToolCall] = None
        blocked = not decision.allowed
        if not blocked:
            for hook in self.lifecycle_hooks:
                proposed = await hook.transform(ctx, proposed)
            executed_order = proposed
            print(
                f"[Agent] Order cleared gateways -> "
                f"{executed_order.arguments['side']} {executed_order.arguments['quantity']} {symbol}"
            )

        # --- Step 3: build structured assessment from real numbers -------
        if regime == "POSITIVE":
            stance = "CONSERVATIVE_SCALP"
            base_score = 58.0
        else:
            stance = "BREAKOUT_LONG"
            base_score = 72.0
        if blocked:
            stance = "DEFENSIVE_CASH"
            base_score = min(base_score, 40.0)

        structured_output = None
        if self.structured_output_schema is not None:
            try:
                structured_output = self.structured_output_schema(
                    ticker=symbol,
                    volatility_regime=regime,
                    composite_momentum_score=round(base_score, 1),
                    recommended_execution_stance=stance,
                    target_price_level=float(flip),
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[Agent] Could not build structured output: {exc}")

        for hook in self.lifecycle_hooks:
            await hook.on_turn_end(ctx)

        return AgentResponse(structured_output=structured_output, blocked=blocked, executed_order=executed_order)
