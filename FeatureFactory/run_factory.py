"""
Local launcher for the TBBFX Feature Factory.

Pulls MASSIVE_API_KEY (and TRADIER_TOKEN, if present) from the persisted *user*
environment via the registry, so the server reliably has its credentials no
matter how it's spawned — Windows `Start-Process` does not always propagate a
shell's in-session `$env:` change to the child, which silently dropped Massive
to the yfinance fallback. The keys are NOT stored in this file.

Run:  python run_factory.py
"""
import os
import sys

# Hydrate credentials from the persisted HKCU\Environment keys (set once via
# [Environment]::SetEnvironmentVariable("MASSIVE_API_KEY", "...", "User")).
try:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as _k:
        for _name in ("MASSIVE_API_KEY", "TRADIER_TOKEN", "MASSIVE_API_BASE_URL", "TRADIER_API_BASE"):
            try:
                _val, _ = winreg.QueryValueEx(_k, _name)
                if _val and not os.environ.get(_name):
                    os.environ[_name] = _val
            except FileNotFoundError:
                pass
except Exception as _e:  # noqa: BLE001 - launcher must never hard-fail on env
    print(f"[launcher] could not read user environment: {_e}")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

print(f"[launcher] MASSIVE_API_KEY configured: {bool(os.environ.get('MASSIVE_API_KEY'))} | "
      f"TRADIER_TOKEN configured: {bool(os.environ.get('TRADIER_TOKEN'))}")

import uvicorn
_port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
print(f"[launcher] starting uvicorn on 0.0.0.0:{_port}")
uvicorn.run("main:app", host="0.0.0.0", port=_port)
