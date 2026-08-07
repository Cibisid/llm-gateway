"""Structured audit logging.

WHY A SEPARATE AUDIT LOG FROM THE APPLICATION LOG
    They answer different questions and have different retention needs.
    Application logs answer "why did this break". The audit log answers "who
    asked what, which model answered, what did it cost, and was it refused" —
    which is a compliance and cost-governance artefact, and is the evidence
    behind the NIST AI RMF and EU AI Act mappings in docs/responsible-ai.md.

THE DEFAULT IS: DO NOT LOG PROMPT CONTENT.
    Prompts are the most sensitive thing flowing through this service. They
    routinely contain customer data, credentials people paste in, and internal
    information. Logging them by default would push all of that into whatever
    log aggregator the deployment happens to use, with whatever retention that
    has, readable by whoever has log access.

    So content logging is OFF unless explicitly enabled, and even when enabled
    it is redacted and truncated. That ordering — off by default, redacted when
    on — is the actual control. Redaction alone is regex and cannot be trusted
    as a primary defence.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from app.security import redact

AUDIT_LOGGER_NAME = "gateway.audit"

#: Cap on any logged content field. Long tool outputs bloat the log and raise
#: the exposure surface without adding audit value.
MAX_LOGGED_CHARS = 500


@dataclass
class AuditRecord:
    """One request, as the audit trail sees it."""

    request_id: str
    timestamp: float
    #: Fingerprint, never the key itself.
    key_id: str
    endpoint: str
    requested_model: str
    served_model: str | None = None
    provider: str | None = None
    status_code: int = 200
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    latency_ms: int = 0
    tools_called: list[str] = field(default_factory=list)
    delegated: bool = False
    fallback_occurred: bool = False
    hit_iteration_cap: bool = False
    error: str | None = None
    #: Populated only when content logging is explicitly enabled.
    prompt_excerpt: str | None = None
    response_excerpt: str | None = None


class JsonFormatter(logging.Formatter):
    """One JSON object per line.

    Structured rather than human-formatted because the audit log's consumers
    are queries ("total spend by key this month", "every refused request"), not
    people reading a terminal.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = getattr(record, "audit", None) or {
            "message": record.getMessage()
        }
        payload["level"] = record.levelname
        return json.dumps(payload, default=str, sort_keys=True)


def configure_logging(level: str = "INFO", audit_path: str | None = None) -> None:
    """Set up the application logger and the separate audit logger."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    audit = logging.getLogger(AUDIT_LOGGER_NAME)
    audit.setLevel(logging.INFO)
    # Does not propagate: the audit stream must not be reformatted, filtered, or
    # duplicated by whatever the application logger is configured to do.
    audit.propagate = False
    audit.handlers.clear()

    handler: logging.Handler
    if audit_path:
        handler = logging.FileHandler(audit_path, encoding="utf-8")
    else:
        # stdout by default, so a container platform collects it without the
        # service needing a writable volume.
        handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    audit.addHandler(handler)


def _excerpt(text: str) -> str:
    """Redact, then truncate. In that order — truncating first could split a
    pattern in half and leave a partial credential in the log."""
    cleaned = redact(text)
    if len(cleaned) <= MAX_LOGGED_CHARS:
        return cleaned
    return cleaned[:MAX_LOGGED_CHARS] + f"... [truncated, {len(cleaned)} chars]"


def write_audit(record: AuditRecord) -> None:
    logging.getLogger(AUDIT_LOGGER_NAME).info("", extra={"audit": asdict(record)})


def new_record(
    *,
    request_id: str,
    key_id: str,
    endpoint: str,
    requested_model: str,
) -> AuditRecord:
    return AuditRecord(
        request_id=request_id,
        timestamp=time.time(),
        key_id=key_id,
        endpoint=endpoint,
        requested_model=requested_model,
    )


def attach_content(
    record: AuditRecord,
    *,
    prompt: str | None,
    response: str | None,
    enabled: bool,
) -> None:
    """Attach prompt/response excerpts — only when explicitly enabled.

    The `enabled` flag is checked here rather than at the call site so there is
    exactly ONE place in the codebase where content can enter the audit log.
    A second path would eventually be added without the check.
    """
    if not enabled:
        return
    if prompt:
        record.prompt_excerpt = _excerpt(prompt)
    if response:
        record.response_excerpt = _excerpt(response)
