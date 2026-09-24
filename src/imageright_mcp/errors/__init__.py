"""IR error catalog (plan §6): registry, ErrorMapper and error explanations."""

from imageright_mcp.errors.mapper import ErrorContext, ErrorMapper, parse_soap_fault
from imageright_mcp.errors.registry import Registry, get_registry, load_registry, render

__all__ = [
    "ErrorContext",
    "ErrorMapper",
    "Registry",
    "get_registry",
    "load_registry",
    "parse_soap_fault",
    "render",
]
