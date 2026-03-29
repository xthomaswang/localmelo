# Quickstart

This guide covers the minimum needed to run localmelo, verify the
installation, and run the test suite.

## Installation

```bash
# Core + dev dependencies (tests, linting)
pip install -e ".[dev]"

# Core + dev + gateway dependencies (FastAPI, uvicorn)
pip install -e ".[dev,gateway]"
```

## Usage modes

### Direct CLI mode

Run a one-shot query without starting a server. No configuration file is
needed -- localmelo defaults to a local MLC-LLM backend at
`http://127.0.0.1:8400/v1`.

```bash
melo "what is 2+2"
```

Override the backend URL and model on the fly:

```bash
melo --base-url http://localhost:11434/v1 --chat-model qwen3:8b "what is 2+2"
```

### Gateway mode

Start the HTTP gateway, which serves an agent-as-a-service API. Requires
a configuration file (run `melo --reconfigure` to create one).

```bash
melo --serve
```

Override host and port:

```bash
melo --serve --host 0.0.0.0 --port 5000
```

### Gateway API

Once the gateway is running, interact via HTTP:

```bash
curl -X POST http://localhost:8401/v1/agent/run \
  -H "Content-Type: application/json" \
  -d '{"query": "hello"}'
```

Sessions are created implicitly by `/v1/agent/run`. Pass an optional
`session_id` to reuse an existing session or create a named one:

```bash
# Run a query (creates a new session automatically)
curl -X POST http://localhost:8401/v1/agent/run \
  -H "Content-Type: application/json" \
  -d '{"query": "hello"}'

# Reuse a named session across requests
curl -X POST http://localhost:8401/v1/agent/run \
  -H "Content-Type: application/json" \
  -d '{"query": "follow up", "session_id": "my-session"}'

# List active sessions
curl http://localhost:8401/v1/sessions

# Delete a session
curl -X DELETE http://localhost:8401/v1/sessions/my-session
```

## Backend options

localmelo supports three backends, configured via `melo --reconfigure`:

| Backend    | Description                          | Embedding support         |
|------------|--------------------------------------|---------------------------|
| `mlc-llm`  | Local MLC-LLM server                | Always (bundled model)    |
| `ollama`   | Ollama-compatible server             | Optional (`embedding_model` field) |
| `online`   | Cloud APIs (OpenAI, Gemini, Anthropic) | Optional (`local_embedding` flag) |

### mlc-llm (default)

Uses a local MLC-LLM server for both chat and embedding. The server must
be running before starting the gateway.

### ollama

Connects to an Ollama-compatible server. If `embedding_model` is left
empty, localmelo falls back to the MLC-LLM embedding server.

### online

Connects to a cloud API provider. Set the API key via the environment
variable named in `api_key_env` (e.g. `OPENAI_API_KEY`).

When `local_embedding` is `false`, the agent runs without long-term
memory -- only short-term (session) context is used. This is the
**no-embedding mode**.

## No-embedding mode

When no embedding provider is available (online backend with
`local_embedding = false`, or `embedding=None` in code), localmelo:

- Skips all long-term memory operations (store and retrieve)
- Uses only short-term (sliding window) context for conversations
- Still supports tool use, history recording, and the full agent loop
- The `Config.has_embedding` property reflects this at config time

This is useful when you want to use a cloud LLM without running a local
embedding server.

## Running tests

### Dev-safe tests (no gateway dependencies)

These tests require only `.[dev]` and cover the core agent loop,
executor, checker, CLI, config, memory, and integration:

```bash
python -m pytest tests/agent tests/checker tests/executor \
                 tests/cli tests/memory tests/integration -q
```

### Gateway tests (require FastAPI + uvicorn)

These tests require `.[dev,gateway]`:

```bash
python -m pytest tests/gateway -q
```

Note: `tests/gateway/test_api.py` uses `pytest.importorskip` for both
`fastapi` and `uvicorn`, so it is safe to collect even when either is
missing -- it will simply skip.

### Full suite

```bash
python -m pytest tests/ -q
```

### Test matrix summary

| Directory              | Install target    | Notes                                |
|------------------------|-------------------|--------------------------------------|
| `tests/agent/`         | `.[dev]`          | Agent loop with fake providers       |
| `tests/executor/`      | `.[dev]`          | Executor v0.2 + builtins             |
| `tests/checker/`       | `.[dev]`          | Checker boundary validators          |
| `tests/cli/`           | `.[dev]`          | CLI entrypoint, config, direct + gateway mode |
| `tests/memory/`        | `.[dev]`          | Memory subsystems + SQLite persistence |
| `tests/integration/`   | `.[dev]`          | Cross-module integration tests       |
| `tests/gateway/`       | `.[dev,gateway]`  | Session management + HTTP API tests  |

## Linting

```bash
python -m ruff check .
```
