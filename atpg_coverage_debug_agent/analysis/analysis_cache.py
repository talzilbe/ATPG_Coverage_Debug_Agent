"""Reuse the expensive, constraint-independent results of an earlier run.

Mapping a fault object onto the netlist, finding the constant driver above a
site and profiling its fan-out cone depend only on the netlist, the analyzer
code and the analysis configuration -- never on the constraint file. Editing
constraints and re-running is the common debug loop, so those results are
kept per netlist and handed back on the next run instead of being recomputed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import pickle
from typing import Any, Dict, Optional

from ..parser import netlist_cache

logger = logging.getLogger(__name__)

CACHE_VERSION = 1


def _code_identity() -> str:
    from . import attribution, connectivity, mapper, reachability
    return "|".join(netlist_cache._module_identity(m)
                    for m in (attribution, connectivity, mapper, reachability))


class AnalysisCache:
    """Per-netlist memo of mappings, tie drivers and site profiles."""

    def __init__(self, path: str = "") -> None:
        self.path = path
        self.mappings: Dict[str, Any] = {}
        self.ties: Dict[str, Any] = {}
        self.profiles: Dict[str, Any] = {}
        self.hits = 0
        self.misses = 0
        self.loaded = False

    @classmethod
    def for_netlist(cls, netlist_path: str, config: Any = None
                    ) -> Optional["AnalysisCache"]:
        """The cache for this netlist + code + config, loaded if present."""
        if not netlist_cache.enabled() or not netlist_path:
            return None
        try:
            base = netlist_cache.cache_path(netlist_path)
        except OSError:
            return None
        patterns = {}
        if config is not None and hasattr(config, "documented_patterns"):
            patterns = config.documented_patterns()
        material = "|".join([
            os.path.basename(base), _code_identity(), str(CACHE_VERSION),
            json.dumps(patterns, sort_keys=True, default=str)])
        digest = hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]
        cache = cls(base[:-4] + f".analysis_{digest}.pkl")
        cache.load()
        return cache

    def load(self) -> None:
        if not self.path or not os.path.isfile(self.path):
            return
        try:
            with open(self.path, "rb") as fh:
                data = pickle.load(fh)
        except Exception as exc:  # noqa: BLE001 - a bad cache is never fatal
            logger.warning("Ignoring unreadable analysis cache %s: %s",
                           self.path, exc)
            return
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            return
        self.mappings = dict(data.get("mappings") or {})
        self.ties = dict(data.get("ties") or {})
        self.profiles = dict(data.get("profiles") or {})
        self.loaded = True

    def save(self) -> None:
        if not self.path:
            return
        data = {"version": CACHE_VERSION, "mappings": self.mappings,
                "ties": self.ties, "profiles": self.profiles}
        tmp = f"{self.path}.{os.getpid()}.tmp"
        try:
            with open(tmp, "wb") as fh:
                pickle.dump(data, fh, protocol=pickle.HIGHEST_PROTOCOL)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.warning("Analysis cache not written (%s): %s", self.path, exc)

    def mapping(self, fault_object: str) -> Optional[Any]:
        found = self.mappings.get(fault_object)
        if found is None:
            self.misses += 1
            return None
        self.hits += 1
        return copy.copy(found)

    def store_mapping(self, fault_object: str, result: Any) -> None:
        self.mappings[fault_object] = copy.copy(result)

    def seed(self, attributor: Any = None, profiler: Any = None) -> None:
        """Hand earlier tie drivers and site profiles to fresh tracers."""
        if attributor is not None:
            attributor._tie_cache.update(self.ties)
        if profiler is not None:
            profiler._cache.update(self.profiles)
            profiler.attributor._tie_cache.update(self.ties)

    def collect(self, attributor: Any = None, profiler: Any = None) -> None:
        """Keep what the tracers computed this run for the next one."""
        if attributor is not None:
            self.ties.update(attributor._tie_cache)
        if profiler is not None:
            self.profiles.update(profiler._cache)
            self.ties.update(profiler.attributor._tie_cache)

    def stats(self) -> Dict[str, Any]:
        return {"reused_mappings": self.hits, "new_mappings": self.misses,
                "cached_profiles": len(self.profiles),
                "cached_tie_drivers": len(self.ties), "path": self.path}
