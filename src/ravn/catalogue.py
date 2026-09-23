"""Provider-independent MCP catalogue and schema validation."""

import hashlib
import re

from jsonschema import Draft202012Validator
from mcp import types

from ravn.common import RavnError, canonical, digest


def tool_name(connection_id: str, name: str) -> str:
    # Short upstream names remain readable. Hash long/nonportable names to keep
    # the complete identifier within 64 ASCII characters; domains cannot overlap.
    suffix = name if re.fullmatch(r"[A-Za-z0-9_-]{1,24}", name) else "h" + digest(name)[:24]
    return connection_id + "__" + suffix


def tool_connection(name: str) -> str:
    match = re.fullmatch(r"(conn_[a-f0-9]{32})__[A-Za-z0-9_-]{1,25}", name)
    if not match:
        raise RavnError(403, "permission_denied", "Use a tool name returned by tools/list.")
    return match[1]


def schema_hash(tool: types.Tool) -> str:
    # Includes output shape: an operator must re-review either direction's drift.
    return hashlib.sha256(
        canonical({"input": tool.input_schema, "output": tool.output_schema})
    ).hexdigest()


def check_schema(value: dict, depth=0):
    if len(canonical(value)) > 262144:
        raise RavnError(502, "schema_rejected", "Upstream tool schema exceeds the limit.")

    def visit(item, level):
        if level > 32:
            raise RavnError(502, "schema_rejected", "Upstream tool schema is too deeply nested.")
        if isinstance(item, dict):
            if any(k in item for k in ("$ref", "$dynamicRef", "$recursiveRef", "$id")):
                raise RavnError(502, "schema_rejected", "Referenced tool schemas are unsupported.")
            for child in item.values():
                visit(child, level + 1)
        elif isinstance(item, list):
            for child in item:
                visit(child, level + 1)

    visit(value, depth)
    try:
        Draft202012Validator.check_schema(value)
    except Exception:
        raise RavnError(502, "schema_rejected", "Upstream tool schema is invalid.") from None


def validate_tools(tools: list[types.Tool]) -> list[types.Tool]:
    seen = set()
    for tool in tools:
        if tool.name in seen:
            raise RavnError(502, "schema_rejected", "Duplicate upstream tool name.")
        seen.add(tool.name)
        check_schema(tool.input_schema)
        if tool.output_schema is not None:
            check_schema(tool.output_schema)
    return sorted(tools, key=lambda tool: tool.name)
