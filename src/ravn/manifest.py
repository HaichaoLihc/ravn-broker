"""Two reviewed read-only tool subsets. Provider schema pins require operator review."""

import hashlib

from jsonschema import Draft202012Validator
from mcp import types

from ravn.common import RavnError, canonical

NAME = {
    "type": "string",
    "minLength": 1,
    "maxLength": 100,
    "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
}
COMMON = {"owner": NAME, "repo": NAME}
SCHEMAS = {
    "issue_read": {
        "type": "object",
        "additionalProperties": False,
        "required": ["method", "owner", "repo", "issue_number"],
        "properties": {
            **COMMON,
            "method": {"const": "get", "type": "string"},
            "issue_number": {"type": "integer", "minimum": 1, "maximum": 2147483647},
        },
    },
    "list_issues": {
        "type": "object",
        "additionalProperties": False,
        "required": ["owner", "repo"],
        "properties": COMMON,
    },
}

SLACK_SCHEMAS = {
    name: {
        "type": "object",
        "additionalProperties": False,
        "required": ["query"],
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 2000},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            "cursor": {"type": "string", "maxLength": 2048},
        },
    }
    for name in ("slack_search_public", "slack_search_public_and_private")
}
SLACK_SCHEMAS["slack_read_thread"] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["channel_id", "message_ts"],
    "properties": {
        "channel_id": {"type": "string", "pattern": r"^[CGD][A-Z0-9]{2,31}$"},
        "message_ts": {"type": "string", "pattern": r"^[0-9]{1,20}\.[0-9]{1,10}$"},
    },
}


def schemas_for(connector="github_cloud"):
    return SLACK_SCHEMAS if connector == "slack" else SCHEMAS


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


def reviewed_tools(
    tools: list[types.Tool], pins: dict[str, str], connector="github_cloud"
) -> list[types.Tool]:
    schemas = schemas_for(connector)
    result = []
    seen = set()
    for tool in tools:
        if tool.name in seen:
            raise RavnError(502, "schema_rejected", "Duplicate upstream tool name.")
        seen.add(tool.name)
        if tool.name not in schemas or tool.name not in pins:
            continue
        check_schema(tool.input_schema)
        if tool.output_schema is not None:
            check_schema(tool.output_schema)
        if schema_hash(tool) != pins[tool.name]:
            raise RavnError(
                409, "schema_drift", "Reviewed tool schema changed; operator review required."
            )
        result.append(
            types.Tool(
                name=tool.name,
                description=f"Read {'Slack' if connector == 'slack' else 'GitHub issues'} using {tool.name}.",
                inputSchema=schemas[tool.name],
                annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False),
            )
        )
    return sorted(result, key=lambda t: t.name)


def validate_arguments(name: str, arguments: dict, connector="github_cloud"):
    schemas = schemas_for(connector)
    if name not in schemas:
        raise RavnError(
            403, "permission_denied", "Only this connector's reviewed read-only tools are enabled."
        )
    if next(Draft202012Validator(schemas[name]).iter_errors(arguments), None):
        raise RavnError(
            422, "invalid_arguments", "Arguments do not match the supported read-only schema."
        )
    if any(arguments.get(k) in {".", ".."} for k in ("owner", "repo")):
        raise RavnError(422, "invalid_arguments", "Invalid repository identifier.")
