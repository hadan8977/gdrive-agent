"""配置加载。单一来源 /etc/gdrive-agent/config.toml。"""
from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path

CONFIG_PATH = Path("/etc/gdrive-agent/config.toml")


@lru_cache(maxsize=1)
def load() -> dict:
    with CONFIG_PATH.open("rb") as fh:
        cfg = tomllib.load(fh)
    token_file = Path(cfg["rc"]["token_file"])
    cfg["rc"]["token"] = token_file.read_text().strip() if token_file.exists() else ""
    return cfg


def rc_url() -> str:
    return f"http://{load()['rc']['addr']}"
