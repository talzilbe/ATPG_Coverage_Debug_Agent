"""Abstract base classes and typed models for the Skills framework."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Typed models
# ---------------------------------------------------------------------------

@dataclass
class SkillMessage:
    """A single message produced by a skill during execution.

    Attributes:
        level: ``info``, ``warning``, or ``error``.
        text:  Human-readable message body.
    """

    level: str  # 'info' | 'warning' | 'error'
    text: str

    def __str__(self) -> str:
        return f"[{self.level.upper()}] {self.text}"


@dataclass
class SkillFinding:
    """A structured finding emitted by a skill."""

    title: str
    description: str
    evidence: List[str] = field(default_factory=list)
    affected_objects: List[str] = field(default_factory=list)
    confidence: str = "medium"  # 'high' | 'medium' | 'low'
    recommendation: str = ""


@dataclass
class SkillResult:
    """Complete result from one skill execution.

    Attributes:
        skill_id:  Unique identifier of the skill that produced this result.
        messages:  Log/warning messages emitted during execution.
        findings:  Structured :class:`SkillFinding` objects.
        summary:   One-sentence human-readable summary.
        success:   ``False`` if the skill raised an exception.
    """

    skill_id: str
    messages: List[SkillMessage] = field(default_factory=list)
    findings: List[SkillFinding] = field(default_factory=list)
    summary: str = ""
    success: bool = True

    def add_info(self, text: str) -> None:
        self.messages.append(SkillMessage("info", text))

    def add_warning(self, text: str) -> None:
        self.messages.append(SkillMessage("warning", text))

    def add_error(self, text: str) -> None:
        self.messages.append(SkillMessage("error", text))

    def add_finding(self, **kwargs) -> SkillFinding:
        f = SkillFinding(**kwargs)
        self.findings.append(f)
        return f

    @property
    def warnings(self) -> List[SkillMessage]:
        return [m for m in self.messages if m.level in ("warning", "error")]


@dataclass
class AnalysisContext:
    """All data available to a skill when it executes.

    Skills receive a read-only view of the parsed artefacts and the
    preliminary core-analysis results.
    """

    netlist: Any          # VerilogNetlist
    faults: Any           # List[FaultRecord]
    constraints: Any      # List[ConstraintRecord]
    fault_results: Any    # List[FaultAnalysisResult]
    pattern_groups: Any   # List[PatternGroup]
    summary: Any          # AnalysisSummary
    #: Optional serialised adjacency used when ``netlist`` is unavailable
    #: (e.g. after loading a saved report) so path tracing still works.
    adjacency: Any = None
    #: Optional baseline report payload for regression tools (or ``None``).
    compare: Any = None


# ---------------------------------------------------------------------------
# Abstract skill base
# ---------------------------------------------------------------------------

class SkillBase(ABC):
    """Abstract base class every skill must inherit from.

    Subclasses must set ``skill_id``, ``display_name``, and ``description``
    as class attributes and implement :meth:`run`.
    """

    #: Unique snake_case identifier used as config key.
    skill_id: str = ""
    #: Human-readable name shown in the Skills panel.
    display_name: str = ""
    #: Short description (one sentence).
    description: str = ""
    #: Whether the skill is enabled by default.
    default_enabled: bool = True
    #: On-demand tools take arguments and answer targeted questions; they are
    #: exposed to the agent as callable tools but skipped in the bulk
    #: "run all skills" pass (where they would run with empty arguments).
    on_demand: bool = False

    def __init__(self) -> None:
        self._params: Dict[str, Any] = {}
        self._enabled: bool = self.default_enabled

    # -- configuration -------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = bool(value)

    def parameters_schema(self) -> Dict[str, Dict[str, Any]]:
        """Return a schema dict describing configurable parameters.

        Returns a dict like::

            {
                "min_fanout": {
                    "type": "int",
                    "default": 10,
                    "description": "Minimum fan-out to flag",
                },
            }

        Override in subclasses that expose configurable knobs.
        The default implementation returns an empty dict (no parameters).
        """
        return {}

    def get_param(self, name: str) -> Any:
        """Return the current value of parameter *name*."""
        schema = self.parameters_schema()
        if name not in schema:
            raise KeyError(f"Unknown parameter '{name}' for skill '{self.skill_id}'")
        return self._params.get(name, schema[name]["default"])

    def set_param(self, name: str, value: Any) -> None:
        """Set parameter *name* to *value*."""
        schema = self.parameters_schema()
        if name not in schema:
            raise KeyError(f"Unknown parameter '{name}' for skill '{self.skill_id}'")
        self._params[name] = value

    def reset_defaults(self) -> None:
        """Reset all parameters to their schema defaults."""
        self._params.clear()
        self._enabled = self.default_enabled

    def to_config(self) -> Dict[str, Any]:
        """Serialise current state to a plain dict for JSON persistence."""
        return {
            "enabled": self._enabled,
            "params": {k: self.get_param(k) for k in self.parameters_schema()},
        }

    def from_config(self, cfg: Dict[str, Any]) -> None:
        """Restore state from a plain dict loaded from JSON."""
        if "enabled" in cfg:
            self._enabled = bool(cfg["enabled"])
        for k, v in cfg.get("params", {}).items():
            try:
                self.set_param(k, v)
            except KeyError:
                logger.warning("Skill %s: unknown param '%s' in saved config",
                               self.skill_id, k)

    # -- tool exposure (agentic mode) ----------------------------------------

    def to_tool_schema(self) -> Dict[str, Any]:
        """Return an OpenAI-compatible tool (function) schema for this skill.

        The schema exposes the skill as a callable tool so an LLM operating in
        agentic mode can decide to invoke it. Configurable parameters from
        :meth:`parameters_schema` become optional function arguments; the LLM
        may omit them to use each skill's defaults.
        """
        _JSON_TYPES = {
            "int": "integer",
            "float": "number",
            "bool": "boolean",
            "str": "string",
        }
        properties: Dict[str, Any] = {}
        for name, spec in self.parameters_schema().items():
            json_type = _JSON_TYPES.get(spec.get("type", "str"), "string")
            prop: Dict[str, Any] = {
                "type": json_type,
                "description": spec.get("description", ""),
            }
            if "default" in spec:
                prop["description"] += f" (default: {spec['default']})"
            properties[name] = prop
        return {
            "type": "function",
            "function": {
                "name": self.skill_id,
                "description": (self.description or self.display_name
                               or self.skill_id),
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    # All params are optional — skills have sensible defaults.
                    "required": [],
                },
            },
        }

    # -- execution -----------------------------------------------------------

    @abstractmethod
    def run(self, ctx: AnalysisContext) -> SkillResult:
        """Execute the skill against *ctx* and return a :class:`SkillResult`.

        Implementations must:

        * never raise exceptions — catch all errors and add them to the result;
        * return a valid :class:`SkillResult` even if nothing was found;
        * not modify *ctx* in place.
        """
