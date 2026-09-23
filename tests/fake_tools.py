"""Schemas advertised by the synthetic MCP servers used in tests."""

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

MAIL_ID = {"type": "string", "pattern": r"^[A-Za-z0-9_-]{1,128}$"}
MAIL_QUERY = {"type": "string", "minLength": 1, "maxLength": 2000}
MESSAGE_FORMAT = {"type": "string", "enum": ["MINIMAL", "FULL_CONTENT", "METADATA_ONLY"]}
# Synthetic mail provider schema.
RECIPIENTS = {
    "type": "array",
    "maxItems": 20,
    "items": {
        "type": "string",
        "maxLength": 254,
        "pattern": r"^[^@\s,;<>\"]{1,64}@[A-Za-z0-9.-]{1,253}$",
    },
}


def mail_object(properties, required=()):
    return {
        "type": "object",
        "additionalProperties": False,
        **({"required": list(required)} if required else {}),
        "properties": properties,
    }


MAIL_SCHEMAS = {
    "search_threads": mail_object(
        {
            "query": MAIL_QUERY,
            "pageSize": {"type": "integer", "minimum": 1, "maximum": 20},
            "pageToken": PAGE_TOKEN,
        },
        ["query"],
    ),
    "get_thread": mail_object({"threadId": MAIL_ID, "messageFormat": MESSAGE_FORMAT}, ["threadId"]),
    "get_message": mail_object(
        {"messageId": MAIL_ID, "messageFormat": MESSAGE_FORMAT}, ["messageId"]
    ),
    "list_labels": mail_object(
        {"pageSize": {"type": "integer", "minimum": 1, "maximum": 100}, "pageToken": PAGE_TOKEN}
    ),
    "list_drafts": mail_object(
        {
            "query": MAIL_QUERY,
            "pageSize": {"type": "integer", "minimum": 1, "maximum": 20},
            "pageToken": PAGE_TOKEN,
        }
    ),
    # Plain text only: no HTML (tracking pixels) and no base64 attachments.
    "create_draft": mail_object(
        {
            "to": RECIPIENTS,
            "cc": RECIPIENTS,
            "bcc": RECIPIENTS,
            "subject": {"type": "string", "maxLength": 998},
            "body": {"type": "string", "minLength": 1, "maxLength": 50000},
            "replyToMessageId": MAIL_ID,
        },
        ["body"],
    ),
}
