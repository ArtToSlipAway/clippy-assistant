import hashlib
import json
import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


TZ = ZoneInfo("Europe/Moscow")

STORE_PATH = Path(
    "data/project_next_actions.json"
)

ALLOWED_SCOPE = "clippy_active_projects"
ALLOWED_PROJECT_NAME = "Для Clippy"

MAX_SOURCES = 50
MAX_ACTIONS_PER_SOURCE = 40
MAX_TOTAL_ACTIONS = 200


def _now_iso() -> str:
    return datetime.now(TZ).isoformat()


def _empty_store() -> dict:
    return {
        "version": 1,
        "scope": ALLOWED_SCOPE,
        "project_name": ALLOWED_PROJECT_NAME,
        "updated_at": None,
        "sources": {},
        "planned": {},
    }


def _load_store() -> dict:
    if not STORE_PATH.exists():
        return _empty_store()

    try:
        payload = json.loads(
            STORE_PATH.read_text(
                encoding="utf-8"
            )
        )
    except Exception:
        return _empty_store()

    if not isinstance(payload, dict):
        return _empty_store()

    if not isinstance(
        payload.get("sources"),
        dict,
    ):
        payload["sources"] = {}

    if not isinstance(
        payload.get("planned"),
        dict,
    ):
        payload["planned"] = {}

    payload["scope"] = ALLOWED_SCOPE
    payload[
        "project_name"
    ] = ALLOWED_PROJECT_NAME

    return payload


def _save_store(payload: dict) -> None:
    payload["updated_at"] = _now_iso()

    tmp = STORE_PATH.with_suffix(
        ".json.tmp"
    )

    tmp.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    os.replace(
        tmp,
        STORE_PATH,
    )


def _clean_text(
    value,
    max_length: int,
) -> str:
    text = " ".join(
        str(value or "").split()
    ).strip()

    return text[:max_length]


def _validate_date(
    value,
) -> str | None:
    if value in {
        None,
        "",
    }:
        return None

    text = str(value).strip()

    try:
        return date.fromisoformat(
            text
        ).isoformat()
    except ValueError:
        raise ValueError(
            "invalid_date"
        )


def _action_id(
    source_chat: str,
    project: str,
    title: str,
) -> str:
    raw = "|".join([
        source_chat.casefold(),
        project.casefold(),
        title.casefold(),
    ])

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:16]


def _normalize_action(
    item: dict,
    source_chat: str,
) -> dict:
    if not isinstance(item, dict):
        raise ValueError(
            "invalid_action"
        )

    title = _clean_text(
        item.get("title"),
        220,
    )

    if not title:
        raise ValueError(
            "missing_title"
        )

    project = _clean_text(
        item.get("project")
        or source_chat,
        120,
    )

    status = _clean_text(
        item.get("status")
        or "active",
        20,
    ).casefold()

    if status not in {
        "active",
        "paused",
        "done",
    }:
        raise ValueError(
            "invalid_status"
        )

    priority = _clean_text(
        item.get("priority")
        or "normal",
        20,
    ).casefold()

    if priority not in {
        "low",
        "normal",
        "high",
    }:
        raise ValueError(
            "invalid_priority"
        )

    raw_minutes = item.get(
        "estimated_minutes"
    )

    estimated_minutes = None

    if raw_minutes not in {
        None,
        "",
    }:
        try:
            estimated_minutes = int(
                raw_minutes
            )
        except Exception:
            raise ValueError(
                "invalid_estimated_minutes"
            )

        if not (
            15
            <= estimated_minutes
            <= 720
        ):
            raise ValueError(
                "invalid_estimated_minutes"
            )

    preferred_date = _validate_date(
        item.get("preferred_date")
    )

    not_before = _validate_date(
        item.get("not_before")
    )

    note = _clean_text(
        item.get("note"),
        500,
    )

    action_id = _clean_text(
        item.get("action_id"),
        80,
    )

    if not action_id:
        action_id = _action_id(
            source_chat,
            project,
            title,
        )

    return {
        "action_id": action_id,
        "title": title,
        "project": project,
        "status": status,
        "priority": priority,
        "estimated_minutes": (
            estimated_minutes
        ),
        "preferred_date": (
            preferred_date
        ),
        "not_before": not_before,
        "note": note or None,
    }


def sync_project_actions(
    *,
    scope: str,
    project_name: str,
    source_chat: str,
    actions: list,
) -> dict:

    if scope != ALLOWED_SCOPE:
        return {
            "ok": False,
            "error": "invalid_scope",
        }

    if (
        project_name
        != ALLOWED_PROJECT_NAME
    ):
        return {
            "ok": False,
            "error": (
                "project_not_allowed"
            ),
        }

    source_chat = _clean_text(
        source_chat,
        160,
    )

    if not source_chat:
        return {
            "ok": False,
            "error": (
                "missing_source_chat"
            ),
        }

    if not isinstance(
        actions,
        list,
    ):
        return {
            "ok": False,
            "error": "invalid_actions",
        }

    if len(
        actions
    ) > MAX_ACTIONS_PER_SOURCE:
        return {
            "ok": False,
            "error": (
                "too_many_actions"
            ),
        }

    try:
        normalized = [
            _normalize_action(
                item,
                source_chat,
            )
            for item in actions
        ]
    except ValueError as exc:
        return {
            "ok": False,
            "error": str(exc),
        }

    payload = _load_store()
    sources = payload.setdefault(
        "sources",
        {},
    )

    if (
        source_chat not in sources
        and len(sources) >= MAX_SOURCES
    ):
        return {
            "ok": False,
            "error": (
                "too_many_sources"
            ),
        }

    sources[source_chat] = {
        "source_chat": source_chat,
        "synced_at": _now_iso(),
        "actions": normalized,
    }

    total = sum(
        len(
            item.get(
                "actions",
                [],
            )
        )
        for item in sources.values()
        if isinstance(
            item,
            dict,
        )
    )

    if total > MAX_TOTAL_ACTIONS:
        return {
            "ok": False,
            "error": (
                "too_many_total_actions"
            ),
        }

    _save_store(
        payload
    )

    return {
        "ok": True,
        "scope": ALLOWED_SCOPE,
        "project_name": (
            ALLOWED_PROJECT_NAME
        ),
        "source_chat": source_chat,
        "synced_count": len(
            normalized
        ),
        "total_actions": total,
        "calendar_changed": False,
        "google_tasks_changed": False,
    }


def get_project_actions(
    *,
    active_only: bool = True,
) -> dict:

    payload = _load_store()

    flattened = []

    for (
        source_chat,
        source,
    ) in payload.get(
        "sources",
        {},
    ).items():

        if not isinstance(
            source,
            dict,
        ):
            continue

        synced_at = source.get(
            "synced_at"
        )

        for action in source.get(
            "actions",
            [],
        ):
            if not isinstance(
                action,
                dict,
            ):
                continue

            action_id = str(
                action.get(
                    "action_id"
                )
                or ""
            )

            planned = payload.get(
                "planned",
                {},
            )

            if (
                active_only
                and (
                    action.get(
                        "status"
                    )
                    != "active"
                    or action_id
                    in planned
                )
            ):
                continue

            row = dict(action)

            if action_id in planned:
                row["planned"] = True
                row["planned_at"] = (
                    planned[
                        action_id
                    ].get(
                        "planned_at"
                    )
                    if isinstance(
                        planned[
                            action_id
                        ],
                        dict,
                    )
                    else None
                )

            row[
                "source_chat"
            ] = source_chat

            row[
                "source_synced_at"
            ] = synced_at

            flattened.append(
                row
            )

    priority_rank = {
        "high": 0,
        "normal": 1,
        "low": 2,
    }

    flattened.sort(
        key=lambda item: (
            priority_rank.get(
                item.get(
                    "priority"
                ),
                1,
            ),
            item.get(
                "preferred_date"
            )
            or "9999-12-31",
            item.get(
                "project"
            )
            or "",
            item.get(
                "title"
            )
            or "",
        )
    )

    return {
        "ok": True,
        "scope": ALLOWED_SCOPE,
        "project_name": (
            ALLOWED_PROJECT_NAME
        ),
        "active_only": active_only,
        "updated_at": payload.get(
            "updated_at"
        ),
        "count": len(
            flattened
        ),
        "actions": flattened,
        "calendar_changed": False,
        "google_tasks_changed": False,
    }



def mark_project_actions_planned(
    action_ids: list[str],
) -> dict:

    cleaned = {
        str(item or "").strip()
        for item in action_ids
        if str(item or "").strip()
    }

    if not cleaned:
        return {
            "ok": True,
            "count": 0,
        }

    payload = _load_store()

    planned = payload.setdefault(
        "planned",
        {},
    )

    now = _now_iso()

    for action_id in cleaned:
        planned[action_id] = {
            "planned_at": now,
        }

    _save_store(
        payload
    )

    return {
        "ok": True,
        "count": len(cleaned),
    }
