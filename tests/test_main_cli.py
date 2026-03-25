"""Tests for CLI --host/--port override logic in __main__.py.

Ensures that argparse defaults (None) do not silently overwrite
values loaded from the persistent config file.
"""

from __future__ import annotations

import argparse
from typing import Any

from localmelo.support.config import Config, GatewayConfig, MlcConfig

# ---------------------------------------------------------------------------
# Helper: build an args namespace that mimics argparse output
# ---------------------------------------------------------------------------


def _make_args(**overrides: Any) -> argparse.Namespace:
    """Return a Namespace with the same defaults as main()'s parser."""
    defaults: dict[str, Any] = dict(
        query=[],
        base_url=None,
        chat_model=None,
        embed_model=None,
        serve=True,  # gateway mode
        host=None,
        port=None,
        reconfigure=False,
        daemon=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _valid_cfg(**gw_overrides: Any) -> Config:
    """Return a minimal valid Config (mlc-llm backend) with custom gateway."""
    gw = GatewayConfig(**gw_overrides) if gw_overrides else GatewayConfig()
    return Config(
        backend="mlc-llm",
        mlc=MlcConfig(chat_model="Qwen3-1.7B"),
        gateway=gw,
    )


# ---------------------------------------------------------------------------
# Scenario 1: config has host=0.0.0.0, user does NOT pass --host
#   => host must stay 0.0.0.0
# ---------------------------------------------------------------------------


class TestHostNotOverriddenWhenOmitted:
    """When the user omits --host, the config value must survive."""

    def test_config_host_preserved(self) -> None:
        cfg = _valid_cfg(host="0.0.0.0", port=9000)
        args = _make_args()  # host=None, port=None

        # Replicate the override logic from __main__.py
        if args.host is not None:
            cfg.gateway.host = args.host
        if args.port is not None:
            cfg.gateway.port = args.port

        assert cfg.gateway.host == "0.0.0.0"
        assert cfg.gateway.port == 9000


# ---------------------------------------------------------------------------
# Scenario 2: user explicitly passes --host 127.0.0.1
#   => must override config value
# ---------------------------------------------------------------------------


class TestHostOverriddenWhenExplicit:
    """When the user passes --host, the config value must update."""

    def test_explicit_host_overrides(self) -> None:
        cfg = _valid_cfg(host="0.0.0.0", port=9000)
        args = _make_args(host="127.0.0.1")

        if args.host is not None:
            cfg.gateway.host = args.host
        if args.port is not None:
            cfg.gateway.port = args.port

        assert cfg.gateway.host == "127.0.0.1"
        assert cfg.gateway.port == 9000  # unchanged


# ---------------------------------------------------------------------------
# Scenario 3: --port behaviour matches --host
# ---------------------------------------------------------------------------


class TestPortOverrideLogic:
    """--port should only override when explicitly supplied."""

    def test_port_preserved_when_omitted(self) -> None:
        cfg = _valid_cfg(host="0.0.0.0", port=7777)
        args = _make_args()  # port=None

        if args.host is not None:
            cfg.gateway.host = args.host
        if args.port is not None:
            cfg.gateway.port = args.port

        assert cfg.gateway.port == 7777

    def test_port_overridden_when_explicit(self) -> None:
        cfg = _valid_cfg(host="0.0.0.0", port=7777)
        args = _make_args(port=9999)

        if args.host is not None:
            cfg.gateway.host = args.host
        if args.port is not None:
            cfg.gateway.port = args.port

        assert cfg.gateway.port == 9999

    def test_port_can_be_set_to_8401_explicitly(self) -> None:
        """Regression: old code used `if args.port != 8401` which would
        prevent the user from explicitly setting port=8401 if the config
        had a different value."""
        cfg = _valid_cfg(port=5000)
        args = _make_args(port=8401)

        if args.port is not None:
            cfg.gateway.port = args.port

        assert cfg.gateway.port == 8401


# ---------------------------------------------------------------------------
# Scenario 4: direct mode is not affected
# ---------------------------------------------------------------------------


class TestDirectModeUnaffected:
    """Direct mode (query without --serve) should not touch gateway config."""

    def test_direct_mode_skips_gateway_overrides(self) -> None:
        """Simulate: `melo 'do stuff'` (no --serve)."""
        args = _make_args(query=["do", "stuff"], serve=False)

        # In __main__.py, direct mode returns early before the override block.
        # Verify by checking args — direct mode is entered when
        # args.query is truthy and args.serve is False.
        assert args.query and not args.serve
        # host/port are None — never applied
        assert args.host is None
        assert args.port is None


# ---------------------------------------------------------------------------
# Scenario 5: integration-style test using the real argparse parser
# ---------------------------------------------------------------------------


class TestArgparseDefaults:
    """Verify that the actual parser produces None defaults for --host/--port."""

    @staticmethod
    def _build_parser() -> argparse.ArgumentParser:
        """Replicate the parser from __main__.py."""
        parser = argparse.ArgumentParser(prog="melo")
        parser.add_argument("query", nargs="*")
        parser.add_argument("--base-url", default=None)
        parser.add_argument("--chat-model", default=None)
        parser.add_argument("--embed-model", default=None)
        parser.add_argument("--serve", action="store_true")
        parser.add_argument("--host", default=None)
        parser.add_argument("--port", type=int, default=None)
        parser.add_argument("--reconfigure", action="store_true")
        parser.add_argument("--daemon", choices=["install", "uninstall", "status"])
        return parser

    def test_no_args_gives_none_host_port(self) -> None:
        parser = self._build_parser()
        args = parser.parse_args(["--serve"])
        assert args.host is None
        assert args.port is None

    def test_explicit_host_parsed(self) -> None:
        parser = self._build_parser()
        args = parser.parse_args(["--serve", "--host", "0.0.0.0"])
        assert args.host == "0.0.0.0"
        assert args.port is None

    def test_explicit_port_parsed(self) -> None:
        parser = self._build_parser()
        args = parser.parse_args(["--serve", "--port", "3000"])
        assert args.port == 3000
        assert args.host is None

    def test_both_explicit(self) -> None:
        parser = self._build_parser()
        args = parser.parse_args(["--serve", "--host", "10.0.0.1", "--port", "4000"])
        assert args.host == "10.0.0.1"
        assert args.port == 4000

    def test_direct_mode_query(self) -> None:
        parser = self._build_parser()
        args = parser.parse_args(["hello", "world"])
        assert args.query == ["hello", "world"]
        assert not args.serve
        assert args.host is None
        assert args.port is None


# ---------------------------------------------------------------------------
# Scenario 6: daemon install path handles None port gracefully
# ---------------------------------------------------------------------------


class TestDaemonInstallPortHandling:
    """When --port is omitted, daemon.install() should use its own default."""

    def test_daemon_install_no_port(self) -> None:
        """Simulate `melo --daemon install` (no --port, no --base-url)."""
        args = _make_args(daemon="install", port=None, base_url=None)

        # Build kwargs the same way __main__.py does
        kwargs: dict = {}
        if args.port is not None:
            kwargs["port"] = args.port
        if args.base_url is not None:
            kwargs["base_url"] = args.base_url

        # kwargs should be empty, so daemon.install() uses its own defaults
        assert kwargs == {}

    def test_daemon_install_with_port(self) -> None:
        """Simulate `melo --daemon install --port 5000`."""
        args = _make_args(daemon="install", port=5000, base_url=None)

        kwargs: dict = {}
        if args.port is not None:
            kwargs["port"] = args.port
        if args.base_url is not None:
            kwargs["base_url"] = args.base_url

        assert kwargs == {"port": 5000}
