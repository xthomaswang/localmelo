"""Interactive onboarding wizard for first-time setup."""

from __future__ import annotations

from localmelo.support import config
from localmelo.support.models import (
    DEFAULT_EMBEDDING,
    compile_model,
    is_compiled,
    pick_chat_model,
)


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"  {prompt}{suffix}: ").strip()
    return val or default


def _ask_int(prompt: str, default: int, lo: int = 0, hi: int = 999999) -> int:
    while True:
        raw = _ask(prompt, str(default))
        try:
            v = int(raw)
            if lo <= v <= hi:
                return v
            print(f"    Must be between {lo} and {hi}")
        except ValueError:
            print("    Enter a number")


def _ask_choice(prompt: str, options: list[str], default: int = 1) -> int:
    for i, opt in enumerate(options, 1):
        print(f"    [{i}] {opt}")
    return _ask_int(prompt, default, lo=1, hi=len(options))


def _confirm(prompt: str = "Start?") -> bool:
    val = _ask(prompt, "Y").lower()
    return val in ("y", "yes", "")


# ── Path 1: MLC-LLM ──


def _onboard_mlc(cfg: config.Config) -> bool:
    print("\n  -- MLC-LLM (local) --\n")

    gb = _ask_int("GPU memory to allocate (GB, min 8)", 16, lo=8, hi=1024)
    cfg.mlc.gpu_memory_gb = gb

    chat = pick_chat_model(gb)
    if chat is None:
        print("    Not enough memory for any chat model (need at least 8 GB)")
        return False

    embed = DEFAULT_EMBEDDING
    total = chat.estimated_gb + embed.estimated_gb

    cfg.mlc.chat_model = chat.name
    cfg.mlc.embedding_model = embed.name

    print("\n  Auto-selected:")
    print(f"    Chat:      {chat.name} ({chat.quantization})  ~{chat.estimated_gb} GB")
    print(
        f"    Embedding: {embed.name} ({embed.quantization})  ~{embed.estimated_gb} GB"
    )
    print(f"    Total:     ~{total:.1f} GB / {gb} GB")
    print()

    if not _confirm():
        return False

    # compile if needed
    for spec in [chat, embed]:
        if is_compiled(spec):
            print(f"  {spec.name}: already compiled")
        else:
            print(f"\n  Compiling {spec.name} ...")
            compile_model(spec)

    cfg.backend = "mlc-llm"
    return True


# ── Path 2: Ollama ──


def _onboard_ollama(cfg: config.Config) -> bool:
    print("\n  -- Ollama --\n")

    cfg.ollama.chat_url = _ask("Ollama API URL", "http://localhost:11434")
    cfg.ollama.chat_model = _ask("Chat model name", "qwen3:8b")

    print("\n  Embedding model (leave empty to use mlc-llm local embedding):")
    emb = _ask("Ollama embedding model", "")

    if emb:
        cfg.ollama.embedding_model = emb
        cfg.ollama.embedding_url = _ask("Embedding API URL", cfg.ollama.chat_url)
    else:
        # fallback to mlc-llm embedding
        cfg.ollama.embedding_model = ""
        embed = DEFAULT_EMBEDDING
        print(
            f"\n  Will use mlc-llm for embedding: {embed.name} (~{embed.estimated_gb} GB)"
        )
        if not is_compiled(embed):
            print(f"  Compiling {embed.name} ...")
            compile_model(embed)
        else:
            print(f"  {embed.name}: already compiled")

    print()
    if not _confirm():
        return False

    cfg.backend = "ollama"
    return True


# ── Path 3: Online API ──


def _onboard_online(cfg: config.Config) -> bool:
    print("\n  -- Cloud API --\n")

    providers = ["OpenAI", "Gemini", "Anthropic"]
    idx = _ask_choice("Provider", providers, default=1)
    provider = providers[idx - 1].lower()
    cfg.online.provider = provider

    default_env = {
        "openai": "OPENAI_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
    }
    cfg.online.api_key_env = _ask("API key env var", default_env.get(provider, ""))

    default_model = {
        "openai": "gpt-4o",
        "gemini": "gemini-2.0-flash",
        "anthropic": "claude-sonnet-4-20250514",
    }
    cfg.online.chat_model = _ask("Chat model", default_model.get(provider, ""))

    print("\n  Local embedding model for memory system?")
    print("    If no: agent runs without long-term memory")
    use_local = _ask("Use local embedding? (y/n)", "n").lower() in ("y", "yes")
    cfg.online.local_embedding = use_local

    if use_local:
        embed = DEFAULT_EMBEDDING
        print(
            f"\n  Will use mlc-llm for embedding: {embed.name} (~{embed.estimated_gb} GB)"
        )
        if not is_compiled(embed):
            print(f"  Compiling {embed.name} ...")
            compile_model(embed)
        else:
            print(f"  {embed.name}: already compiled")
    else:
        print("\n  No memory mode: agent will not have long-term memory.")

    print()
    if not _confirm():
        return False

    cfg.backend = "online"
    return True


# ── Main wizard ──


def run_wizard() -> config.Config | None:
    """Run the onboarding wizard. Returns Config if successful, None if cancelled."""

    print("\n  Welcome to localmelo.\n")

    backends = [
        "Local - mlc-llm (default, fully local)",
        "Local - ollama",
        "Cloud API (OpenAI / Gemini / Anthropic)",
    ]
    choice = _ask_choice("LLM backend", backends, default=1)

    cfg = config.load()

    handlers = {1: _onboard_mlc, 2: _onboard_ollama, 3: _onboard_online}
    ok = handlers[choice](cfg)

    if not ok:
        print("  Setup cancelled.")
        return None

    config.save(cfg)
    print(f"\n  Config saved to {config.CONFIG_PATH}")
    return cfg
