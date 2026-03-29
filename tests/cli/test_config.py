"""Tests for the support layer: config, providers, serving paths."""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from localmelo.support.config import (
    Config,
    ConfigError,
    GatewayConfig,
    MlcConfig,
    OllamaConfig,
    OnlineConfig,
    load,
    save,
)

# ── Config roundtrip ──


class TestConfigRoundtrip:
    """save() then load() should preserve all fields."""

    def test_mlc_roundtrip(self, tmp_path: Path) -> None:
        cfg = Config(
            backend="mlc-llm",
            mlc=MlcConfig(
                gpu_memory_gb=24.0,
                chat_model="Qwen3-8B",
                embedding_model="Qwen3-Embedding-0.6B",
                chat_port=9000,
            ),
            gateway=GatewayConfig(port=9999, host="0.0.0.0"),
        )
        config_path = str(tmp_path / "config.toml")
        with (
            mock.patch("localmelo.support.config.CONFIG_PATH", config_path),
            mock.patch("localmelo.support.config.CONFIG_DIR", str(tmp_path)),
        ):
            save(cfg)
            loaded = load()

        assert loaded.backend == "mlc-llm"
        assert loaded.mlc.gpu_memory_gb == 24.0
        assert loaded.mlc.chat_model == "Qwen3-8B"
        assert loaded.mlc.embedding_model == "Qwen3-Embedding-0.6B"
        assert loaded.mlc.chat_port == 9000
        assert loaded.gateway.port == 9999
        assert loaded.gateway.host == "0.0.0.0"

    def test_ollama_roundtrip(self, tmp_path: Path) -> None:
        cfg = Config(
            backend="ollama",
            ollama=OllamaConfig(
                chat_url="http://myhost:11434",
                chat_model="llama3.2",
                embedding_model="nomic-embed",
                embedding_url="http://myhost:11434",
            ),
        )
        config_path = str(tmp_path / "config.toml")
        with (
            mock.patch("localmelo.support.config.CONFIG_PATH", config_path),
            mock.patch("localmelo.support.config.CONFIG_DIR", str(tmp_path)),
        ):
            save(cfg)
            loaded = load()

        assert loaded.backend == "ollama"
        assert loaded.ollama.chat_url == "http://myhost:11434"
        assert loaded.ollama.chat_model == "llama3.2"
        assert loaded.ollama.embedding_model == "nomic-embed"

    def test_online_roundtrip(self, tmp_path: Path) -> None:
        cfg = Config(
            backend="online",
            online=OnlineConfig(
                provider="openai",
                api_key_env="OPENAI_API_KEY",
                chat_model="gpt-4o",
                local_embedding=True,
            ),
        )
        config_path = str(tmp_path / "config.toml")
        with (
            mock.patch("localmelo.support.config.CONFIG_PATH", config_path),
            mock.patch("localmelo.support.config.CONFIG_DIR", str(tmp_path)),
        ):
            save(cfg)
            loaded = load()

        assert loaded.backend == "online"
        assert loaded.online.provider == "openai"
        assert loaded.online.api_key_env == "OPENAI_API_KEY"
        assert loaded.online.chat_model == "gpt-4o"
        assert loaded.online.local_embedding is True

    def test_load_missing_file_returns_default(self, tmp_path: Path) -> None:
        config_path = str(tmp_path / "nonexistent.toml")
        with mock.patch("localmelo.support.config.CONFIG_PATH", config_path):
            loaded = load()
        assert loaded.backend == ""
        assert not loaded.is_configured


# ── Config validation ──


class TestConfigValidation:
    """Config.validate() should catch misconfigurations."""

    def test_empty_backend(self) -> None:
        cfg = Config(backend="")
        errors = cfg.validate()
        assert len(errors) == 1
        assert "backend is not set" in errors[0]

    def test_invalid_backend(self) -> None:
        cfg = Config(backend="not-a-backend")
        errors = cfg.validate()
        assert any("invalid" in e for e in errors)

    def test_mlc_missing_model(self) -> None:
        cfg = Config(backend="mlc-llm", mlc=MlcConfig(chat_model=""))
        errors = cfg.validate()
        assert any("chat_model" in e for e in errors)

    def test_mlc_bad_port(self) -> None:
        cfg = Config(
            backend="mlc-llm", mlc=MlcConfig(chat_model="Qwen3-1.7B", chat_port=0)
        )
        errors = cfg.validate()
        assert any("chat_port" in e for e in errors)

    def test_mlc_valid(self) -> None:
        cfg = Config(
            backend="mlc-llm", mlc=MlcConfig(chat_model="Qwen3-1.7B", chat_port=8400)
        )
        assert cfg.validate() == []

    def test_ollama_missing_url(self) -> None:
        cfg = Config(
            backend="ollama", ollama=OllamaConfig(chat_url="", chat_model="llama3")
        )
        errors = cfg.validate()
        assert any("chat_url" in e for e in errors)

    def test_ollama_missing_model(self) -> None:
        cfg = Config(
            backend="ollama",
            ollama=OllamaConfig(chat_url="http://localhost:11434", chat_model=""),
        )
        errors = cfg.validate()
        assert any("chat_model" in e for e in errors)

    def test_ollama_valid(self) -> None:
        cfg = Config(
            backend="ollama",
            ollama=OllamaConfig(
                chat_url="http://localhost:11434", chat_model="qwen3:8b"
            ),
        )
        assert cfg.validate() == []

    def test_online_missing_provider(self) -> None:
        cfg = Config(
            backend="online",
            online=OnlineConfig(provider="", api_key_env="KEY", chat_model="gpt-4o"),
        )
        errors = cfg.validate()
        assert any("provider" in e for e in errors)

    def test_online_invalid_provider(self) -> None:
        cfg = Config(
            backend="online",
            online=OnlineConfig(provider="deepseek", api_key_env="KEY", chat_model="m"),
        )
        errors = cfg.validate()
        assert any("invalid" in e for e in errors)

    def test_online_missing_api_key_env(self) -> None:
        cfg = Config(
            backend="online",
            online=OnlineConfig(provider="openai", api_key_env="", chat_model="gpt-4o"),
        )
        errors = cfg.validate()
        assert any("api_key_env" in e for e in errors)

    def test_online_env_var_not_set(self) -> None:
        env_var = "_LOCALMELO_TEST_KEY_NONEXISTENT"
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(env_var, None)
            cfg = Config(
                backend="online",
                online=OnlineConfig(
                    provider="openai", api_key_env=env_var, chat_model="gpt-4o"
                ),
            )
            errors = cfg.validate()
            assert any(env_var in e for e in errors)

    def test_online_env_var_set(self) -> None:
        env_var = "_LOCALMELO_TEST_KEY"
        with mock.patch.dict(os.environ, {env_var: "sk-test123"}):
            cfg = Config(
                backend="online",
                online=OnlineConfig(
                    provider="openai", api_key_env=env_var, chat_model="gpt-4o"
                ),
            )
            assert cfg.validate() == []

    def test_online_missing_model(self) -> None:
        env_var = "_LOCALMELO_TEST_KEY"
        with mock.patch.dict(os.environ, {env_var: "sk-test123"}):
            cfg = Config(
                backend="online",
                online=OnlineConfig(
                    provider="openai", api_key_env=env_var, chat_model=""
                ),
            )
            errors = cfg.validate()
            assert any("chat_model" in e for e in errors)

    def test_bad_gateway_port(self) -> None:
        cfg = Config(
            backend="mlc-llm",
            mlc=MlcConfig(chat_model="Qwen3-1.7B"),
            gateway=GatewayConfig(port=99999),
        )
        errors = cfg.validate()
        assert any("gateway" in e and "port" in e for e in errors)

    def test_validate_or_raise(self) -> None:
        cfg = Config(backend="")
        with pytest.raises(ConfigError, match="Configuration errors"):
            cfg.validate_or_raise()

    def test_validate_or_raise_passes(self) -> None:
        cfg = Config(backend="mlc-llm", mlc=MlcConfig(chat_model="Qwen3-1.7B"))
        cfg.validate_or_raise()  # should not raise


# ── has_embedding property ──


class TestHasEmbedding:
    """has_embedding should reflect embedding availability per backend."""

    def test_mlc_always_has_embedding(self) -> None:
        """mlc-llm backend always includes a local embedding model."""
        cfg = Config(backend="mlc-llm", mlc=MlcConfig(chat_model="Qwen3-1.7B"))
        assert cfg.has_embedding is True

    def test_ollama_with_embedding_model(self) -> None:
        """ollama has embedding when embedding_model is set."""
        cfg = Config(
            backend="ollama",
            ollama=OllamaConfig(
                chat_url="http://localhost:11434",
                chat_model="qwen3:8b",
                embedding_model="nomic-embed",
            ),
        )
        assert cfg.has_embedding is True

    def test_ollama_without_embedding_model(self) -> None:
        """ollama without embedding_model -> no embedding."""
        cfg = Config(
            backend="ollama",
            ollama=OllamaConfig(
                chat_url="http://localhost:11434",
                chat_model="qwen3:8b",
                embedding_model="",
            ),
        )
        assert cfg.has_embedding is False

    def test_online_with_local_embedding(self) -> None:
        """online backend with local_embedding=True has embedding."""
        cfg = Config(
            backend="online",
            online=OnlineConfig(
                provider="openai",
                api_key_env="OPENAI_API_KEY",
                chat_model="gpt-4o",
                local_embedding=True,
            ),
        )
        assert cfg.has_embedding is True

    def test_online_without_local_embedding(self) -> None:
        """online backend with local_embedding=False has no embedding."""
        cfg = Config(
            backend="online",
            online=OnlineConfig(
                provider="openai",
                api_key_env="OPENAI_API_KEY",
                chat_model="gpt-4o",
                local_embedding=False,
            ),
        )
        assert cfg.has_embedding is False

    def test_unknown_backend_no_embedding(self) -> None:
        """Unknown/empty backend -> no embedding."""
        cfg = Config(backend="")
        assert cfg.has_embedding is False

        cfg2 = Config(backend="some-unknown")
        assert cfg2.has_embedding is False


# ── Fail-fast config validation ──


class TestFailFastValidation:
    """validate() should catch all errors eagerly and validate_or_raise()
    should raise ConfigError with all collected issues."""

    def test_multiple_errors_collected(self) -> None:
        """A backend with multiple issues should report all of them."""
        cfg = Config(
            backend="online",
            online=OnlineConfig(provider="", api_key_env="", chat_model=""),
        )
        errors = cfg.validate()
        # Should catch: missing provider, missing api_key_env, missing chat_model
        assert len(errors) >= 3

    def test_validate_or_raise_includes_all_errors(self) -> None:
        """ConfigError message should contain all validation errors."""
        cfg = Config(
            backend="online",
            online=OnlineConfig(provider="", api_key_env="", chat_model=""),
        )
        with pytest.raises(ConfigError) as exc_info:
            cfg.validate_or_raise()
        msg = str(exc_info.value)
        assert "provider" in msg
        assert "api_key_env" in msg
        assert "chat_model" in msg

    def test_empty_backend_returns_early(self) -> None:
        """Empty backend should report just that one error and stop."""
        cfg = Config(backend="")
        errors = cfg.validate()
        assert len(errors) == 1
        assert "backend is not set" in errors[0]

    def test_invalid_backend_returns_early(self) -> None:
        """Invalid backend should report just that one error and stop."""
        cfg = Config(backend="nope")
        errors = cfg.validate()
        assert len(errors) == 1
        assert "invalid" in errors[0]

    def test_valid_config_returns_no_errors(self) -> None:
        """A fully valid config should produce zero errors."""
        cfg = Config(
            backend="mlc-llm",
            mlc=MlcConfig(chat_model="Qwen3-1.7B", chat_port=8400),
            gateway=GatewayConfig(port=8401, host="127.0.0.1"),
        )
        assert cfg.validate() == []
        # validate_or_raise should not raise
        cfg.validate_or_raise()


# ── Provider factory ──


class TestProviderFactory:
    """Provider creation should fail clearly for unknown providers."""

    def test_unknown_llm_provider_raises(self) -> None:
        from localmelo.support.providers import create_llm

        with pytest.raises(ValueError, match="Unknown LLM provider"):
            create_llm("nonexistent_provider", base_url="http://x", model="m")

    def test_unknown_embedding_provider_raises(self) -> None:
        from localmelo.support.providers import create_embedding

        with pytest.raises(ValueError, match="Unknown embedding provider"):
            create_embedding("nonexistent_provider", base_url="http://x", model="m")

    def test_openai_compat_llm_creates(self) -> None:
        from localmelo.support.providers import create_llm
        from localmelo.support.providers.llm.openai_compat import OpenAICompatLLM

        provider = create_llm(
            "openai_compat", base_url="http://localhost:8400/v1", model="test"
        )
        assert isinstance(provider, OpenAICompatLLM)

    def test_openai_compat_embedding_creates(self) -> None:
        from localmelo.support.providers import create_embedding
        from localmelo.support.providers.embedding.openai_compat import (
            OpenAICompatEmbedding,
        )

        provider = create_embedding(
            "openai_compat", base_url="http://localhost:8400/v1", model="test"
        )
        assert isinstance(provider, OpenAICompatEmbedding)

    def test_list_providers(self) -> None:
        from localmelo.support.providers import (
            list_embedding_providers,
            list_llm_providers,
        )

        assert "openai_compat" in list_llm_providers()
        assert "openai_compat" in list_embedding_providers()


# ── Serving path resolution ──


class TestServingPaths:
    """Serving paths must use support.models as the single source of truth."""

    def test_no_hardcoded_paths_in_source(self) -> None:
        """The model_config source must not contain hardcoded user paths."""
        import localmelo.support.serving.model_config as mc

        source_path = Path(mc.__file__)
        source = source_path.read_text()
        assert "/Users/tuomasier/Desktop/mlsys/models" not in source

    def test_models_base_matches_support_models(self) -> None:
        """models_base() must point to localmelo/support/models/."""
        from localmelo.support import models as sm
        from localmelo.support.serving.model_config import models_base

        base = models_base()
        # Must be the same directory that MODELS_DIR / EMBED_DIR live in
        assert str(base) == os.path.dirname(sm.__file__)
        assert base.name == "models"

    def test_default_config_paths_match_compiled_dir(self) -> None:
        """default_config() paths must agree with support.models.compiled_dir()."""
        from localmelo.support.models import (
            CHAT_MODELS,
            DEFAULT_EMBEDDING,
            compiled_dir,
            dylib_path,
        )
        from localmelo.support.serving.model_config import default_config

        cfg = default_config()

        # Find the embedding entry
        emb_entries = [e for e in cfg.models if e.model_type == "embedding"]
        assert len(emb_entries) >= 1
        emb = emb_entries[0]
        assert emb.model_dir == compiled_dir(DEFAULT_EMBEDDING)
        assert emb.model_lib == dylib_path(DEFAULT_EMBEDDING)

        # Find the chat entry
        chat_entries = [e for e in cfg.models if e.model_type == "chat"]
        assert len(chat_entries) >= 1
        chat = chat_entries[0]
        assert chat.model_dir == compiled_dir(CHAT_MODELS[0])
        assert chat.model_lib == dylib_path(CHAT_MODELS[0])

    def test_default_config_paths_start_with_models_base(self) -> None:
        from localmelo.support.serving.model_config import default_config, models_base

        cfg = default_config()
        base_str = str(models_base())
        for entry in cfg.models:
            assert entry.model_dir.startswith(
                base_str
            ), f"model_dir not under models_base: {entry.model_dir}"
            assert entry.model_lib.startswith(
                base_str
            ), f"model_lib not under models_base: {entry.model_lib}"

    def test_default_config_entries_are_valid(self) -> None:
        from localmelo.support.serving.model_config import default_config

        cfg = default_config()
        assert len(cfg.models) > 0
        for entry in cfg.models:
            assert entry.name
            assert entry.model_dir
            assert entry.model_lib
            assert entry.device in ("metal", "cuda", "vulkan", "cpu")
            assert entry.model_type in ("chat", "embedding")
            assert 0 < entry.port < 65536

    def test_default_config_device_matches_platform(self) -> None:
        import sys

        from localmelo.support.serving.model_config import default_config

        cfg = default_config()
        expected = "metal" if sys.platform == "darwin" else "cuda"
        for entry in cfg.models:
            assert entry.device == expected

    def test_models_base_resolves_to_existing_parent(self) -> None:
        """The parent of models_base() should exist (support/ dir)."""
        from localmelo.support.serving.model_config import models_base

        assert models_base().parent.exists()
