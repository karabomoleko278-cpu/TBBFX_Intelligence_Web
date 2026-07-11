"""
OpenBB-compatible governance agent.

This module replaces the local Antigravity simulation shim with OpenBB AI SDK
request/SSE schemas while keeping TBBFX execution rules immutable. The agent can
inspect live exposure, momentum, and risk posture, but it cannot write or mutate
the approved per-symbol risk tiers.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncGenerator, Dict, List, Literal, Optional

import requests
from fastapi import APIRouter
from openbb_ai import QueryRequest, message_chunk, reasoning_step
from openbb_ai.models import AgentTool
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from core.config import settings
from core.options_exposure_engine import OptionsExposureEngine
from core.portfolio_risk_engine import PortfolioRiskEngine
from core.state_db import get_state_db
from core.tbbfx_mcp_server import (
    MCP_SERVER_DESCRIPTOR,
    execute_tool as execute_mcp_tool,
    get_tool_definitions as get_mcp_tool_definitions,
)
from core.tbbfx_object import unwrap_tbbfx_results
from google_antigravity.sdk import LifecycleHook
from google_antigravity.sdk.models import Decision, ToolCall, TurnContext


TARGET_R = 4.0
MIN_TRADES_PER_DAY = 1
STRUCTURAL_STOP_POLICY = "H1 unmitigated FVG outer edge or 78.6% OTE boundary plus 2.0 pips breathing room"

# These are strategy constants, not prompt variables. The LLM can inspect them
# for governance context but has no tool or endpoint that can modify them.
IMMUTABLE_SYMBOL_RISK_TIERS: Dict[str, float] = {
    "EURUSD": 0.15,
    "GBPUSD": 0.12,
    "XAUUSD": 0.15,
    "US30": 0.12,
    "USTEC": 0.12,
    "USDJPY": 0.15,
}

SYSTEM_CONTEXT: List[str] = [
    "You are the TBBFX OpenBB governance agent for institutional trading-desk oversight.",
    f"Target R is immutable at {TARGET_R:.2f}. Do not propose changing it.",
    f"Structural stops are immutable: {STRUCTURAL_STOP_POLICY}.",
    f"Execution frequency boundary: at least {MIN_TRADES_PER_DAY} qualified trade per day when market quality permits.",
    "Symbol risk tiers are immutable backend constants. Never modify, overwrite, or recommend edits to those tiers.",
    "Governance outputs may inspect risk and market state, but may not execute trades or expose broker credentials.",
]

MINIMAL_SYSTEM_INSTRUCTION = (
    "You are the TBBFX OpenBB governance agent. Keep the prompt lean: use the "
    "read-only local MCP provider tbbfx-mcp-server to fetch historical GEX, live "
    "order-flow, and macro context only when a candidate SMC/FVG/OTE zone needs "
    "validation. The MCP tools are inspection-only; risk tiers, Target R, stops, "
    "and trade execution state are immutable backend constraints."
)


class MicrostructureAssessment(BaseModel):
    ticker: str = Field(description="The instrument being analyzed")
    volatility_regime: str = Field(description="GEX regime: POSITIVE or NEGATIVE")
    composite_momentum_score: float = Field(description="Composite momentum score from 0 to 100")
    recommended_execution_stance: str = Field(
        description="CONSERVATIVE_SCALP, BREAKOUT_LONG, DEFENSIVE_CASH, or LIQUIDITY_PROVISION"
    )
    target_price_level: float = Field(description="Near-term institutional support, magnet, or gamma flip level")


class RiskGatewayRequest(BaseModel):
    symbol: str = Field(description="Instrument being evaluated")
    proposed_risk_pct: Optional[float] = Field(default=None, description="Optional proposed risk percentage")
    quantity: Optional[float] = Field(default=None, description="Optional order quantity to inspect")
    target_r: Optional[float] = Field(default=None, description="Optional target R to inspect")
    stop_policy: Optional[str] = Field(default=None, description="Optional stop policy text to inspect")


class RiskGatewayResult(BaseModel):
    gateway: Literal["Decide", "Transform"]
    allowed: bool
    symbol: str
    immutable_risk_pct: float
    target_r: float
    stop_policy: str
    action: str
    reason: str
    normalized_quantity: Optional[int] = None
    portfolio_var_99_zar: Optional[float] = None
    portfolio_var_99_fraction: Optional[float] = None
    portfolio_var_status: Optional[str] = None


_telemetry_engine = OptionsExposureEngine()
_portfolio_risk_engine = PortfolioRiskEngine()
router = APIRouter(tags=["openbb-governance"])


def _clean_symbol(symbol: str) -> str:
    sym = symbol.upper().strip()
    return sym[:-1] if sym.endswith("M") and sym[:-1] in IMMUTABLE_SYMBOL_RISK_TIERS else sym


def _extract_user_query(payload: QueryRequest) -> str:
    for msg in reversed(payload.messages or []):
        if str(msg.role).lower().endswith("human"):
            return str(msg.content)
    return "Assess current market governance posture."


def _infer_symbol(text: str) -> str:
    upper = text.upper()
    for symbol in settings.WATCHLIST:
        if symbol.upper() in upper:
            return symbol.upper()
    return settings.WATCHLIST[0].upper()


def _post_signalr_feature(symbol: str, text: str, event_type: str = "governance_ai_token") -> None:
    record = {
        "symbol": _clean_symbol(symbol),
        "type": event_type,
        "ai_governance": text,
        "target_r": TARGET_R,
        "stop_policy": STRUCTURAL_STOP_POLICY,
        "risk_tiers": IMMUTABLE_SYMBOL_RISK_TIERS,
        "timestamp": time.time(),
    }
    try:
        headers = {}
        feature_key = getattr(settings, "TBBFX_FEATURE_UPDATE_KEY", "")
        if feature_key:
            headers["X-TBBFX-FEATURE-KEY"] = feature_key
        requests.post(settings.SIGNALR_URL, json=record, headers=headers, timeout=0.8)
    except Exception:
        # AI text streaming should never kill the governance response.
        pass


async def _ship_token(symbol: str, text: str, event_type: str = "governance_ai_token") -> None:
    await asyncio.to_thread(_post_signalr_feature, symbol, text, event_type)


def _sse_payload(obj: Any) -> Dict[str, Any]:
    dumped = obj.model_dump()
    return {"event": dumped["event"], "data": dumped["data"]}


def _normalize_risk_input(value: Optional[float]) -> Optional[float]:
    """Accept both decimal risk (0.15) and percent notation (15)."""
    if value is None:
        return None
    risk = float(value)
    return risk / 100.0 if risk > 1.0 else risk


def _record_governance_audit(
    symbol: str,
    action_taken: str,
    active_trade_parameters: Dict[str, Any],
    execution_telemetry: Dict[str, Any],
) -> None:
    try:
        get_state_db().record_governance_audit(
            symbol=symbol,
            action_taken=action_taken,
            data_lineage_source="governance_agent.risk_decide",
            active_trade_parameters=active_trade_parameters,
            execution_telemetry=execution_telemetry,
        )
    except Exception:
        # Audit failure must never mutate or crash the immutable risk gateway.
        pass


def _build_risk_result(
    *,
    allowed: bool,
    symbol: str,
    immutable_risk: float,
    action: str,
    reason: str,
    var_matrix: Optional[Dict[str, Any]] = None,
) -> RiskGatewayResult:
    return RiskGatewayResult(
        gateway="Decide",
        allowed=allowed,
        symbol=symbol,
        immutable_risk_pct=immutable_risk,
        target_r=TARGET_R,
        stop_policy=STRUCTURAL_STOP_POLICY,
        action=action,
        reason=reason,
        portfolio_var_99_zar=(var_matrix or {}).get("var_99_zar"),
        portfolio_var_99_fraction=(var_matrix or {}).get("var_99_fraction"),
        portfolio_var_status=(var_matrix or {}).get("status"),
    )


def risk_decide(payload: RiskGatewayRequest) -> RiskGatewayResult:
    symbol = _clean_symbol(payload.symbol)
    immutable_risk = IMMUTABLE_SYMBOL_RISK_TIERS.get(symbol)
    proposed_risk = _normalize_risk_input(payload.proposed_risk_pct)
    audit_params = {
        "symbol": symbol,
        "proposed_risk_pct": proposed_risk,
        "quantity": payload.quantity,
        "target_r": payload.target_r,
        "stop_policy": payload.stop_policy,
        "immutable_risk_pct": immutable_risk,
        "structural_target_r": TARGET_R,
        "structural_stop_policy": STRUCTURAL_STOP_POLICY,
    }

    if immutable_risk is None:
        result = _build_risk_result(
            allowed=False,
            symbol=symbol,
            immutable_risk=0.0,
            action="BLOCK",
            reason="Symbol is not on the sanctioned immutable TBBFX watchlist.",
        )
        _record_governance_audit(symbol, "Risk_Veto", audit_params, result.model_dump())
        return result

    if proposed_risk is not None and abs(proposed_risk - immutable_risk) > 1e-9:
        result = _build_risk_result(
            allowed=False,
            symbol=symbol,
            immutable_risk=immutable_risk,
            action="BLOCK",
            reason="Proposed risk percentage attempts to override the immutable backend tier.",
        )
        _record_governance_audit(symbol, "Risk_Veto", audit_params, result.model_dump())
        return result

    if payload.target_r is not None and abs(float(payload.target_r) - TARGET_R) > 1e-9:
        result = _build_risk_result(
            allowed=False,
            symbol=symbol,
            immutable_risk=immutable_risk,
            action="BLOCK",
            reason="Target R is structural and cannot be changed by agent tools.",
        )
        _record_governance_audit(symbol, "Risk_Veto", audit_params, result.model_dump())
        return result

    try:
        var_matrix = _portfolio_risk_engine.calculate_symbol_var(symbol)
    except Exception as exc:
        var_matrix = {
            "status": "unavailable",
            "var_99_zar": None,
            "var_99_fraction": None,
            "exceeds_max_variance": False,
            "warnings": [str(exc)],
        }

    if bool(var_matrix.get("exceeds_max_variance")):
        result = _build_risk_result(
            allowed=False,
            symbol=symbol,
            immutable_risk=immutable_risk,
            action="BLOCK",
            reason="99% parametric VaR exceeds the configured portfolio variance allowance.",
            var_matrix=var_matrix,
        )
        _record_governance_audit(symbol, "Risk_Veto", audit_params, var_matrix)
        return result

    result = _build_risk_result(
        allowed=True,
        symbol=symbol,
        action="ALLOW",
        reason="Risk request is inspection-only and matches immutable strategy boundaries.",
        immutable_risk=immutable_risk,
        var_matrix=var_matrix,
    )
    _record_governance_audit(symbol, "Strategy_Pass", audit_params, var_matrix)
    return result


def risk_transform(payload: RiskGatewayRequest) -> RiskGatewayResult:
    symbol = _clean_symbol(payload.symbol)
    base = risk_decide(payload)
    if not base.allowed:
        base.gateway = "Transform"
        return base

    quantity = payload.quantity
    normalized = None
    if quantity is not None:
        normalized = max(0, min(int(float(quantity)), int(settings.RISK_MAX_ORDER_SIZE)))

    return RiskGatewayResult(
        gateway="Transform",
        allowed=True,
        symbol=symbol,
        immutable_risk_pct=IMMUTABLE_SYMBOL_RISK_TIERS[symbol],
        target_r=TARGET_R,
        stop_policy=STRUCTURAL_STOP_POLICY,
        action="NORMALIZE_ONLY",
        reason="Quantity was normalized to the execution cap. Risk tier and strategy boundaries were not modified.",
        normalized_quantity=normalized,
    )


class TradingRiskGatekeeper(LifecycleHook):
    """Backwards-compatible Decide/Transform hook for the local SDK shim.

    The OpenBB governance router is now the primary interface, but older tests
    and local agents still exercise this lifecycle contract. Keep it as a thin
    adapter over the same immutable risk gateway so legacy callers cannot bypass
    the VaR, symbol, risk-tier, or quantity controls.
    """

    async def decide(self, turn_context: TurnContext, tool_call: ToolCall) -> Decision:
        symbol = _clean_symbol(str(tool_call.arguments.get("symbol", "")))
        quantity = tool_call.arguments.get("quantity")

        if symbol not in IMMUTABLE_SYMBOL_RISK_TIERS:
            return Decision.block("Symbol is not on the sanctioned immutable TBBFX watchlist.")

        try:
            if quantity is not None and float(quantity) > float(settings.RISK_MAX_ORDER_SIZE):
                return Decision.block("Order quantity exceeds the immutable execution cap.")
        except (TypeError, ValueError):
            return Decision.block("Order quantity is not numeric.")

        gate = risk_decide(
            RiskGatewayRequest(
                symbol=symbol,
                proposed_risk_pct=IMMUTABLE_SYMBOL_RISK_TIERS[symbol],
                quantity=float(quantity) if quantity is not None else None,
            )
        )
        return Decision.allow(gate.reason) if gate.allowed else Decision.block(gate.reason)

    async def transform(self, turn_context: TurnContext, tool_call: ToolCall) -> ToolCall:
        symbol = _clean_symbol(str(tool_call.arguments.get("symbol", "")))
        quantity = tool_call.arguments.get("quantity")

        if symbol:
            tool_call.arguments["symbol"] = symbol

        if quantity is not None:
            try:
                tool_call.arguments["quantity"] = max(
                    0,
                    min(int(float(quantity)), int(settings.RISK_MAX_ORDER_SIZE)),
                )
            except (TypeError, ValueError):
                tool_call.arguments["quantity"] = 0

        return tool_call


def governance_tool_definitions(base_url: str = "") -> List[AgentTool]:
    tools = [
        AgentTool(
            name="tbbfx_decide_risk_gateway",
            url=base_url,
            endpoint="/api/governance/tools/decide",
            description=(
                "Read-only Decide gateway. Allows or blocks proposed governance actions against immutable "
                "symbol risk tiers, Target R, and structural stop policy. Cannot mutate configuration."
            ),
            input_schema=RiskGatewayRequest.model_json_schema(),
        ),
        AgentTool(
            name="tbbfx_transform_risk_gateway",
            url=base_url,
            endpoint="/api/governance/tools/transform",
            description=(
                "Read-only Transform gateway. Normalizes proposed quantity against hard execution caps while "
                "preserving immutable risk tiers. Cannot write risk configuration."
            ),
            input_schema=RiskGatewayRequest.model_json_schema(),
        ),
    ]
    for tool in get_mcp_tool_definitions():
        tools.append(
            AgentTool(
                server_id=MCP_SERVER_DESCRIPTOR["id"],
                name=tool["name"],
                url=f"mcp+stdio://{MCP_SERVER_DESCRIPTOR['id']}",
                endpoint=tool["name"],
                description=f"{tool['description']} Read-only; cannot mutate TBBFX execution settings.",
                input_schema=tool["inputSchema"],
            )
        )
    return tools


def _assessment_from_exposure(symbol: str, exposure: Dict[str, Any]) -> MicrostructureAssessment:
    net_gex = float(exposure.get("net_gex") or 0.0)
    regime = str(exposure.get("regime") or ("POSITIVE" if net_gex >= 0 else "NEGATIVE"))
    dex = float(exposure.get("dex") or 0.0)
    vex = float(exposure.get("vex") or 0.0)
    chex = float(exposure.get("chex") or 0.0)
    if exposure.get("composite_momentum_score") is not None:
        momentum = float(exposure.get("composite_momentum_score") or 0.0)
    else:
        momentum = 50.0 + dex * 25.0 + vex * 15.0 + chex * 10.0
    momentum = max(0.0, min(100.0, momentum))

    if regime == "POSITIVE" and momentum >= 55:
        stance = "LIQUIDITY_PROVISION"
    elif regime == "NEGATIVE" and momentum >= 55:
        stance = "BREAKOUT_LONG"
    elif momentum < 42:
        stance = "DEFENSIVE_CASH"
    else:
        stance = "CONSERVATIVE_SCALP"

    return MicrostructureAssessment(
        ticker=symbol,
        volatility_regime=regime,
        composite_momentum_score=round(momentum, 1),
        recommended_execution_stance=stance,
        target_price_level=float(exposure.get("gamma_flip") or exposure.get("underlying_price") or 0.0),
    )


def _exposure_from_mcp_payload(symbol: str, gex_payload: Dict[str, Any], flow_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    gex_payload = unwrap_tbbfx_results(gex_payload) or {}
    flow_payload = unwrap_tbbfx_results(flow_payload) or {}
    latest = gex_payload.get("latest") or {}
    telemetry = flow_payload.get("telemetry") or {}
    momentum = flow_payload.get("momentum") or {}
    gamma_context = flow_payload.get("gamma_context") or {}

    net_gex = gamma_context.get("net_gex", latest.get("net_gex"))
    gamma_flip = gamma_context.get("gamma_flip", latest.get("gamma_flip"))
    underlying_price = (
        gamma_context.get("underlying_price")
        or latest.get("spot")
        or telemetry.get("microprice")
        or telemetry.get("spread_weighted_microprice")
    )

    has_market_payload = any(value not in (None, "", 0, 0.0) for value in (net_gex, gamma_flip, underlying_price))
    if not has_market_payload:
        return None

    composite = momentum.get("score") if isinstance(momentum, dict) else None
    regime = gamma_context.get("regime") or latest.get("regime")
    if not regime and net_gex is not None:
        regime = "POSITIVE" if float(net_gex) >= 0 else "NEGATIVE"

    return {
        "ticker": symbol,
        "net_gex": float(net_gex or 0.0),
        "gamma_flip": float(gamma_flip or underlying_price or 0.0),
        "underlying_price": float(underlying_price or gamma_flip or 0.0),
        "regime": regime,
        "dex": float(gamma_context.get("dex") or 0.0),
        "vex": float(gamma_context.get("vex") or 0.0),
        "chex": float(gamma_context.get("chex") or 0.0),
        "composite_momentum_score": composite,
        "mcp_snapshot_count": gex_payload.get("snapshot_count", 0),
        "mcp_telemetry_status": flow_payload.get("status"),
    }


async def _fetch_mcp_market_payload(symbol: str) -> Optional[Dict[str, Any]]:
    try:
        gex_payload, flow_payload = await asyncio.gather(
            asyncio.to_thread(
                execute_mcp_tool,
                "fetch_historical_gex_matrix",
                {"symbol": symbol, "lookback_hours": 24},
            ),
            asyncio.to_thread(
                execute_mcp_tool,
                "fetch_live_orderflow_telemetry",
                {"symbol": symbol},
            ),
        )
        return _exposure_from_mcp_payload(symbol, gex_payload, flow_payload)
    except Exception:
        return None


async def execute_agent_assessment(ticker: str) -> MicrostructureAssessment:
    symbol = _clean_symbol(ticker)
    mcp_exposure = await _fetch_mcp_market_payload(symbol)
    if mcp_exposure is not None:
        return _assessment_from_exposure(symbol, mcp_exposure)
    exposure = await asyncio.to_thread(_telemetry_engine.analyze, symbol, False)
    return _assessment_from_exposure(symbol, exposure)


async def stream_governance_query(payload: QueryRequest) -> AsyncGenerator[Dict[str, Any], None]:
    user_query = _extract_user_query(payload)
    symbol = _infer_symbol(user_query)

    steps = [
        ("Loading lean OpenBB governance instruction", {"target_r": TARGET_R, "min_trades_per_day": MIN_TRADES_PER_DAY}),
        ("Registering local read-only MCP provider", MCP_SERVER_DESCRIPTOR),
        ("Inspecting approved symbol risk tier", {"symbol": symbol, "risk_pct": IMMUTABLE_SYMBOL_RISK_TIERS.get(symbol)}),
        (
            "Calling MCP telemetry and VaR tools on demand",
            {
                "symbol": symbol,
                "tools": [
                    "fetch_historical_gex_matrix",
                    "fetch_live_orderflow_telemetry",
                    "fetch_portfolio_var_matrix",
                ],
            },
        ),
    ]

    for message, details in steps:
        yield _sse_payload(reasoning_step(message, "INFO", details))
        await _ship_token(symbol, message, "governance_reasoning_step")
        await asyncio.sleep(0.05)

    try:
        exposure = await _fetch_mcp_market_payload(symbol)
        if exposure is None:
            exposure = await asyncio.to_thread(_telemetry_engine.analyze, symbol, False)
        assessment = _assessment_from_exposure(symbol, exposure)
        gate = risk_decide(RiskGatewayRequest(symbol=symbol, proposed_risk_pct=IMMUTABLE_SYMBOL_RISK_TIERS.get(symbol)))
        result_text = (
            f"{symbol} governance assessment: regime {assessment.volatility_regime}, "
            f"momentum {assessment.composite_momentum_score:.1f}, stance {assessment.recommended_execution_stance}. "
            f"Risk remains immutable at {gate.immutable_risk_pct:.0%}, Target R remains {TARGET_R:.2f}, "
            f"and stop placement remains anchored to {STRUCTURAL_STOP_POLICY}."
        )
    except Exception as exc:
        yield _sse_payload(reasoning_step("Governance telemetry failed", "ERROR", str(exc)))
        result_text = (
            f"{symbol} governance assessment could not complete telemetry fetch. "
            "No risk tier, Target R, or stop policy changes were made."
        )

    for token in result_text.split(" "):
        chunk = token + " "
        yield _sse_payload(message_chunk(chunk))
        await _ship_token(symbol, chunk)
        await asyncio.sleep(0.02)


@router.get("/agents.json")
def agents_json() -> Dict[str, Any]:
    return {
        "agents": [
            {
                "id": "tbbfx-governance-agent",
                "name": "TBBFX Governance AI",
                "description": "OpenBB-compatible agent for SMC/GEX/orderflow governance with immutable risk tiers.",
                "version": "2.0.0",
                "query_endpoint": "/api/governance/query",
                "features": [
                    "openbb-ai-sse",
                    "local-mcp-tool-provider",
                    "gex-svi-telemetry",
                    "immutable-risk-gateways",
                    "signalr-cache-streaming",
                    "h1-fvg-ote-stop-protection",
                    "parametric-var-risk-gateway",
                    "cryptographic-governance-audit-ledger",
                ],
                "system_context": [MINIMAL_SYSTEM_INSTRUCTION],
                "immutable_strategy_context": SYSTEM_CONTEXT,
                "immutable_symbol_risk_tiers": IMMUTABLE_SYMBOL_RISK_TIERS,
                "mcp_servers": [MCP_SERVER_DESCRIPTOR],
                "tools": [tool.model_dump() for tool in governance_tool_definitions()],
            }
        ]
    }


@router.post("/api/governance/query")
async def governance_query(payload: QueryRequest) -> EventSourceResponse:
    return EventSourceResponse(stream_governance_query(payload))


@router.post("/api/governance/tools/decide", response_model=RiskGatewayResult)
def decide_tool(payload: RiskGatewayRequest) -> RiskGatewayResult:
    return risk_decide(payload)


@router.post("/api/governance/tools/transform", response_model=RiskGatewayResult)
def transform_tool(payload: RiskGatewayRequest) -> RiskGatewayResult:
    return risk_transform(payload)


if __name__ == "__main__":
    import asyncio as _asyncio

    async def test_run() -> None:
        assessment = await execute_agent_assessment("XAUUSD")
        print(assessment.model_dump())

    _asyncio.run(test_run())
