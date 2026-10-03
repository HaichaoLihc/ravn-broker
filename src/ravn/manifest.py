"""Reviewed per-connector tool subsets. Provider schema pins require operator review."""

import hashlib
import re

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

# Tools that change provider state. They are never retried and an ambiguous
# outcome is recorded as unknown rather than failed.
# A header value may never carry a line break, or an agent could add headers or recipients.
HEADER_TEXT = {"type": "string", "maxLength": 998, "pattern": r"^[^\r\n]*$"}
GMAIL_SEND_SCHEMAS = {
    "send_email": gmail_object(
        {
            "to": {**RECIPIENTS, "minItems": 1},
            "cc": RECIPIENTS,
            "bcc": RECIPIENTS,
            "subject": HEADER_TEXT,
            "body": {"type": "string", "minLength": 1, "maxLength": 50000},
        },
        ["to", "subject", "body"],
    ),
}

WRITE_TOOLS = {
    "gmail": {"create_draft": "Create a Gmail draft. Drafts are never sent."},
    "gmail_api": {
        "send_email": "Send a plain-text email from the connected Gmail account. "
        "A person approves each send; a sent email cannot be recalled."
    },
}
MANIFESTS = {
    "github_cloud": SCHEMAS,
    "slack": SLACK_SCHEMAS,
    "gmail": GMAIL_SCHEMAS,
    "gmail_api": GMAIL_SEND_SCHEMAS,
}
DESCRIPTIONS = {
    "github_cloud": "Read GitHub issues using {name}.",
    "slack": "Read Slack using {name}.",
    "gmail": "Read Gmail using {name}.",
    "gmail_api": "Use Gmail with {name}.",
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


LOCAL_REF = re.compile(r"#/\$defs/([A-Za-z0-9_.-]{1,100})")


def inline_local_refs(value: dict) -> dict:
    """Resolve `#/$defs/Name` references in place, the only kind accepted.

    Remote, dynamic and recursive references stay unsupported, and a node budget
    stops a schema that fans out on expansion before it can exhaust memory.
    """
    defs = value.get("$defs") if isinstance(value, dict) else None
    if not isinstance(defs, dict):
        return value
    budget = [20000]

    def resolve(item, seen):
        budget[0] -= 1
        if budget[0] < 0:
            raise RavnError(502, "schema_rejected", "Upstream tool schema exceeds the limit.")
        if isinstance(item, list):
            return [resolve(child, seen) for child in item]
        if not isinstance(item, dict):
            return item
        ref = item.get("$ref")
        if ref is None:
            return {k: resolve(v, seen) for k, v in item.items()}
        match = isinstance(ref, str) and LOCAL_REF.fullmatch(ref)
        if not match or match[1] not in defs or match[1] in seen:
            raise RavnError(502, "schema_rejected", "Referenced tool schemas are unsupported.")
        target = resolve(defs[match[1]], seen | {match[1]})
        siblings = {k: resolve(v, seen) for k, v in item.items() if k != "$ref"}
        if not isinstance(target, dict):
            raise RavnError(502, "schema_rejected", "Upstream tool schema is invalid.")
        return {**target, **siblings}

    return resolve({k: v for k, v in value.items() if k != "$defs"}, frozenset())


def check_schema(value: dict, depth=0):
    if len(canonical(value)) > 262144:
        raise RavnError(502, "schema_rejected", "Upstream tool schema exceeds the limit.")
    value = inline_local_refs(value)

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
