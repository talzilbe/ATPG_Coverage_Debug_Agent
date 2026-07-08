"""Skills plugin system for the ATPG coverage-debug agent.

Skills are optional analysis modules that can be enabled, disabled, and
configured independently.  They run after core parsing/correlation and
contribute additional analysis, warnings, and structured findings to the
final report.
"""

from .base import AnalysisContext, SkillBase, SkillMessage, SkillResult
from .registry import SkillRegistry
from .manager import SkillManager

__all__ = [
    "AnalysisContext",
    "SkillBase",
    "SkillMessage",
    "SkillResult",
    "SkillRegistry",
    "SkillManager",
]
