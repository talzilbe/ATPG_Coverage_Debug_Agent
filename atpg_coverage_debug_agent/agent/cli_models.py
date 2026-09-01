"""Discover which models the GitHub Copilot CLI can actually use.

The CLI validates ``--model`` against a list it fetches from the service, but
it has no subcommand that prints that list. Its Agent Client Protocol mode
(``copilot --acp``) does: the ``session/new`` response carries
``models.availableModels``. That is the only local, machine-readable source,
and it reuses the CLI's own authentication, so no credential handling is
duplicated here.

Results are cached per user so the GUI can populate the model list instantly
at startup and refresh it in the background.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from ..config.credentials import user_config_dir

logger = logging.getLogger(__name__)

#: Used until a live list arrives — kept deliberately short, since the whole
#: point of this module is that a hard-coded list goes stale.
FALLBACK_MODELS = ["auto"]

_CACHE_FILE = user_config_dir() / "cli_models.json"

#: Give up on a wedged CLI rather than hanging the refresh thread forever.
_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class ModelInfo:
    """One selectable CLI model."""

    model_id: str
    name: str = ""
    description: str = ""

    def label(self) -> str:
        """Human-readable one-liner for a tooltip."""
        return f"{self.model_id} — {self.name}" if self.name else self.model_id


def _reader(stream, sink: "queue.Queue") -> None:
    """Push every line of *stream* into *sink*, then a None sentinel."""
    try:
        for line in stream:
            sink.put(line)
    except Exception:  # noqa: BLE001
        pass
    finally:
        sink.put(None)


def _read_message(lines: "queue.Queue", deadline: float) -> Optional[dict]:
    """Return the next JSON-RPC message, or None on EOF/timeout.

    Reading happens on a helper thread so a CLI that never answers cannot
    wedge the caller on a blocking ``readline``.
    """
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            line = lines.get(timeout=remaining)
        except queue.Empty:
            return None
        if line is None:                      # stdout closed
            return None
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue  # Stray non-protocol output; the CLI logs to stderr too.


def fetch_models(cli_path: str, cli_home: str = "", token: str = "",
                 timeout: float = _TIMEOUT_S) -> List[ModelInfo]:
    """Ask the Copilot CLI which models it can use.

    Returns an empty list if the CLI is missing, unauthenticated, or does not
    answer in time — callers fall back to the cache.
    """
    if not cli_path or not os.path.isfile(cli_path):
        return []

    env = dict(os.environ)
    if cli_home.strip():
        env["COPILOT_HOME"] = cli_home.strip()
    if token.strip():
        env["COPILOT_GITHUB_TOKEN"] = token.strip()

    deadline = time.monotonic() + timeout
    proc = None
    try:
        proc = subprocess.Popen(
            [cli_path, "--acp", "--no-color", "--log-level", "error"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env, text=True, bufsize=1)

        lines: "queue.Queue" = queue.Queue()
        threading.Thread(target=_reader, args=(proc.stdout, lines),
                         daemon=True).start()

        def send(payload: dict) -> None:
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": 1, "clientCapabilities": {}}})
        send({"jsonrpc": "2.0", "id": 2, "method": "session/new",
              "params": {"cwd": os.getcwd(), "mcpServers": []}})

        while True:
            msg = _read_message(lines, deadline)
            if msg is None:
                logger.debug("No model list from the Copilot CLI (EOF/timeout)")
                return []
            if msg.get("id") != 2:
                continue
            if "error" in msg:
                logger.debug("Copilot CLI refused session/new: %s",
                             msg["error"])
                return []
            available = ((msg.get("result") or {}).get("models") or {}
                         ).get("availableModels") or []
            return [
                ModelInfo(model_id=str(m.get("modelId") or "").strip(),
                          name=str(m.get("name") or "").strip(),
                          description=str(m.get("description") or "").strip())
                for m in available
                if str(m.get("modelId") or "").strip()
            ]
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not query the Copilot CLI for models: %s", exc)
        return []
    finally:
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass


# -- cache -------------------------------------------------------------------

def load_cached_models(path: Optional[Path] = None) -> List[ModelInfo]:
    """Return the last fetched model list, or an empty list."""
    cache = path or _CACHE_FILE
    try:
        with open(cache, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return [ModelInfo(model_id=m["model_id"], name=m.get("name", ""),
                          description=m.get("description", ""))
                for m in data.get("models", []) if m.get("model_id")]
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001
        logger.debug("Ignoring unreadable model cache %s: %s", cache, exc)
        return []


def save_cached_models(models: List[ModelInfo],
                       path: Optional[Path] = None) -> None:
    """Persist *models* so the next launch can show them immediately."""
    if not models:
        return
    cache = path or _CACHE_FILE
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fetched_at": time.time(),
            "models": [{"model_id": m.model_id, "name": m.name,
                        "description": m.description} for m in models],
        }
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not write the model cache %s: %s", cache, exc)


def model_ids(models: List[ModelInfo]) -> List[str]:
    """Return the ids of *models*, with ``auto`` guaranteed to lead the list."""
    ids = [m.model_id for m in models]
    if "auto" in ids:
        ids.remove("auto")
    return ["auto"] + ids
