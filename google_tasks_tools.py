import logging
from datetime import date, datetime, timezone
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build



TOKEN_PATH = Path(
    "data/google_tasks_token.json"
)

SCOPES = [
    "https://www.googleapis.com/auth/tasks",
]




def complete_google_task(
    task_list_id: str,
    task_id: str,
):
    service = _get_tasks_service()

    result = (
        service.tasks()
        .patch(
            tasklist=task_list_id,
            task=task_id,
            body={
                "status": "completed"
            },
        )
        .execute()
    )

    return result



def find_task_by_title(
    title: str,
    target_date: date | None = None,
):
    service = _get_tasks_service()

    lists = (
        service.tasklists()
        .list(maxResults=100)
        .execute()
    )

    target_iso = (
        target_date.isoformat()
        if target_date
        else None
    )

    for task_list in lists.get("items", []):

        tasks = (
            service.tasks()
            .list(
                tasklist=task_list["id"],
                showCompleted=False,
                maxResults=100,
            )
            .execute()
        )

        for task in tasks.get("items", []):

            if task.get("title") != title:
                continue

            if target_iso:
                due = str(
                    task.get("due") or ""
                )

                if due[:10] != target_iso:
                    continue

            return {
                "task_list_id": task_list["id"],
                "task_id": task["id"],
                "title": task["title"],
            }

    return None


def _get_tasks_service():
    if not TOKEN_PATH.exists():
        raise FileNotFoundError(
            "google_tasks_token.json not found"
        )

    creds = Credentials.from_authorized_user_file(
        str(TOKEN_PATH),
        SCOPES,
    )

    return build(
        "tasks",
        "v1",
        credentials=creds,
        cache_discovery=False,
    )


def list_google_task_lists() -> list[dict]:
    service = _get_tasks_service()

    result = []
    page_token = None

    while True:
        response = (
            service
            .tasklists()
            .list(
                maxResults=100,
                pageToken=page_token,
            )
            .execute()
        )

        for item in response.get("items", []):
            if item.get("id"):
                result.append({
                    "id": item["id"],
                    "title": (
                        item.get("title")
                        or "Без названия"
                    ),
                })

        page_token = response.get(
            "nextPageToken"
        )

        if not page_token:
            break

    return result


def _default_task_list_id(
    service=None,
) -> str:
    service = service or _get_tasks_service()

    response = (
        service
        .tasklists()
        .list(
            maxResults=100,
        )
        .execute()
    )

    items = response.get(
        "items",
        [],
    )

    if not items:
        raise RuntimeError(
            "Google Tasks: нет доступного списка задач"
        )

    # У пользователя всегда есть default list.
    # API отдаёт списки пользователя; для наших новых
    # задач используем первый доступный список.
    task_list_id = items[0].get("id")

    if not task_list_id:
        raise RuntimeError(
            "Google Tasks: список не имеет id"
        )

    return task_list_id


def _task_due_value(
    target_date: date,
) -> str:
    # Tasks API хранит только дату.
    return (
        target_date.isoformat()
        + "T00:00:00.000Z"
    )


def create_google_task(
    title: str,
    target_date: date,
    notes: str = "",
    task_list_id: str = "",
) -> dict:
    service = _get_tasks_service()

    title = (
        title
        or ""
    ).strip()

    if not title:
        return {
            "ok": False,
            "error": "Название задачи не задано",
        }

    task_list_id = (
        task_list_id
        or _default_task_list_id(service)
    )

    body = {
        "title": title,
        "due": _task_due_value(
            target_date
        ),
    }

    if notes.strip():
        body["notes"] = notes.strip()

    task = (
        service
        .tasks()
        .insert(
            tasklist=task_list_id,
            body=body,
        )
        .execute()
    )

    return {
        "ok": True,
        "task_list_id": task_list_id,
        "task_id": task.get("id", ""),
        "title": task.get("title", title),
        "status": task.get(
            "status",
            "needsAction",
        ),
        "due": task.get("due", ""),
    }


def get_google_task(
    task_list_id: str,
    task_id: str,
) -> dict:
    service = _get_tasks_service()

    task = (
        service
        .tasks()
        .get(
            tasklist=task_list_id,
            task=task_id,
        )
        .execute()
    )

    return {
        "ok": True,
        "task_list_id": task_list_id,
        "task_id": task_id,
        "title": task.get(
            "title",
            "Без названия",
        ),
        "status": task.get(
            "status",
            "needsAction",
        ),
        "due": task.get("due", ""),
        "completed": task.get(
            "completed",
            "",
        ),
        "notes": task.get(
            "notes",
            "",
        ),
    }


def set_google_task_completed(
    task_list_id: str,
    task_id: str,
    completed: bool = True,
) -> dict:
    service = _get_tasks_service()

    if completed:
        body = {
            "status": "completed",
            "completed": (
                datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            ),
        }
    else:
        body = {
            "status": "needsAction",
            "completed": None,
        }

    task = (
        service
        .tasks()
        .patch(
            tasklist=task_list_id,
            task=task_id,
            body=body,
        )
        .execute()
    )

    return {
        "ok": True,
        "task_list_id": task_list_id,
        "task_id": task_id,
        "title": task.get(
            "title",
            "Без названия",
        ),
        "completed": (
            task.get("status")
            == "completed"
        ),
        "status": task.get(
            "status",
            "needsAction",
        ),
    }


def toggle_google_task(
    task_list_id: str,
    task_id: str,
) -> dict:
    current = get_google_task(
        task_list_id,
        task_id,
    )

    if not current.get("ok"):
        return current

    return set_google_task_completed(
        task_list_id,
        task_id,
        completed=(
            current.get("status")
            != "completed"
        ),
    )


def reschedule_google_task(
    task_list_id: str,
    task_id: str,
    target_date: date,
    planned_start: datetime | None = None,
    planned_end: datetime | None = None,
    new_title: str | None = None,
) -> dict:
    """
    Синхронизирует Google Task со связанным
    Calendar block.

    Tasks API хранит due только как дату,
    поэтому точное время Clippy хранит
    в технической строке notes.
    """

    service = _get_tasks_service()

    body = {
        "due": _task_due_value(
            target_date
        ),
    }

    if new_title is not None:
        clean_title = (
            str(new_title)
            .strip()
        )

        if clean_title:
            body["title"] = clean_title

    if (
        planned_start is not None
        and planned_end is not None
    ):
        current = (
            service
            .tasks()
            .get(
                tasklist=task_list_id,
                task=task_id,
            )
            .execute()
        )

        notes = (
            current.get(
                "notes",
                "",
            )
            or ""
        )

        schedule_line = (
            "Плановое время Clippy: "
            + planned_start.strftime(
                "%H:%M"
            )
            + "–"
            + planned_end.strftime(
                "%H:%M"
            )
        )

        old_lines = notes.splitlines()
        new_lines = []
        replaced = False

        for line in old_lines:
            normalized = (
                line
                .strip()
                .casefold()
                .replace(
                    "ё",
                    "е",
                )
            )

            if normalized.startswith(
                "плановое время clippy:"
            ):
                if not replaced:
                    new_lines.append(
                        schedule_line
                    )
                    replaced = True

                # Если старых технических строк
                # несколько, лишние удаляем.
                continue

            new_lines.append(
                line
            )

        if not replaced:
            # Ставим техническую строку перед
            # маркером связанного Calendar block,
            # если такой маркер уже существует.
            insert_at = len(
                new_lines
            )

            for index, line in enumerate(
                new_lines
            ):
                normalized = (
                    line
                    .strip()
                    .casefold()
                    .replace(
                        "ё",
                        "е",
                    )
                )

                if normalized.startswith(
                    "связано с существующим "
                    "calendar block"
                ):
                    insert_at = index
                    break

            new_lines.insert(
                insert_at,
                schedule_line,
            )

        body["notes"] = (
            "\n".join(
                new_lines
            ).strip()
        )

    task = (
        service
        .tasks()
        .patch(
            tasklist=task_list_id,
            task=task_id,
            body=body,
        )
        .execute()
    )

    return {
        "ok": True,
        "task_list_id": task_list_id,
        "task_id": task_id,
        "title": task.get(
            "title",
            "Без названия",
        ),
        "due": task.get(
            "due",
            "",
        ),
        "notes": task.get(
            "notes",
            "",
        ),
    }


def delete_google_task(
    task_list_id: str,
    task_id: str,
) -> dict:
    service = _get_tasks_service()

    (
        service
        .tasks()
        .delete(
            tasklist=task_list_id,
            task=task_id,
        )
        .execute()
    )

    return {
        "ok": True,
        "task_list_id": task_list_id,
        "task_id": task_id,
    }


def get_google_tasks_for_date(
    target_date: date,
) -> list[dict]:
    """
    Возвращает незавершённые Google Tasks,
    назначенные на конкретную дату.

    Google Tasks не имеют нормального временного
    интервала внутри дня, поэтому они представлены
    как all-day элементы, НЕ блокирующие время.
    """

    if not TOKEN_PATH.exists():
        return []

    service = _get_tasks_service()

    task_lists = []
    page_token = None

    while True:
        response = (
            service
            .tasklists()
            .list(
                maxResults=100,
                pageToken=page_token,
            )
            .execute()
        )

        task_lists.extend(
            response.get("items", [])
        )

        page_token = response.get(
            "nextPageToken"
        )

        if not page_token:
            break

    target_iso = target_date.isoformat()
    result = []

    for task_list in task_lists:
        task_list_id = task_list.get("id")

        if not task_list_id:
            continue

        page_token = None

        while True:
            response = (
                service
                .tasks()
                .list(
                    tasklist=task_list_id,
                    showCompleted=False,
                    showHidden=False,
                    maxResults=100,
                    pageToken=page_token,
                )
                .execute()
            )

            for task in response.get(
                "items",
                [],
            ):
                if (
                    task.get("status")
                    == "completed"
                ):
                    continue

                due = str(
                    task.get("due")
                    or ""
                )

                # В Google Tasks значима дата due.
                if due[:10] != target_iso:
                    continue

                result.append({
                    "title": (
                        task.get("title")
                        or "Без названия"
                    ),
                    "all_day": True,
                    "start": "",
                    "end": "",
                    "start_iso": "",
                    "end_iso": "",
                    "calendar_id": "google_tasks",
                    "event_id": (
                        task.get("id")
                        or ""
                    ),
                    "task_id": (
                        task.get("id")
                        or ""
                    ),
                    "movable": False,
                    "blocks_time": False,
                    "planning_type": (
                        "google_task"
                    ),
                    "source": "google_tasks",
                    "task_list_id": (
                        task_list_id
                    ),
                    "status": (
                        task.get(
                            "status",
                            "unknown",
                        )
                    ),
                    "notes": (
                        task.get(
                            "notes",
                            "",
                        )
                    ),
                })

            page_token = response.get(
                "nextPageToken"
            )

            if not page_token:
                break

    result.sort(
        key=lambda item: (
            item.get("title")
            or ""
        ).lower()
    )

    return result


def get_day_overview(
    target_date: date,
) -> list[dict]:
    """
    Полное представление дня для ПОКАЗА пользователю:
    Google Tasks + обычные события Calendar.

    Не использовать вместо get_day_schedule()
    при расчёте свободных окон.
    """

    # Lazy import avoids the calendar_tools -> google_tasks_tools
    # dependency cycle used by the combined day overview.
    from calendar_tools import get_day_schedule

    calendar_events = get_day_schedule(
        target_date
    )

    try:
        google_tasks = (
            get_google_tasks_for_date(
                target_date
            )
        )
    except Exception:
        logging.exception(
            "Google Tasks read failed; "
            "calendar-only fallback"
        )
        google_tasks = []

    # Tasks первыми, как all-day пункты.
    combined = [
        *google_tasks,
        *calendar_events,
    ]

    return sorted(
        combined,
        key=lambda item: (
            0
            if item.get("all_day")
            else 1,
            item.get("start_iso")
            or item.get("start")
            or "",
        ),
    )
