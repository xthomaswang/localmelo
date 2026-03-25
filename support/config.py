"""Persistent configuration stored at ~/.cache/localmelo/config.toml."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field


class ConfigError(Exception):
    """Raised when configuration is invalid or incomplete."""


CONFIG_DIR = os.path.expanduser("~/.cache/localmelo")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.toml")


@dataclass
class MlcConfig:
    gpu_memory_gb: float = 16.0
    chat_model: str = ""
    embedding_model: str = ""
    chat_port: int = 8400


@dataclass
class OllamaConfig:
    chat_url: str = "http://localhost:11434"
    chat_model: str = ""
    embedding_model: str = ""  # empty = use mlc-llm embedding
    embedding_url: str = ""  # empty = same as chat_url


@dataclass
class OnlineConfig:
    provider: str = ""  # openai | gemini | anthropic
    api_key_env: str = ""  # env var name, e.g. OPENAI_API_KEY
    chat_model: str = ""
    local_embedding: bool = False  # True = mlc-llm embedding, False = no memory


@dataclass
class GatewayConfig:
    port: int = 8401
    host: str = "127.0.0.1"


@dataclass
class Config:
    backend: str = ""  # mlc-llm | ollama | online
    mlc: MlcConfig = field(default_factory=MlcConfig)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    online: OnlineConfig = field(default_factory=OnlineConfig)
    gateway: GatewayConfig = field(default_factory=GatewayConfig)

    @property
    def is_configured(self) -> bool:
        return self.backend != ""

    @property
    def has_embedding(self) -> bool:
        if self.backend == "mlc-llm":
            return True
        if self.backend == "ollama":
            return bool(self.ollama.embedding_model)
        if self.backend == "online":
            return self.online.local_embedding
        return False

    def validate(self) -> list[str]:
        """Return a list of validation errors. Empty list means valid."""
        errors: list[str] = []

        valid_backends = ("mlc-llm", "ollama", "online")
        if not self.backend:
            errors.append("backend is not set (run 'melo --reconfigure')")
            return errors
        if self.backend not in valid_backends:
            errors.append(
                f"backend {self.backend!r} is invalid; "
                f"must be one of {valid_backends}"
            )
            return errors

        if self.backend == "mlc-llm":
            if not self.mlc.chat_model:
                errors.append(
                    "[mlc] chat_model is empty. "
                    "Run 'melo --reconfigure' to select a model."
                )
            if self.mlc.chat_port <= 0 or self.mlc.chat_port > 65535:
                errors.append(
                    f"[mlc] chat_port={self.mlc.chat_port} is out of range (1-65535)"
                )

        elif self.backend == "ollama":
            if not self.ollama.chat_url:
                errors.append(
                    "[ollama] chat_url is empty. "
                    "Set it to your Ollama server URL (e.g. http://localhost:11434)."
                )
            if not self.ollama.chat_model:
                errors.append(
                    "[ollama] chat_model is empty. "
                    "Specify the Ollama model name (e.g. qwen3:8b)."
                )

        elif self.backend == "online":
            valid_providers = ("openai", "gemini", "anthropic")
            if not self.online.provider:
                errors.append(
                    "[online] provider is empty. " f"Must be one of {valid_providers}."
                )
            elif self.online.provider not in valid_providers:
                errors.append(
                    f"[online] provider {self.online.provider!r} is invalid. "
                    f"Must be one of {valid_providers}."
                )
            if not self.online.api_key_env:
                errors.append(
                    "[online] api_key_env is empty. "
                    "Set the environment variable name that holds your API key "
                    "(e.g. OPENAI_API_KEY)."
                )
            elif not os.environ.get(self.online.api_key_env):
                errors.append(
                    f"[online] environment variable ${self.online.api_key_env} "
                    f"is not set. Export it before starting the gateway."
                )
            if not self.online.chat_model:
                errors.append(
                    "[online] chat_model is empty. "
                    "Specify the model name (e.g. gpt-4o, gemini-2.0-flash)."
                )

        # Gateway validation
        if self.gateway.port <= 0 or self.gateway.port > 65535:
            errors.append(
                f"[gateway] port={self.gateway.port} is out of range (1-65535)"
            )

        return errors

    def validate_or_raise(self) -> None:
        """Validate and raise ConfigError if any issues are found."""
        errors = self.validate()
        if errors:
            msg = "Configuration errors:\n" + "\n".join(f"  - {e}" for e in errors)
            raise ConfigError(msg)


def load() -> Config:
    if not os.path.isfile(CONFIG_PATH):
        return Config()

    with open(CONFIG_PATH, "rb") as f:
        data = tomllib.load(f)

    cfg = Config()
    cfg.backend = data.get("backend", "")

    if "mlc" in data:
        for k, v in data["mlc"].items():
            if hasattr(cfg.mlc, k):
                setattr(cfg.mlc, k, v)

    if "ollama" in data:
        for k, v in data["ollama"].items():
            if hasattr(cfg.ollama, k):
                setattr(cfg.ollama, k, v)

    if "online" in data:
        for k, v in data["online"].items():
            if hasattr(cfg.online, k):
                setattr(cfg.online, k, v)

    if "gateway" in data:
        for k, v in data["gateway"].items():
            if hasattr(cfg.gateway, k):
                setattr(cfg.gateway, k, v)

    return cfg


def save(cfg: Config) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)

    lines: list[str] = []
    lines.append(f'backend = "{cfg.backend}"')
    lines.append("")

    lines.append("[mlc]")
    lines.append(f"gpu_memory_gb = {cfg.mlc.gpu_memory_gb}")
    lines.append(f'chat_model = "{cfg.mlc.chat_model}"')
    lines.append(f'embedding_model = "{cfg.mlc.embedding_model}"')
    lines.append(f"chat_port = {cfg.mlc.chat_port}")
    lines.append("")

    lines.append("[ollama]")
    lines.append(f'chat_url = "{cfg.ollama.chat_url}"')
    lines.append(f'chat_model = "{cfg.ollama.chat_model}"')
    lines.append(f'embedding_model = "{cfg.ollama.embedding_model}"')
    lines.append(f'embedding_url = "{cfg.ollama.embedding_url}"')
    lines.append("")

    lines.append("[online]")
    lines.append(f'provider = "{cfg.online.provider}"')
    lines.append(f'api_key_env = "{cfg.online.api_key_env}"')
    lines.append(f'chat_model = "{cfg.online.chat_model}"')
    lines.append(f"local_embedding = {str(cfg.online.local_embedding).lower()}")
    lines.append("")

    lines.append("[gateway]")
    lines.append(f"port = {cfg.gateway.port}")
    lines.append(f'host = "{cfg.gateway.host}"')
    lines.append("")

    with open(CONFIG_PATH, "w") as f:
        f.write("\n".join(lines))
