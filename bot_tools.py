import csv
import hashlib
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request

from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

CLIENT_BOT_ENV_FILE = Path("data/client-bot.env")
CLIENT_BOT_SERVICE = os.environ.get(
    "CLIENT_BOT_SERVICE",
    "client-bot.service",
).strip()
LEADS_LOG_FILE = Path(
    "data/leads_log.csv"
)
COMPLETED_EVENTS = {
    "brief_completed",
    "sitebrief_completed",
}
PENDING_CLIENT_MESSAGE_FILE = Path(
    "data/pending_client_message.json"
)
CLIENT_MESSAGE_QUEUE_FILE = Path(
    "data/client_message_queue.json"
)
CLIENT_MESSAGE_AUDIT_FILE = Path(
    "data/client_messages_audit.csv"
)
MAX_CLIENT_MESSAGE_LENGTH = 1500


def _run_read_only(
    command: list[str],
    timeout: int = 5,
) -> subprocess.CompletedProcess:
    """Run one fixed read-only command without a shell."""

    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
    )


def get_client_bot_status() -> dict:
    command = [
        "systemctl",
        "show",
        CLIENT_BOT_SERVICE,
        "--property=Id,LoadState,ActiveState,SubState,MainPID,"
        "ExecMainStartTimestamp,ExecMainStatus,NRestarts",
        "--no-pager",
    ]

    try:
        result = _run_read_only(command)
    except Exception as exc:
        return {
            "ok": False,
            "error": type(exc).__name__,
        }

    if result.returncode != 0:
        return {
            "ok": False,
            "error": "service_status_unavailable",
        }

    values = {}

    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value

    return {
        "ok": True,
        "service": CLIENT_BOT_SERVICE,
        "active": (
            values.get("ActiveState") == "active"
            and values.get("SubState") == "running"
        ),
        "load_state": values.get("LoadState", ""),
        "active_state": values.get("ActiveState", ""),
        "sub_state": values.get("SubState", ""),
        "main_pid": values.get("MainPID", ""),
        "started_at": values.get("ExecMainStartTimestamp", ""),
        "exit_status": values.get("ExecMainStatus", ""),
        "restart_count": values.get("NRestarts", ""),
    }


def _read_lead_rows() -> list[dict]:
    with LEADS_LOG_FILE.open(
        "r",
        encoding="utf-8",
        newline="",
    ) as source:
        reader = csv.DictReader(source)
        required = {
            "timestamp",
            "event",
            "source",
            "request_id",
        }

        if not required.issubset(
            set(reader.fieldnames or [])
        ):
            raise ValueError("unexpected_leads_log_schema")

        return list(reader)


def get_lead_stats() -> dict:
    try:
        rows = _read_lead_rows()
    except Exception as exc:
        return {
            "ok": False,
            "error": type(exc).__name__,
        }

    event_counts = Counter(
        row.get("event", "")
        for row in rows
        if row.get("event")
    )
    source_counts = Counter(
        row.get("source", "")
        for row in rows
        if row.get("source")
    )
    completed = [
        row
        for row in rows
        if row.get("event") in COMPLETED_EVENTS
    ]
    request_ids = {
        row.get("request_id")
        for row in completed
        if row.get("request_id")
    }

    return {
        "ok": True,
        "total_events": len(rows),
        "completed_applications": len(completed),
        "unique_completed_requests": len(request_ids),
        "events_by_type": dict(event_counts),
        "events_by_source": dict(source_counts),
        "latest_event_at": (
            rows[-1].get("timestamp", "")
            if rows
            else ""
        ),
    }


def get_recent_completed_leads(
    limit: int = 5,
) -> dict:
    limit = max(1, min(int(limit), 10))

    try:
        rows = _read_lead_rows()
    except Exception as exc:
        return {
            "ok": False,
            "error": type(exc).__name__,
        }

    completed = []

    for row in reversed(rows):
        if row.get("event") not in COMPLETED_EVENTS:
            continue

        completed.append({
            "timestamp": row.get("timestamp", ""),
            "event": row.get("event", ""),
            "source": row.get("source", ""),
            "request_id": row.get("request_id", ""),
            "username": row.get("username", ""),
            "full_name": row.get("full_name", ""),
            "project_id": row.get("project_id", ""),
            "priority": row.get("priority", ""),
            "selected_date": row.get("selected_date", ""),
        })

        if len(completed) >= limit:
            break

    return {
        "ok": True,
        "count": len(completed),
        "applications": completed,
    }


def _redact_log_line(line: str) -> str:
    value = re.sub(
        r"\b[0-9]{8,12}:[A-Za-z0-9_-]{20,}\b",
        "[TELEGRAM_TOKEN_REDACTED]",
        line,
    )
    value = re.sub(
        r"(?i)\b(token|secret|password|api[_-]?key)"
        r"(\s*[=:]\s*)\S+",
        r"\1\2[REDACTED]",
        value,
    )
    value = re.sub(
        r"(?i)\bBearer\s+[A-Za-z0-9._~-]+",
        "Bearer [REDACTED]",
        value,
    )
    return value[:1000]


def get_recent_client_bot_errors(
    limit: int = 8,
) -> dict:
    limit = max(1, min(int(limit), 20))
    command = [
        "journalctl",
        "--unit",
        CLIENT_BOT_SERVICE,
        "--priority",
        "warning..alert",
        "--lines",
        str(limit),
        "--no-pager",
        "--output",
        "short-iso",
    ]

    try:
        result = _run_read_only(command)
    except Exception as exc:
        return {
            "ok": False,
            "error": type(exc).__name__,
        }

    if result.returncode != 0:
        return {
            "ok": False,
            "error": "service_log_unavailable",
        }

    lines = [
        _redact_log_line(line)
        for line in result.stdout.splitlines()
        if line.strip()
        and not line.startswith("-- No entries --")
    ]

    return {
        "ok": True,
        "count": len(lines),
        "errors": lines,
    }


def _normalize_recipient(value: str) -> str:
    return (value or "").strip().lstrip("@").casefold()


def _recipient_matches(recipient: str) -> list[dict]:
    rows = _read_lead_rows()
    normalized = _normalize_recipient(recipient)

    if not normalized:
        return []

    exact = []
    partial = []

    for row in reversed(rows):
        values = {
            _normalize_recipient(row.get("request_id", "")),
            _normalize_recipient(row.get("username", "")),
            _normalize_recipient(row.get("full_name", "")),
        }
        values.discard("")

        if normalized in values:
            exact.append(row)
        elif any(normalized in value for value in values):
            partial.append(row)

    source = exact or partial
    unique = {}

    for row in source:
        user_id = (row.get("user_id") or "").strip()
        if user_id and user_id not in unique:
            unique[user_id] = row

    return list(unique.values())


def find_client_recipient(recipient: str) -> dict:
    try:
        matches = _recipient_matches(recipient)
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}

    if not matches:
        return {
            "ok": False,
            "error": "client_not_found",
        }

    if len(matches) > 1:
        return {
            "ok": False,
            "error": "ambiguous_client",
            "candidates": [
                {
                    "full_name": row.get("full_name", ""),
                    "username": row.get("username", ""),
                    "request_id": row.get("request_id", ""),
                }
                for row in matches[:5]
            ],
        }

    row = matches[0]
    return {
        "ok": True,
        "recipient": {
            "user_id": row.get("user_id", ""),
            "full_name": row.get("full_name", ""),
            "username": row.get("username", ""),
            "request_id": row.get("request_id", ""),
        },
    }


def _parse_send_at(value: str | None) -> datetime | None:
    if not value:
        return None

    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MOSCOW_TZ)
    return parsed.astimezone(MOSCOW_TZ)


def prepare_client_message(
    recipient: str,
    message: str,
    send_at: str | None = None,
) -> dict:
    text = (message or "").strip()

    if not text:
        return {"ok": False, "error": "empty_message"}

    if len(text) > MAX_CLIENT_MESSAGE_LENGTH:
        return {"ok": False, "error": "message_too_long"}

    resolved = find_client_recipient(recipient)
    if not resolved.get("ok"):
        return resolved

    try:
        scheduled_at = _parse_send_at(send_at)
    except Exception:
        return {"ok": False, "error": "invalid_send_at"}

    now = datetime.now(MOSCOW_TZ)
    if scheduled_at and scheduled_at <= now:
        return {"ok": False, "error": "send_at_is_in_the_past"}

    target = resolved["recipient"]
    payload = {
        "created_at": now.isoformat(),
        "send_at": scheduled_at.isoformat() if scheduled_at else None,
        "recipient": target,
        "message": text,
    }

    PENDING_CLIENT_MESSAGE_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    PENDING_CLIENT_MESSAGE_FILE.chmod(0o600)

    return {
        "ok": True,
        "requires_confirmation": True,
        "recipient": {
            "full_name": target.get("full_name", ""),
            "username": target.get("username", ""),
            "request_id": target.get("request_id", ""),
        },
        "message": text,
        "send_at": payload["send_at"],
    }


def has_pending_client_message() -> bool:
    return PENDING_CLIENT_MESSAGE_FILE.exists()


def clear_pending_client_message() -> None:
    if PENDING_CLIENT_MESSAGE_FILE.exists():
        PENDING_CLIENT_MESSAGE_FILE.unlink()


def _read_client_bot_token() -> str:
    for raw_line in CLIENT_BOT_ENV_FILE.read_text(
        encoding="utf-8"
    ).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "TELEGRAM_BOT_TOKEN":
            return value.strip().strip("\"'")
    raise RuntimeError("client_bot_token_unavailable")


def _telegram_send_message(user_id: str, text: str) -> dict:
    token = _read_client_bot_token()
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({
        "chat_id": user_id,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    request = urllib.request.Request(endpoint, data=body, method="POST")

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}

    if not payload.get("ok"):
        return {"ok": False, "error": "telegram_send_failed"}

    return {
        "ok": True,
        "message_id": payload.get("result", {}).get("message_id"),
    }


def _load_message_queue() -> list[dict]:
    if not CLIENT_MESSAGE_QUEUE_FILE.exists():
        return []
    try:
        payload = json.loads(
            CLIENT_MESSAGE_QUEUE_FILE.read_text(encoding="utf-8")
        )
        return payload if isinstance(payload, list) else []
    except Exception:
        return []


def _save_message_queue(queue: list[dict]) -> None:
    CLIENT_MESSAGE_QUEUE_FILE.write_text(
        json.dumps(queue, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    CLIENT_MESSAGE_QUEUE_FILE.chmod(0o600)


def _audit_client_message(payload: dict, status: str) -> None:
    new_file = not CLIENT_MESSAGE_AUDIT_FILE.exists()
    with CLIENT_MESSAGE_AUDIT_FILE.open(
        "a",
        encoding="utf-8",
        newline="",
    ) as target:
        fieldnames = [
            "timestamp",
            "status",
            "request_id",
            "username",
            "send_at",
            "message_length",
            "message_sha256",
        ]
        writer = csv.DictWriter(target, fieldnames=fieldnames)
        if new_file:
            writer.writeheader()
        text = payload.get("message", "")
        recipient = payload.get("recipient", {})
        writer.writerow({
            "timestamp": datetime.now(MOSCOW_TZ).isoformat(),
            "status": status,
            "request_id": recipient.get("request_id", ""),
            "username": recipient.get("username", ""),
            "send_at": payload.get("send_at") or "",
            "message_length": len(text),
            "message_sha256": hashlib.sha256(
                text.encode("utf-8")
            ).hexdigest(),
        })
    CLIENT_MESSAGE_AUDIT_FILE.chmod(0o600)


def confirm_pending_client_message() -> dict:
    if not PENDING_CLIENT_MESSAGE_FILE.exists():
        return {"ok": False, "error": "no_pending_client_message"}

    try:
        payload = json.loads(
            PENDING_CLIENT_MESSAGE_FILE.read_text(encoding="utf-8")
        )
        created_at = datetime.fromisoformat(payload["created_at"])
    except Exception:
        return {"ok": False, "error": "invalid_pending_client_message"}

    if datetime.now(MOSCOW_TZ) - created_at > timedelta(hours=24):
        clear_pending_client_message()
        return {"ok": False, "error": "pending_client_message_expired"}

    scheduled_at = _parse_send_at(payload.get("send_at"))
    if scheduled_at and scheduled_at > datetime.now(MOSCOW_TZ) + timedelta(seconds=30):
        queue = _load_message_queue()
        queue.append(payload)
        _save_message_queue(queue)
        _audit_client_message(payload, "scheduled")
        clear_pending_client_message()
        return {
            "ok": True,
            "status": "scheduled",
            "send_at": scheduled_at.isoformat(),
            "recipient": payload.get("recipient", {}),
        }

    result = _telegram_send_message(
        payload["recipient"]["user_id"],
        payload["message"],
    )
    if not result.get("ok"):
        _audit_client_message(payload, "failed")
        return result

    _audit_client_message(payload, "sent")
    clear_pending_client_message()
    return {
        "ok": True,
        "status": "sent",
        "recipient": payload.get("recipient", {}),
    }


def send_due_client_messages() -> dict:
    now = datetime.now(MOSCOW_TZ)
    queue = _load_message_queue()
    remaining = []
    sent = 0
    failed = 0

    for payload in queue:
        try:
            send_at = _parse_send_at(payload.get("send_at"))
            next_attempt = _parse_send_at(payload.get("next_attempt_at"))
        except Exception:
            _audit_client_message(payload, "invalid")
            continue

        if (send_at and send_at > now) or (next_attempt and next_attempt > now):
            remaining.append(payload)
            continue

        result = _telegram_send_message(
            payload["recipient"]["user_id"],
            payload["message"],
        )
        if result.get("ok"):
            sent += 1
            _audit_client_message(payload, "sent")
            continue

        failed += 1
        payload["attempts"] = int(payload.get("attempts", 0)) + 1
        payload["next_attempt_at"] = (
            now + timedelta(minutes=5)
        ).isoformat()
        remaining.append(payload)
        _audit_client_message(payload, "failed")

    _save_message_queue(remaining)
    return {
        "ok": failed == 0,
        "sent": sent,
        "failed": failed,
        "remaining": len(remaining),
    }
