import json
import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from calendar_tools import (
    classify_planning_event,
    clean_calendar_title,
    get_calendar_service,
)
from memory_store import get_facts, save_fact
from project_next_actions import sync_project_actions


TZ = ZoneInfo("Europe/Moscow")
LOOKAHEAD_DAYS = 60
NIGHTLY_SYNC_TIME = time(3, 15)
SNAPSHOT_FACT_KEY = "calendar_project_snapshot"
LAST_SYNC_FACT_KEY = "nightly_project_sync_last_date"
TATTOO_ACTION_SOURCE = "Google Calendar: tattoo sketches"


def _normalize(value: str) -> str:
    return " ".join(
        str(value or "").casefold().replace("ё", "е").split()
    )


def _sketch_is_ready(event: dict) -> bool:
    value = _normalize(
        "\n".join(
            (
                event.get("summary", ""),
                event.get("description", ""),
            )
        )
    )
    return "эскиз готов" in value


def _is_tattoo_session(event: dict) -> bool:
    title = _normalize(event.get("summary", ""))
    description = _normalize(event.get("description", ""))
    return bool(
        "тату" in title
        or "тату" in description
        or "сеанс" in title
        or (
            "клиент:" in description
            and "проект:" in description
        )
    )


def _event_start(event: dict) -> datetime:
    start = event.get("start") or {}
    if start.get("dateTime"):
        value = datetime.fromisoformat(
            start["dateTime"].replace("Z", "+00:00")
        )
        if value.tzinfo is None:
            value = value.replace(tzinfo=TZ)
        return value.astimezone(TZ)
    return datetime.combine(
        date.fromisoformat(start["date"]),
        time.min,
        tzinfo=TZ,
    )


def _event_end(event: dict) -> datetime:
    end = event.get("end") or {}
    if end.get("dateTime"):
        value = datetime.fromisoformat(
            end["dateTime"].replace("Z", "+00:00")
        )
        if value.tzinfo is None:
            value = value.replace(tzinfo=TZ)
        return value.astimezone(TZ)
    return datetime.combine(
        date.fromisoformat(end["date"]),
        time.min,
        tzinfo=TZ,
    )


def _calendar_events(
    calendar_id: str,
    start_at: datetime,
    end_at: datetime,
) -> list[dict]:
    service = get_calendar_service(write=False)
    result = []
    page_token = None
    while True:
        response = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=start_at.isoformat(),
                timeMax=end_at.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=2500,
                pageToken=page_token,
            )
            .execute()
        )
        result.extend(response.get("items", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return result


def _snapshot_event(event: dict, calendar_name: str) -> dict:
    start = _event_start(event)
    end = _event_end(event)
    private = (
        (event.get("extendedProperties") or {}).get("private")
        or {}
    )
    return {
        "calendar": calendar_name,
        "event_id": event.get("id", ""),
        "title": clean_calendar_title(
            event.get("summary", "Без названия")
        ),
        "description": str(event.get("description") or "")[:700],
        "location": str(event.get("location") or "")[:300],
        "start": start.isoformat(),
        "end": end.isoformat(),
        "all_day": bool((event.get("start") or {}).get("date")),
        "manual": not bool(private.get("arttoslipaway_writer")),
        "authority": "google_calendar",
    }


def _tattoo_client_label(title: str) -> str:
    value = str(title or "").strip()
    normalized = _normalize(value)
    for prefix in (
        "тату-сеанс —",
        "тату-сеанс -",
        "тату сеанс —",
        "тату сеанс -",
    ):
        if normalized.startswith(_normalize(prefix)):
            candidate = value[len(prefix) :].strip()
            if candidate:
                return candidate[:70]
    return value[:70] or "клиент"


def _tattoo_sketch_action(event: dict, today: date) -> dict:
    start = _event_start(event)
    title = clean_calendar_title(
        event.get("summary", "Тату-сеанс")
    )
    client = _tattoo_client_label(title)
    preparation_date = max(today, start.date() - timedelta(days=3))
    return {
        "title": (
            f"Отрисовать эскиз к сеансу «{client}» "
            f"{start.strftime('%d.%m')} к {start.strftime('%H:%M')}"
        ),
        "project": f"Эскиз к сеансу / {client}"[:120],
        "status": "active",
        "priority": "high",
        "estimated_minutes": 120,
        "preferred_date": preparation_date.isoformat(),
        "not_before": preparation_date.isoformat(),
        "note": (
            "Источник истины — Google Calendar. После завершения добавь "
            "«Эскиз готов» в название или описание тату-сеанса. "
            f"Calendar event_id: {event.get('id', '')}"
        ),
    }


def nightly_sync_is_due(now: datetime | None = None) -> bool:
    now = now or datetime.now(TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    now = now.astimezone(TZ)
    if now.time() < NIGHTLY_SYNC_TIME:
        return False
    last_sync = get_facts().get(LAST_SYNC_FACT_KEY, "")
    return last_sync != now.date().isoformat()


def sync_calendar_projects(now: datetime | None = None) -> dict:
    now = now or datetime.now(TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    now = now.astimezone(TZ)
    today = now.date()
    start_at = datetime.combine(today, time.min, tzinfo=TZ)
    end_at = start_at + timedelta(days=LOOKAHEAD_DAYS)

    calendars = {
        "Projects": os.environ["GOOGLE_PROJECTS_CALENDAR_ID"],
        "Personal": os.environ["GOOGLE_PERSONAL_CALENDAR_ID"],
        "Tattoo": os.environ["GOOGLE_CALENDAR_ID"],
    }

    project_calendar_events = _calendar_events(
        calendars["Projects"], start_at, end_at
    )
    personal_events = _calendar_events(
        calendars["Personal"], start_at, end_at
    )
    personal_project_events = [
        event
        for event in personal_events
        if classify_planning_event(
            "Личный",
            clean_calendar_title(event.get("summary", "")),
        )
        == "flexible"
    ]
    project_events = project_calendar_events + personal_project_events
    tattoo_events = [
        event
        for event in _calendar_events(
            calendars["Tattoo"], start_at, end_at
        )
        if _is_tattoo_session(event)
    ]

    snapshot = {
        "updated_at": now.isoformat(),
        "authority": "google_calendar",
        "lookahead_days": LOOKAHEAD_DAYS,
        "rule": (
            "События Google Calendar, включая внесённые владельцем "
            "вручную, считаются актуальной истиной. Не дублировать и "
            "не переносить их без явной команды."
        ),
        "project_events": [
            *[
                _snapshot_event(event, "Projects")
                for event in project_calendar_events
            ],
            *[
                _snapshot_event(event, "Personal")
                for event in personal_project_events
            ],
        ],
        "tattoo_sessions": [
            {
                **_snapshot_event(event, "Tattoo"),
                "sketch_ready": _sketch_is_ready(event),
            }
            for event in tattoo_events
        ],
    }

    sketch_actions = [
        _tattoo_sketch_action(event, today)
        for event in tattoo_events
        if not _sketch_is_ready(event)
    ]

    sync_result = sync_project_actions(
        scope="clippy_active_projects",
        project_name="Для Clippy",
        source_chat=TATTOO_ACTION_SOURCE,
        actions=sketch_actions,
    )
    if not sync_result.get("ok"):
        raise RuntimeError(
            "tattoo sketch sync failed: "
            f"{sync_result.get('error', 'unknown')}"
        )

    save_fact(
        SNAPSHOT_FACT_KEY,
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
    )
    save_fact(LAST_SYNC_FACT_KEY, today.isoformat())

    return {
        "ok": True,
        "date": today.isoformat(),
        "project_events": len(project_events),
        "tattoo_sessions": len(tattoo_events),
        "sketch_actions": len(sketch_actions),
    }
