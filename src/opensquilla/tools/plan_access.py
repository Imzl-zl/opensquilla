"""Compatibility seam: Plan intent uses ordinary tool authorization."""

from __future__ import annotations

from opensquilla.tool_boundary import ToolCall, ToolResult
from opensquilla.tools.types import RegisteredTool, ToolContext, ToolSpec


def is_plan_mode(ctx: ToolContext | None) -> bool:
    if ctx is None:
        return False
    mode = getattr(ctx, "collaboration_mode", "default")
    return getattr(mode, "value", mode) == "plan"


def plan_access_allows(spec: ToolSpec, ctx: ToolContext | None) -> bool:
    """Legacy metadata cannot override ordinary tool authorization."""
    return True


def preflight_plan_access(
    tool_call: ToolCall,
    registered: RegisteredTool,
    ctx: ToolContext | None,
) -> ToolResult | None:
    """Normal dispatch enforces permissions, approval and sandbox policy."""
    return None
