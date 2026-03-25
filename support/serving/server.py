"""Multi-model MLC-LLM server launcher.

Loads compiled chat and/or embedding models and exposes them via
OpenAI-compatible REST endpoints on per-model ports.

Usage (CLI)::

    # All models in default config
    python -m localmelo.support.serving.server

    # Only embedding models
    python -m localmelo.support.serving.server --type embedding

    # Specific model by name
    python -m localmelo.support.serving.server --name bge

Usage (programmatic)::

    from localmelo.support.serving import serve, default_config
    serve(default_config(), model_type="embedding")
"""

from __future__ import annotations

import argparse
import multiprocessing
import sys
import time
from collections.abc import Awaitable, Callable

from .model_config import ModelEntry, ServingConfig, default_config

# ── Request stats ──


class _RequestStats:
    def __init__(self) -> None:
        self.total = 0
        self.success = 0
        self.failed = 0
        self.start_time = time.time()

    def log(self, method: str, path: str, status: int, latency_ms: float) -> None:
        self.total += 1
        if 200 <= status < 400:
            self.success += 1
            tag = "OK"
        else:
            self.failed += 1
            tag = "FAIL"
        uptime = time.time() - self.start_time
        rps = self.total / uptime if uptime > 0 else 0
        print(
            f"  [{tag}] #{self.total:>5}  {method} {path}  "
            f"status={status}  {latency_ms:>7.1f}ms  "
            f"(ok={self.success} fail={self.failed} rps={rps:.1f})"
        )


# ── Engine loaders ──


def _ensure_mlc_path() -> None:
    """Add MLC-LLM Python path if not already importable."""
    from ._mlc_path import ensure_mlc_importable

    ensure_mlc_importable()


def _load_embedding(entry: ModelEntry):  # type: ignore[no-untyped-def]
    _ensure_mlc_path()
    from mlc_llm.serve.embedding_engine import AsyncEmbeddingEngine

    print(f"[embedding] Loading '{entry.name}' from {entry.model_dir}...")
    engine = AsyncEmbeddingEngine(
        model=entry.model_dir,
        model_lib=entry.model_lib,
        device=entry.device,
    )
    print(f"  type={engine.model_type}  pooling={engine.pooling_strategy}")
    return engine


def _load_chat(entry: ModelEntry):  # type: ignore[no-untyped-def]
    _ensure_mlc_path()
    from mlc_llm.serve.engine import AsyncMLCEngine

    print(f"[chat] Loading '{entry.name}' from {entry.model_dir}...")
    engine = AsyncMLCEngine(
        model=entry.model_dir,
        model_lib=entry.model_lib,
        device=entry.device,
    )
    return engine


# ── Single-model server process ──


def _run_model(entry: ModelEntry, host: str) -> None:
    """Start a single-model FastAPI server (runs in its own process)."""
    _ensure_mlc_path()

    import fastapi
    import uvicorn
    from fastapi import Request, Response
    from mlc_llm.serve.entrypoints import openai_entrypoints
    from mlc_llm.serve.server.server_context import ServerContext

    stats = _RequestStats()

    app = fastapi.FastAPI(title=f"MLC-LLM {entry.name}")

    @app.middleware("http")
    async def track_requests(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        t0 = time.perf_counter()
        response = await call_next(request)
        latency_ms = (time.perf_counter() - t0) * 1000
        stats.log(request.method, request.url.path, response.status_code, latency_ms)
        return response

    app.include_router(openai_entrypoints.app)

    server_context = ServerContext()
    ServerContext.server_context = server_context

    if entry.model_type == "embedding":
        engine = _load_embedding(entry)
        server_context.add_embedding_engine(entry.name, engine)
    else:
        engine = _load_chat(entry)
        server_context.add_model(entry.name, engine)

    print(f"\nServing '{entry.name}' on http://{host}:{entry.port}")
    uvicorn.run(app, host=host, port=entry.port, log_level="warning")


# ── Public API ──


def serve(
    config: ServingConfig | None = None,
    model_type: str | None = None,
    names: list[str] | None = None,
) -> None:
    """Launch model servers according to config.

    Parameters
    ----------
    config : ServingConfig | None
        Model configuration. Uses ``default_config()`` if not provided.
    model_type : str | None
        Filter to "chat" or "embedding" only.
    names : list[str] | None
        Filter to specific model names.
    """
    if config is None:
        config = default_config()

    selected = config.filter(model_type=model_type, names=names)

    if not selected:
        print("No models matched. Available:")
        for m in config.models:
            print(f"  [{m.model_type}] {m.name} (port {m.port})")
        sys.exit(1)

    print("=" * 60)
    print("Starting models:")
    for m in selected:
        print(f"  [{m.model_type}] {m.name} -> http://{config.host}:{m.port}")
    print("=" * 60)

    if len(selected) == 1:
        _run_model(selected[0], config.host)
    else:
        processes: list[multiprocessing.Process] = []
        for entry in selected:
            p = multiprocessing.Process(
                target=_run_model,
                args=(entry, config.host),
                name=entry.name,
            )
            p.start()
            processes.append(p)
            print(f"  Started PID {p.pid} for '{entry.name}'")

        try:
            for p in processes:
                p.join()
        except KeyboardInterrupt:
            print("\nShutting down all servers...")
            for p in processes:
                p.terminate()
            for p in processes:
                p.join(timeout=5)


# ── CLI entry ──


def main() -> None:
    parser = argparse.ArgumentParser(description="MLC-LLM Model Server")
    parser.add_argument(
        "--type", choices=["chat", "embedding"], help="Only start models of this type"
    )
    parser.add_argument(
        "--name", action="append", help="Only start model(s) with this name"
    )
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    config = default_config()
    config.host = args.host
    serve(config, model_type=args.type, names=args.name)


if __name__ == "__main__":
    main()
