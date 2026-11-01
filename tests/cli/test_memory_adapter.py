"""``[memory] adapter`` and ``melo memory adapter rebuild``: a developer setting, off by default.

The setting is ``off`` or ``on``, and ``adapter_penalty`` is how strongly the
map is held to the identity. They are saved only when not the defaults, checked,
set for one run with ``-X``, and listed by the developer help only. The command is
for developer mode; ``melo memory status`` shows what is built.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import numpy as np
import pytest

import localmelo.__main__ as cli
from localmelo.support import config, config_cli, memory_cli
from localmelo.support.config import Config, MemoryConfig, save
from localmelo.support.memory import ChatMemory
from localmelo.support.memory import adapter as adapting

KEY = "memory.adapter"
PENALTY_KEY = "memory.adapter_penalty"
BUILT_AT = 1_700_000_000.0


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "cfg" / "config.toml"))
    yield tmp_path


def developer(home: Path, extra: str = "") -> None:
    path = home / "cfg" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[ui]\nmode = "developer"\n' + extra)


def test_it_is_off_by_default_saved_only_when_set_and_checked(tmp_path: Path) -> None:
    assert MemoryConfig().adapter == "off" and MemoryConfig().adapter_penalty == 1.0
    path = tmp_path / "config.toml"
    cfg = Config(chat_backend="vllm", embedding_backend="none")
    with (
        mock.patch.object(config, "CONFIG_PATH", str(path)),
        mock.patch.object(config, "CONFIG_DIR", str(tmp_path)),
    ):
        save(cfg)
        assert "adapter" not in path.read_text()
        cfg.memory = MemoryConfig(adapter="on")
        assert not [e for e in cfg.validate() if "[memory]" in e]
        save(cfg)
        assert '[memory]\nadapter = "on"\n' in path.read_text()
        assert config.load().memory == MemoryConfig(adapter="on")
        cfg.memory = MemoryConfig(adapter_penalty=2.5)
        save(cfg)
        assert "adapter_penalty = 2.5" in path.read_text()
        assert 'adapter = "' not in path.read_text()
        assert config.load().memory == MemoryConfig(adapter_penalty=2.5)
    cfg.memory = MemoryConfig(adapter="maybe")
    assert any(
        "[memory] adapter 'maybe' is not recognised" in e for e in cfg.validate()
    )
    for bad in (-1.0, float("nan"), float("inf")):
        cfg.memory = MemoryConfig(adapter_penalty=bad)
        assert any("adapter_penalty must be a number" in e for e in cfg.validate()), bad


def test_x_sets_it_for_one_run_and_only_the_developer_help_lists_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli._parse(["-X", f"{KEY}=on", "--base-url", "http://127.0.0.1:1/v1", "q"])
    assert config.overrides() == {KEY: "on"}
    assert config.load().memory.adapter == "on"
    assert config_cli.main(["keys", "adapter", "-X", f"{KEY}=on"]) == 0
    listed = capsys.readouterr().out.splitlines()
    assert any(line.startswith(KEY) for line in listed)
    assert any(line.startswith(PENALTY_KEY) for line in listed)
    help_lines = cli._build_parser(developer=True).format_help().splitlines()
    for key in (KEY, PENALTY_KEY):
        assert any(
            line.startswith(f"  {key} ") and line.endswith("(#373)")
            for line in help_lines
        ), key
    plain = cli._build_parser(developer=False).format_help()
    assert KEY not in plain and PENALTY_KEY not in plain


def test_the_command_is_for_developer_mode(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert memory_cli.main(["adapter", "rebuild"]) == 2
    err = capsys.readouterr().err
    assert 'melo: "memory adapter" needs [ui] mode = "developer"' in err


def test_the_command_says_why_nothing_was_built(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    developer(home, "[memory]\nacross_chats = false\n")
    assert memory_cli.main(["adapter", "rebuild"]) == 1
    assert "Adapter not built: memory across chats is off" in capsys.readouterr().out
    developer(home)
    assert memory_cli.main(["adapter", "rebuild"]) == 1
    assert "Adapter not built: no embedding model is set up" in capsys.readouterr().out


def built(home: Path, **fields: object) -> None:
    values: dict[str, object] = {
        "W": np.eye(8),
        "embed_model": "m",
        "dim": 8,
        "train_hash": "h",
        "built_at": BUILT_AT,
        "pairs": 61,
        "held_out_loss": 0.2,
    }
    adapting.save(
        adapting.path_of(ChatMemory(config.load).directory),
        adapting.Adapter(**{**values, **fields}),  # type: ignore[arg-type]
    )


def status_rows(capsys: pytest.CaptureFixture[str]) -> dict[str, str]:
    assert memory_cli.main(["status"]) == 0
    rows = (line.split(None, 1) for line in capsys.readouterr().out.splitlines())
    return {row[0]: row[1] if len(row) > 1 else "" for row in rows}


def test_status_has_no_row_until_it_is_on_or_built(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert "adapter" not in status_rows(capsys)
    developer(home, '[memory]\nadapter = "on"\n')
    assert status_rows(capsys)["adapter"] == "on, none built"


def test_status_says_what_is_built_and_whether_searches_use_it(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    built(home)
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(BUILT_AT))
    assert status_rows(capsys)["adapter"] == f"off, 61 pairs, built {stamp}"
    # On, but no embedding model matches the one it was built for.
    developer(home, '[memory]\nadapter = "on"\n')
    assert (
        status_rows(capsys)["adapter"]
        == f"on, but built for another embedding model, 61 pairs, built {stamp}"
    )
