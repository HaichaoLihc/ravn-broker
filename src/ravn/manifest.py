"""Reviewed per-connector tool subsets. Provider schema pins require operator review."""

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

PAGE_TOKEN = {"type": "string", "minLength": 1, "maxLength": 2048}

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
# Slack's own channel_id here accepts a user ID too, which would read direct
# messages under a tool named "read channel". Public conversations only.
SLACK_SCHEMAS["slack_read_channel"] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["channel_id"],
    "properties": {
        "channel_id": {"type": "string", "pattern": r"^C[A-Z0-9]{2,31}$"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        "cursor": PAGE_TOKEN,
    },
}

GMAIL_ID = {"type": "string", "pattern": r"^[A-Za-z0-9_-]{1,128}$"}
GMAIL_QUERY = {"type": "string", "minLength": 1, "maxLength": 2000}
MESSAGE_FORMAT = {"type": "string", "enum": ["MINIMAL", "FULL_CONTENT", "METADATA_ONLY"]}
# Plain addresses only; Google's server rejects "Name <address>" forms.
RECIPIENTS = {
    "type": "array",
    "maxItems": 20,
    "items": {
        "type": "string",
        "maxLength": 254,
        "pattern": r"^[^@\s,;<>\"]{1,64}@[A-Za-z0-9.-]{1,253}$",
    },
}


def gmail_object(properties, required=()):
    return {
        "type": "object",
        "additionalProperties": False,
        **({"required": list(required)} if required else {}),
        "properties": properties,
    }


GMAIL_SCHEMAS = {
    "search_threads": gmail_object(
        {
            "query": GMAIL_QUERY,
            "pageSize": {"type": "integer", "minimum": 1, "maximum": 20},
            "pageToken": PAGE_TOKEN,
        },
        ["query"],
    ),
    "get_thread": gmail_object(
        {"threadId": GMAIL_ID, "messageFormat": MESSAGE_FORMAT}, ["threadId"]
    ),
    "get_message": gmail_object(
        {"messageId": GMAIL_ID, "messageFormat": MESSAGE_FORMAT}, ["messageId"]
    ),
    "list_labels": gmail_object(
        {"pageSize": {"type": "integer", "minimum": 1, "maximum": 100}, "pageToken": PAGE_TOKEN}
    ),
    "list_drafts": gmail_object(
        {
            "query": GMAIL_QUERY,
            "pageSize": {"type": "integer", "minimum": 1, "maximum": 20},
            "pageToken": PAGE_TOKEN,
        }
    ),
    # Plain text only: no HTML (tracking pixels) and no base64 attachments.
    "create_draft": gmail_object(
        {
            "to": RECIPIENTS,
            "cc": RECIPIENTS,
            "bcc": RECIPIENTS,
            "subject": {"type": "string", "maxLength": 998},
            "body": {"type": "string", "minLength": 1, "maxLength": 50000},
            "replyToMessageId": GMAIL_ID,
        },
        ["body"],
    ),
}

# examples/gmail_agent/mcp_server.py's self-hosted wrapper over the real Gmail
# REST API, standing in for gmailmcp.googleapis.com where Workspace Developer
# Preview access is unavailable. Same four read tools as GMAIL_SCHEMAS, but
# the wrapper's own argument shapes (snake_case; no messageFormat). It also
# serves create_draft (so visibility and permission can be shown as
# independent), but that tool is deliberately never reviewed here.
GMAIL_LOCAL_SCHEMAS = {
    "search_threads": gmail_object(
        {
            "query": GMAIL_QUERY,
            "page_size": {"type": "integer", "minimum": 1, "maximum": 20},
            "page_token": PAGE_TOKEN,
        },
        ["query"],
    ),
    "get_thread": gmail_object({"thread_id": GMAIL_ID}, ["thread_id"]),
    "get_message": gmail_object({"message_id": GMAIL_ID}, ["message_id"]),
    "list_labels": gmail_object({}),
}

# Tools that change provider state. They are never retried and an ambiguous
# outcome is recorded as unknown rather than failed.
WRITE_TOOLS = {"gmail": {"create_draft": "Create a Gmail draft. Drafts are never sent."}}
MANIFESTS = {
    "github_cloud": SCHEMAS,
    "slack": SLACK_SCHEMAS,
    "gmail": GMAIL_SCHEMAS,
    "gmail_local": GMAIL_LOCAL_SCHEMAS,
}
DESCRIPTIONS = {
    "github_cloud": "Read GitHub issues using {name}.",
    "slack": "Read Slack using {name}.",
    "gmail": "Read Gmail using {name}.",
    "gmail_local": "Read Gmail using {name}.",
}


def schemas_for(connector="github_cloud"):
    return MANIFESTS[connector]


def is_write(name: str, connector="github_cloud") -> bool:
    return name in WRITE_TOOLS.get(connector, {})


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
        if is_write(tool.name, connector):
            description = WRITE_TOOLS[connector][tool.name]
            annotations = types.ToolAnnotations(
                readOnlyHint=False, destructiveHint=False, idempotentHint=False
            )
        else:
            description = DESCRIPTIONS[connector].format(name=tool.name)
            annotations = types.ToolAnnotations(readOnlyHint=True, destructiveHint=False)
        result.append(
            types.Tool(
                name=tool.name,
                description=description,
                inputSchema=schemas[tool.name],
                annotations=annotations,
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
