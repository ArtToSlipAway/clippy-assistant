import json
import os

from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build


TIMEZONE_NAME = "Europe/Moscow"
TZ = ZoneInfo(TIMEZONE_NAME)

PENDING_FILE = Path(
    "data/pending_calendar_changes.json"
)

PLAN_PROPOSAL_FILE = Path(
    "data/latest_plan_proposal.json"
)


def get_calendar_service(write=False):
    if write:
        scopes = [
            "https://www.googleapis.com/auth/calendar"
        ]
    else:
        scopes = [
            "https://www.googleapis.com/auth/calendar.readonly"
        ]

    credentials = (
        service_account.Credentials
        .from_service_account_file(
            os.environ["GOOGLE_CREDENTIALS_FILE"],
            scopes=scopes,
        )
    )

    return build(
        "calendar",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )


def _parse_event_datetime(value):
    return datetime.fromisoformat(
        value.replace("Z", "+00:00")
    ).astimezone(TZ)


def _parse_input_datetime(value):
    dt = datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    else:
        dt = dt.astimezone(TZ)

    return dt


def _event_start(event):
    start = event.get("start", {})

    if start.get("dateTime"):
        return _parse_event_datetime(
            start["dateTime"]
        )

    if start.get("date"):
        d = date.fromisoformat(
            start["date"]
        )

        return datetime.combine(
            d,
            time.min,
            tzinfo=TZ,
        )

    return datetime.max.replace(
        tzinfo=TZ
    )



def classify_planning_event(
    calendar_name: str,
    title: str,
) -> str:
    """
    fixed      — двигать нельзя;
    anchor     — опорная точка дня;
    adjustable — можно немного двигать/изменять;
    flexible   — свободно переставляемый рабочий блок.
    """

    title_lower = (
        title or ""
    ).strip().lower()

    # Рабочая смена и клиентская запись
    # всегда имеют приоритет.
    if calendar_name in {
        "OZON",
        "Татуировки",
    }:
        return "fixed"

    anchor_words = (
        "сон",
        "подъём",
        "подъем",
        "подготовка ко сну",
    )

    if any(
        word in title_lower
        for word in anchor_words
    ):
        return "anchor"

    adjustable_words = (
        "тренировка",
        "спорт",
        "зал",
    )

    if any(
        word in title_lower
        for word in adjustable_words
    ):
        return "adjustable"

    flexible_words = (
        "эскиз",
        "скетч",
        "картина",
        "рисован",
        "стикер",
        "сайт",
        "бот",
        "clippy",
        "клиппи",
        "ассистент",
        "assistant",
        "telegram",
        "телеграм",
        "3d",
        "3д",
        "модель",
        "обработка фото",
        "фото",
        "визитк",
        "сертификат",
        "мерч",
    )

    if any(
        word in title_lower
        for word in flexible_words
    ):
        return "flexible"

    # Неизвестные личные события
    # самостоятельно не двигаем.
    return "fixed"


def classify_personal_create_event(
    title: str,
) -> str:
    """
    Классификация именно НОВОГО объекта
    личного расписания.

    Существующий общий classifier остаётся
    консервативным: неизвестные старые события
    по-прежнему fixed.

    При создании нового объекта:
    - явные встречи/записи/поездки = fixed;
    - сон/подъём = anchor;
    - тренировки = adjustable;
    - известная работа = flexible;
    - прочий новый личный объект по умолчанию
      считается задачей = flexible.
    """

    current = classify_planning_event(
        "Личный",
        title,
    )

    if current != "fixed":
        return current

    value = (
        title
        or ""
    ).strip().casefold()

    explicit_fixed_markers = (
        "встреч",
        "созвон",
        "звонок",
        "приём",
        "прием",
        "врач",
        "стоматолог",
        "клиник",
        "больниц",
        "парикмах",
        "маникюр",
        "массаж",
        "собеседован",
        "поезд",
        "самолёт",
        "самолет",
        "перелёт",
        "перелет",
        "автобус",
        "вокзал",
        "аэропорт",
        "концерт",
        "театр",
        "кино",
        "ресторан",
        "столик",
        "бронь",
        "бронирован",
        "мероприят",
        "день рождения",
        "курьер",
        "доставка",
        "запись на",
    )

    if any(
        marker in value
        for marker in explicit_fixed_markers
    ):
        return "fixed"

    # Новый личный объект, который не является
    # явным событием времени, считаем задачей.
    return "flexible"


TECHNICAL_AI_PLAN_PREFIXES = (
    "AI-план —",
    "AI-план -",
    "AI-план:",
    "AI план —",
    "AI план -",
    "AI план:",
    "АИ-план —",
    "АИ-план -",
    "АИ-план:",
    "АИ план —",
    "АИ план -",
    "АИ план:",
)


def has_technical_ai_plan_prefix(
    value: str,
) -> bool:
    title = (
        value
        or ""
    ).strip()

    title_folded = (
        title.casefold()
    )

    return any(
        title_folded.startswith(
            prefix.casefold()
        )
        for prefix
        in TECHNICAL_AI_PLAN_PREFIXES
    )


def clean_calendar_title(value: str) -> str:
    """
    Старые события с техническим префиксом
    всё ещё можно нормально отображать.

    Создание таких событий запрещается
    отдельно в write-path.
    """

    title = (
        value
        or ""
    ).strip()

    for prefix in TECHNICAL_AI_PLAN_PREFIXES:
        if title.casefold().startswith(
            prefix.casefold()
        ):
            return title[
                len(prefix):
            ].strip()

    return title



def _fact_status_from_description(
    value: str,
) -> str | None:
    """
    Читает служебную отметку фактического хода дня
    из описания календарного события.

    Google Calendar может возвращать описание
    как обычный текст или HTML.
    """

    text = (
        value
        or ""
    ).strip().casefold()

    if not text:
        return None

    markers = (
        (
            "факт дня: выполнено",
            "completed",
        ),
        (
            "факт дня: пропущено",
            "skipped",
        ),
        (
            "факт дня: изменено",
            "changed",
        ),
    )

    for marker, status in markers:
        if marker in text:
            return status

    return None


def _mark_clippy_event_write(
    event: dict,
    action_type: str,
) -> dict:

    extended = event.get(
        "extendedProperties"
    )

    if not isinstance(
        extended,
        dict,
    ):
        extended = {}
        event[
            "extendedProperties"
        ] = extended

    private = extended.get(
        "private"
    )

    if not isinstance(
        private,
        dict,
    ):
        private = {}
        extended[
            "private"
        ] = private

    private.update({
        "clippy_writer": (
            "clippy_server"
        ),
        "clippy_action": (
            action_type
        ),
        "clippy_written_at": (
            datetime.now(TZ)
            .isoformat()
        ),
    })

    return event


def _linked_google_task_meta(
    event: dict,
) -> dict:
    extended = (
        event.get("extendedProperties")
        or {}
    )

    private = (
        extended.get("private")
        or {}
    )

    task_list_id = (
        private.get(
            "google_task_list_id"
        )
        or ""
    )

    task_id = (
        private.get(
            "google_task_id"
        )
        or ""
    )

    if not task_list_id or not task_id:
        return {}

    return {
        "task_list_id": task_list_id,
        "task_id": task_id,
    }


def _mark_linked_task_event(
    event: dict,
    task_list_id: str,
    task_id: str,
) -> dict:

    event = _mark_clippy_event_write(
        event,
        "task_block",
    )

    private = (
        event
        .setdefault(
            "extendedProperties",
            {},
        )
        .setdefault(
            "private",
            {},
        )
    )

    private.update({
        "clippy_object": (
            "linked_google_task"
        ),
        "google_task_list_id": (
            task_list_id
        ),
        "google_task_id": (
            task_id
        ),
    })

    return event


def get_day_schedule(
    target_date: date,
) -> list[dict]:

    service = get_calendar_service(
        write=False
    )

    day_start = datetime.combine(
        target_date,
        time.min,
        tzinfo=TZ,
    )

    day_end = day_start + timedelta(
        days=1
    )

    calendars = {
        "Личный": os.environ[
            "GOOGLE_PERSONAL_CALENDAR_ID"
        ],
        "Татуировки": os.environ[
            "GOOGLE_CALENDAR_ID"
        ],
        "Проекты Clippy": os.environ[
            "GOOGLE_PROJECTS_CALENDAR_ID"
        ],
        "OZON": os.environ[
            "GOOGLE_OZON_CALENDAR_ID"
        ],
    }

    result = []

    for calendar_name, calendar_id \
            in calendars.items():

        response = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=day_start.isoformat(),
                timeMax=day_end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=100,
            )
            .execute()
        )

        for event in response.get(
            "items",
            [],
        ):
            start = event.get(
                "start",
                {},
            )

            end = event.get(
                "end",
                {},
            )

            all_day = bool(
                start.get("date")
            )

            if all_day:
                start_text = "весь день"
                end_text = ""
                start_iso = start.get("date")
                end_iso = end.get("date")

            else:
                start_dt = (
                    _parse_event_datetime(
                        start["dateTime"]
                    )
                )

                end_dt = (
                    _parse_event_datetime(
                        end["dateTime"]
                    )
                )

                start_text = (
                    start_dt.strftime("%H:%M")
                )

                end_text = (
                    end_dt.strftime("%H:%M")
                )

                start_iso = (
                    start_dt.isoformat()
                )

                end_iso = (
                    end_dt.isoformat()
                )

            event_title = clean_calendar_title(event.get(
                "summary",
                "Без названия",
            ))

            planning_type = classify_planning_event(
                calendar_name,
                event_title,
            )

            linked_task = (
                _linked_google_task_meta(
                    event
                )
            )

            result.append({
                "calendar": calendar_name,
                "calendar_id": calendar_id,
                "event_id": event["id"],
                "source": (
                    "linked_google_task"
                    if linked_task
                    else "calendar"
                ),
                "task_list_id": (
                    linked_task.get(
                        "task_list_id",
                        "",
                    )
                ),
                "task_id": (
                    linked_task.get(
                        "task_id",
                        "",
                    )
                ),
                "title": event_title,
                "description": event.get(
                    "description",
                    "",
                ),
                "location": event.get(
                    "location",
                    "",
                ),
                "fact_status": _fact_status_from_description(
                    event.get(
                        "description",
                        "",
                    )
                ),
                "writer": (
                    (
                        (
                            event.get(
                                "extendedProperties"
                            )
                            or {}
                        ).get(
                            "private"
                        )
                        or {}
                    ).get(
                        "clippy_writer"
                    )
                ),
                "write_action": (
                    (
                        (
                            event.get(
                                "extendedProperties"
                            )
                            or {}
                        ).get(
                            "private"
                        )
                        or {}
                    ).get(
                        "clippy_action"
                    )
                ),
                "written_at": (
                    (
                        (
                            event.get(
                                "extendedProperties"
                            )
                            or {}
                        ).get(
                            "private"
                        )
                        or {}
                    ).get(
                        "clippy_written_at"
                    )
                ),
                "planning_type": planning_type,
                "movable": planning_type in {
                    "adjustable",
                    "flexible",
                },
                "start": start_text,
                "end": end_text,
                "start_iso": start_iso,
                "end_iso": end_iso,
                "all_day": all_day,
                "_sort": _event_start(
                    event
                ),
            })

    try:
        from google_tasks_tools import (
            get_google_tasks_for_date
        )

        google_tasks = (
            get_google_tasks_for_date(
                target_date
            )
        )

    except Exception:
        google_tasks = []

    for task in google_tasks:
        result.append({
            "calendar": "Google Tasks",
            "calendar_id": "google_tasks",
            "event_id": task.get(
                "id",
                "",
            ),
            "source": "google_tasks",
            "task_list_id": task.get(
                "task_list_id",
                "",
            ),
            "task_id": task.get(
                "task_id",
                task.get("id", ""),
            ),
            "title": task.get(
                "title",
                "Без названия",
            ),
            "description": "",
            "location": "",
            "fact_status": "",
            "writer": "",
            "write_action": "",
            "written_at": "",
            "planning_type": "google_task",
            "movable": True,
            "start": "весь день",
            "end": "",
            "start_iso": "",
            "end_iso": "",
            "all_day": True,
            "_sort": day_start,
            "task_status": task.get(
                "status",
                "needsAction",
            ),
        })

    result.sort(
        key=lambda item: item["_sort"]
    )

    for item in result:
        item.pop("_sort", None)

    return result


def format_day_schedule(
    target_date: date,
    events: list[dict],
) -> str:

    date_text = target_date.strftime(
        "%d.%m.%Y"
    )

    if not events:
        return (
            f"{date_text}: событий "
            "в подключённых календарях нет."
        )

    lines = [
        f"Расписание на {date_text}:"
    ]

    for event in events:
        if event["all_day"]:
            when = "весь день"
        else:
            when = (
                f'{event["start"]}'
                f'–{event["end"]}'
            )

        lines.append(
            f'• {when} — '
            f'{event["title"]} '
            f'[{event["calendar"]}]'
        )

    return "\n".join(lines)


def get_managed_calendars() -> list[dict]:
    """Return the explicit calendar allowlist without discovery."""

    return [
        {
            "name": "Личный",
            "calendar_id": os.environ["GOOGLE_PERSONAL_CALENDAR_ID"],
        },
        {
            "name": "Татуировки",
            "calendar_id": os.environ["GOOGLE_CALENDAR_ID"],
        },
        {
            "name": "Проекты Clippy",
            "calendar_id": os.environ["GOOGLE_PROJECTS_CALENDAR_ID"],
        },
        {
            "name": "OZON",
            "calendar_id": os.environ["GOOGLE_OZON_CALENDAR_ID"],
        },
    ]


def _assert_managed_calendar(calendar_id) -> str:
    for item in get_managed_calendars():
        if calendar_id == item["calendar_id"]:
            return item["name"]

    raise ValueError(
        "Изменения разрешены только в подключённых календарях"
    )


def calendar_actions_require_confirmation(
    actions: list[dict],
) -> bool:
    ozon_id = os.environ["GOOGLE_OZON_CALENDAR_ID"]
    return any(
        action.get("calendar_id") == ozon_id
        or bool(action.get("requires_confirmation"))
        for action in actions
    )


def _assert_personal_calendar(
    calendar_id,
):
    personal_id = os.environ[
        "GOOGLE_PERSONAL_CALENDAR_ID"
    ]

    if calendar_id != personal_id:
        raise ValueError(
            "Изменения пока разрешены "
            "только в личном календаре"
        )


def _assert_tattoo_calendar(
    calendar_id,
):
    tattoo_id = os.environ[
        "GOOGLE_CALENDAR_ID"
    ]

    if calendar_id != tattoo_id:
        raise ValueError(
            "Неверный календарь тату-сеансов"
        )


def _format_dt(dt):
    return dt.strftime(
        "%d.%m %H:%M"
    )


def prepare_calendar_changes(
    actions: list[dict],
) -> dict:

    if not actions:
        return {
            "ok": False,
            "error": "Нет изменений",
        }

    service = get_calendar_service(
        write=False
    )

    prepared = []
    summaries = []

    for action in actions:
        action_type = action.get(
            "type"
        )

        calendar_id = action.get(
            "calendar_id",
            "",
        )

        event_id = action.get(
            "event_id",
            "",
        )

        calendar_name = _assert_managed_calendar(
            calendar_id
        )

        if action_type not in {
            "update",
            "delete",
        }:
            raise ValueError(
                "Недопустимый тип изменения"
            )

        if not event_id:
            raise ValueError(
                "Не указан event_id"
            )

        event = (
            service.events()
            .get(
                calendarId=calendar_id,
                eventId=event_id,
            )
            .execute()
        )

        title = event.get(
            "summary",
            "Без названия",
        )

        if action_type == "delete":
            start = event.get(
                "start",
                {},
            )

            if start.get("dateTime"):
                old_start = (
                    _parse_event_datetime(
                        start["dateTime"]
                    )
                )

                when = _format_dt(
                    old_start
                )
            else:
                when = start.get(
                    "date",
                    "дата неизвестна",
                )

            prepared.append({
                "type": "delete",
                "calendar_id": calendar_id,
                "event_id": event_id,
                "title": title,
            })

            summaries.append(
                f'Удалить «{title}» [{calendar_name}] '
                f'({when})'
            )

            continue

        # UPDATE

        old_start_data = event.get(
            "start",
            {},
        )

        old_end_data = event.get(
            "end",
            {},
        )

        new_start_raw = action.get(
            "new_start"
        )

        new_end_raw = action.get(
            "new_end"
        )

        if bool(new_start_raw) != bool(new_end_raw):
            raise ValueError(
                "new_start и new_end должны быть указаны вместе"
            )

        new_title = action.get("new_title")
        new_description = action.get("new_description")
        new_location = action.get("new_location")
        time_changed = bool(new_start_raw and new_end_raw)
        metadata_changed = any(
            value is not None
            for value in (
                new_title,
                new_description,
                new_location,
            )
        )

        if not time_changed and not metadata_changed:
            raise ValueError(
                "Для update не указано ни одного изменения"
            )

        prepared_action = {
            "type": "update",
            "calendar_id": calendar_id,
            "event_id": event_id,
            "title": title,
        }
        summary_parts = []

        if time_changed:
            new_start = _parse_input_datetime(
                new_start_raw
            )
            new_end = _parse_input_datetime(
                new_end_raw
            )

            if new_end <= new_start:
                raise ValueError(
                    "Конец должен быть позже начала"
                )

            prepared_action["new_start"] = new_start.isoformat()
            prepared_action["new_end"] = new_end.isoformat()

            if (
                old_start_data.get("dateTime")
                and old_end_data.get("dateTime")
            ):
                old_start = _parse_event_datetime(
                    old_start_data["dateTime"]
                )
                old_end = _parse_event_datetime(
                    old_end_data["dateTime"]
                )
                summary_parts.append(
                    f'{_format_dt(old_start)}–{old_end.strftime("%H:%M")}'
                    f' → {_format_dt(new_start)}–{new_end.strftime("%H:%M")}'
                )
            else:
                summary_parts.append(
                    f'новое время {_format_dt(new_start)}'
                    f'–{new_end.strftime("%H:%M")}'
                )

        if new_title is not None:
            new_title = clean_calendar_title(new_title)
            if not new_title:
                raise ValueError("Название события не может быть пустым")
            prepared_action["new_title"] = new_title
            summary_parts.append(f'название → «{new_title}»')

        if new_description is not None:
            prepared_action["new_description"] = new_description.strip()
            summary_parts.append("изменить описание")

        if new_location is not None:
            prepared_action["new_location"] = new_location.strip()
            summary_parts.append("изменить место")

        prepared.append(prepared_action)
        summaries.append(
            f'Изменить «{title}» [{calendar_name}]: '
            + "; ".join(summary_parts)
        )

    payload = {
        "created_at": (
            datetime.now(TZ)
            .isoformat()
        ),
        "actions": prepared,
        "summary": summaries,
    }

    PENDING_FILE.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return {
        "ok": True,
        "count": len(prepared),
        "summary": summaries,
        "requires_confirmation": (
            calendar_actions_require_confirmation(prepared)
        ),
    }



def _normalize_linked_task_title(
    value: str,
) -> str:
    return " ".join(
        "".join(
            ch
            if ch.isalnum()
            else " "
            for ch in (
                value
                or ""
            ).casefold()
        ).split()
    )


def _linked_task_title_matches(
    actual: str,
    requested: str,
) -> bool:

    actual_norm = (
        _normalize_linked_task_title(
            actual
        )
    )

    requested_norm = (
        _normalize_linked_task_title(
            requested
        )
    )

    if (
        not actual_norm
        or not requested_norm
    ):
        return False

    if (
        actual_norm == requested_norm
        or actual_norm in requested_norm
        or requested_norm in actual_norm
    ):
        return True

    actual_words = actual_norm.split()
    requested_words = (
        requested_norm.split()
    )

    def word_match(
        left: str,
        right: str,
    ) -> bool:

        if left == right:
            return True

        return (
            len(left) >= 5
            and len(right) >= 5
            and left[:5] == right[:5]
        )

    return all(
        any(
            word_match(
                actual_word,
                requested_word,
            )
            for requested_word
            in requested_words
        )
        for actual_word
        in actual_words
    )


def prepare_linked_task_reschedule(
    title: str,
    source_date: str,
    new_start: str = "",
    new_end: str = "",
    duration_minutes: int | None = None,
    allow_ozon_overlap: bool = False,
) -> dict:
    """
    Переносит/меняет длительность существующей
    связанной задачи Google Task + Calendar block.

    Сам Google Task синхронизируется в
    apply_pending_changes() через metadata event.
    """

    title = (
        title
        or ""
    ).strip()

    if not title:
        return {
            "ok": False,
            "error": (
                "Название задачи не задано"
            ),
        }

    try:
        source_day = (
            date.fromisoformat(
                source_date
            )
        )
    except Exception:
        return {
            "ok": False,
            "error": (
                "Некорректная исходная дата"
            ),
        }

    events = get_day_schedule(
        source_day
    )

    matches = [
        event
        for event in events
        if (
            event.get("source")
            == "linked_google_task"
            and _linked_task_title_matches(
                event.get(
                    "title",
                    "",
                ),
                title,
            )
        )
    ]

    if not matches:
        return {
            "ok": False,
            "error": (
                "linked_task_not_found"
            ),
            "message": (
                f'Связанная задача '
                f'«{title}» за '
                f'{source_date} не найдена'
            ),
        }

    if len(matches) > 1:
        return {
            "ok": False,
            "error": (
                "linked_task_ambiguous"
            ),
            "matches": [
                {
                    "title": item.get(
                        "title",
                        "",
                    ),
                    "start": item.get(
                        "start",
                        "",
                    ),
                    "end": item.get(
                        "end",
                        "",
                    ),
                }
                for item in matches
            ],
        }

    event = matches[0]

    if (
        event.get("calendar")
        != "Личный"
    ):
        return {
            "ok": False,
            "error": (
                "Связанная задача должна "
                "находиться в личном календаре"
            ),
        }

    if not event.get(
        "event_id"
    ):
        return {
            "ok": False,
            "error": (
                "У задачи отсутствует Calendar event_id"
            ),
        }

    old_start = (
        _parse_input_datetime(
            event["start_iso"]
        )
    )

    old_end = (
        _parse_input_datetime(
            event["end_iso"]
        )
    )

    old_duration = (
        old_end - old_start
    )

    # Если новое начало не указано,
    # оставляем текущее.
    if (
        new_start
        and new_start.strip()
    ):
        try:
            start_dt = (
                _parse_input_datetime(
                    new_start
                )
            )
        except Exception:
            return {
                "ok": False,
                "error": (
                    "Некорректное новое время начала"
                ),
            }
    else:
        start_dt = old_start

    # Приоритет:
    # 1. explicit new_end;
    # 2. duration_minutes;
    # 3. прежняя длительность.
    if (
        new_end
        and new_end.strip()
    ):
        try:
            end_dt = (
                _parse_input_datetime(
                    new_end
                )
            )
        except Exception:
            return {
                "ok": False,
                "error": (
                    "Некорректное новое время окончания"
                ),
            }

    elif duration_minutes is not None:

        try:
            duration_minutes = int(
                duration_minutes
            )
        except Exception:
            return {
                "ok": False,
                "error": (
                    "Некорректная длительность"
                ),
            }

        if duration_minutes <= 0:
            return {
                "ok": False,
                "error": (
                    "Длительность должна быть больше нуля"
                ),
            }

        end_dt = (
            start_dt
            + timedelta(
                minutes=duration_minutes
            )
        )

    else:
        end_dt = (
            start_dt
            + old_duration
        )

    if end_dt <= start_dt:
        return {
            "ok": False,
            "error": (
                "Конец должен быть позже начала"
            ),
        }

    if (
        start_dt
        < datetime.now(TZ)
    ):
        return {
            "ok": False,
            "error": (
                "Нельзя перенести задачу "
                "в уже прошедшее время"
            ),
        }

    planning_type = (
        classify_planning_event(
            "Личный",
            event.get(
                "title",
                title,
            ),
        )
    )

    if planning_type not in {
        "flexible",
        "adjustable",
    }:
        return {
            "ok": False,
            "error": (
                f'«{event.get("title", title)}» '
                "не является гибкой задачей"
            ),
        }

    # Выполненные задачи не переносим,
    # пока пользователь явно не вернёт их
    # в состояние needsAction.
    task_list_id = (
        event.get(
            "task_list_id"
        )
        or ""
    )

    task_id = (
        event.get(
            "task_id"
        )
        or ""
    )

    if (
        task_list_id
        and task_id
    ):
        try:
            from google_tasks_tools import (
                get_google_task,
            )

            task_state = (
                get_google_task(
                    task_list_id,
                    task_id,
                )
            )

            if (
                task_state.get(
                    "status"
                )
                == "completed"
            ):
                return {
                    "ok": False,
                    "error": (
                        "task_already_completed"
                    ),
                    "message": (
                        "Задача уже отмечена выполненной"
                    ),
                }

        except Exception:
            # Недоступность Tasks API сама по себе
            # не должна уничтожать возможность
            # работать с Calendar block.
            pass

    target_events = (
        get_day_schedule(
            start_dt.date()
        )
    )

    conflicts = []

    for other in target_events:

        if other.get(
            "all_day"
        ):
            continue

        if (
            other.get("event_id")
            == event.get("event_id")
        ):
            continue

        other_start_raw = (
            other.get(
                "start_iso"
            )
        )

        other_end_raw = (
            other.get(
                "end_iso"
            )
        )

        if (
            not other_start_raw
            or not other_end_raw
        ):
            continue

        other_start = (
            _parse_input_datetime(
                other_start_raw
            )
        )

        other_end = (
            _parse_input_datetime(
                other_end_raw
            )
        )

        if not (
            start_dt < other_end
            and end_dt > other_start
        ):
            continue

        if (
            allow_ozon_overlap
            and other.get(
                "calendar"
            )
            == "OZON"
        ):
            continue

        conflicts.append({
            "title": other.get(
                "title",
                "Без названия",
            ),
            "calendar": other.get(
                "calendar",
                "",
            ),
            "start": other.get(
                "start",
                "",
            ),
            "end": other.get(
                "end",
                "",
            ),
        })

    if conflicts:
        return {
            "ok": False,
            "error": (
                "Новое время пересекается "
                "с существующим расписанием"
            ),
            "conflicts": conflicts,
        }

    action = {
        "type": "update",
        "calendar_id": event[
            "calendar_id"
        ],
        "event_id": event[
            "event_id"
        ],
        "title": event.get(
            "title",
            title,
        ),
        "new_start": (
            start_dt.isoformat()
        ),
        "new_end": (
            end_dt.isoformat()
        ),
    }

    payload = {
        "created_at": (
            datetime.now(TZ)
            .isoformat()
        ),
        "actions": [
            action
        ],
        "summary": [
            (
                f'Перенести '
                f'«{event.get("title", title)}» '
                f'→ '
                f'{start_dt.strftime("%d.%m %H:%M")}'
                f'–{end_dt.strftime("%H:%M")}'
            )
        ],
    }

    PENDING_FILE.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return {
        "ok": True,
        "summary": payload[
            "summary"
        ],
        "requires_confirmation": False,
        "linked_google_task": True,
        "new_start": (
            start_dt.isoformat()
        ),
        "new_end": (
            end_dt.isoformat()
        ),
    }


def prepare_personal_event_create(
    title: str,
    start_value: str,
    end_value: str,
    description: str = "",
    calendar_id: str = "",
    location: str = "",
    allow_ozon_overlap: bool = False,
) -> dict:

    raw_title = (
        title
        or ""
    ).strip()

    if has_technical_ai_plan_prefix(
        raw_title
    ):
        return {
            "ok": False,
            "error": (
                "Техническое название «AI-план» "
                "запрещено. Используй обычное "
                "пользовательское название задачи."
            ),
        }

    title = clean_calendar_title(
        raw_title
    )

    if not title:
        return {
            "ok": False,
            "error": "Название события не задано",
        }

    calendar_id = calendar_id or os.environ[
        "GOOGLE_PERSONAL_CALENDAR_ID"
    ]
    calendar_name = _assert_managed_calendar(
        calendar_id
    )

    try:
        start_dt = _parse_input_datetime(
            start_value
        )

        end_dt = _parse_input_datetime(
            end_value
        )

    except Exception:
        return {
            "ok": False,
            "error": "Некорректные дата или время",
        }

    if end_dt <= start_dt:
        return {
            "ok": False,
            "error": "Конец должен быть позже начала",
        }

    # Проверяем конфликты сразу во всех
    # подключённых календарях.
    events = get_day_schedule(
        start_dt.date()
    )

    conflicts = []

    for event in events:

        if event.get("all_day"):
            continue

        event_start_raw = event.get(
            "start_iso"
        )

        event_end_raw = event.get(
            "end_iso"
        )

        if (
            not event_start_raw
            or not event_end_raw
        ):
            continue

        event_start = _parse_input_datetime(
            event_start_raw
        )

        event_end = _parse_input_datetime(
            event_end_raw
        )

        if (
            start_dt < event_end
            and end_dt > event_start
        ):
            conflicts.append({
                "title": event.get(
                    "title",
                    "Без названия",
                ),
                "calendar": event.get(
                    "calendar",
                    "",
                ),
                "start": event.get(
                    "start",
                    "",
                ),
                "end": event.get(
                    "end",
                    "",
                ),
            })

    if conflicts:
        non_ozon_conflicts = [
            conflict
            for conflict in conflicts
            if conflict.get("calendar") != "OZON"
        ]

        if not allow_ozon_overlap or non_ozon_conflicts:
            return {
                "ok": False,
                "error": "Новое событие пересекается с существующим расписанием",
                "conflicts": (
                    non_ozon_conflicts
                    if allow_ozon_overlap
                    else conflicts
                ),
            }

    planning_type = classify_personal_create_event(
        title,
    )

    # HARD INVARIANT:
    # личная работа/задача создаётся как
    # Google Task + linked Calendar block.
    #
    # Обычным Calendar event остаются только
    # fixed/anchor события: встречи, врачи,
    # записи и другие реальные события времени.
    if planning_type in {
        "flexible",
        "adjustable",
    }:
        action_type = (
            "create_task_block"
        )
    else:
        action_type = "create"

    action = {
        "type": action_type,
        "calendar_id": calendar_id,
        "title": title,
        "new_start": start_dt.isoformat(),
        "new_end": end_dt.isoformat(),
        "description": (
            description or ""
        ).strip(),
    }

    if conflicts and allow_ozon_overlap:
        action["requires_confirmation"] = True
        action["overlaps_ozon"] = True
    location = (location or "").strip()

    if location:
        action["location"] = location

    if action_type == "create_task_block":
        summary = (
            f'Создать задачу «{title}» '
            f'{start_dt.strftime("%d.%m %H:%M")}'
            f'–{end_dt.strftime("%H:%M")}'
        )
    else:
        summary = (
            f'Создать событие «{title}» '
            f'[{calendar_name}]: '
            f'{start_dt.strftime("%d.%m %H:%M")}'
            f'–{end_dt.strftime("%H:%M")}'
        )

    payload = {
        "created_at": (
            datetime.now(TZ)
            .isoformat()
        ),
        "actions": [action],
        "summary": [summary],
    }

    PENDING_FILE.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return {
        "ok": True,
        "summary": [summary],
        "requires_confirmation": (
            calendar_actions_require_confirmation([action])
        ),
    }



def prepare_managed_event_create(
    calendar_id: str,
    title: str,
    start_value: str,
    end_value: str,
    description: str = "",
    location: str = "",
    allow_ozon_overlap: bool = False,
) -> dict:
    return prepare_personal_event_create(
        title=title,
        start_value=start_value,
        end_value=end_value,
        description=description,
        calendar_id=calendar_id,
        location=location,
        allow_ozon_overlap=allow_ozon_overlap,
    )


def prepare_tattoo_session_create(
    client_name: str,
    start_value: str,
    end_value: str,
    city: str = "Санкт-Петербург",
    project_note: str = "",
    price: str = "",
) -> dict:

    client_name = (
        client_name or ""
    ).strip()

    invalid_client_names = {
        "",
        "не указано",
        "не указан",
        "неизвестно",
        "неизвестный",
        "клиент",
        "без имени",
    }

    if client_name.lower() in invalid_client_names:
        return {
            "ok": False,
            "error": (
                "Имя клиента не указано. "
                "Нужно сначала уточнить имя клиента."
            ),
        }

    city = (
        city or "Санкт-Петербург"
    ).strip()

    project_note = (
        project_note or ""
    ).strip()

    price = (
        price or ""
    ).strip()

    if not client_name:
        return {
            "ok": False,
            "error": "Не указано имя клиента",
        }

    try:
        start_dt = _parse_input_datetime(
            start_value
        )

        end_dt = _parse_input_datetime(
            end_value
        )

    except Exception:
        return {
            "ok": False,
            "error": "Некорректные дата или время",
        }

    if end_dt <= start_dt:
        return {
            "ok": False,
            "error": "Конец должен быть позже начала",
        }

    if start_dt < datetime.now(TZ):
        return {
            "ok": False,
            "error": "Нельзя создать сеанс в прошлом",
        }

    # Проверяем конфликты сразу во всех
    # подключённых календарях.
    events = get_day_schedule(
        start_dt.date()
    )

    conflicts = []

    for event in events:

        if event.get("all_day"):
            continue

        event_start_raw = event.get(
            "start_iso"
        )

        event_end_raw = event.get(
            "end_iso"
        )

        if (
            not event_start_raw
            or not event_end_raw
        ):
            continue

        event_start = _parse_input_datetime(
            event_start_raw
        )

        event_end = _parse_input_datetime(
            event_end_raw
        )

        if (
            start_dt < event_end
            and end_dt > event_start
        ):
            conflicts.append({
                "title": event.get(
                    "title",
                    "Без названия",
                ),
                "calendar": event.get(
                    "calendar",
                    "",
                ),
                "start": event.get(
                    "start",
                    "",
                ),
                "end": event.get(
                    "end",
                    "",
                ),
            })

    if conflicts:
        return {
            "ok": False,
            "error": (
                "Тату-сеанс пересекается "
                "с существующим расписанием"
            ),
            "conflicts": conflicts,
        }

    calendar_id = os.environ[
        "GOOGLE_CALENDAR_ID"
    ]

    title = (
        f"Тату-сеанс — {client_name}"
    )

    description_parts = [
        f"Клиент: {client_name}",
        f"Город: {city}",
    ]

    if project_note:
        description_parts.append(
            f"Проект: {project_note}"
        )

    if price:
        description_parts.append(
            f"Стоимость: {price}"
        )

    description_parts.append(
        "Добавлено через Clippy Assistant"
    )

    description = "\n".join(
        description_parts
    )

    action = {
        "type": "create_tattoo",
        "calendar_id": calendar_id,
        "title": title,
        "client_name": client_name,
        "city": city,
        "project_note": project_note,
        "price": price,
        "description": description,
        "new_start": start_dt.isoformat(),
        "new_end": end_dt.isoformat(),
    }

    summary = (
        f'Записать {client_name}: '
        f'{start_dt.strftime("%d.%m %H:%M")}'
        f'–{end_dt.strftime("%H:%M")}, '
        f'{city}'
    )

    if project_note:
        summary += (
            f" — {project_note}"
        )

    if price:
        summary += (
            f" — стоимость: {price}"
        )

    payload = {
        "created_at": (
            datetime.now(TZ)
            .isoformat()
        ),
        "actions": [action],
        "summary": [summary],
    }

    PENDING_FILE.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return {
        "ok": True,
        "summary": [summary],
        "requires_confirmation": (
            calendar_actions_require_confirmation([action])
        ),
    }



def prepare_tattoo_session_changes(
    actions: list[dict],
) -> dict:

    if not actions:
        return {
            "ok": False,
            "error": "Нет изменений",
        }

    service = get_calendar_service(
        write=False
    )

    tattoo_calendar_id = os.environ[
        "GOOGLE_CALENDAR_ID"
    ]

    prepared = []
    summaries = []

    for action in actions:

        action_type = action.get(
            "type"
        )

        calendar_id = action.get(
            "calendar_id",
            ""
        )

        event_id = action.get(
            "event_id",
            ""
        )

        _assert_tattoo_calendar(
            calendar_id
        )

        if not event_id:
            raise ValueError(
                "Не указан event_id"
            )

        if action_type not in {
            "update_tattoo",
            "delete_tattoo",
        }:
            raise ValueError(
                "Недопустимый тип изменения"
            )

        event = (
            service.events()
            .get(
                calendarId=tattoo_calendar_id,
                eventId=event_id,
            )
            .execute()
        )

        title = event.get(
            "summary",
            "Тату-сеанс",
        )

        start_data = event.get(
            "start",
            {},
        )

        end_data = event.get(
            "end",
            {},
        )

        if action_type == "delete_tattoo":

            prepared.append({
                "type": "delete_tattoo",
                "calendar_id": tattoo_calendar_id,
                "event_id": event_id,
                "title": title,
            })

            summaries.append(
                f'Удалить тату-сеанс '
                f'«{title}»'
            )

            continue

        if (
            not start_data.get("dateTime")
            or not end_data.get("dateTime")
        ):
            raise ValueError(
                "События на весь день пока не поддерживаются"
            )

        new_start = _parse_input_datetime(
            action["new_start"]
        )

        new_end = _parse_input_datetime(
            action["new_end"]
        )

        if new_end <= new_start:
            raise ValueError(
                "Конец должен быть позже начала"
            )

        # Проверяем новое время на конфликты.
        events = get_day_schedule(
            new_start.date()
        )

        conflicts = []

        for other in events:

            if other.get("event_id") == event_id:
                continue

            if other.get("all_day"):
                continue

            other_start_raw = other.get(
                "start_iso"
            )

            other_end_raw = other.get(
                "end_iso"
            )

            if (
                not other_start_raw
                or not other_end_raw
            ):
                continue

            other_start = _parse_input_datetime(
                other_start_raw
            )

            other_end = _parse_input_datetime(
                other_end_raw
            )

            if (
                new_start < other_end
                and new_end > other_start
            ):
                conflicts.append({
                    "title": other.get(
                        "title",
                        "Без названия",
                    ),
                    "calendar": other.get(
                        "calendar",
                        "",
                    ),
                    "start": other.get(
                        "start",
                        "",
                    ),
                    "end": other.get(
                        "end",
                        "",
                    ),
                })

        if conflicts:
            return {
                "ok": False,
                "error": (
                    "Новое время сеанса "
                    "пересекается с расписанием"
                ),
                "conflicts": conflicts,
            }

        old_start = _parse_event_datetime(
            start_data["dateTime"]
        )

        old_end = _parse_event_datetime(
            end_data["dateTime"]
        )

        prepared.append({
            "type": "update_tattoo",
            "calendar_id": tattoo_calendar_id,
            "event_id": event_id,
            "title": title,
            "new_start": new_start.isoformat(),
            "new_end": new_end.isoformat(),
        })

        summaries.append(
            f'Перенести «{title}»: '
            f'{old_start.strftime("%d.%m %H:%M")}'
            f'–{old_end.strftime("%H:%M")} → '
            f'{new_start.strftime("%d.%m %H:%M")}'
            f'–{new_end.strftime("%H:%M")}'
        )

    payload = {
        "created_at": datetime.now(TZ).isoformat(),
        "actions": prepared,
        "summary": summaries,
    }

    PENDING_FILE.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return {
        "ok": True,
        "count": len(prepared),
        "summary": summaries,
        "requires_confirmation": (
            calendar_actions_require_confirmation(prepared)
        ),
    }




def _plan_action_calendar_id(
    action: dict,
) -> str:
    """
    Выбирает календарь для действия плана.

    Личные рабочие блоки -> личный календарь.
    Тату-сеансы -> календарь клиентов.
    """

    personal_id = os.environ[
        "GOOGLE_PERSONAL_CALENDAR_ID"
    ]

    tattoo_id = os.environ[
        "GOOGLE_CALENDAR_ID"
    ]

    title = (
        action.get("title")
        or ""
    ).strip().lower()

    calendar_kind = (
        action.get("calendar_kind")
        or ""
    ).strip().lower()

    # Явная маршрутизация от AI.
    if calendar_kind in {
        "tattoo",
        "tattoo_client",
        "tattoo_clients",
        "client",
    }:
        return tattoo_id

    # Защита для уже сохранённых и старых предложений,
    # где calendar_id ошибочно был личным.
    tattoo_markers = (
        "тату-сеанс",
        "тату сеанс",
        "сеанс тату",
        "татуировка —",
        "татуировка -",
    )

    if (
        action.get("type") == "create"
        and any(
            marker in title
            for marker in tattoo_markers
        )
    ):
        return tattoo_id

    explicit_id = (
        action.get("calendar_id")
        or ""
    ).strip()

    if explicit_id:
        return explicit_id

    return personal_id


def _plan_tattoo_client_name(
    action: dict,
    title: str,
) -> str:
    explicit = (
        action.get("client_name")
        or ""
    ).strip()

    if explicit:
        return explicit

    for separator in ("—", "-"):
        if separator in title:
            candidate = title.split(separator, 1)[1].strip()
            if candidate:
                return candidate

    return title


def save_plan_proposal(
    target_date: str,
    actions: list[dict],
    summary: str = "",
) -> dict:

    try:
        plan_date = date.fromisoformat(
            target_date
        )
    except ValueError:
        return {
            "ok": False,
            "error": "Некорректная дата плана",
        }

    if not actions:
        return {
            "ok": False,
            "error": "План не содержит действий",
        }

    personal_id = os.environ[
        "GOOGLE_PERSONAL_CALENDAR_ID"
    ]

    tattoo_id = os.environ[
        "GOOGLE_CALENDAR_ID"
    ]

    normalized = []

    for action in actions:

        action_type = action.get(
            "type"
        )

        if action_type not in {
            "update",
            "delete",
            "create",
        }:
            return {
                "ok": False,
                "error": (
                    "План может содержать "
                    "только create/update/delete"
                ),
            }

        raw_title = (
            action.get("title")
            or ""
        ).strip()

        if has_technical_ai_plan_prefix(
            raw_title
        ):
            return {
                "ok": False,
                "error": (
                    "План содержит запрещённое "
                    "техническое название «AI-план»"
                ),
            }

        title = clean_calendar_title(
            raw_title
        )

        if not title:
            return {
                "ok": False,
                "error": (
                    "У одного из блоков "
                    "нет названия"
                ),
            }

        calendar_id = _plan_action_calendar_id(
            action
        )

        if calendar_id not in {
            personal_id,
            tattoo_id,
        }:
            return {
                "ok": False,
                "error": (
                    f"Недопустимый календарь "
                    f"для «{title}»"
                ),
            }

        if (
            action_type in {"update", "delete"}
            and calendar_id != personal_id
        ):
            return {
                "ok": False,
                "error": (
                    f"Планировщик может переносить или удалять "
                    f"только личные гибкие блоки: «{title}»"
                ),
            }

        item = {
            "type": action_type,
            "calendar_id": calendar_id,
            "title": title,
            "description": (
                action.get("description")
                or ""
            ).strip(),
            "allow_ozon_overlap": bool(
                action.get(
                    "allow_ozon_overlap",
                    False,
                )
            ),
        }

        if (
            item["allow_ozon_overlap"]
            and (
                calendar_id != personal_id
                or action_type not in {
                    "create",
                    "update",
                }
            )
        ):
            return {
                "ok": False,
                "error": (
                    "Пересечение со сменой OZON "
                    "разрешено только для создания "
                    "или переноса личной задачи"
                ),
            }

        if action_type in {"update", "delete"}:
            event_id = (
                action.get("event_id")
                or ""
            ).strip()

            if not event_id:
                return {
                    "ok": False,
                    "error": (
                        f"Для изменения «{title}» "
                        "не указан event_id"
                    ),
                }

            item["event_id"] = event_id

        if action_type == "delete":
            item["start"] = None
            item["end"] = None
            normalized.append(item)
            continue

        try:
            start_dt = _parse_input_datetime(
                action["start"]
            )

            end_dt = _parse_input_datetime(
                action["end"]
            )

        except Exception:
            return {
                "ok": False,
                "error": (
                    f"Некорректное время "
                    f"для «{title}»"
                ),
            }

        if end_dt <= start_dt:
            return {
                "ok": False,
                "error": (
                    f"Некорректная длительность "
                    f"«{title}»"
                ),
            }

        if end_dt <= datetime.now(TZ):
            return {
                "ok": False,
                "error": (
                    f"План не может создавать или переносить "
                    f"задачу в прошедшее время: «{title}»"
                ),
            }

        item["start"] = start_dt.isoformat()
        item["end"] = end_dt.isoformat()

        if (
            action_type == "create"
            and calendar_id == tattoo_id
        ):
            item["client_name"] = (
                _plan_tattoo_client_name(
                    action,
                    title,
                )
            )
            item["city"] = (
                action.get("city")
                or "Санкт-Петербург"
            ).strip()
            item["project_note"] = (
                action.get("project_note")
                or ""
            ).strip()
            item["price"] = (
                action.get("price")
                or ""
            ).strip()

        normalized.append(
            item
        )

    # --------------------------------------------------
    # BACKEND INVARIANT:
    # перенос существующей личной задачи никогда
    # не должен превращаться в delete + create.
    #
    # Если модель предложила:
    #   create «эскизы» на новое время
    #   delete «эскизы» со старого времени
    #
    # автоматически превращаем это в UPDATE старого
    # Calendar event. Для linked_google_task это
    # сохраняет тот же Google task_id и его историю.
    # --------------------------------------------------

    creates_by_title = {}
    deletes_by_title = {}

    for index, item in enumerate(
        normalized
    ):
        if (
            item.get("calendar_id")
            != personal_id
        ):
            continue

        key = (
            clean_calendar_title(
                item.get(
                    "title",
                    "",
                )
            )
            .casefold()
            .strip()
        )

        if not key:
            continue

        if item.get("type") == "create":
            creates_by_title.setdefault(
                key,
                [],
            ).append(index)

        elif item.get("type") == "delete":
            deletes_by_title.setdefault(
                key,
                [],
            ).append(index)

    replacements = {}
    remove_indexes = set()

    for key in (
        set(creates_by_title)
        & set(deletes_by_title)
    ):
        create_indexes = (
            creates_by_title[key]
        )

        delete_indexes = (
            deletes_by_title[key]
        )

        # Автоматически нормализуем только
        # однозначную пару 1 create + 1 delete.
        if (
            len(create_indexes) != 1
            or len(delete_indexes) != 1
        ):
            continue

        create_index = (
            create_indexes[0]
        )

        delete_index = (
            delete_indexes[0]
        )

        create_item = (
            normalized[
                create_index
            ]
        )

        delete_item = (
            normalized[
                delete_index
            ]
        )

        event_id = (
            delete_item.get(
                "event_id"
            )
            or ""
        ).strip()

        if not event_id:
            continue

        replacements[
            create_index
        ] = {
            "type": "update",
            "calendar_id": (
                personal_id
            ),
            "event_id": event_id,
            "title": (
                delete_item.get(
                    "title"
                )
                or create_item.get(
                    "title"
                )
                or ""
            ),
            "description": (
                create_item.get(
                    "description"
                )
                or ""
            ),
            "allow_ozon_overlap": bool(
                create_item.get(
                    "allow_ozon_overlap",
                    False,
                )
            ),
            "start": (
                create_item[
                    "start"
                ]
            ),
            "end": (
                create_item[
                    "end"
                ]
            ),
        }

        remove_indexes.add(
            delete_index
        )

    if replacements:
        normalized = [
            replacements.get(
                index,
                item,
            )
            for index, item
            in enumerate(normalized)
            if index not in remove_indexes
        ]

    payload = {
        "created_at": (
            datetime.now(TZ).isoformat()
        ),
        "target_date": (
            plan_date.isoformat()
        ),
        "summary": (
            summary or ""
        ).strip(),
        "actions": normalized,
    }

    PLAN_PROPOSAL_FILE.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return {
        "ok": True,
        "saved": True,
        "target_date": (
            plan_date.isoformat()
        ),
        "actions_count": len(
            normalized
        ),
    }



def clear_saved_plan_proposal():
    if PLAN_PROPOSAL_FILE.exists():
        PLAN_PROPOSAL_FILE.unlink()


def get_saved_plan_proposal():
    if not PLAN_PROPOSAL_FILE.exists():
        return None

    try:
        return json.loads(
            PLAN_PROPOSAL_FILE.read_text(
                encoding="utf-8"
            )
        )
    except Exception:
        return None


def _assert_plan_has_no_plain_personal_creates(
    prepared: list[dict],
    personal_calendar_id: str,
) -> dict:
    """
    HARD backend invariant для plan-proposal.

    После подготовки плана новая личная задача
    обязана иметь type=create_task_block.

    Обычный type=create в личном календаре
    из планировщика запрещён.
    """

    violations = []

    for item in (
        prepared
        or []
    ):
        if (
            item.get("type")
            == "create"
            and item.get(
                "calendar_id"
            )
            == personal_calendar_id
        ):
            violations.append(
                item.get(
                    "title",
                    "Без названия",
                )
            )

    if violations:
        return {
            "ok": False,
            "error": (
                "plan_plain_personal_create_forbidden"
            ),
            "violations": violations,
        }

    return {
        "ok": True,
    }


def prepare_saved_plan_for_confirmation():
    proposal = get_saved_plan_proposal()

    if not proposal:
        return {
            "ok": False,
            "error": (
                "Нет сохранённого плана. "
                "Сначала распланируй день."
            ),
        }

    actions = proposal.get(
        "actions",
        []
    )

    if not actions:
        return {
            "ok": False,
            "error": "Сохранённый план пуст",
        }

    service = get_calendar_service(
        write=False
    )

    personal_id = os.environ[
        "GOOGLE_PERSONAL_CALENDAR_ID"
    ]

    tattoo_id = os.environ[
        "GOOGLE_CALENDAR_ID"
    ]

    prepared = []
    summaries = []

    # События, которые план собирается переносить,
    # не должны конфликтовать сами со своим старым временем.
    vacating_event_ids = {
        item["event_id"]
        for item in actions
        if (
            item.get("type") in {"update", "delete"}
            and item.get("event_id")
        )
    }

    candidate_intervals = []

    for item in actions:

        raw_title = (
            item.get("title")
            or ""
        ).strip()

        if has_technical_ai_plan_prefix(
            raw_title
        ):
            return {
                "ok": False,
                "error": (
                    "Сохранённый план содержит "
                    "запрещённое техническое "
                    "название «AI-план»"
                ),
            }

        action_type = item["type"]

        calendar_id = (
            _plan_action_calendar_id(
                item
            )
        )

        if action_type == "delete":
            if calendar_id != personal_id:
                return {
                    "ok": False,
                    "error": (
                        f'«{item["title"]}» находится не в личном '
                        "календаре и не может быть удалено планировщиком"
                    ),
                }

            event = (
                service.events()
                .get(
                    calendarId=calendar_id,
                    eventId=item["event_id"],
                )
                .execute()
            )

            current_title = event.get(
                "summary",
                item["title"],
            )

            planning_type = classify_planning_event(
                "Личный",
                current_title,
            )

            if planning_type not in {
                "flexible",
                "adjustable",
            }:
                return {
                    "ok": False,
                    "error": (
                        f'«{current_title}» не разрешено '
                        "автоматически удалять"
                    ),
                }

            prepared.append({
                "type": "delete",
                "calendar_id": calendar_id,
                "event_id": item["event_id"],
                "title": current_title,
            })

            summaries.append(
                f'Удалить «{current_title}»'
            )
            continue

        if action_type not in {"update", "create"}:
            return {
                "ok": False,
                "error": "Неизвестный тип действия плана",
            }

        start_dt = _parse_input_datetime(
            item["start"]
        )

        end_dt = _parse_input_datetime(
            item["end"]
        )

        if end_dt <= start_dt:
            return {
                "ok": False,
                "error": (
                    f'Некорректное время '
                    f'«{item["title"]}»'
                ),
            }

        # Не допускаем пересечений
        # внутри самого нового плана.
        for other_start, other_end, other_title \
                in candidate_intervals:

            if (
                start_dt < other_end
                and end_dt > other_start
            ):
                return {
                    "ok": False,
                    "error": (
                        f'В плане пересекаются '
                        f'«{item["title"]}» и '
                        f'«{other_title}»'
                    ),
                }

        candidate_intervals.append(
            (
                start_dt,
                end_dt,
                item["title"],
            )
        )

        if action_type == "update":

            # Планировщик может автоматически двигать
            # только личные гибкие блоки.
            # Клиентские записи изменяются отдельными
            # инструментами календаря.
            if calendar_id != personal_id:
                return {
                    "ok": False,
                    "error": (
                        f'«{item["title"]}» находится '
                        "не в личном календаре и не может "
                        "быть автоматически перенесено "
                        "планировщиком"
                    ),
                }

            event = (
                service.events()
                .get(
                    calendarId=calendar_id,
                    eventId=item[
                        "event_id"
                    ],
                )
                .execute()
            )

            current_title = event.get(
                "summary",
                item["title"],
            )

            planning_type = (
                classify_planning_event(
                    "Личный",
                    current_title,
                )
            )

            if planning_type not in {
                "flexible",
                "adjustable",
            }:
                return {
                    "ok": False,
                    "error": (
                        f'«{current_title}» '
                        "не разрешено автоматически "
                        "переносить"
                    ),
                }

            prepared_action = {
                "type": "update",
                "calendar_id": calendar_id,
                "event_id": item[
                    "event_id"
                ],
                "title": current_title,
                "new_start": (
                    start_dt.isoformat()
                ),
                "new_end": (
                    end_dt.isoformat()
                ),
            }

            if item.get(
                "allow_ozon_overlap",
                False,
            ):
                prepared_action[
                    "overlaps_ozon"
                ] = True

            prepared.append(
                prepared_action
            )

            summaries.append(
                f'Перенести «{current_title}» '
                f'→ {start_dt.strftime("%H:%M")}'
                f'–{end_dt.strftime("%H:%M")}'
            )

        elif action_type == "create":
            if calendar_id == tattoo_id:
                prepared.append({
                    "type": "create_tattoo",
                    "calendar_id": calendar_id,
                    "title": item["title"],
                    "client_name": item.get(
                        "client_name",
                        item["title"],
                    ),
                    "city": item.get(
                        "city",
                        "Санкт-Петербург",
                    ),
                    "project_note": item.get("project_note", ""),
                    "price": item.get("price", ""),
                    "description": item.get("description", ""),
                    "new_start": start_dt.isoformat(),
                    "new_end": end_dt.isoformat(),
                })

                summaries.append(
                    f'Создать тату-сеанс «{item["title"]}» '
                    f'{start_dt.strftime("%H:%M")}'
                    f'–{end_dt.strftime("%H:%M")}'
                )
            elif calendar_id == personal_id:

                if item.get(
                    "allow_ozon_overlap",
                    False,
                ):
                    planning_type = (
                        classify_personal_create_event(
                            item["title"],
                        )
                    )

                    if planning_type not in {
                        "flexible",
                        "adjustable",
                    }:
                        return {
                            "ok": False,
                            "error": (
                                f'«{item["title"]}» '
                                "не является гибкой личной "
                                "задачей и не может быть "
                                "поставлено поверх смены OZON"
                            ),
                        }

                prepared_action = {
                    "type": "create_task_block",
                    "calendar_id": calendar_id,
                    "title": item["title"],
                    "description": item.get("description", ""),
                    "new_start": start_dt.isoformat(),
                    "new_end": end_dt.isoformat(),
                }

                if item.get(
                    "allow_ozon_overlap",
                    False,
                ):
                    prepared_action[
                        "overlaps_ozon"
                    ] = True

                prepared.append(
                    prepared_action
                )

                summaries.append(
                    f'Создать «{item["title"]}» '
                    f'{start_dt.strftime("%H:%M")}'
                    f'–{end_dt.strftime("%H:%M")}'
                )
            else:
                return {
                    "ok": False,
                    "error": f'Недопустимый календарь для «{item["title"]}»',
                }

    # HARD PLAN PERSONAL TASK INVARIANT
    plan_create_guard = (
        _assert_plan_has_no_plain_personal_creates(
            prepared,
            personal_id,
        )
    )

    if not plan_create_guard.get("ok"):
        return plan_create_guard


    # Проверяем новый план против реального календаря.
    scheduled_actions = [
        item
        for item in actions
        if item.get("type") != "delete"
    ]

    dates_to_check = {
        _parse_input_datetime(
            item["start"]
        ).date()
        for item in scheduled_actions
    }

    existing_events = []

    for check_date in dates_to_check:
        existing_events.extend(
            get_day_schedule(
                check_date
            )
        )

    for item in scheduled_actions:

        start_dt = _parse_input_datetime(
            item["start"]
        )

        end_dt = _parse_input_datetime(
            item["end"]
        )

        item_calendar_id = (
            _plan_action_calendar_id(
                item
            )
        )

        allow_ozon_overlap = bool(
            item.get(
                "allow_ozon_overlap",
                False,
            )
        )

        for event in existing_events:

            if event.get("all_day"):
                continue

            event_id = event.get(
                "event_id"
            )

            # Старые положения переносимых задач
            # будут заменены.
            if event_id in vacating_event_ids:
                continue

            event_start_raw = event.get(
                "start_iso"
            )

            event_end_raw = event.get(
                "end_iso"
            )

            if (
                not event_start_raw
                or not event_end_raw
            ):
                continue

            event_start = _parse_input_datetime(
                event_start_raw
            )

            event_end = _parse_input_datetime(
                event_end_raw
            )

            if (
                start_dt < event_end
                and end_dt > event_start
            ):

                event_calendar = (
                    event.get("calendar")
                    or ""
                ).strip()

                is_ozon = (
                    event_calendar.casefold()
                    == "ozon"
                )

                if (
                    allow_ozon_overlap
                    and item_calendar_id
                    == personal_id
                    and is_ozon
                ):
                    # Смена OZON остаётся в календаре,
                    # но не блокирует подтверждённую
                    # гибкую личную задачу.
                    continue

                return {
                    "ok": False,
                    "error": (
                        f'«{item["title"]}» '
                        f'пересекается с '
                        f'«{event.get("title", "событие")}» '
                        f'[{event.get("calendar", "")}] '
                        f'{event.get("start", "")}'
                        f'–{event.get("end", "")}'
                    ),
                }

    payload = {
        "created_at": (
            datetime.now(TZ).isoformat()
        ),
        "actions": prepared,
        "summary": summaries,
    }

    PENDING_FILE.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return {
        "ok": True,
        "requires_confirmation": (
            calendar_actions_require_confirmation(prepared)
        ),
        "actions_count": len(
            prepared
        ),
        "summary": summaries,
    }


def has_pending_changes():
    if not PENDING_FILE.exists():
        return False

    try:
        data = json.loads(
            PENDING_FILE.read_text(
                encoding="utf-8"
            )
        )

        return bool(
            data.get("actions")
        )

    except Exception:
        return False


def get_pending_target_dates() -> list[date]:
    """Return affected dates without exposing or applying pending actions."""
    if not PENDING_FILE.exists():
        return []

    try:
        payload = json.loads(
            PENDING_FILE.read_text(
                encoding="utf-8"
            )
        )
    except Exception:
        return []

    target_dates = set()

    for action in payload.get("actions", []):
        raw_value = (
            action.get("new_start")
            or action.get("old_start")
            or action.get("start")
        )

        if isinstance(raw_value, dict):
            raw_value = (
                raw_value.get("dateTime")
                or raw_value.get("date")
            )

        if not raw_value:
            continue

        try:
            target_dates.add(
                _parse_input_datetime(raw_value).date()
            )
        except Exception:
            try:
                target_dates.add(date.fromisoformat(raw_value))
            except Exception:
                continue

    return sorted(target_dates)


def clear_pending_changes():
    if PENDING_FILE.exists():
        PENDING_FILE.unlink()


def _save_remaining(
    payload,
    actions,
):
    if not actions:
        clear_pending_changes()
        return

    payload["actions"] = actions

    PENDING_FILE.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def apply_pending_changes():
    if not PENDING_FILE.exists():
        return {
            "ok": False,
            "error": (
                "Нет ожидающих изменений"
            ),
            "applied": [],
        }

    payload = json.loads(
        PENDING_FILE.read_text(
            encoding="utf-8"
        )
    )

    remaining = list(
        payload.get(
            "actions",
            [],
        )
    )

    if not remaining:
        clear_pending_changes()

        return {
            "ok": False,
            "error": (
                "Нет ожидающих изменений"
            ),
            "applied": [],
        }

    service = get_calendar_service(
        write=True
    )

    applied = []

    while remaining:
        action = remaining[0]

        # Технические AI-префиксы никогда не должны
        # попадать в Google Calendar.
        if action.get("title"):
            action["title"] = clean_calendar_title(
                action["title"]
            )

        if action.get("new_title"):
            action["new_title"] = clean_calendar_title(
                action["new_title"]
            )

        try:
            action_type = action["type"]

            if action_type in {
                "create",
                "create_task_block",
                "update",
                "delete",
            }:
                _assert_managed_calendar(
                    action["calendar_id"]
                )

            elif action_type in {
                "create_tattoo",
                "update_tattoo",
                "delete_tattoo",
            }:
                _assert_tattoo_calendar(
                    action["calendar_id"]
                )

            else:
                raise ValueError(
                    "Неизвестный тип изменения"
                )

            if action_type == "delete_tattoo":

                (
                    service.events()
                    .delete(
                        calendarId=action[
                            "calendar_id"
                        ],
                        eventId=action[
                            "event_id"
                        ],
                    )
                    .execute()
                )

                applied.append(
                    f'Тату-сеанс удалён: '
                    f'«{action["title"]}»'
                )

            elif action_type == "update_tattoo":

                event = (
                    service.events()
                    .get(
                        calendarId=action[
                            "calendar_id"
                        ],
                        eventId=action[
                            "event_id"
                        ],
                    )
                    .execute()
                )

                event["start"] = {
                    "dateTime": action[
                        "new_start"
                    ],
                    "timeZone": TIMEZONE_NAME,
                }

                event["end"] = {
                    "dateTime": action[
                        "new_end"
                    ],
                    "timeZone": TIMEZONE_NAME,
                }

                (
                    service.events()
                    .update(
                        calendarId=action[
                            "calendar_id"
                        ],
                        eventId=action[
                            "event_id"
                        ],
                        body=_mark_clippy_event_write(
                            event,
                            action_type,
                        ),
                    )
                    .execute()
                )

                start_dt = _parse_input_datetime(
                    action["new_start"]
                )

                end_dt = _parse_input_datetime(
                    action["new_end"]
                )

                applied.append(
                    f'Тату-сеанс перенесён: '
                    f'«{action["title"]}» '
                    f'{start_dt.strftime("%d.%m %H:%M")}'
                    f'–{end_dt.strftime("%H:%M")}'
                )

            elif action_type == "create_tattoo":

                event_body = {
                    "summary": action["title"],
                    "description": action.get(
                        "description",
                        "",
                    ),
                    "location": action.get(
                        "city",
                        "",
                    ),
                    "start": {
                        "dateTime": action[
                            "new_start"
                        ],
                        "timeZone": TIMEZONE_NAME,
                    },
                    "end": {
                        "dateTime": action[
                            "new_end"
                        ],
                        "timeZone": TIMEZONE_NAME,
                    },
                }

                (
                    service.events()
                    .insert(
                        calendarId=action[
                            "calendar_id"
                        ],
                        body=_mark_clippy_event_write(
                            event_body,
                            action_type,
                        ),
                    )
                    .execute()
                )

                start_dt = (
                    _parse_input_datetime(
                        action["new_start"]
                    )
                )

                end_dt = (
                    _parse_input_datetime(
                        action["new_end"]
                    )
                )

                applied.append(
                    f'Тату-сеанс создан: '
                    f'{action["client_name"]}, '
                    f'{start_dt.strftime("%d.%m %H:%M")}'
                    f'–{end_dt.strftime("%H:%M")}'
                )

            elif action_type == "create_task_block":

                from google_tasks_tools import (
                    create_google_task,
                    delete_google_task,
                )

                start_dt = (
                    _parse_input_datetime(
                        action["new_start"]
                    )
                )

                end_dt = (
                    _parse_input_datetime(
                        action["new_end"]
                    )
                )

                description = (
                    action.get(
                        "description",
                        "",
                    )
                    or ""
                ).strip()

                notes_parts = []

                if description:
                    notes_parts.append(
                        description
                    )

                notes_parts.append(
                    "Плановое время Clippy: "
                    f'{start_dt.strftime("%H:%M")}'
                    "–"
                    f'{end_dt.strftime("%H:%M")}'
                )

                task_result = (
                    create_google_task(
                        title=action[
                            "title"
                        ],
                        target_date=(
                            start_dt.date()
                        ),
                        notes="\n".join(
                            notes_parts
                        ),
                    )
                )

                if not task_result.get(
                    "ok"
                ):
                    raise RuntimeError(
                        task_result.get(
                            "error",
                            "google_task_create_failed",
                        )
                    )

                task_list_id = (
                    task_result[
                        "task_list_id"
                    ]
                )

                task_id = (
                    task_result[
                        "task_id"
                    ]
                )

                event_body = {
                    "summary": action[
                        "title"
                    ],
                    "description": (
                        description
                    ),
                    "location": action.get(
                        "location",
                        "",
                    ),
                    "start": {
                        "dateTime": action[
                            "new_start"
                        ],
                        "timeZone": TIMEZONE_NAME,
                    },
                    "end": {
                        "dateTime": action[
                            "new_end"
                        ],
                        "timeZone": TIMEZONE_NAME,
                    },
                    "transparency": (
                        "transparent"
                    ),
                }

                try:
                    (
                        service.events()
                        .insert(
                            calendarId=action[
                                "calendar_id"
                            ],
                            body=(
                                _mark_linked_task_event(
                                    event_body,
                                    task_list_id,
                                    task_id,
                                )
                            ),
                        )
                        .execute()
                    )

                except Exception:
                    try:
                        delete_google_task(
                            task_list_id,
                            task_id,
                        )
                    except Exception:
                        logging.exception(
                            "Rollback Google Task failed"
                        )

                    raise

                applied.append(
                    f'Создана задача: '
                    f'«{action["title"]}» '
                    f'{start_dt.strftime("%d.%m %H:%M")}'
                    f'–{end_dt.strftime("%H:%M")}'
                )

            elif action_type == "create":

                event_body = {
                    "summary": action["title"],
                    "description": action.get(
                        "description",
                        "",
                    ),
                    "location": action.get(
                        "location",
                        "",
                    ),
                    "start": {
                        "dateTime": action[
                            "new_start"
                        ],
                        "timeZone": TIMEZONE_NAME,
                    },
                    "end": {
                        "dateTime": action[
                            "new_end"
                        ],
                        "timeZone": TIMEZONE_NAME,
                    },
                }

                (
                    service.events()
                    .insert(
                        calendarId=action[
                            "calendar_id"
                        ],
                        body=_mark_clippy_event_write(
                            event_body,
                            action_type,
                        ),
                    )
                    .execute()
                )

                start_dt = (
                    _parse_input_datetime(
                        action["new_start"]
                    )
                )

                end_dt = (
                    _parse_input_datetime(
                        action["new_end"]
                    )
                )

                applied.append(
                    f'Создано: '
                    f'«{action["title"]}» '
                    f'{start_dt.strftime("%d.%m %H:%M")}'
                    f'–{end_dt.strftime("%H:%M")}'
                )

            elif action["type"] == "delete":

                linked_task = {}

                try:
                    existing_event = (
                        service.events()
                        .get(
                            calendarId=action[
                                "calendar_id"
                            ],
                            eventId=action[
                                "event_id"
                            ],
                        )
                        .execute()
                    )

                    linked_task = (
                        _linked_google_task_meta(
                            existing_event
                        )
                    )

                except Exception:
                    linked_task = {}

                (
                    service.events()
                    .delete(
                        calendarId=action[
                            "calendar_id"
                        ],
                        eventId=action[
                            "event_id"
                        ],
                    )
                    .execute()
                )

                if linked_task:
                    try:
                        from google_tasks_tools import (
                            delete_google_task,
                        )

                        delete_google_task(
                            linked_task[
                                "task_list_id"
                            ],
                            linked_task[
                                "task_id"
                            ],
                        )

                    except Exception:
                        logging.exception(
                            "Linked Google Task "
                            "delete failed"
                        )

                applied.append(
                    f'Удалено: '
                    f'«{action["title"]}»'
                )

            elif action["type"] == "update":
                event = (
                    service.events()
                    .get(
                        calendarId=action[
                            "calendar_id"
                        ],
                        eventId=action[
                            "event_id"
                        ],
                    )
                    .execute()
                )

                linked_task = (
                    _linked_google_task_meta(
                        event
                    )
                )

                if action.get("new_start") and action.get("new_end"):
                    event["start"] = {
                        "dateTime": action["new_start"],
                        "timeZone": TIMEZONE_NAME,
                    }
                    event["end"] = {
                        "dateTime": action["new_end"],
                        "timeZone": TIMEZONE_NAME,
                    }

                if "new_title" in action:
                    event["summary"] = action["new_title"]

                if "new_description" in action:
                    event["description"] = action["new_description"]

                if "new_location" in action:
                    event["location"] = action["new_location"]

                (
                    service.events()
                    .update(
                        calendarId=action[
                            "calendar_id"
                        ],
                        eventId=action[
                            "event_id"
                        ],
                        body=_mark_clippy_event_write(
                            event,
                            action_type,
                        ),
                    )
                    .execute()
                )

                if linked_task:
                    try:
                        from google_tasks_tools import (
                            reschedule_google_task,
                        )

                        final_start_raw = (
                            event.get(
                                "start",
                                {},
                            ).get(
                                "dateTime"
                            )
                        )

                        final_end_raw = (
                            event.get(
                                "end",
                                {},
                            ).get(
                                "dateTime"
                            )
                        )

                        if (
                            final_start_raw
                            and final_end_raw
                        ):
                            linked_start = (
                                _parse_input_datetime(
                                    final_start_raw
                                )
                            )

                            linked_end = (
                                _parse_input_datetime(
                                    final_end_raw
                                )
                            )

                            reschedule_google_task(
                                linked_task[
                                    "task_list_id"
                                ],
                                linked_task[
                                    "task_id"
                                ],
                                linked_start.date(),
                                planned_start=(
                                    linked_start
                                ),
                                planned_end=(
                                    linked_end
                                ),
                                new_title=(
                                    event.get(
                                        "summary"
                                    )
                                    or action.get(
                                        "title",
                                        "",
                                    )
                                ),
                            )

                    except Exception:
                        logging.exception(
                            "Linked Google Task "
                            "sync failed"
                        )

                if action.get("new_start") and action.get("new_end"):
                    start_dt = _parse_input_datetime(
                        action["new_start"]
                    )
                    end_dt = _parse_input_datetime(
                        action["new_end"]
                    )
                    applied.append(
                        f'Изменено: «{action["title"]}» '
                        f'{start_dt.strftime("%H:%M")}'
                        f'–{end_dt.strftime("%H:%M")}'
                    )
                else:
                    applied.append(
                        f'Изменено: «{action["title"]}»'
                    )

            remaining.pop(0)

            _save_remaining(
                payload,
                remaining,
            )

        except Exception as exc:
            return {
                "ok": False,
                "error": (
                    type(exc).__name__
                ),
                "applied": applied,
                "remaining": len(
                    remaining
                ),
            }

    clear_saved_plan_proposal()

    return {
        "ok": True,
        "applied": applied,
    }


def find_linked_task_slot(
    title: str,
    source_date: str,
    target_date: str,
    window_start: str = "09:00",
    window_end: str = "23:00",
    allow_ozon_overlap: bool = True,
) -> dict:
    """
    Ищет безопасное новое время для ОДНОЙ уже
    существующей source=linked_google_task.

    Текущий блок выбранной задачи при поиске
    игнорируется, потому что он будет перенесён.

    ВСЕ остальные календарные события блокируют
    время, включая другие flexible/linked задачи.

    Единственное разрешённое пересечение —
    OZON, если allow_ozon_overlap=True.
    """

    title = (
        title
        or ""
    ).strip()

    try:
        source_day = date.fromisoformat(
            source_date
        )

        target_day = date.fromisoformat(
            target_date
        )

    except Exception:
        return {
            "ok": False,
            "error": "invalid_date",
        }

    source_events = get_day_schedule(
        source_day
    )

    matches = [
        event
        for event in source_events
        if (
            event.get("source")
            == "linked_google_task"
            and _linked_task_title_matches(
                event.get(
                    "title",
                    "",
                ),
                title,
            )
        )
    ]

    if not matches:
        return {
            "ok": False,
            "error": "linked_task_not_found",
        }

    if len(matches) > 1:
        return {
            "ok": False,
            "error": "linked_task_ambiguous",
        }

    source_event = matches[0]

    if (
        source_event.get("task_status")
        == "completed"
    ):
        return {
            "ok": False,
            "error": "task_already_completed",
        }

    old_start = _parse_input_datetime(
        source_event["start_iso"]
    )

    old_end = _parse_input_datetime(
        source_event["end_iso"]
    )

    duration = (
        old_end - old_start
    )

    try:
        start_clock = datetime.strptime(
            window_start,
            "%H:%M",
        ).time()

        end_clock = datetime.strptime(
            window_end,
            "%H:%M",
        ).time()

    except Exception:
        return {
            "ok": False,
            "error": "invalid_window",
        }

    day_start = datetime.combine(
        target_day,
        start_clock,
        tzinfo=TZ,
    )

    day_end = datetime.combine(
        target_day,
        end_clock,
        tzinfo=TZ,
    )

    if day_end <= day_start:
        return {
            "ok": False,
            "error": "invalid_window",
        }

    now = datetime.now(TZ)

    if target_day < now.date():
        return {
            "ok": False,
            "error": "target_date_in_past",
        }

    # Если Clippy сама предлагает время на сегодня,
    # оставляем минимум 15 минут на подтверждение.
    if target_day == now.date():

        earliest = (
            now
            + timedelta(
                minutes=15
            )
        ).replace(
            second=0,
            microsecond=0,
        )

        remainder = (
            earliest.minute
            % 15
        )

        if remainder:
            earliest += timedelta(
                minutes=(
                    15 - remainder
                )
            )

        day_start = max(
            day_start,
            earliest,
        )

    if (
        day_start + duration
        > day_end
    ):
        return {
            "ok": False,
            "error": "no_time_left_in_window",
        }

    target_events = get_day_schedule(
        target_day
    )

    busy = []
    ozon_intervals = []

    source_event_id = (
        source_event.get(
            "event_id"
        )
        or ""
    )

    for event in target_events:

        if event.get("all_day"):
            continue

        # Старое положение именно переносимой задачи
        # не блокирует её новое положение.
        if (
            event.get("event_id")
            == source_event_id
        ):
            continue

        start_raw = event.get(
            "start_iso"
        )

        end_raw = event.get(
            "end_iso"
        )

        if (
            not start_raw
            or not end_raw
        ):
            continue

        event_start = _parse_input_datetime(
            start_raw
        )

        event_end = _parse_input_datetime(
            end_raw
        )

        start = max(
            event_start,
            day_start,
        )

        end = min(
            event_end,
            day_end,
        )

        if end <= start:
            continue

        # Только OZON разрешено не считать blocker.
        if (
            allow_ozon_overlap
            and event.get("calendar")
            == "OZON"
        ):
            ozon_intervals.append(
                (
                    event_start,
                    event_end,
                )
            )
            continue

        busy.append(
            (
                start,
                end,
            )
        )

    busy.sort(
        key=lambda item: item[0]
    )

    merged = []

    for start, end in busy:

        if (
            not merged
            or start > merged[-1][1]
        ):
            merged.append(
                [start, end]
            )

        else:
            merged[-1][1] = max(
                merged[-1][1],
                end,
            )

    cursor = day_start
    slot = None

    for busy_start, busy_end in merged:

        if (
            busy_start - cursor
            >= duration
        ):
            slot = (
                cursor,
                cursor + duration,
            )
            break

        cursor = max(
            cursor,
            busy_end,
        )

    if (
        slot is None
        and day_end - cursor
        >= duration
    ):
        slot = (
            cursor,
            cursor + duration,
        )

    if slot is None:
        return {
            "ok": False,
            "error": "no_free_slot",
            "duration_minutes": int(
                duration.total_seconds()
                // 60
            ),
        }

    proposed_start, proposed_end = slot

    overlaps_ozon = any(
        (
            proposed_start < ozon_end
            and proposed_end > ozon_start
        )
        for (
            ozon_start,
            ozon_end,
        )
        in ozon_intervals
    )

    return {
        "ok": True,
        "title": source_event.get(
            "title",
            title,
        ),
        "event_id": source_event[
            "event_id"
        ],
        "calendar_id": source_event[
            "calendar_id"
        ],
        "source_date": (
            source_day.isoformat()
        ),
        "target_date": (
            target_day.isoformat()
        ),
        "old_start": (
            old_start.isoformat()
        ),
        "old_end": (
            old_end.isoformat()
        ),
        "start": (
            proposed_start.isoformat()
        ),
        "end": (
            proposed_end.isoformat()
        ),
        "duration_minutes": int(
            duration.total_seconds()
            // 60
        ),
        "allow_ozon_overlap": (
            overlaps_ozon
        ),
    }


def get_free_intervals(
    target_date: date,
    window_start: str = "09:00",
    window_end: str = "23:00",
) -> list[dict]:

    start_time = datetime.strptime(
        window_start,
        "%H:%M",
    ).time()

    end_time = datetime.strptime(
        window_end,
        "%H:%M",
    ).time()

    day_start = datetime.combine(
        target_date,
        start_time,
        tzinfo=TZ,
    )

    day_end = datetime.combine(
        target_date,
        end_time,
        tzinfo=TZ,
    )

    if day_end <= day_start:
        raise ValueError(
            "Конец окна должен быть позже начала"
        )

    events = get_day_schedule(
        target_date
    )

    busy = []

    for event in events:

        if event.get("all_day"):
            continue

        start_raw = event.get(
            "start_iso"
        )

        end_raw = event.get(
            "end_iso"
        )

        if not start_raw or not end_raw:
            continue

        event_start = _parse_input_datetime(
            start_raw
        )

        event_end = _parse_input_datetime(
            end_raw
        )

        # Ограничиваем событие рабочим окном.
        start = max(
            event_start,
            day_start,
        )

        end = min(
            event_end,
            day_end,
        )

        if end > start:
            busy.append(
                (start, end)
            )

    busy.sort(
        key=lambda item: item[0]
    )

    # Объединяем пересекающиеся события.
    merged = []

    for start, end in busy:

        if (
            not merged
            or start > merged[-1][1]
        ):
            merged.append(
                [start, end]
            )
        else:
            merged[-1][1] = max(
                merged[-1][1],
                end,
            )

    free = []
    cursor = day_start

    for start, end in merged:

        if start > cursor:
            minutes = int(
                (
                    start - cursor
                ).total_seconds()
                // 60
            )

            free.append({
                "start": cursor.isoformat(),
                "end": start.isoformat(),
                "start_time": cursor.strftime(
                    "%H:%M"
                ),
                "end_time": start.strftime(
                    "%H:%M"
                ),
                "minutes": minutes,
            })

        cursor = max(
            cursor,
            end,
        )

    if cursor < day_end:
        minutes = int(
            (
                day_end - cursor
            ).total_seconds()
            // 60
        )

        free.append({
            "start": cursor.isoformat(),
            "end": day_end.isoformat(),
            "start_time": cursor.strftime(
                "%H:%M"
            ),
            "end_time": day_end.strftime(
                "%H:%M"
            ),
            "minutes": minutes,
        })

    return free



def get_planning_intervals(
    target_date: date,
    window_start: str = "09:00",
    window_end: str = "23:00",
) -> dict:

    start_time = datetime.strptime(
        window_start,
        "%H:%M",
    ).time()

    end_time = datetime.strptime(
        window_end,
        "%H:%M",
    ).time()

    day_start = datetime.combine(
        target_date,
        start_time,
        tzinfo=TZ,
    )

    day_end = datetime.combine(
        target_date,
        end_time,
        tzinfo=TZ,
    )

    if day_end <= day_start:
        raise ValueError(
            "Конец окна должен быть позже начала"
        )

    events = get_day_schedule(
        target_date
    )

    hard_busy = []
    movable_events = []

    for event in events:

        planning_type = event.get(
            "planning_type",
            "fixed",
        )

        if planning_type in {
            "flexible",
            "adjustable",
        }:
            movable_events.append(
                event
            )
            continue

        if event.get("all_day"):
            continue

        start_raw = event.get(
            "start_iso"
        )

        end_raw = event.get(
            "end_iso"
        )

        if not start_raw or not end_raw:
            continue

        event_start = _parse_input_datetime(
            start_raw
        )

        event_end = _parse_input_datetime(
            end_raw
        )

        clipped_start = max(
            event_start,
            day_start,
        )

        clipped_end = min(
            event_end,
            day_end,
        )

        if clipped_end > clipped_start:
            hard_busy.append(
                (
                    clipped_start,
                    clipped_end,
                )
            )

    hard_busy.sort(
        key=lambda item: item[0]
    )

    merged = []

    for start, end in hard_busy:

        if (
            not merged
            or start > merged[-1][1]
        ):
            merged.append(
                [start, end]
            )
        else:
            merged[-1][1] = max(
                merged[-1][1],
                end,
            )

    intervals = []
    cursor = day_start

    for start, end in merged:

        if start > cursor:
            intervals.append({
                "start": cursor.isoformat(),
                "end": start.isoformat(),
                "start_time": cursor.strftime(
                    "%H:%M"
                ),
                "end_time": start.strftime(
                    "%H:%M"
                ),
                "minutes": int(
                    (
                        start - cursor
                    ).total_seconds()
                    // 60
                ),
            })

        cursor = max(
            cursor,
            end,
        )

    if cursor < day_end:
        intervals.append({
            "start": cursor.isoformat(),
            "end": day_end.isoformat(),
            "start_time": cursor.strftime(
                "%H:%M"
            ),
            "end_time": day_end.strftime(
                "%H:%M"
            ),
            "minutes": int(
                (
                    day_end - cursor
                ).total_seconds()
                // 60
            ),
        })

    return {
        "date": target_date.isoformat(),
        "planning_window": {
            "start": window_start,
            "end": window_end,
        },
        "available_intervals": intervals,
        "movable_events": movable_events,
        "fixed_events": [
            event
            for event in events
            if not event.get(
                "movable",
                False,
            )
        ],
    }
