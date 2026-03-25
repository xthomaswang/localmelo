"""Standalone test server for the chat UI — no LLM needed.

Usage:  python -m localmelo.support.gateway._test_webapp
Opens on http://127.0.0.1:8401
"""

from __future__ import annotations

import uuid
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .webapp import mount

app = FastAPI()

# Mount the real chat UI
mount(app)


# Mock /v1/agent/run — echoes back the query
@app.post("/v1/agent/run")
async def mock_agent_run(body: dict[str, Any]) -> JSONResponse:
    query = body.get("query", "")
    sid = body.get("session_id") or uuid.uuid4().hex[:12]
    # Return a mock response with some markdown to test rendering
    reply = (
        f"You said: **{query}**\n\n"
        "Here's a test of markdown rendering:\n\n"
        "- Item one\n"
        "- Item two\n"
        "- Item three\n\n"
        "And some code:\n\n"
        "```python\nprint('hello from localmelo')\n```\n\n"
        "The web UI is working correctly!"
    )
    return JSONResponse(content={"result": reply, "session_id": sid})


@app.get("/v1/health")
async def health() -> JSONResponse:
    return JSONResponse(content={"status": "ok"})


if __name__ == "__main__":
    print("\n  Test server: http://127.0.0.1:8401/")
    print("  (mock mode — no LLM, just echoes back)\n")
    uvicorn.run(app, host="127.0.0.1", port=8401, log_level="info")
