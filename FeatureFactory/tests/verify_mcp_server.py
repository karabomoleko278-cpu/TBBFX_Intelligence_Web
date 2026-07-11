"""Offline verification for the local TBBFX MCP server.

Run from FeatureFactory:
    python -m tests.verify_mcp_server
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Dict


FEATURE_FACTORY_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, FEATURE_FACTORY_ROOT)

from core.tbbfx_mcp_server import execute_tool, get_tool_definitions  # noqa: E402
from core.tbbfx_object import unwrap_tbbfx_results  # noqa: E402


def _rpc(proc: subprocess.Popen, message: Dict[str, Any]) -> Dict[str, Any]:
    assert proc.stdin is not None
    assert proc.stdout is not None
    proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("MCP server closed stdout before sending a response")
    return json.loads(line)


def main() -> int:
    failures = []

    tools = get_tool_definitions()
    names = {tool["name"] for tool in tools}
    print(f"direct tools={sorted(names)}")
    expected = {
        "fetch_historical_gex_matrix",
        "fetch_live_orderflow_telemetry",
        "fetch_macroeconomic_calendar",
        "fetch_quantitative_feature_pack",
        "fetch_portfolio_var_matrix",
        "verify_governance_audit_integrity",
    }
    if names != expected:
        failures.append(f"unexpected MCP tool registry: {names}")

    gex = execute_tool("fetch_historical_gex_matrix", {"symbol": "XAUUSD", "lookback_hours": 24})
    gex = unwrap_tbbfx_results(gex)
    print(f"direct historical status symbol={gex.get('symbol')} snapshots={gex.get('snapshot_count')}")
    if not gex.get("strategy_boundaries", {}).get("read_only"):
        failures.append("historical GEX tool must advertise read_only strategy boundaries")

    flow = execute_tool("fetch_live_orderflow_telemetry", {"symbol": "GBPUSD"})
    flow = unwrap_tbbfx_results(flow)
    print(f"direct orderflow status={flow.get('status')} symbol={flow.get('symbol')}")
    if flow.get("symbol") != "GBPUSD":
        failures.append("live orderflow tool did not cleanly echo GBPUSD")

    var_matrix = execute_tool("fetch_portfolio_var_matrix", {"symbol": "XAUUSD"})
    var_matrix = unwrap_tbbfx_results(var_matrix)
    print(f"direct VaR symbols={len(var_matrix.get('symbols', []))}")
    if not var_matrix.get("strategy_boundaries", {}).get("read_only"):
        failures.append("VaR tool must advertise read_only strategy boundaries")

    audit = execute_tool("verify_governance_audit_integrity", {"limit": 50})
    audit = unwrap_tbbfx_results(audit)
    print(f"direct audit status={audit.get('status')} checked={audit.get('total_checked')}")
    if audit.get("status") not in ("valid", "empty"):
        failures.append("governance audit ledger integrity check failed")

    proc = subprocess.Popen(
        [sys.executable, "-m", "core.tbbfx_mcp_server"],
        cwd=FEATURE_FACTORY_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        init = _rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        print(f"stdio initialize={init.get('result', {}).get('serverInfo', {}).get('name')}")
        if init.get("result", {}).get("serverInfo", {}).get("name") != "tbbfx-mcp-server":
            failures.append("stdio initialize did not return serverInfo.name")

        listed = _rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed_names = {tool["name"] for tool in listed.get("result", {}).get("tools", [])}
        print(f"stdio tools={sorted(listed_names)}")
        if listed_names != expected:
            failures.append("stdio tools/list registry mismatch")

        called = _rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "fetch_historical_gex_matrix",
                    "arguments": {"symbol": "EURUSD", "lookback_hours": 6},
                },
            },
        )
        content = called.get("result", {}).get("content", [])
        print(f"stdio call content_items={len(content)}")
        if not content:
            failures.append("stdio tools/call returned no content")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

    if failures:
        print("\nFAILURES:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\nALL MCP CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
