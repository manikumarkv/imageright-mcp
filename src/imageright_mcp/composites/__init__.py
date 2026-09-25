"""Phase-2 composite tools: the flows in annotations/flows.yaml that carry a ``composite`` name,
run step by step through the shared client."""

from imageright_mcp.composites.tools import register_composite_tools

__all__ = ["register_composite_tools"]
