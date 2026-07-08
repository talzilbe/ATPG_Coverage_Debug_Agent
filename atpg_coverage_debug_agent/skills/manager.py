"""Skill manager — orchestrates skill lifecycle and execution."""

from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

from .base import AnalysisContext, SkillBase, SkillResult
from .registry import SkillRegistry

logger = logging.getLogger(__name__)


class SkillManager:
    """Manages skill instances, configuration, and execution pipeline.

    Usage::

        mgr = SkillManager()
        mgr.load_from_config(saved_cfg)   # optional — restore saved state
        results = mgr.run_all(ctx)
    """

    def __init__(self) -> None:
        self._registry = SkillRegistry()
        self._registry.discover()
        self._skills: List[SkillBase] = self._registry.instantiate_all()
        logger.info("SkillManager: loaded %d skill(s)", len(self._skills))

    # -- skill access --------------------------------------------------------

    @property
    def skills(self) -> List[SkillBase]:
        """All skill instances in registration order."""
        return list(self._skills)

    def get(self, skill_id: str) -> Optional[SkillBase]:
        """Return the skill with *skill_id*, or ``None``."""
        for s in self._skills:
            if s.skill_id == skill_id:
                return s
        return None

    def load_custom_skills(self, directory: str) -> List[str]:
        """Import custom skills from *directory* and add new ones.

        Returns the list of newly added skill_ids (skills already present are
        not duplicated).
        """
        added_ids = self._registry.discover_custom(directory)
        existing = {s.skill_id for s in self._skills}
        new_ids: List[str] = []
        for skill_id in added_ids:
            if skill_id in existing:
                continue
            cls = self._registry.get(skill_id)
            if cls is None:
                continue
            try:
                self._skills.append(cls())
                new_ids.append(skill_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not instantiate custom skill %s: %s",
                               skill_id, exc)
        if new_ids:
            logger.info("SkillManager: added %d custom skill(s): %s",
                        len(new_ids), ", ".join(new_ids))
        return new_ids

    def enabled_skills(self) -> List[SkillBase]:
        return [s for s in self._skills if s.enabled]

    # -- bulk enable/disable -------------------------------------------------

    def enable_all(self) -> None:
        for s in self._skills:
            s.enabled = True

    def disable_all(self) -> None:
        for s in self._skills:
            s.enabled = False

    def reset_defaults(self) -> None:
        for s in self._skills:
            s.reset_defaults()

    # -- execution -----------------------------------------------------------

    def run_all(
        self,
        ctx: AnalysisContext,
        progress: Optional[Callable[[str], None]] = None,
    ) -> List[SkillResult]:
        """Run all enabled skills against *ctx*.

        Each skill is isolated — an exception in one does not prevent others
        from running.

        Args:
            ctx:      Analysis context to pass to each skill.
            progress: Optional callback ``(message)`` for progress reporting.

        Returns:
            List of :class:`SkillResult` objects, one per enabled skill.
        """
        results: List[SkillResult] = []
        enabled = self.enabled_skills()
        for skill in enabled:
            if progress:
                progress(f"Running skill: {skill.display_name}")
            try:
                result = skill.run(ctx)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Skill '%s' raised an exception", skill.skill_id)
                result = SkillResult(skill_id=skill.skill_id, success=False,
                                     summary=f"Skill crashed: {exc}")
                result.add_error(str(exc))
            results.append(result)
            logger.info("Skill '%s' finished: %d finding(s), %d warning(s)",
                        skill.skill_id,
                        len(result.findings),
                        len(result.warnings))
        return results

    # -- config persistence --------------------------------------------------

    def to_config(self) -> Dict[str, dict]:
        """Serialise all skill settings to a plain dict."""
        return {s.skill_id: s.to_config() for s in self._skills}

    def from_config(self, cfg: Dict[str, dict]) -> None:
        """Restore skill settings from a saved config dict."""
        for skill in self._skills:
            if skill.skill_id in cfg:
                try:
                    skill.from_config(cfg[skill.skill_id])
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Could not restore config for skill '%s': %s",
                                   skill.skill_id, exc)
