"""Local GUI preferences (JSON in the user config directory). Per-source settings keyed by path."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir

from framesift import APP_ID

DEFAULTS: dict[str, Any] = {
    "language": "system",
    "cache_limit_gb": 2.0,
    "workers": 0,
    "low_priority": False,
    "recent": [],
    "sources": {},
}


class Prefs:
    def __init__(self, path: Path | None = None):
        base = Path(
            os.environ.get("FRAMESIFT_CONFIG_DIR") or user_config_dir(APP_ID, appauthor=False)
        )
        self.path = path or base / "prefs.json"
        self.data: dict[str, Any] = dict(DEFAULTS)
        try:
            self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()

    def source_settings(self, source: str) -> dict[str, Any]:
        return dict(self.data.setdefault("sources", {}).get(source, {}))

    def set_source_settings(self, source: str, **values: Any) -> None:
        entry = self.data.setdefault("sources", {}).setdefault(source, {})
        entry.update(values)
        recent = [s for s in self.data.get("recent", []) if s != source]
        self.data["recent"] = [source, *recent][:10]
        self.save()
