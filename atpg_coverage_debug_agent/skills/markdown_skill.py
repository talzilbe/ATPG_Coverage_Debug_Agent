"""Support for Markdown-defined skills.

A Markdown skill is simply a ``.md`` file containing guidance / instructions.
It is wrapped in a dynamically-generated :class:`SkillBase` subclass so it can
be enabled in the Skills tab like any other skill. When run, it surfaces its
content as a finding (which also flows into the AI Debug Agent payload).
"""

from __future__ import annotations

import os
import re
from typing import Type

from .base import AnalysisContext, SkillBase, SkillResult


def _sanitize_skill_id(stem: str) -> str:
    """Turn a file stem into a safe snake_case skill_id."""
    sid = re.sub(r"[^0-9a-zA-Z]+", "_", stem).strip("_").lower()
    return sid or "markdown_skill"


def _extract_title(text: str, fallback: str) -> str:
    """Return the first Markdown ``# heading`` or *fallback*."""
    for line in text.splitlines():
        m = re.match(r"^\s*#\s+(.*\S)", line)
        if m:
            return m.group(1).strip()
    return fallback


def _extract_description(text: str) -> str:
    """Return the first non-heading, non-empty line (truncated)."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return (stripped[:160] + "…") if len(stripped) > 160 else stripped
    return "Markdown guidance skill."


def make_markdown_skill_class(md_path: str) -> Type[SkillBase]:
    """Build a :class:`SkillBase` subclass from a Markdown file.

    Args:
        md_path: Path to the ``.md`` file.

    Returns:
        A dynamically-created ``SkillBase`` subclass.
    """
    with open(md_path, "r", encoding="utf-8") as fh:
        content = fh.read()

    stem = os.path.splitext(os.path.basename(md_path))[0]
    skill_id = _sanitize_skill_id(stem)
    display_name = _extract_title(content, stem)
    description = _extract_description(content)

    def run(self, ctx: AnalysisContext) -> SkillResult:  # noqa: ARG001
        result = SkillResult(skill_id=self.skill_id)
        result.add_info(
            f"Markdown skill loaded from {os.path.basename(self._source_path)}")
        result.add_finding(
            title=self.display_name,
            description=self._content,
            confidence="medium",
            recommendation="Apply this guidance during manual coverage debug.",
        )
        result.summary = f"Markdown guidance: {self.display_name}"
        return result

    cls = type(
        f"MarkdownSkill_{skill_id}",
        (SkillBase,),
        {
            "skill_id": skill_id,
            "display_name": display_name,
            "description": description,
            "default_enabled": True,
            "_content": content,
            "_source_path": md_path,
            "run": run,
            "__doc__": f"Markdown skill generated from {md_path}.",
        },
    )
    return cls
