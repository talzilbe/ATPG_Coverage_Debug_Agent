"""Support for Markdown-defined skills.

A Markdown skill is a ``.md`` guidance document, optionally opening with a
YAML-style front-matter block (``name:`` / ``description:``). It is wrapped in
a dynamically-generated :class:`SkillBase` subclass so it can be enabled in
the Skills tab like any other skill. The agent reads it section by section
through the ``read_guidance`` tool; a single-pass (no-tools) run gets the
whole body once, without the front matter.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Type

from .base import AnalysisContext, SkillBase, SkillResult

#: Prefixed to guidance whenever it reaches the model.
GUIDANCE_NOTE = (
    "Guidance, not evidence: name patterns or cell-prefix tables in it say "
    "where to look. A scan-status or root-cause claim still needs tool "
    "evidence (scan_status, get_fault_detail, trace_path).")

_FRONT_MATTER = re.compile(r"\A\s*---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)


def _sanitize_skill_id(stem: str) -> str:
    """Turn a file stem into a safe snake_case skill_id."""
    sid = re.sub(r"[^0-9a-zA-Z]+", "_", stem).strip("_").lower()
    return sid or "markdown_skill"


def split_front_matter(text: str) -> tuple:
    """Return ``(fields, body)``; *fields* is ``{}`` without front matter."""
    m = _FRONT_MATTER.match(text or "")
    if not m:
        return {}, text or ""
    fields: Dict[str, str] = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() and not key.startswith((" ", "\t")):
            fields[key.strip().lower()] = value.strip().strip("'\"")
    return fields, text[m.end():]


def guidance_sections(text: str) -> List[Dict[str, str]]:
    """Split a guidance body into ``## `` sections (preamble = "Overview")."""
    _fields, body = split_front_matter(text)
    sections: List[Dict[str, str]] = []
    title, buf = "Overview", []
    for line in body.splitlines():
        m = re.match(r"^##\s+(.*\S)\s*$", line)
        if m:
            if "\n".join(buf).strip():
                sections.append({"title": title,
                                 "text": "\n".join(buf).strip()})
            title, buf = m.group(1), []
            continue
        if re.match(r"^#\s+\S", line) and not sections and not "".join(buf).strip():
            continue
        buf.append(line)
    if "\n".join(buf).strip():
        sections.append({"title": title, "text": "\n".join(buf).strip()})
    return sections


def guidance_entry(skill: Any) -> Dict[str, Any]:
    """The serialisable form ``read_guidance`` answers from."""
    content = getattr(skill, "_content", "") or ""
    fields, _body = split_front_matter(content)
    return {
        "skill_id": skill.skill_id,
        "title": getattr(skill, "display_name", "") or skill.skill_id,
        "description": (fields.get("description")
                        or getattr(skill, "description", "")),
        "sections": guidance_sections(content),
    }


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

    fields, body = split_front_matter(content)
    stem = os.path.splitext(os.path.basename(md_path))[0]
    # Keyed on the file name so saved Skills-tab settings stay attached.
    skill_id = _sanitize_skill_id(stem)
    display_name = _extract_title(body, fields.get("name") or stem)
    description = fields.get("description") or _extract_description(body)
    if len(description) > 300:
        description = description[:300] + "…"

    def run(self, ctx: AnalysisContext) -> SkillResult:  # noqa: ARG001
        result = SkillResult(skill_id=self.skill_id)
        _f, text = split_front_matter(self._content)
        result.add_info(
            f"Markdown skill loaded from {os.path.basename(self._source_path)}")
        result.add_finding(
            title=f"Guidance: {self.display_name}",
            description=f"{GUIDANCE_NOTE}\n\n{text.strip()}",
            confidence="guidance",
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
            "guidance": True,
            "_content": content,
            "_source_path": md_path,
            "run": run,
            "__doc__": f"Markdown skill generated from {md_path}.",
        },
    )
    return cls
