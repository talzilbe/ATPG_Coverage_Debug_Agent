"""Pickle cache for parsed netlists, and the netlist hand-off to the agent.

Two problems share one artefact:

* Every analysis run, every partition and every agent launch re-parsed the
  netlist from source, although the file had not changed.
* The out-of-process MCP server never received the parsed design at all, so
  the tools the agent could call answered from recorded evidence only and the
  richest analysis machinery (driver resolution, cone tracing, structural
  profiling) was out of its reach.

A parsed netlist is plain dataclasses, so it pickles. The cache key is the
source file's identity (absolute path, size, mtime) plus the parser module's
own identity, so a parser change invalidates every cached design without a
manual flush. Loading a wrong or damaged pickle is never fatal: the caller
falls back to parsing.

Only files this tool wrote itself are ever unpickled, from a directory it
created with owner-only permissions. Nothing user-supplied is unpickled.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pickle
import tempfile
import time
from typing import Any, Optional, Tuple

from .. import session
from . import verilog_parser

logger = logging.getLogger(__name__)

#: Bump when the pickled shape changes in a way ``fingerprint`` cannot see.
CACHE_VERSION = 1

#: Set to ``0`` to bypass the cache (always parse, never write).
CACHE_ENV = "ATPG_NETLIST_CACHE"

#: Names the pickle the MCP server should load. Set by the parent process.
NETLIST_FILE_ENV = "ATPG_NETLIST_FILE"

#: Cache folder under the session root, so one ``rm -rf`` clears everything.
CACHE_DIR_NAME = "netlist_cache"

_PROTOCOL = pickle.HIGHEST_PROTOCOL


def enabled() -> bool:
    return os.environ.get(CACHE_ENV, "1").strip() not in ("0", "false", "no")


def cache_dir() -> str:
    """Create and return the cache directory (owner-only)."""
    root = os.path.join(tempfile.gettempdir(), session.ROOT_NAME, CACHE_DIR_NAME)
    os.makedirs(root, mode=0o700, exist_ok=True)
    return root


def _module_identity(module: Any) -> str:
    path = getattr(module, "__file__", "") or ""
    try:
        st = os.stat(path)
        return f"{path}:{st.st_size}:{st.st_mtime_ns}"
    except OSError:
        return path


def fingerprint(source_path: str) -> str:
    """Identity of *source_path* as parsed by the current parser."""
    st = os.stat(source_path)
    material = "|".join([
        os.path.abspath(source_path),
        str(st.st_size),
        str(st.st_mtime_ns),
        _module_identity(verilog_parser),
        str(CACHE_VERSION),
    ])
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:20]


def cache_path(source_path: str) -> str:
    """Where the pickle for *source_path* lives (whether or not it exists)."""
    base = session.safe_name(os.path.basename(source_path), "netlist")
    return os.path.join(cache_dir(), f"{base}_{fingerprint(source_path)}.pkl")


def dump(netlist: Any, path: str) -> str:
    """Pickle *netlist* to *path* atomically (owner-readable only)."""
    os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "wb") as fh:
        pickle.dump(netlist, fh, protocol=_PROTOCOL)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def load(path: str) -> Optional[Any]:
    """Unpickle a netlist this tool wrote earlier, or ``None`` on any failure."""
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as fh:
            netlist = pickle.load(fh)
    except Exception as exc:  # noqa: BLE001 - a bad cache entry is not fatal
        logger.warning("Ignoring unreadable netlist pickle %s: %s", path, exc)
        return None
    if not hasattr(netlist, "modules"):
        logger.warning("Ignoring netlist pickle %s: not a netlist", path)
        return None
    return netlist


def load_or_parse(source_path: str,
                  use_cache: Optional[bool] = None) -> Tuple[Any, str]:
    """Return ``(netlist, origin)`` for *source_path*.

    *origin* is ``"cache"`` when the pickle was reused, ``"parsed"`` when the
    file was parsed (and, if caching is on, written to the cache) and
    ``"parsed_uncached"`` when the cache is disabled or unwritable.
    """
    if use_cache is None:
        use_cache = enabled()
    target = ""
    if use_cache:
        try:
            target = cache_path(source_path)
        except OSError:
            target = ""
        netlist = load(target) if target else None
        if netlist is not None:
            logger.info("Netlist %s loaded from cache", source_path)
            return netlist, "cache"

    started = time.monotonic()
    netlist = verilog_parser.parse_verilog_file(source_path)
    logger.info("Netlist %s parsed in %.1fs", source_path,
                time.monotonic() - started)
    if not target:
        return netlist, "parsed_uncached"
    try:
        dump(netlist, target)
    except OSError as exc:
        logger.warning("Netlist cache not written (%s): %s", target, exc)
        return netlist, "parsed_uncached"
    return netlist, "parsed"


def cached_path_if_present(source_path: Optional[str]) -> Optional[str]:
    """The existing cache pickle for *source_path*, or ``None``."""
    if not source_path or not os.path.isfile(source_path):
        return None
    try:
        path = cache_path(source_path)
    except OSError:
        return None
    return path if os.path.isfile(path) else None


def handoff_path(netlist: Any, source_path: Optional[str],
                 work_dir: str) -> Optional[str]:
    """Return a pickle path the MCP server can load for *netlist*.

    Reuses the cache entry when the source file already has one (no second
    copy of a large design); otherwise writes ``netlist.pkl`` into
    *work_dir*. Returns ``None`` when there is no netlist to hand off.
    """
    if netlist is None:
        return None
    cached = cached_path_if_present(source_path)
    if cached:
        return cached
    try:
        return dump(netlist, os.path.join(work_dir, "netlist.pkl"))
    except OSError as exc:
        logger.warning("Netlist hand-off pickle not written: %s", exc)
        return None
