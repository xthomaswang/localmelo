from __future__ import annotations

import atexit
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

try:
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
except ImportError as e:
    raise ImportError(
        "Gateway requires fastapi and uvicorn: pip install fastapi uvicorn"
    ) from e

from localmelo.melo.checker import Checker, GatewayIngressPayload, SessionTransition
from localmelo.support import config
from localmelo.support.gateway.session import SessionManager

if TYPE_CHECKING:
    from localmelo.support.models import ModelSpec

logger = logging.getLogger("localmelo.gateway")


# ── LLM subprocess manager ──


class LLMProcess:
    """Manages the MLC-LLM serving subprocess via support.serving."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen[bytes] | None = None

    def start(self, entries: list[dict[str, Any]], port: int = 8400) -> None:
        """Start serving models on a single combined server.

        entries: list of dicts with keys matching ModelEntry fields
                 (name, model_dir, model_lib, device, port, model_type)
        """
        if not entries:
            return

        project_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        )

        # Build a script that loads all models into a single ServerContext
        config_data = json.dumps(entries)
        script = (
            f"import sys, json\n"
            f"sys.path.insert(0, {project_root!r})\n"
            f"from localmelo.support.serving.server import _ensure_mlc_path, _load_chat, _load_embedding\n"
            f"from localmelo.support.serving.model_config import ModelEntry\n"
            f"_ensure_mlc_path()\n"
            f"import fastapi, uvicorn\n"
            f"from mlc_llm.serve.entrypoints import openai_entrypoints\n"
            f"from mlc_llm.serve.server.server_context import ServerContext\n"
            f"app = fastapi.FastAPI(title='localmelo llm')\n"
            f"app.include_router(openai_entrypoints.app)\n"
            f"ctx = ServerContext()\n"
            f"ServerContext.server_context = ctx\n"
            f"for e in json.loads({config_data!r}):\n"
            f"    entry = ModelEntry(**e)\n"
            f"    if entry.model_type == 'chat':\n"
            f"        ctx.add_model(entry.name, _load_chat(entry))\n"
            f"    else:\n"
            f"        ctx.add_embedding_engine(entry.name, _load_embedding(entry))\n"
            f"uvicorn.run(app, host='127.0.0.1', port={port}, log_level='warning')\n"
        )

        self._proc = subprocess.Popen(
            [sys.executable, "-c", script],
        )
        atexit.register(self.stop)

        # Wait and verify it didn't crash
        logger.info("Waiting for LLM server to start ...")
        time.sleep(8)
        if self._proc.poll() is not None:
            raise RuntimeError("LLM server exited immediately")
        logger.info("LLM server started (pid=%d) on :%d", self._proc.pid, port)

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            logger.info("LLM server stopped")
        self._proc = None

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None


# ── Gateway Server ──


class GatewayServer:
    def __init__(self, cfg: config.Config) -> None:
        self.cfg = cfg
        self.host = cfg.gateway.host
        self.port = cfg.gateway.port
        self._started_at: float | None = None
        self.llm = LLMProcess()
        self._checker = Checker()

        # SessionManager now builds providers from Config directly
        self.sessions = SessionManager(
            max_sessions=50,
            idle_ttl=3600.0,
            config=cfg,
        )

        self.app = FastAPI(title="localmelo gateway", lifespan=self._lifespan)
        self._register_routes()

        from localmelo.support.gateway.webapp import mount as mount_webapp

        mount_webapp(self.app)

    # ── Lifespan ──

    @asynccontextmanager
    async def _lifespan(self, app: FastAPI) -> AsyncIterator[None]:  # type: ignore[type-arg]
        self._started_at = time.time()
        self._start_llm_server()
        logger.info("gateway ready on %s:%d", self.host, self.port)
        yield
        logger.info("gateway shutting down")
        await self.sessions.close_all()
        self.llm.stop()

    def _start_llm_server(self) -> None:
        """Start the LLM serving backend based on config."""
        cfg = self.cfg
        entries: list[dict[str, Any]] = []

        if cfg.backend == "mlc-llm":
            from localmelo.support.models import (
                DEFAULT_EMBEDDING,
                compiled_dir,
                dylib_path,
            )

            chat_spec = _find_chat_spec(cfg.mlc.chat_model)
            if chat_spec is None:
                raise RuntimeError(f"Unknown chat model: {cfg.mlc.chat_model}")

            entries.append(
                {
                    "name": chat_spec.name,
                    "model_dir": compiled_dir(chat_spec),
                    "model_lib": dylib_path(chat_spec),
                    "device": "metal" if sys.platform == "darwin" else "cuda",
                    "port": cfg.mlc.chat_port,
                    "model_type": "chat",
                }
            )
            entries.append(
                {
                    "name": DEFAULT_EMBEDDING.name,
                    "model_dir": compiled_dir(DEFAULT_EMBEDDING),
                    "model_lib": dylib_path(DEFAULT_EMBEDDING),
                    "device": "metal" if sys.platform == "darwin" else "cuda",
                    "port": cfg.mlc.chat_port,
                    "model_type": "embedding",
                }
            )

            self.llm.start(entries, port=cfg.mlc.chat_port)
            print(f"  LLM server started on :{cfg.mlc.chat_port}")

        elif cfg.backend == "ollama":
            if not cfg.ollama.embedding_model:
                # Use mlc-llm for embedding only.
                # The agent (melo/agent/agent.py) hardcodes the lowercase
                # model name for ollama/online fallback embedding, so the
                # registered name here must match exactly.
                from localmelo.support.models import (
                    DEFAULT_EMBEDDING,
                    compiled_dir,
                    dylib_path,
                )

                _emb_name = DEFAULT_EMBEDDING.name.lower()
                entries.append(
                    {
                        "name": _emb_name,
                        "model_dir": compiled_dir(DEFAULT_EMBEDDING),
                        "model_lib": dylib_path(DEFAULT_EMBEDDING),
                        "device": "metal" if sys.platform == "darwin" else "cuda",
                        "port": cfg.mlc.chat_port,
                        "model_type": "embedding",
                    }
                )
                self.llm.start(entries, port=cfg.mlc.chat_port)
                print(f"  Embedding server started on :{cfg.mlc.chat_port}")
            print(f"  Ollama chat at {cfg.ollama.chat_url}")

        elif cfg.backend == "online":
            if cfg.online.local_embedding:
                # Same as ollama fallback: agent hardcodes lowercase name.
                from localmelo.support.models import (
                    DEFAULT_EMBEDDING,
                    compiled_dir,
                    dylib_path,
                )

                _emb_name = DEFAULT_EMBEDDING.name.lower()
                entries.append(
                    {
                        "name": _emb_name,
                        "model_dir": compiled_dir(DEFAULT_EMBEDDING),
                        "model_lib": dylib_path(DEFAULT_EMBEDDING),
                        "device": "metal" if sys.platform == "darwin" else "cuda",
                        "port": cfg.mlc.chat_port,
                        "model_type": "embedding",
                    }
                )
                self.llm.start(entries, port=cfg.mlc.chat_port)
                print(f"  Embedding server started on :{cfg.mlc.chat_port}")
            else:
                print("  No memory mode: running without embedding")

    # ── Routes ──

    def _register_routes(self) -> None:
        app = self.app

        @app.post("/v1/agent/run")
        async def agent_run(body: dict[str, Any]) -> JSONResponse:
            query = body.get("query", "")
            session_id = body.get("session_id")

            # ── Ingress validation (Checker v0.2) ──
            ingress_check = self._checker.check_gateway_ingress(
                GatewayIngressPayload(query=query or "", session_id=session_id)
            )
            if not ingress_check.allowed:
                return JSONResponse(
                    status_code=400,
                    content={"error": ingress_check.reason},
                )

            sanitized = ingress_check.sanitized_payload
            query = sanitized.query
            session_id = sanitized.session_id

            session = await self.sessions.get_or_create(session_id)

            # Per-session serialization: only one run at a time per session
            async with session._lock:
                # ── Session transition check: idle → running ──
                trans_check = self._checker.check_session_transition(
                    SessionTransition(
                        from_status=session.status,
                        to_status="running",
                        session_id=session.session_id,
                    )
                )
                if not trans_check.allowed:
                    return JSONResponse(
                        status_code=409,
                        content={
                            "error": trans_check.reason,
                            "session_id": session.session_id,
                        },
                    )

                session.status = "running"
                session.touch()

                try:
                    result = await session.agent.run(query)

                    # ── Session transition: running → idle ──
                    self._checker.check_session_transition(
                        SessionTransition(
                            from_status="running",
                            to_status="idle",
                            session_id=session.session_id,
                        )
                    )
                    session.status = "idle"
                    session.touch()
                    return JSONResponse(
                        content={
                            "result": result,
                            "session_id": session.session_id,
                        }
                    )
                except Exception as e:
                    session.status = "idle"
                    logger.exception("agent run failed")
                    return JSONResponse(
                        status_code=500,
                        content={
                            "error": str(e),
                            "session_id": session.session_id,
                        },
                    )

        @app.get("/v1/sessions")
        async def list_sessions() -> JSONResponse:
            return JSONResponse(
                content={
                    "sessions": self.sessions.list_sessions(),
                }
            )

        @app.delete("/v1/sessions/{session_id}")
        async def close_session(session_id: str) -> JSONResponse:
            closed = await self.sessions.close(session_id)
            if not closed:
                return JSONResponse(
                    status_code=404,
                    content={"error": "session not found"},
                )
            return JSONResponse(content={"closed": session_id})

        @app.get("/v1/health")
        async def health() -> JSONResponse:
            sessions = self.sessions.list_sessions()
            busy_count = sum(1 for s in sessions if s.get("is_busy"))
            return JSONResponse(
                content={
                    "status": "ok",
                    "uptime": round(time.time() - (self._started_at or time.time()), 1),
                    "sessions": {
                        "active": len(sessions),
                        "busy": busy_count,
                        "max": self.sessions._max,
                    },
                    "llm": {
                        "running": self.llm.is_running,
                        "pid": self.llm.pid,
                    },
                    "backend": self.cfg.backend,
                }
            )

    # ── Run ──

    def run(self) -> None:
        uvicorn.run(self.app, host=self.host, port=self.port, log_level="info")


def _find_chat_spec(name: str) -> ModelSpec | None:
    """Look up a ModelSpec by name from the budget table."""
    from localmelo.support.models import CHAT_MODELS

    for m in CHAT_MODELS:
        if m.name == name:
            return m
    return None


def start_gateway(cfg: config.Config) -> None:
    cfg.validate_or_raise()
    server = GatewayServer(cfg)
    server.run()
