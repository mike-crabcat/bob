"""Tool definitions for LLM function calling.

Usage:
    @tool
    async def create_task(title: str, project_id: str, priority: str = "medium") -> str:
        \"\"\"Create a new task in a project.\"\"\"
        ...

The @tool decorator auto-generates an OpenAI-compatible JSON schema from
type hints and docstring. The decorated function becomes a Tool instance.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Optional, Union, get_args, get_origin, get_type_hints

logger = logging.getLogger(__name__)

# Python type -> JSON schema type mapping
_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


def _python_type_to_schema(py_type: Any) -> dict[str, Any]:
    """Convert a Python type annotation to a JSON schema dict."""
    # Handle basic types
    if py_type in _TYPE_MAP:
        return {"type": _TYPE_MAP[py_type]}

    origin = get_origin(py_type)
    args = get_args(py_type)

    # Literal["a", "b"] -> {"type": "string", "enum": ["a", "b"]}
    if origin is Literal:
        return {"type": "string" if isinstance(args[0], str) else "integer", "enum": list(args)}

    # Optional[X] -> just X (optionality handled via required list)
    if origin is Union and type(None) in args:
        inner = [a for a in args if a is not type(None)]
        if inner:
            return _python_type_to_schema(inner[0])
        return {"type": "string"}

    # list[X] -> {"type": "array", "items": ...}
    if origin is list:
        if args:
            return {"type": "array", "items": _python_type_to_schema(args[0])}
        return {"type": "array"}

    return {"type": "string"}


@dataclass
class Tool:
    """An LLM-callable tool with schema and handler."""

    name: str
    description: str
    parameters: dict[str, Any]
    required: list[str]
    handler: Callable[..., Awaitable[str]]

    def to_openai_format(self) -> dict[str, Any]:
        """Convert to OpenAI Responses API tool definition format."""
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": self.parameters,
                "required": self.required,
            },
        }


def tool(func: Callable[..., Any]) -> Tool:
    """Decorator that creates a Tool from an async function.

    Extracts name, description (from docstring), parameter schema
    (from type hints), and required params (from defaults).
    """
    name = func.__name__

    # Extract description from docstring
    description = ""
    doc = inspect.getdoc(func)
    if doc:
        description = doc.split("\n")[0].strip()

    # Build parameter schema from type hints
    hints = get_type_hints(func)
    sig = inspect.signature(func)

    parameters: dict[str, Any] = {}
    required: list[str] = []

    for param_name, param in sig.parameters.items():
        # Skip 'self' for methods
        if param_name == "self":
            continue

        hint = hints.get(param_name, str)
        parameters[param_name] = _python_type_to_schema(hint)

        has_default = param.default is not inspect.Parameter.empty
        if not has_default:
            required.append(param_name)

    return Tool(
        name=name,
        description=description,
        parameters=parameters,
        required=required,
        handler=func,
    )
