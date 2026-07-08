"""Application settings — JSON-backed persistent configuration.

Settings are stored in ``~/.atpg_debug_agent/settings.json`` and loaded
automatically when an :class:`AppSettings` instance is created.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_DIR = Path.home() / ".atpg_debug_agent"
_DEFAULT_CONFIG_FILE = _DEFAULT_CONFIG_DIR / "settings.json"


@dataclass
class AppSettings:
    """All persisted application settings in one container.

    Attributes:
        last_netlist:       Last used netlist path.
        last_faults:        Last used fault-list path.
        last_constraints:   Last used constraint file path.
        last_output_dir:    Last used output directory.
        skills:             Per-skill configuration (enabled + params).
        window_geometry:    Saved window geometry bytes (base64 string).
        window_state:       Saved window state bytes (base64 string).
        filter_text:        Last filter text in the table view.
        class_filter:       Last class filter selection.
        conf_filter:        Last confidence filter selection.
    """

    last_netlist: str = ""
    last_faults: str = ""
    last_constraints: str = ""
    last_output_dir: str = ""
    skills: Dict[str, Any] = field(default_factory=dict)
    window_geometry: str = ""
    window_state: str = ""
    filter_text: str = ""
    class_filter: str = "all"
    conf_filter: str = "all"
    #: AI Debug Agent LLM connection settings (API key intentionally excluded).
    agent: Dict[str, Any] = field(default_factory=dict)
    #: Last used custom-skills directory.
    custom_skills_dir: str = ""

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "AppSettings":
        """Load settings from *path* (default: ``~/.atpg_debug_agent/settings.json``).

        Returns default settings if the file does not exist or is corrupt.
        """
        config_path = path or _DEFAULT_CONFIG_FILE
        if not config_path.is_file():
            logger.debug("No settings file at %s — using defaults", config_path)
            return cls()
        try:
            with open(config_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            inst = cls()
            for key, val in data.items():
                if hasattr(inst, key):
                    setattr(inst, key, val)
            logger.debug("Settings loaded from %s", config_path)
            return inst
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load settings from %s: %s — using defaults",
                           config_path, exc)
            return cls()

    def save(self, path: Optional[Path] = None) -> None:
        """Persist settings to *path* (default: ``~/.atpg_debug_agent/settings.json``)."""
        config_path = path or _DEFAULT_CONFIG_FILE
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(config_path, "w", encoding="utf-8") as fh:
                json.dump(asdict(self), fh, indent=2)
            logger.debug("Settings saved to %s", config_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not save settings to %s: %s", config_path, exc)

    def update_skills(self, skill_cfg: Dict[str, Any]) -> None:
        """Replace the skills sub-config with *skill_cfg*."""
        self.skills = dict(skill_cfg)
