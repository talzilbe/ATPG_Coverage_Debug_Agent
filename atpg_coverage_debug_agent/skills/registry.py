"""Skill registry — discovers and tracks all available built-in skills."""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import os
import pkgutil
import sys
from typing import Dict, List, Optional, Type

from .base import SkillBase

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level registry dict populated by automatic discovery
# ---------------------------------------------------------------------------
_REGISTRY: Dict[str, Type[SkillBase]] = {}


def register(cls: Type[SkillBase]) -> Type[SkillBase]:
    """Class decorator that registers a skill in the global registry."""
    if not cls.skill_id:
        logger.warning("Skill class %s has no skill_id — skipping", cls.__name__)
        return cls
    _REGISTRY[cls.skill_id] = cls
    logger.debug("Registered skill: %s", cls.skill_id)
    return cls


class SkillRegistry:
    """Provides access to all registered :class:`SkillBase` subclasses.

    Call :meth:`discover` once at startup to auto-import all built-in skill
    modules and populate the registry.
    """

    def __init__(self) -> None:
        self._discovered = False

    def discover(self) -> None:
        """Auto-import every module in the ``skills`` package."""
        if self._discovered:
            return
        import atpg_coverage_debug_agent.skills as skills_pkg
        pkg_path = skills_pkg.__path__
        pkg_name = skills_pkg.__name__
        for _finder, module_name, _ispkg in pkgutil.iter_modules(pkg_path):
            full_name = f"{pkg_name}.{module_name}"
            if module_name in ("base", "registry", "manager"):
                continue
            try:
                importlib.import_module(full_name)
                logger.debug("Discovered skill module: %s", full_name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not import skill module %s: %s",
                               full_name, exc)
        self._discovered = True

    def discover_custom(self, directory: str) -> List[str]:
        """Load custom skills from *directory* (``*.py`` and ``*.md`` files).

        ``.py`` files are imported and any :class:`SkillBase` subclass found is
        registered (whether or not it used the ``@register`` decorator).
        ``.md`` files are wrapped as Markdown guidance skills. Returns the list
        of newly registered ``skill_id`` values.

        Args:
            directory: Filesystem path containing custom skill ``.py``/``.md`` files.

        Returns:
            List of skill_ids that were added by this call.
        """
        added: List[str] = []
        if not directory or not os.path.isdir(directory):
            logger.warning("Custom skills directory not found: %s", directory)
            return added

        before = set(_REGISTRY.keys())
        for fname in sorted(os.listdir(directory)):
            if fname.startswith("_"):
                continue
            full_path = os.path.join(directory, fname)
            if fname.endswith(".py"):
                self._load_python_skill_file(full_path, fname)
            elif fname.endswith(".md"):
                self._load_markdown_skill_file(full_path)

        added = [sid for sid in _REGISTRY if sid not in before]
        if added:
            logger.info("Loaded %d custom skill(s) from %s: %s",
                        len(added), directory, ", ".join(added))
        return added

    def _load_python_skill_file(self, full_path: str, fname: str) -> None:
        """Import a single ``.py`` file and register its SkillBase subclasses."""
        mod_name = f"atpg_custom_skill_{os.path.splitext(fname)[0]}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, full_path)
            if spec is None or spec.loader is None:
                return
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not import custom skill file %s: %s",
                           full_path, exc)
            return

        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if (issubclass(obj, SkillBase)
                    and obj is not SkillBase
                    and obj.__module__ == mod_name
                    and getattr(obj, "skill_id", "")):
                register(obj)

    def _load_markdown_skill_file(self, full_path: str) -> None:
        """Wrap a single ``.md`` file as a Markdown guidance skill."""
        from .markdown_skill import make_markdown_skill_class
        try:
            cls = make_markdown_skill_class(full_path)
            register(cls)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load Markdown skill %s: %s",
                           full_path, exc)

    def all_classes(self) -> List[Type[SkillBase]]:
        """Return all registered skill classes in registration order."""
        return list(_REGISTRY.values())

    def get(self, skill_id: str) -> Optional[Type[SkillBase]]:
        """Return the class for *skill_id*, or ``None`` if not found."""
        return _REGISTRY.get(skill_id)

    def instantiate_all(self) -> List[SkillBase]:
        """Create one instance of every registered skill class."""
        instances = []
        for cls in self.all_classes():
            try:
                instances.append(cls())
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not instantiate skill %s: %s",
                               cls.skill_id, exc)
        return instances
