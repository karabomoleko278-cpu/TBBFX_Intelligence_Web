"""
Visual Studio startup entry point for the TBBFX Centralized Feature Factory.

VS Python projects launch a *startup file* (not a shell command), so this script
boots the FastAPI app with uvicorn programmatically. Press F5 / Ctrl+F5 on the
FeatureFactory project and the API comes up on http://localhost:8000 (docs at
/docs). Override host/port/reload via env vars if needed.
"""

import os
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=os.getenv("TBBFX_HOST", "127.0.0.1"),
        port=int(os.getenv("TBBFX_PORT", "8000")),
        reload=os.getenv("TBBFX_RELOAD", "1") == "1",
    )
