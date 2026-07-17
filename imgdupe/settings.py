from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path


DEFAULT_SETTINGS_PATH = Path(__file__).resolve().parent.parent / "settings.ini"


@dataclass(frozen=True)
class AppSettings:
    thumbnail_cache_limit: int = 1000
    disable_advanced_options: bool = False


def load_settings(path: Path = DEFAULT_SETTINGS_PATH) -> AppSettings:
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    try:
        cache_limit = parser.getint("web", "thumbnail_cache_limit", fallback=1000)
    except ValueError:
        cache_limit = 1000
    try:
        disable_advanced = parser.getboolean(
            "web",
            "disable_advanced_options",
            fallback=False,
        )
    except ValueError:
        disable_advanced = False
    return AppSettings(
        thumbnail_cache_limit=max(1, cache_limit),
        disable_advanced_options=disable_advanced,
    )
