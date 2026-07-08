"""LLM-backed ATPG/DFT coverage debug agent.

This package wraps a strict, evidence-driven system prompt and a small,
dependency-free OpenAI-compatible client so the structural analysis results
can be handed to a large language model for narrative root-cause reasoning.

If no LLM endpoint is configured the agent still produces the fully-assembled
prompt so the user can paste it into any chat model manually.
"""

from .debug_agent import (
    SYSTEM_PROMPT,
    AgentConfig,
    DebugAgent,
    build_user_payload,
)

__all__ = [
    "SYSTEM_PROMPT",
    "AgentConfig",
    "DebugAgent",
    "build_user_payload",
]
