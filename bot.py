import hashlib
import io
import json
from urllib.parse import urlencode
from urllib.request import urlopen
import re
import sqlite3
import zipfile
import asyncio
import logging
import os
from contextlib import suppress
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    BufferedInputFile,
)

from ai_agent import (
    ask_assistant,
    get_openai_cost_status,
)
from creative_knowledge import (
    install_creative_database,
    is_creative_database_filename,
)
from chatgpt_archive import (
    import_chatgpt_export_zip,
    is_chatgpt_export_zip,
)
from clippy_task_classifier import classify_task
from voice_tools import (
    transcribe_voice,
    synthesize_voice,
)
from memory_store import (
    clear_booking_context,
    get_booking_context,
    get_last_message,
    get_recent_context,
    has_booking_context,
    save_message,
)
from calendar_tools import (
    apply_pending_changes,
    clear_pending_changes,
    has_pending_changes,
    get_pending_target_dates,
    clear_saved_plan_proposal,
    get_saved_plan_proposal,
    prepare_saved_plan_for_confirmation,
    save_plan_proposal,
    get_day_schedule,
    get_free_intervals,
)
from google_tasks_tools import (
    get_day_overview,
    get_google_task,
)
from project_next_actions import (
    get_project_actions,
    mark_project_actions_planned,
    set_project_action_status,
)
from nightly_project_sync import (
    nightly_sync_is_due,
    sync_calendar_projects,
)

from bot_tools import (
    clear_pending_client_message,
    confirm_pending_client_message,
    has_pending_client_message,
    send_due_client_messages,
)


TOKEN = os.environ.get("TELEGRAM_ASSISTANT_TOKEN", "").strip()
OWNER_ID = int(os.environ.get("OWNER_TELEGRAM_ID", "0"))

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
EVENING_BRIEF_TIME = time(21, 30)
EVENING_BRIEF_WINDOW_END = time(22, 0)
EVENING_BRIEF_STATE_FILE = Path(
    "data/evening_brief_state.json"
)
BRIEF_SEARCH_DAYS = 7
BRIEF_WINDOW_START = "10:00"
BRIEF_WINDOW_END = "23:00"
DAILY_ROUTINE_STATE_FILE = Path(
    "data/daily_routine_state.json"
)
DEFAULT_WAKE_TIME = time(10, 0)
REMINDER_BEFORE = timedelta(minutes=15)
CALENDAR_REFRESH_INTERVAL = timedelta(minutes=5)

if not TOKEN:
    raise RuntimeError("TELEGRAM_ASSISTANT_TOKEN не задан")

if not OWNER_ID:
    raise RuntimeError("OWNER_TELEGRAM_ID не задан")


dp = Dispatcher()


def _load_daily_routine_state() -> dict:
    if not DAILY_ROUTINE_STATE_FILE.exists():
        return {"days": {}}

    try:
        payload = json.loads(
            DAILY_ROUTINE_STATE_FILE.read_text(
                encoding="utf-8"
            )
        )
        if not isinstance(payload.get("days"), dict):
            return {"days": {}}
        return payload
    except Exception:
        return {"days": {}}


def _save_daily_routine_state(payload: dict) -> None:
    days = payload.setdefault("days", {})
    cutoff = datetime.now(MOSCOW_TZ).date() - timedelta(days=14)

    for key in list(days):
        try:
            if date.fromisoformat(key) < cutoff:
                days.pop(key, None)
        except ValueError:
            days.pop(key, None)

    DAILY_ROUTINE_STATE_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _routine_day(payload: dict, target_date: date) -> dict:
    return payload.setdefault("days", {}).setdefault(
        target_date.isoformat(),
        {
            "completed": [],
            "reminders": [],
            "morning_sent": False,
            "tasks": {},
        },
    )


def _event_object_identity(
    event: dict,
) -> tuple[str, str, str]:

    source = str(
        event.get("source")
        or "calendar"
    )

    calendar_id = str(
        event.get("calendar_id")
        or source
    )

    object_id = str(
        event.get("event_id")
        or event.get("task_id")
        or event.get("id")
        or event.get("title")
        or "unknown"
    )

    return (
        source,
        calendar_id,
        object_id,
    )


def _event_task_token(
    event: dict,
) -> str:

    raw = "|".join(
        _event_object_identity(
            event
        )
    )

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:12]


def _event_reminder_token(
    event: dict,
) -> str:

    raw = "|".join([
        *_event_object_identity(
            event
        ),
        str(
            event.get("start_iso")
            or ""
        ),
    ])

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:12]


def _legacy_event_task_token(
    event: dict,
) -> str:
    """
    Формат токена, использовавшийся до Stage 8C.
    Нужен только для миграции уже отмеченных задач.
    """

    raw = "|".join([
        str(
            event.get("calendar_id")
            or ""
        ),
        str(
            event.get("event_id")
            or ""
        ),
        str(
            event.get("start_iso")
            or ""
        ),
    ])

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:12]


def _morning_action_id_for_event(
    day_state: dict,
    event: dict,
) -> str:
    proposal = day_state.get("morning_proposal")

    if not isinstance(proposal, dict):
        return ""

    if (
        proposal.get("source") != "project_next_actions"
        or not proposal.get("applied")
    ):
        return ""

    title = str(event.get("title") or "").strip()
    start_iso = str(event.get("start_iso") or "").strip()
    end_iso = str(event.get("end_iso") or "").strip()

    if not (title and start_iso and end_iso):
        return ""

    matches = set()

    for item in proposal.get("items", []):
        if not isinstance(item, dict) or not item.get("selected"):
            continue
        if str(item.get("title") or "").strip() != title:
            continue
        if str(item.get("start") or "").strip() != start_iso:
            continue
        if str(item.get("end") or "").strip() != end_iso:
            continue

        action_id = str(item.get("action_id") or "").strip()
        if action_id:
            matches.add(action_id)

    if len(matches) != 1:
        return ""

    return next(iter(matches))


def _register_task_events(
    events: list[dict],
    target_date: date,
) -> list[tuple[str, dict]]:

    payload = _load_daily_routine_state()

    day_state = _routine_day(
        payload,
        target_date,
    )

    tasks = day_state.setdefault(
        "tasks",
        {},
    )

    completed = set(
        day_state.get(
            "completed",
            [],
        )
    )

    registered = []

    for event in events:

        if (
            not event.get(
                "movable"
            )
            and event.get(
                "source"
            )
            not in {
                "google_tasks",
                "linked_google_task",
            }
        ):
            continue

        token = _event_task_token(
            event
        )

        current_task = tasks.get(token, {})
        if not isinstance(current_task, dict):
            current_task = {}
        action_id = str(
            current_task.get("action_id") or ""
        ).strip()

        calendar_id = str(
            event.get(
                "calendar_id"
            )
            or ""
        )

        event_id = str(
            event.get(
                "event_id"
            )
            or ""
        )

        # Миграция старых токенов календарных событий.
        # Старый токен зависел от start_iso, поэтому после
        # переноса события галочка выполнения терялась.
        if event_id:
            for old_token, old_task                     in list(tasks.items()):

                if old_token == token:
                    continue

                if (
                    str(
                        old_task.get(
                            "calendar_id"
                        )
                        or ""
                    )
                    == calendar_id
                    and str(
                        old_task.get(
                            "event_id"
                        )
                        or ""
                    )
                    == event_id
                ):
                    if not action_id:
                        action_id = str(
                            old_task.get("action_id") or ""
                        ).strip()
                    if old_token in completed:
                        completed.discard(
                            old_token
                        )
                        completed.add(
                            token
                        )

                    tasks.pop(
                        old_token,
                        None,
                    )

        # Дополнительная миграция текущего legacy token.
        legacy_token = (
            _legacy_event_task_token(
                event
            )
        )

        if (
            legacy_token != token
            and legacy_token in completed
        ):
            completed.discard(
                legacy_token
            )
            completed.add(
                token
            )

        if not action_id:
            action_id = _morning_action_id_for_event(
                day_state,
                event,
            )

        tasks[token] = {
            "action_id": action_id,
            "calendar_id": (
                event.get(
                    "calendar_id"
                )
                or ""
            ),
            "event_id": (
                event.get(
                    "event_id"
                )
                or ""
            ),
            "source": (
                event.get(
                    "source"
                )
                or "calendar"
            ),
            "task_list_id": (
                event.get(
                    "task_list_id"
                )
                or ""
            ),
            "task_id": (
                event.get(
                    "task_id"
                )
                or (
                    event.get(
                        "event_id"
                    )
                    if event.get(
                        "source"
                    )
                    == "google_tasks"
                    else ""
                )
                or ""
            ),
            "title": (
                event.get(
                    "title"
                )
                or "Без названия"
            ),
            "start_iso": (
                event.get(
                    "start_iso"
                )
                or ""
            ),
            "end_iso": (
                event.get(
                    "end_iso"
                )
                or ""
            ),
        }

        registered.append(
            (
                token,
                event,
            )
        )

    day_state[
        "completed"
    ] = sorted(
        completed
    )

    _save_daily_routine_state(
        payload
    )

    return registered


def _completed_task_tokens(target_date: date) -> set[str]:
    payload = _load_daily_routine_state()
    day_state = _routine_day(payload, target_date)
    return set(day_state.get("completed", []))


def _toggle_task_token(token: str) -> bool | None:
    payload = _load_daily_routine_state()

    for day_state in payload.get("days", {}).values():
        if token not in day_state.get("tasks", {}):
            continue

        completed = set(
            day_state.get(
                "completed",
                [],
            )
        )

        task_info = (
            day_state
            .get(
                "tasks",
                {},
            )
            .get(
                token,
                {},
            )
        )

        task_list_id = (
            task_info.get(
                "task_list_id"
            )
            or ""
        )

        task_id = (
            task_info.get(
                "task_id"
            )
            or ""
        )

        source = (
            task_info.get(
                "source"
            )
            or ""
        )

        project_action_id = str(
            task_info.get("action_id") or ""
        ).strip()

        if (
            task_list_id
            and task_id
            and source
            in {
                "google_tasks",
                "linked_google_task",
            }
        ):
            try:
                from google_tasks_tools import (
                    toggle_google_task,
                )

                result = (
                    toggle_google_task(
                        task_list_id,
                        task_id,
                    )
                )

                if not result.get(
                    "ok"
                ):
                    return None

                is_completed = bool(
                    result.get(
                        "completed"
                    )
                )

            except Exception:
                logging.exception(
                    "Google Task toggle failed"
                )
                return None

            if is_completed:
                completed.add(
                    token
                )
            else:
                completed.discard(
                    token
                )

        else:
            if token in completed:
                completed.remove(
                    token
                )
                is_completed = False
            else:
                completed.add(
                    token
                )
                is_completed = True

        if project_action_id:
            try:
                project_result = set_project_action_status(
                    project_action_id,
                    "done" if is_completed else "active",
                )
                if not project_result.get("ok"):
                    logging.error(
                        "Project action status sync failed: %s",
                        project_result,
                    )
            except Exception:
                logging.exception(
                    "Project action status sync failed"
                )

        day_state[
            "completed"
        ] = sorted(
            completed
        )

        _save_daily_routine_state(
            payload
        )

        return is_completed

    return None


def _sync_google_task_completion(
    registered: list[tuple[str, dict]],
    target_date: date,
) -> set[str]:

    payload = _load_daily_routine_state()

    day_state = _routine_day(
        payload,
        target_date,
    )

    completed = set(
        day_state.get(
            "completed",
            [],
        )
    )

    for token, event in registered:

        source = (
            event.get("source")
            or ""
        )

        if source not in {
            "google_tasks",
            "linked_google_task",
        }:
            continue

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
            not task_list_id
            or not task_id
        ):
            continue

        try:
            task = get_google_task(
                task_list_id,
                task_id,
            )

            if (
                task.get("status")
                == "completed"
            ):
                completed.add(token)
            else:
                completed.discard(token)

        except Exception:
            logging.exception(
                "Google Task status sync failed"
            )

    day_state["completed"] = sorted(
        completed
    )

    _save_daily_routine_state(
        payload
    )

    return completed


def task_checklist_keyboard(
    events: list[dict],
    target_date: date,
    finish_callback: str | None = None,
) -> InlineKeyboardMarkup | None:

    registered = _register_task_events(
        events,
        target_date,
    )

    completed = (
        _sync_google_task_completion(
            registered,
            target_date,
        )
    )

    rows = []

    for number, (token, event) in enumerate(
        registered,
        start=1,
    ):
        marker = (
            "✅"
            if token in completed
            else "☐"
        )

        title = (
            event.get("title")
            or "Без названия"
        )[:42]

        rows.append([
            InlineKeyboardButton(
                text=(
                    f"{marker} "
                    f"{number}. "
                    f"{title}"
                ),
                callback_data=(
                    f"task_toggle:{token}"
                ),
            )
        ])

    if finish_callback:
        rows.append([
            InlineKeyboardButton(
                text="✅ Готово, проверить остаток",
                callback_data=finish_callback,
            )
        ])

    if not rows:
        return None

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


def _updated_task_keyboard(
    markup: InlineKeyboardMarkup | None,
    changed_token: str,
    is_completed: bool,
) -> InlineKeyboardMarkup | None:
    if not markup:
        return None

    rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for button in row:
            text = button.text
            if button.callback_data == f"task_toggle:{changed_token}":
                base = re.sub(r"^[☐✅]\s*", "", text)
                marker = "✅" if is_completed else "☐"
                text = f"{marker} {base}"
            new_row.append(
                InlineKeyboardButton(
                    text=text,
                    callback_data=button.callback_data,
                )
            )
        rows.append(new_row)

    return InlineKeyboardMarkup(inline_keyboard=rows)


WEATHER_CITY = os.environ.get(
    "CLIPPY_WEATHER_CITY",
    "Санкт-Петербург",
)

WEATHER_LAT = float(
    os.environ.get(
        "CLIPPY_WEATHER_LAT",
        "59.9386",
    )
)

WEATHER_LON = float(
    os.environ.get(
        "CLIPPY_WEATHER_LON",
        "30.3141",
    )
)


def _weather_code_text(
    code: int,
) -> str:

    if code == 0:
        return "ясно"

    if code in {1, 2}:
        return "переменная облачность"

    if code == 3:
        return "пасмурно"

    if code in {45, 48}:
        return "туман"

    if code in {51, 53, 55, 56, 57}:
        return "морось"

    if code in {61, 63, 65, 66, 67}:
        return "дождь"

    if code in {71, 73, 75, 77}:
        return "снег"

    if code in {80, 81, 82}:
        return "ливни"

    if code in {85, 86}:
        return "снегопад"

    if code in {95, 96, 99}:
        return "гроза"

    return "без выраженных осадков"


def get_weather_summary(
    target_date: date,
) -> str | None:
    """
    Компактный прогноз Open-Meteo.

    Ошибка погоды никогда не должна ломать
    утренний план.
    """

    try:
        params = {
            "latitude": WEATHER_LAT,
            "longitude": WEATHER_LON,
            "daily": ",".join([
                "weather_code",
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_probability_max",
                "precipitation_sum",
                "wind_speed_10m_max",
            ]),
            "timezone": "Europe/Moscow",
            "start_date": (
                target_date.isoformat()
            ),
            "end_date": (
                target_date.isoformat()
            ),
            "temperature_unit": "celsius",
            "wind_speed_unit": "kmh",
        }

        url = (
            "https://api.open-meteo.com/v1/forecast?"
            + urlencode(params)
        )

        with urlopen(
            url,
            timeout=8,
        ) as response:
            payload = json.loads(
                response.read()
                .decode("utf-8")
            )

        daily = payload.get(
            "daily",
            {},
        )

        if not daily.get("time"):
            return None

        def first(name, default=None):
            values = daily.get(
                name,
                [],
            )

            if not values:
                return default

            return values[0]

        temp_min = first(
            "temperature_2m_min"
        )

        temp_max = first(
            "temperature_2m_max"
        )

        rain_probability = first(
            "precipitation_probability_max",
            0,
        )

        precipitation = first(
            "precipitation_sum",
            0,
        )

        wind = first(
            "wind_speed_10m_max",
            0,
        )

        code = int(
            first(
                "weather_code",
                -1,
            )
        )

        condition = (
            _weather_code_text(
                code
            )
        )

        parts = [
            "🌦 Погода — "
            + WEATHER_CITY
            + ": "
            + condition
            + "."
        ]

        if (
            temp_min is not None
            and temp_max is not None
        ):
            parts.append(
                "🌡 "
                + f"{round(temp_min):+d}"
                + "…"
                + f"{round(temp_max):+d}"
                + " °C."
            )

        parts.append(
            "☔ Осадки: "
            + str(
                round(
                    float(
                        rain_probability
                        or 0
                    )
                )
            )
            + "%, "
            + f"{float(precipitation or 0):.1f}"
            + " мм."
        )

        parts.append(
            "💨 Ветер до "
            + str(
                round(
                    float(
                        wind
                        or 0
                    )
                )
            )
            + " км/ч."
        )

        advice = []

        if (
            float(
                rain_probability
                or 0
            )
            >= 50
            or float(
                precipitation
                or 0
            )
            >= 1
        ):
            advice.append(
                "зонт лучше взять"
            )

        if float(wind or 0) >= 30:
            advice.append(
                "на улице ветрено"
            )

        if (
            temp_min is not None
            and float(temp_min) <= 8
        ):
            advice.append(
                "утром прохладно"
            )

        if (
            temp_max is not None
            and float(temp_max) >= 27
        ):
            advice.append(
                "днём будет жарко"
            )

        if advice:
            parts.append(
                "💡 "
                + "; ".join(advice)
                + "."
            )

        return " ".join(
            parts
        )

    except Exception:
        logging.exception(
            "Morning weather fetch failed"
        )
        return None


def get_daylight_summary(
    target_date: date,
) -> str | None:
    """
    Восход, закат и длина светового дня.

    Использует тот же Open-Meteo, что и прогноз.
    Ошибка никогда не должна ломать утренний бриф.
    """

    try:
        params = {
            "latitude": WEATHER_LAT,
            "longitude": WEATHER_LON,
            "daily": ",".join([
                "sunrise",
                "sunset",
                "daylight_duration",
            ]),
            "timezone": "Europe/Moscow",
            "start_date": (
                target_date.isoformat()
            ),
            "end_date": (
                target_date.isoformat()
            ),
        }

        url = (
            "https://api.open-meteo.com/v1/forecast?"
            + urlencode(params)
        )

        with urlopen(
            url,
            timeout=8,
        ) as response:
            payload = json.loads(
                response.read()
                .decode("utf-8")
            )

        daily = payload.get(
            "daily",
            {},
        )

        sunrise_values = daily.get(
            "sunrise",
            [],
        )

        sunset_values = daily.get(
            "sunset",
            [],
        )

        duration_values = daily.get(
            "daylight_duration",
            [],
        )

        if (
            not sunrise_values
            or not sunset_values
        ):
            return None

        sunrise = datetime.fromisoformat(
            sunrise_values[0]
        )

        sunset = datetime.fromisoformat(
            sunset_values[0]
        )

        duration_seconds = int(
            float(
                duration_values[0]
                if duration_values
                else (
                    sunset - sunrise
                ).total_seconds()
            )
        )

        hours = (
            duration_seconds
            // 3600
        )

        minutes = (
            duration_seconds
            % 3600
            // 60
        )

        return (
            "🌅 Световой день\n"
            f"Восход: {sunrise.strftime('%H:%M')} · "
            f"Закат: {sunset.strftime('%H:%M')} · "
            f"{hours} ч {minutes} мин"
        )

    except Exception:
        logging.exception(
            "Morning daylight fetch failed"
        )
        return None


def _is_wakeup_event(event: dict) -> bool:
    title = (event.get("title") or "").lower()
    return any(word in title for word in ("подъём", "подъем", "пробуждение"))


def _morning_time(events: list[dict], target_date: date) -> datetime:
    for event in events:
        if _is_wakeup_event(event) and event.get("start_iso"):
            return datetime.fromisoformat(event["start_iso"]).astimezone(MOSCOW_TZ)

    return datetime.combine(target_date, DEFAULT_WAKE_TIME, tzinfo=MOSCOW_TZ)


def _is_morning_task_event(
    event: dict,
) -> bool:
    return bool(
        event.get("movable")
        or event.get("source")
        in {
            "google_tasks",
            "linked_google_task",
        }
    )


def _format_minutes_short(
    minutes: int,
) -> str:
    minutes = max(
        0,
        int(minutes),
    )

    hours, rest = divmod(
        minutes,
        60,
    )

    if hours and rest:
        return (
            f"{hours} ч {rest} мин"
        )

    if hours:
        return f"{hours} ч"

    return f"{rest} мин"


def build_day_snapshot(
    now: datetime,
    events: list[dict],
) -> str | None:
    """
    Короткая сводка дня.

    В статистику задач попадают только задачи,
    которые НАЧИНАЮТСЯ в target_date.

    Поэтому ночной хвост задачи предыдущего дня
    остаётся видимым в расписании, но второй раз
    в новом дне не считается.

    OZON не блокирует свободное окно для
    flexible/adjustable личной работы.
    """

    if not events:
        return None

    target_date = now.date()

    task_count = 0
    task_minutes = 0
    task_keys = set()

    longest_task = None
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

        if (
            not start_raw
            or not end_raw
        ):
            continue

        try:
            start = (
                datetime.fromisoformat(
                    start_raw
                )
                .astimezone(
                    MOSCOW_TZ
                )
            )

            end = (
                datetime.fromisoformat(
                    end_raw
                )
                .astimezone(
                    MOSCOW_TZ
                )
            )

        except Exception:
            continue

        if end <= start:
            continue

        is_task = (
            _is_morning_task_event(
                event
            )
        )

        # Главное исправление:
        # carry-over с предыдущего дня
        # не считается новой задачей дня.
        starts_today = (
            start.date()
            == target_date
        )

        if (
            is_task
            and starts_today
        ):
            key = (
                event.get("task_id")
                or event.get("event_id")
                or (
                    str(
                        event.get(
                            "title",
                            "",
                        )
                    )
                    + "|"
                    + start.isoformat()
                )
            )

            if key not in task_keys:
                task_keys.add(key)

                duration_minutes = int(
                    (
                        end - start
                    ).total_seconds()
                    // 60
                )

                task_count += 1
                task_minutes += (
                    duration_minutes
                )

                if (
                    longest_task is None
                    or duration_minutes
                    > longest_task[
                        "minutes"
                    ]
                ):
                    longest_task = {
                        "title": (
                            event.get(
                                "title",
                                "Без названия",
                            )
                        ),
                        "start": start,
                        "end": end,
                        "minutes": (
                            duration_minutes
                        ),
                    }

        # Для поиска свободного времени
        # учитываем реальные занятые интервалы,
        # включая хвост предыдущего дня.
        if (
            event.get("calendar")
            == "OZON"
        ):
            continue

        busy.append(
            (
                start,
                end,
            )
        )

    # ----------------------------------------------
    # Свободное окно от текущего времени до 23:00.
    # ----------------------------------------------

    window_start = now.replace(
        second=0,
        microsecond=0,
    )

    remainder = (
        window_start.minute
        % 15
    )

    if remainder:
        window_start += timedelta(
            minutes=(
                15 - remainder
            )
        )

    window_end = datetime.combine(
        target_date,
        time(
            hour=23,
            minute=0,
        ),
        tzinfo=MOSCOW_TZ,
    )

    free_intervals = []

    if window_start < window_end:

        clipped_busy = []

        for start, end in busy:
            clipped_start = max(
                start,
                window_start,
            )

            clipped_end = min(
                end,
                window_end,
            )

            if (
                clipped_end
                > clipped_start
            ):
                clipped_busy.append(
                    (
                        clipped_start,
                        clipped_end,
                    )
                )

        clipped_busy.sort(
            key=lambda item:
            item[0]
        )

        merged = []

        for start, end in clipped_busy:

            if (
                not merged
                or start
                > merged[-1][1]
            ):
                merged.append(
                    [
                        start,
                        end,
                    ]
                )

            else:
                merged[-1][1] = max(
                    merged[-1][1],
                    end,
                )

        cursor = window_start

        for start, end in merged:

            if start > cursor:
                free_intervals.append(
                    (
                        cursor,
                        start,
                    )
                )

            cursor = max(
                cursor,
                end,
            )

        if cursor < window_end:
            free_intervals.append(
                (
                    cursor,
                    window_end,
                )
            )

    longest_free = None

    for start, end in free_intervals:

        minutes = int(
            (
                end - start
            ).total_seconds()
            // 60
        )

        if (
            longest_free is None
            or minutes
            > longest_free[
                "minutes"
            ]
        ):
            longest_free = {
                "start": start,
                "end": end,
                "minutes": minutes,
            }

    lines = []

    if task_count:
        lines.append(
            "📊 "
            f"Задач: {task_count} · "
            f"{_format_minutes_short(task_minutes)}"
        )

    visible_free = []

    for start, end in free_intervals:
        minutes = int(
            (
                end - start
            ).total_seconds()
            // 60
        )

        # Сохраняем прежний порог:
        # короткие окна меньше 30 минут
        # в утреннюю сводку не выводим.
        if minutes < 30:
            continue

        visible_free.append({
            "start": start,
            "end": end,
            "minutes": minutes,
        })

    if visible_free:
        lines.append(
            "🕳 Свободные окна:"
        )

        for interval in visible_free:
            lines.append(
                interval[
                    "start"
                ].strftime("%H:%M")
                + "–"
                + interval[
                    "end"
                ].strftime("%H:%M")
                + " · "
                + _format_minutes_short(
                    interval[
                        "minutes"
                    ]
                )
            )

    if longest_task:
        lines.append(
            "🎯 Главный блок: "
            + str(
                longest_task[
                    "title"
                ]
            )
            + " · "
            + longest_task[
                "start"
            ].strftime("%H:%M")
            + "–"
            + longest_task[
                "end"
            ].strftime("%H:%M")
        )

    if not lines:
        return None

    return "\n".join(
        lines
    )


def _morning_task_identity(
    event: dict,
) -> str:

    source = (
        event.get("source")
        or ""
    )

    if source not in {
        "google_tasks",
        "linked_google_task",
    }:
        return ""

    return str(
        event.get("task_id")
        or event.get("event_id")
        or ""
    )


def _morning_visible_events(
    events: list[dict],
) -> list[dict]:

    linked_task_ids = {
        task_id
        for event in events
        if (
            event.get("source")
            == "linked_google_task"
            and (
                task_id
                := _morning_task_identity(
                    event
                )
            )
        )
    }

    timed_event_titles = {
        title_key
        for event in events
        if (
            event.get("source")
            != "google_tasks"
            and not event.get("all_day")
            and event.get("start_iso")
            and (
                title_key
                := _normalize_morning_title(
                    event.get("title", "")
                )
            )
        )
    }

    visible = []
    seen_google_task_titles = set()

    for event in events:

        # Google Tasks отображаются Calendar как
        # all-day строки. Если для этой же Task
        # существует linked timed block, показываем
        # только timed block.
        if (
            event.get("source")
            == "google_tasks"
        ):
            task_id = (
                _morning_task_identity(
                    event
                )
            )

            if (
                task_id
                and task_id
                in linked_task_ids
            ):
                continue

            title_key = _normalize_morning_title(
                event.get("title", "")
            )

            # Иногда связанный Calendar block приходит
            # без task_id. В утреннем представлении всё
            # равно предпочитаем конкретный timed block
            # одноимённой all-day Google Task.
            if title_key in timed_event_titles:
                continue

            if (
                title_key
                and title_key
                in seen_google_task_titles
            ):
                continue

            if title_key:
                seen_google_task_titles.add(
                    title_key
                )

        visible.append(
            event
        )

    return visible


def _morning_display_events(
    events: list[dict],
    target_date: date,
) -> list[dict]:
    """
    Только события, которые нужно показывать
    пользователю в утреннем сообщении.

    Внутреннюю структуру календаря не изменяет.
    """

    result = []

    for event in _morning_visible_events(
        events
    ):

        normalized_title = (
            str(event.get("title", ""))
            .strip()
            .casefold()
            .replace("ё", "е")
        )

        # В календаре остаётся, утром не показываем.
        if normalized_title == "подготовка ко сну":
            continue

        title = (
            str(
                event.get(
                    "title",
                    "",
                )
            )
            .strip()
            .casefold()
            .replace("ё", "е")
        )

        # Служебный ночной блок остаётся
        # в календаре, но утром не показывается.
        if title == "подготовка ко сну":
            continue

        if event.get("all_day"):
            result.append(
                event
            )
            continue

        start_raw = event.get(
            "start_iso"
        )
        end_raw = event.get(
            "end_iso"
        )

        if start_raw and end_raw:
            try:
                start = (
                    datetime.fromisoformat(
                        start_raw
                    )
                    .astimezone(
                        MOSCOW_TZ
                    )
                )

                end = (
                    datetime.fromisoformat(
                        end_raw
                    )
                    .astimezone(
                        MOSCOW_TZ
                    )
                )

                # Ночной хвост предыдущего дня
                # тоже не показываем утром.
                if (
                    start.date() < target_date
                    and end.date() >= target_date
                ):
                    continue

            except Exception:
                pass

        result.append(
            event
        )

    return result



def get_moon_phase_summary(
    target_date: date,
) -> str | None:
    """
    Приблизительная астрономическая фаза Луны
    и процент освещённости.

    Расчёт локальный, без внешнего API.
    """

    try:
        import math

        synodic_month = 29.530588853

        # Известное новолуние:
        # 06.01.2000 18:14 UTC.
        reference = datetime(
            2000,
            1,
            6,
            18,
            14,
        )

        # Для суточного брифа берём середину дня.
        current = datetime(
            target_date.year,
            target_date.month,
            target_date.day,
            12,
            0,
        )

        age_days = (
            (
                current - reference
            ).total_seconds()
            / 86400
        ) % synodic_month

        phase = (
            age_days
            / synodic_month
        )

        illumination = (
            1
            - math.cos(
                2
                * math.pi
                * phase
            )
        ) / 2

        illumination_percent = round(
            illumination * 100
        )

        if (
            phase < 0.0625
            or phase >= 0.9375
        ):
            emoji = "🌑"
            name = "новолуние"

        elif phase < 0.1875:
            emoji = "🌒"
            name = "растущий серп"

        elif phase < 0.3125:
            emoji = "🌓"
            name = "первая четверть"

        elif phase < 0.4375:
            emoji = "🌔"
            name = "растущая Луна"

        elif phase < 0.5625:
            emoji = "🌕"
            name = "полнолуние"

        elif phase < 0.6875:
            emoji = "🌖"
            name = "убывающая Луна"

        elif phase < 0.8125:
            emoji = "🌗"
            name = "последняя четверть"

        else:
            emoji = "🌘"
            name = "убывающий серп"

        return (
            f"🌙 Луна: {name} {emoji} · "
            f"освещено {illumination_percent}%"
        )

    except Exception:
        logging.exception(
            "Morning moon phase calculation failed"
        )
        return None


def get_openai_budget_summary() -> str:
    """
    Короткий безопасный статус расходов OpenAI.

    Только читает локальный snapshot.
    Никаких запросов к OpenAI API не делает.
    """

    try:
        data = get_openai_cost_status()

        if not data.get("ok"):
            return (
                "💳 OpenAI API: "
                "расчётный остаток сейчас недоступен"
            )

        balance = (
            data.get("estimated_balance_usd")
            if data.get("estimated_balance_usd")
            is not None
            else data.get("estimated_balance")
        )

        spend = (
            data.get("spend_since_baseline_usd")
            if data.get("spend_since_baseline_usd")
            is not None
            else data.get("spend_since_baseline")
        )

        checked_at = data.get(
            "checked_at"
        )

        if balance is None:
            return (
                "💳 OpenAI API: "
                "расчётный остаток отсутствует в snapshot"
            )

        try:
            balance_text = (
                f"${float(balance):.2f}"
            )
        except Exception:
            balance_text = str(
                balance
            )

        parts = [
            (
                "💳 OpenAI API: "
                f"расчётный остаток {balance_text}"
            )
        ]

        if spend is not None:
            try:
                spend_text = (
                    f"${float(spend):.2f}"
                )
            except Exception:
                spend_text = str(
                    spend
                )

            parts.append(
                f"расход {spend_text}"
            )

        if checked_at:
            try:
                checked = (
                    datetime.fromisoformat(
                        str(
                            checked_at
                        ).replace(
                            "Z",
                            "+00:00",
                        )
                    )
                )

                if checked.tzinfo is not None:
                    checked = (
                        checked.astimezone(
                            MOSCOW_TZ
                        )
                    )

                parts.append(
                    "обновлено "
                    + checked.strftime(
                        "%d.%m %H:%M"
                    )
                )

            except Exception:
                pass

        return " · ".join(parts)

    except Exception:
        logging.exception(
            "Morning OpenAI budget summary failed"
        )

        return (
            "💳 OpenAI API: "
            "расчётный остаток сейчас недоступен"
        )



def build_morning_plan(
    now: datetime,
    events: list[dict],
    weather: str | None = None,
    daylight: str | None = None,
    moon: str | None = None,
    budget: str | None = None,
) -> str:

    snapshot_events = (
        _morning_visible_events(
            events
        )
    )

    display_events = (
        _morning_display_events(
            events,
            now.date(),
        )
    )

    lines = [
        (
            "Доброе утро, пользователь. "
            f"План на {now.strftime('%d.%m.%Y')}:"
        ),
        "",
    ]

    if weather:
        lines.extend([
            weather,
            "",
        ])

    if daylight:
        lines.extend([
            daylight,
            "",
        ])

    if moon:
        lines.extend([
            moon,
            "",
        ])

    if budget:
        lines.extend([
            budget,
            "",
        ])

    snapshot = build_day_snapshot(
        now,
        snapshot_events,
    )

    if snapshot:
        lines.extend([
            snapshot,
            "",
        ])

    if not display_events:
        lines.append(
            "В календаре пока нет событий."
        )
        return "\n".join(lines)

    lines.append("📌 План")

    for event in display_events:

        is_task = (
            _is_morning_task_event(
                event
            )
        )

        if (
            is_task
            and event.get(
                "task_completed"
            ) is True
        ):
            marker = "✅"
        elif is_task:
            marker = "☐"
        else:
            marker = "•"

        if event.get("all_day"):
            when = "весь день"
        else:
            when = (
                f'{event.get("start", "")}'
                f'–{event.get("end", "")}'
            )

        lines.append(
            f'{marker} {when} — '
            f'{event.get("title", "Без названия")}'
        )

    return "\n".join(lines)





def _morning_was_sent(target_date: date) -> bool:
    payload = _load_daily_routine_state()
    return bool(_routine_day(payload, target_date).get("morning_sent"))


def _mark_morning_sent(
    target_date: date,
    message_id: int | None = None,
) -> None:
    payload = _load_daily_routine_state()
    day_state = _routine_day(
        payload,
        target_date,
    )
    day_state["morning_sent"] = True
    if message_id is not None:
        day_state["morning_message_id"] = int(
            message_id
        )
    _save_daily_routine_state(payload)


def _reminder_was_sent(target_date: date, token: str) -> bool:
    payload = _load_daily_routine_state()
    return token in set(_routine_day(payload, target_date).get("reminders", []))


def _mark_reminder_sent(target_date: date, token: str) -> None:
    payload = _load_daily_routine_state()
    day_state = _routine_day(payload, target_date)
    reminders = set(day_state.get("reminders", []))
    reminders.add(token)
    day_state["reminders"] = sorted(reminders)
    _save_daily_routine_state(payload)


async def _send_voice_notification(
    bot: Bot,
    text: str,
    filename: str,
) -> None:
    try:
        voice_bytes = await asyncio.wait_for(
            synthesize_voice(text),
            timeout=60,
        )
        await bot.send_voice(
            OWNER_ID,
            voice=BufferedInputFile(
                voice_bytes,
                filename=filename,
            ),
        )
    except Exception:
        logging.exception("Voice notification failed")


def _normalize_morning_title(
    value: str,
) -> str:
    return " ".join(
        re.sub(
            r"[^а-яёa-z0-9]+",
            " ",
            str(value or "")
            .casefold()
            .replace("ё", "е"),
        ).split()
    )


def _morning_is_ozon_event(
    event: dict,
) -> bool:

    if (
        str(
            event.get(
                "calendar"
            )
            or ""
        ).casefold()
        == "ozon"
    ):
        return True

    title = (
        str(
            event.get(
                "title"
            )
            or ""
        )
        .strip()
        .casefold()
    )

    return (
        title == "смена ozon"
        or title.startswith(
            "смена ozon "
        )
    )


def _ceil_quarter(
    value: datetime,
) -> datetime:

    value = value.replace(
        second=0,
        microsecond=0,
    )

    remainder = (
        value.minute
        % 15
    )

    if remainder:
        value += timedelta(
            minutes=(
                15 - remainder
            )
        )

    return value


def _morning_find_slot(
    target_date: date,
    duration_minutes: int,
    reserved: list[
        tuple[
            datetime,
            datetime,
        ]
    ] | None = None,
) -> dict | None:

    duration = timedelta(
        minutes=max(
            15,
            int(
                duration_minutes
                or 60
            ),
        )
    )

    window_start = datetime.combine(
        target_date,
        time(10, 0),
        tzinfo=MOSCOW_TZ,
    )

    window_end = datetime.combine(
        target_date,
        time(23, 0),
        tzinfo=MOSCOW_TZ,
    )

    now = datetime.now(
        MOSCOW_TZ
    )

    if target_date == now.date():
        window_start = max(
            window_start,
            _ceil_quarter(
                now
                + timedelta(
                    minutes=15
                )
            ),
        )

    if (
        window_start
        + duration
        > window_end
    ):
        return None

    events = get_day_schedule(
        target_date
    )

    blockers = []
    ozon_intervals = []

    for event in events:

        if event.get(
            "all_day"
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

        try:
            start = (
                datetime.fromisoformat(
                    start_raw
                )
                .astimezone(
                    MOSCOW_TZ
                )
            )

            end = (
                datetime.fromisoformat(
                    end_raw
                )
                .astimezone(
                    MOSCOW_TZ
                )
            )

        except Exception:
            continue

        if end <= start:
            continue

        if _morning_is_ozon_event(
            event
        ):
            ozon_intervals.append(
                (
                    start,
                    end,
                )
            )
            continue

        blockers.append(
            (
                start,
                end,
            )
        )

    for start, end in (
        reserved
        or []
    ):
        blockers.append(
            (
                start,
                end,
            )
        )

    blockers.sort(
        key=lambda item: item[0]
    )

    merged = []

    for start, end in blockers:

        if end <= window_start:
            continue

        if start >= window_end:
            continue

        start = max(
            start,
            window_start,
        )

        end = min(
            end,
            window_end,
        )

        if not merged:
            merged.append(
                [
                    start,
                    end,
                ]
            )
            continue

        if (
            start
            <= merged[-1][1]
        ):
            merged[-1][1] = max(
                merged[-1][1],
                end,
            )
        else:
            merged.append(
                [
                    start,
                    end,
                ]
            )

    cursor = window_start

    slot = None

    for start, end in merged:

        if (
            start - cursor
            >= duration
        ):
            slot = (
                cursor,
                cursor + duration,
            )
            break

        if end > cursor:
            cursor = end

    if (
        slot is None
        and (
            window_end
            - cursor
            >= duration
        )
    ):
        slot = (
            cursor,
            cursor + duration,
        )

    if slot is None:
        return None

    start, end = slot

    overlaps_ozon = any(
        (
            start < ozon_end
            and end > ozon_start
        )
        for (
            ozon_start,
            ozon_end,
        )
        in ozon_intervals
    )

    return {
        "start": start,
        "end": end,
        "overlaps_ozon": (
            overlaps_ozon
        ),
    }


def _save_morning_proposal(
    target_date: date,
    proposal: dict,
) -> None:

    payload = (
        _load_daily_routine_state()
    )

    day_state = _routine_day(
        payload,
        target_date,
    )

    day_state[
        "morning_proposal"
    ] = proposal

    _save_daily_routine_state(
        payload
    )


def _get_morning_proposal(
    target_date: date,
) -> dict | None:

    payload = (
        _load_daily_routine_state()
    )

    day_state = _routine_day(
        payload,
        target_date,
    )

    proposal = day_state.get(
        "morning_proposal"
    )

    if not isinstance(
        proposal,
        dict,
    ):
        return None

    return proposal


def _build_morning_project_proposal(
    target_date: date,
    events: list[dict],
) -> dict:

    source = get_project_actions(
        active_only=True,
        target_date=target_date,
    )

    actions = (
        source.get(
            "actions",
            []
        )
        if source.get("ok")
        else []
    )

    existing_titles = {
        _normalize_morning_title(
            event.get(
                "title",
                "",
            )
        )
        for event in events
        if event.get(
            "title"
        )
    }

    reserved = []

    selected_projects = set()

    items = []

    for action in actions:

        if len(items) >= 5:
            break

        title = str(
            action.get(
                "title"
            )
            or ""
        ).strip()

        if not title:
            continue

        project = str(
            action.get(
                "project"
            )
            or ""
        ).strip()

        project_key = (
            _normalize_morning_title(
                project
            )
        )

        if (
            not project_key
            or project_key in {
                "personal management",
                "general",
                "общее",
                "прочее",
            }
            or project_key
            in selected_projects
        ):
            continue

        classification = classify_task(
            title
        )

        if not classification.get(
            "plan",
            True,
        ):
            continue

        vague_prefixes = (
            "планировать ",
            "контролировать ",
            "вести учет ",
            "вести учёт ",
            "еженедельный анализ ",
            "анализировать ",
        )

        if title.casefold().startswith(
            vague_prefixes
        ):
            continue

        normalized_title = (
            _normalize_morning_title(
                title
            )
        )

        if (
            normalized_title
            in existing_titles
        ):
            continue

        not_before = action.get(
            "not_before"
        )

        if not_before:
            try:
                if (
                    date.fromisoformat(
                        not_before
                    )
                    > target_date
                ):
                    continue
            except Exception:
                pass

        preferred_date = (
            action.get(
                "preferred_date"
            )
        )

        if preferred_date:
            try:
                preferred = (
                    date.fromisoformat(
                        preferred_date
                    )
                )

                if (
                    preferred
                    > target_date
                ):
                    continue

            except Exception:
                pass

        duration = (
            action.get(
                "estimated_minutes"
            )
            or 60
        )

        slot = _morning_find_slot(
            target_date,
            duration,
            reserved,
        )

        if not slot:
            continue

        start = slot[
            "start"
        ]

        end = slot[
            "end"
        ]

        reserved.append(
            (
                start,
                end,
            )
        )

        selected_projects.add(
            project_key
        )

        action_id = str(
            action.get(
                "action_id"
            )
            or hashlib.sha256(
                (
                    title
                    + "|"
                    + str(
                        action.get(
                            "project"
                        )
                        or ""
                    )
                )
                .encode(
                    "utf-8"
                )
            ).hexdigest()[:16]
        )

        items.append({
            "token": (
                action_id[:16]
            ),
            "action_id": action_id,
            "title": title,
            "project": project,
            "source_chat": (
                action.get(
                    "source_chat"
                )
                or ""
            ),
            "priority": (
                action.get(
                    "priority"
                )
                or "normal"
            ),
            "estimated_minutes": (
                int(duration)
            ),
            "target_date": (
                target_date.isoformat()
            ),
            "start": (
                start.isoformat()
            ),
            "end": (
                end.isoformat()
            ),
            "allow_ozon_overlap": (
                bool(
                    slot.get(
                        "overlaps_ozon"
                    )
                )
            ),
            "selected": False,
        })

    proposal = {
        "created_at": (
            datetime.now(
                MOSCOW_TZ
            ).isoformat()
        ),
        "source": (
            "project_next_actions"
        ),
        "items": items,
        "applied": False,
    }

    _save_morning_proposal(
        target_date,
        proposal,
    )

    return proposal


def _find_morning_item(
    token: str,
    proposal_date: date | None = None,
) -> tuple[
    date,
    dict,
    dict,
] | None:

    payload = (
        _load_daily_routine_state()
    )

    days = payload.get(
        "days",
        {},
    )

    if proposal_date is not None:
        raw_dates = [proposal_date.isoformat()]
    else:
        raw_dates = sorted(days.keys(), reverse=True)

    for raw_date in raw_dates:
        day_state = days.get(raw_date)

        if not isinstance(
            day_state,
            dict,
        ):
            continue

        proposal = day_state.get(
            "morning_proposal"
        )

        if not isinstance(
            proposal,
            dict,
        ):
            continue

        for item in proposal.get(
            "items",
            [],
        ):

            if (
                isinstance(
                    item,
                    dict,
                )
                and str(
                    item.get(
                        "token"
                    )
                    or ""
                )
                == token
            ):
                try:
                    proposal_date = (
                        date.fromisoformat(
                            raw_date
                        )
                    )
                except ValueError:
                    continue

                return (
                    proposal_date,
                    proposal,
                    item,
                )

    return None


def _decode_morning_item_key(
    raw_value: str,
) -> tuple[date | None, str]:
    raw_value = str(raw_value or "")
    parts = raw_value.split(":", 1)

    if len(parts) == 2:
        try:
            return date.fromisoformat(parts[0]), parts[1]
        except ValueError:
            pass

    return None, raw_value


def _update_morning_item(
    proposal_date: date,
    token: str,
    **updates,
) -> bool:

    payload = (
        _load_daily_routine_state()
    )

    day_state = _routine_day(
        payload,
        proposal_date,
    )

    proposal = day_state.get(
        "morning_proposal"
    )

    if not isinstance(
        proposal,
        dict,
    ):
        return False

    for item in proposal.get(
        "items",
        [],
    ):

        if (
            isinstance(
                item,
                dict,
            )
            and str(
                item.get(
                    "token"
                )
                or ""
            )
            == token
        ):
            item.update(
                updates
            )

            _save_daily_routine_state(
                payload
            )

            return True

    return False


def morning_approval_keyboard(
    proposal_date: date,
) -> InlineKeyboardMarkup | None:

    proposal = (
        _get_morning_proposal(
            proposal_date
        )
    )

    if not proposal:
        return None

    items = proposal.get(
        "items",
        []
    )

    if not items:
        return None

    rows = []

    for number, item in enumerate(
        items,
        start=1,
    ):

        marker = (
            "✅"
            if item.get(
                "selected"
            )
            else "☐"
        )

        title = str(
            item.get(
                "title"
            )
            or "Без названия"
        )

        project = str(
            item.get(
                "project"
            )
            or ""
        ).strip()

        project_label = (
            project
            .replace(
                "Clippy / ",
                "",
            )
        )

        label = (
            f"[{project_label}] {title}"
            if project_label
            else title
        )

        if len(label) > 48:
            label = (
                label[:47]
                + "…"
            )

        rows.append([
            InlineKeyboardButton(
                text=(
                    f"{marker} "
                    f"{number}. "
                    f"{label}"
                ),
                callback_data=(
                    "morning_select:"
                    + proposal_date.isoformat()
                    + ":"
                    + str(
                        item.get(
                            "token"
                        )
                    )
                ),
            )
        ])

        try:
            target = (
                date.fromisoformat(
                    item[
                        "target_date"
                    ]
                )
            )

            start = (
                datetime.fromisoformat(
                    item["start"]
                )
                .astimezone(
                    MOSCOW_TZ
                )
            )

            end = (
                datetime.fromisoformat(
                    item["end"]
                )
                .astimezone(
                    MOSCOW_TZ
                )
            )

            real_today = datetime.now(
                MOSCOW_TZ
            ).date()

            if target == real_today:
                date_text = "Сегодня"
            elif target == (
                real_today
                + timedelta(days=1)
            ):
                date_text = "Завтра"
            else:
                date_text = target.strftime(
                    "%d.%m"
                )

            time_text = (
                start.strftime(
                    "%H:%M"
                )
                + "–"
                + end.strftime(
                    "%H:%M"
                )
            )

        except Exception:
            date_text = "Дата"
            time_text = ""

        rows.append([
            InlineKeyboardButton(
                text=(
                    "📅 "
                    + date_text
                    + (
                        " · "
                        + time_text
                        if time_text
                        else ""
                    )
                ),
                callback_data=(
                    "morning_move:"
                    + proposal_date.isoformat()
                    + ":"
                    + str(
                        item.get(
                            "token"
                        )
                    )
                ),
            )
        ])

    rows.append([
        InlineKeyboardButton(
            text=(
                "✅ Утвердить выбранное"
            ),
            callback_data=(
                "morning_apply:"
                + proposal_date.isoformat()
            ),
        )
    ])

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


def _morning_date_keyboard(
    token: str,
    proposal_date: date,
) -> InlineKeyboardMarkup:

    item_key = proposal_date.isoformat() + ":" + token

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Сегодня",
                    callback_data=(
                        f"morning_date:{item_key}:0"
                    ),
                ),
                InlineKeyboardButton(
                    text="Завтра",
                    callback_data=(
                        f"morning_date:{item_key}:1"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="+2 дня",
                    callback_data=(
                        f"morning_date:{item_key}:2"
                    ),
                ),
                InlineKeyboardButton(
                    text="+3 дня",
                    callback_data=(
                        f"morning_date:{item_key}:3"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="+7 дней",
                    callback_data=(
                        f"morning_date:{item_key}:7"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="← Назад",
                    callback_data=(
                        f"morning_back:{item_key}"
                    ),
                ),
            ],
        ]
    )



async def _send_morning_plan(
    bot: Bot,
    now: datetime,
    events: list[dict],
) -> None:

    weather, daylight, moon, budget = await asyncio.gather(
        asyncio.to_thread(
            get_weather_summary,
            now.date(),
        ),
        asyncio.to_thread(
            get_daylight_summary,
            now.date(),
        ),
        asyncio.to_thread(
            get_moon_phase_summary,
            now.date(),
        ),
        asyncio.to_thread(
            get_openai_budget_summary,
        ),
    )

    text = build_morning_plan(
        now,
        events,
        weather=weather,
        daylight=daylight,
        moon=moon,
        budget=budget,
    )

    keyboard_events = (
        _morning_display_events(
            events,
            now.date(),
        )
    )

    proposal = (
        _build_morning_project_proposal(
            now.date(),
            events,
        )
    )

    keyboard = (
        morning_approval_keyboard(
            now.date()
        )
    )

    if proposal.get(
        "items"
    ):
        text += (
            "\n\n🧭 Следующие шаги по проектам"
            "\nClippy подобрала конкретные действия и свободное время."
            "\nОтметь то, что хочешь добавить в план."
            "\n📅 При необходимости выбери другой день."
            "\nДо «Утвердить выбранное» ничего не создаётся."
        )

    sent_message = await bot.send_message(
        OWNER_ID,
        text,
        reply_markup=keyboard,
    )

    _mark_morning_sent(
        now.date(),
        sent_message.message_id,
    )

    logging.info(
        "Morning plan sent: date=%s message_id=%s",
        now.date().isoformat(),
        sent_message.message_id,
    )

    await asyncio.to_thread(
        save_message,
        "assistant",
        text,
        "general",
    )

    await _send_voice_notification(
        bot,
        text,
        "morning_plan.ogg",
    )



async def _send_event_reminder(
    bot: Bot,
    now: datetime,
    event: dict,
    token: str,
) -> None:
    start = datetime.fromisoformat(event["start_iso"]).astimezone(MOSCOW_TZ)
    minutes = max(1, round((start - now).total_seconds() / 60))
    text = (
        f'Напоминание: через {minutes} минут — '
        f'«{event.get("title", "событие")}». '
        f'Начало в {start.strftime("%H:%M")}.'
    )
    await bot.send_message(OWNER_ID, text)
    _mark_reminder_sent(now.date(), token)
    await _send_voice_notification(bot, text, "event_reminder.ogg")


async def daily_routine_loop(bot: Bot) -> None:
    cached_date = None
    cached_events = []
    next_refresh = None

    while True:
        now = datetime.now(MOSCOW_TZ)

        if (
            cached_date != now.date()
            or next_refresh is None
            or now >= next_refresh
        ):
            try:
                cached_events = await asyncio.to_thread(
                    get_day_overview,
                    now.date(),
                )
                _register_task_events(cached_events, now.date())
                cached_date = now.date()
                next_refresh = now + CALENDAR_REFRESH_INTERVAL
            except Exception:
                logging.exception("Daily calendar refresh failed")
                next_refresh = now + timedelta(minutes=1)

        wake_at = _morning_time(cached_events, now.date())
        if (
            not _morning_was_sent(now.date())
            and wake_at <= now < wake_at + timedelta(minutes=15)
        ):
            try:
                await _send_morning_plan(bot, now, cached_events)
            except Exception:
                logging.exception("Morning plan failed")

        completed = _completed_task_tokens(now.date())
        for event in cached_events:
            if (
                event.get("all_day")
                or not event.get("start_iso")
                or _is_wakeup_event(event)
            ):
                continue

            task_token = _event_task_token(
                event
            )

            if (
                event.get("movable")
                and task_token in completed
            ):
                continue

            reminder_token = (
                _event_reminder_token(
                    event
                )
            )

            start = datetime.fromisoformat(
                event["start_iso"]
            ).astimezone(
                MOSCOW_TZ
            )

            remind_at = (
                start
                - REMINDER_BEFORE
            )

            if (
                remind_at <= now < start
                and not _reminder_was_sent(
                    now.date(),
                    reminder_token,
                )
            ):
                try:
                    await _send_event_reminder(
                        bot,
                        now,
                        event,
                        reminder_token,
                    )
                except Exception:
                    logging.exception(
                        "Event reminder failed"
                    )

        await asyncio.sleep(30)


async def nightly_project_sync_loop() -> None:
    while True:
        now = datetime.now(MOSCOW_TZ)
        try:
            due = await asyncio.to_thread(
                nightly_sync_is_due,
                now,
            )
            if due:
                result = await asyncio.to_thread(
                    sync_calendar_projects,
                    now,
                )
                logging.info(
                    "Nightly project sync complete: %s",
                    result,
                )
        except Exception:
            logging.exception("Nightly project sync failed")

        await asyncio.sleep(60)


def _brief_last_sent_date() -> str:
    if not EVENING_BRIEF_STATE_FILE.exists():
        return ""

    try:
        payload = json.loads(
            EVENING_BRIEF_STATE_FILE.read_text(
                encoding="utf-8"
            )
        )
        return str(payload.get("last_sent_date") or "")
    except Exception:
        return ""


def _save_brief_sent_date(value: date) -> None:
    payload = {
        "last_sent_date": value.isoformat(),
        "sent_at": datetime.now(MOSCOW_TZ).isoformat(),
    }
    EVENING_BRIEF_STATE_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _brief_candidate_events(review_date: date) -> list[dict]:
    return [
        event
        for event in get_day_schedule(review_date)
        if not event.get("all_day")
        and event.get("movable")
        and event.get("start_iso")
        and event.get("end_iso")
    ]


def _brief_task_state(
    review_date: date,
) -> tuple[
    list[dict],
    list[tuple[str, dict]],
    set[str],
]:
    candidates = (
        _brief_candidate_events(
            review_date
        )
    )

    registered = (
        _register_task_events(
            candidates,
            review_date,
        )
    )

    completed = (
        _sync_google_task_completion(
            registered,
            review_date,
        )
    )

    return (
        candidates,
        registered,
        completed,
    )


def _brief_open_tasks(
    review_date: date,
) -> list[dict]:

    (
        candidates,
        registered,
        completed,
    ) = _brief_task_state(
        review_date
    )

    event_by_token = {
        token: event
        for token, event
        in registered
    }

    return [
        event_by_token[token]
        for token in event_by_token
        if token not in completed
    ]


def _ceil_to_quarter(
    value: datetime,
) -> datetime:

    value = value.replace(
        second=0,
        microsecond=0,
    )

    remainder = (
        value.minute
        % 15
    )

    if remainder:
        value += timedelta(
            minutes=(
                15 - remainder
            )
        )

    return value


def _subtract_busy_interval(
    free: list[
        tuple[datetime, datetime]
    ],
    busy_start: datetime,
    busy_end: datetime,
) -> list[
    tuple[datetime, datetime]
]:

    result = []

    for start, end in free:

        if (
            busy_end <= start
            or busy_start >= end
        ):
            result.append(
                (start, end)
            )
            continue

        if busy_start > start:
            result.append(
                (
                    start,
                    min(
                        busy_start,
                        end,
                    ),
                )
            )

        if busy_end < end:
            result.append(
                (
                    max(
                        busy_end,
                        start,
                    ),
                    end,
                )
            )

    return [
        (start, end)
        for start, end in result
        if end > start
    ]


def _brief_replan_pool(
    review_date: date,
    now: datetime,
) -> list[dict]:
    """
    Свободные окна для переноса.

    OZON намеренно НЕ является blocker.
    Другие события остаются blocker.
    """

    pool = []

    start_clock = time.fromisoformat(
        BRIEF_WINDOW_START
    )

    end_clock = time.fromisoformat(
        BRIEF_WINDOW_END
    )

    for offset in range(
        BRIEF_SEARCH_DAYS
    ):

        # Вечерний перенос ищет новое место
        # начиная со следующего дня.
        target_date = (
            review_date
            + timedelta(
                days=offset + 1
            )
        )

        window_start = (
            datetime.combine(
                target_date,
                start_clock,
                tzinfo=MOSCOW_TZ,
            )
        )

        window_end = (
            datetime.combine(
                target_date,
                end_clock,
                tzinfo=MOSCOW_TZ,
            )
        )

        if target_date == now.date():
            window_start = max(
                window_start,
                _ceil_to_quarter(
                    now
                ),
            )

        if (
            window_end
            <= window_start
        ):
            continue

        free = [
            (
                window_start,
                window_end,
            )
        ]

        ozon_intervals = []

        events = get_day_schedule(
            target_date
        )

        for event in events:

            if event.get(
                "all_day"
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

            event_start = (
                datetime.fromisoformat(
                    start_raw
                )
                .astimezone(
                    MOSCOW_TZ
                )
            )

            event_end = (
                datetime.fromisoformat(
                    end_raw
                )
                .astimezone(
                    MOSCOW_TZ
                )
            )

            if (
                event.get("calendar")
                == "OZON"
            ):
                ozon_intervals.append(
                    (
                        event_start,
                        event_end,
                    )
                )
                continue

            free = (
                _subtract_busy_interval(
                    free,
                    event_start,
                    event_end,
                )
            )

        for start, end in free:
            pool.append({
                "start": start,
                "end": end,
                "ozon": (
                    ozon_intervals
                ),
            })

    pool.sort(
        key=lambda item:
        item["start"]
    )

    return pool


def _allocate_replan_slot(
    pool: list[dict],
    duration: timedelta,
) -> tuple[
    datetime,
    datetime,
    bool,
] | None:

    for interval in pool:

        start = interval[
            "start"
        ]

        end = interval[
            "end"
        ]

        if (
            end - start
            < duration
        ):
            continue

        proposed_end = (
            start + duration
        )

        overlaps_ozon = any(
            (
                start < ozon_end
                and proposed_end
                > ozon_start
            )
            for (
                ozon_start,
                ozon_end,
            )
            in interval.get(
                "ozon",
                [],
            )
        )

        interval[
            "start"
        ] = proposed_end

        return (
            start,
            proposed_end,
            overlaps_ozon,
        )

    return None


def _prepare_evening_replan(
    review_date: date,
) -> dict:

    now = datetime.now(
        MOSCOW_TZ
    )

    open_tasks = (
        _brief_open_tasks(
            review_date
        )
    )

    if not open_tasks:
        return {
            "ok": False,
            "error": (
                "no_open_tasks"
            ),
        }

    pool = _brief_replan_pool(
        review_date,
        now,
    )

    actions = []
    proposed = []
    not_placed = []

    for event in open_tasks:

        start = (
            datetime.fromisoformat(
                event[
                    "start_iso"
                ]
            )
        )

        end = (
            datetime.fromisoformat(
                event[
                    "end_iso"
                ]
            )
        )

        duration = (
            end - start
        )

        slot = (
            _allocate_replan_slot(
                pool,
                duration,
            )
        )

        if not slot:
            not_placed.append(
                event.get(
                    "title",
                    "Без названия",
                )
            )
            continue

        (
            proposed_start,
            proposed_end,
            overlaps_ozon,
        ) = slot

        actions.append({
            "type": "update",
            "calendar_kind": (
                "personal"
            ),
            "event_id": event[
                "event_id"
            ],
            "title": event[
                "title"
            ],
            "start": (
                proposed_start
                .isoformat()
            ),
            "end": (
                proposed_end
                .isoformat()
            ),
            "description": (
                event.get(
                    "description",
                    "",
                )
            ),
            "allow_ozon_overlap": (
                overlaps_ozon
            ),
        })

        proposed.append({
            "title": event[
                "title"
            ],
            "start": proposed_start,
            "end": proposed_end,
            "overlaps_ozon": (
                overlaps_ozon
            ),
        })

    if not_placed:
        return {
            "ok": False,
            "error": (
                "not_enough_free_time"
            ),
            "not_placed": (
                not_placed
            ),
            "proposed": (
                proposed
            ),
        }

    if not actions:
        return {
            "ok": False,
            "error": (
                "no_actions"
            ),
        }

    target_date = (
        proposed[0][
            "start"
        ]
        .date()
        .isoformat()
    )

    summary = (
        "Перенос невыполненных "
        "задач после вечернего брифа."
    )

    saved = save_plan_proposal(
        target_date=target_date,
        actions=actions,
        summary=summary,
    )

    if not saved.get(
        "ok"
    ):
        return saved

    return {
        "ok": True,
        "proposed": proposed,
        "count": len(
            proposed
        ),
    }


def _brief_free_pool(
    first_date: date,
    now: datetime,
) -> list[dict]:
    pool = []

    for offset in range(BRIEF_SEARCH_DAYS):
        target_date = first_date + timedelta(days=offset)

        for interval in get_free_intervals(
            target_date,
            BRIEF_WINDOW_START,
            BRIEF_WINDOW_END,
        ):
            start = datetime.fromisoformat(interval["start"])
            end = datetime.fromisoformat(interval["end"])

            if end <= now:
                continue

            start = max(start, now)

            if end > start:
                pool.append({
                    "start": start,
                    "end": end,
                })

    return pool


def _allocate_brief_slot(
    pool: list[dict],
    duration: timedelta,
) -> tuple[datetime, datetime] | None:
    for interval in pool:
        start = interval["start"]
        end = interval["end"]

        if end - start < duration:
            continue

        proposed_end = start + duration
        interval["start"] = proposed_end
        return start, proposed_end

    return None


def build_evening_brief(
    now: datetime | None = None,
) -> str:

    now = now or datetime.now(
        MOSCOW_TZ
    )

    if now.tzinfo is None:
        now = now.replace(
            tzinfo=MOSCOW_TZ
        )
    else:
        now = now.astimezone(
            MOSCOW_TZ
        )

    # Вечерний бриф относится
    # к ТЕКУЩЕМУ дню.
    review_date = now.date()

    (
        candidates,
        registered,
        completed,
    ) = _brief_task_state(
        review_date
    )

    lines = [
        (
            "Вечерний бриф за "
            f"{review_date.strftime('%d.%m.%Y')}"
        ),
        "",
    ]

    if not candidates:
        lines.extend([
            (
                "Запланированных рабочих "
                "задач на сегодня нет."
            ),
            (
                "Переносить на другой день "
                "нечего."
            ),
        ])

        return "\n".join(
            lines
        )

    completed_count = sum(
        1
        for token, _event
        in registered
        if token in completed
    )

    lines.append(
        (
            f"Задач: {len(registered)}. "
            f"Выполнено: "
            f"{completed_count}."
        )
    )

    lines.extend([
        "",
        (
            "Отметь галочками, "
            "что сегодня выполнено."
        ),
        (
            "Когда закончишь — нажми "
            "«Готово, проверить остаток»."
        ),
        "",
    ])

    for number, (
        token,
        event,
    ) in enumerate(
        registered,
        start=1,
    ):

        marker = (
            "✅"
            if token in completed
            else "☐"
        )

        lines.append(
            f'{marker} {number}. '
            f'{event.get("title", "Без названия")} '
            f'{event.get("start", "")}'
            f'–{event.get("end", "")}'
        )

    return "\n".join(
        lines
    )


async def evening_brief_loop(
    bot: Bot,
) -> None:

    while True:

        now = datetime.now(
            MOSCOW_TZ
        )

        sent_today = (
            _brief_last_sent_date()
            == now.date().isoformat()
        )

        in_send_window = (
            EVENING_BRIEF_TIME
            <= now.time()
            < EVENING_BRIEF_WINDOW_END
        )

        if (
            in_send_window
            and not sent_today
        ):
            try:
                review_date = (
                    now.date()
                )

                brief = (
                    await asyncio.to_thread(
                        build_evening_brief,
                        now,
                    )
                )

                candidates = (
                    _brief_candidate_events(
                        review_date
                    )
                )

                keyboard = (
                    await asyncio.to_thread(
                        task_checklist_keyboard,
                        candidates,
                        review_date,
                        (
                            "brief_review_done:"
                            + review_date.isoformat()
                        ),
                    )
                )

                await bot.send_message(
                    OWNER_ID,
                    brief,
                    reply_markup=keyboard,
                )

                await asyncio.to_thread(
                    save_message,
                    "assistant",
                    brief,
                    "general",
                )

                await asyncio.to_thread(
                    _save_brief_sent_date,
                    now.date(),
                )

                logging.info(
                    "Evening brief sent"
                )

            except Exception:
                logging.exception(
                    "Evening brief failed"
                )

        await asyncio.sleep(30)


def is_owner(message: Message) -> bool:
    return bool(
        message.from_user
        and message.from_user.id == OWNER_ID
    )


@dp.message(CommandStart())
async def start(message: Message):
    if not is_owner(message):
        return

    await message.answer(
        "Clippy запущена.\n\n"
        "Доступ подтверждён. Пока это базовая версия."
    )



def calendar_confirm_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить",
                    callback_data="calendar_confirm",
                ),
                InlineKeyboardButton(
                    text="❌ Отменить",
                    callback_data="calendar_cancel",
                ),
            ]
        ]
    )


def client_message_confirm_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📨 Отправить",
                    callback_data="client_message_confirm",
                ),
                InlineKeyboardButton(
                    text="❌ Отменить",
                    callback_data="client_message_cancel",
                ),
            ]
        ]
    )


def format_client_message_result(result: dict) -> str:
    if not result.get("ok"):
        return "Сообщение клиенту не отправлено. Черновик сохранён."

    recipient = result.get("recipient", {})
    name = recipient.get("full_name") or recipient.get("username") or "клиент"

    if result.get("status") == "scheduled":
        send_at = datetime.fromisoformat(result["send_at"])
        return (
            f"Напоминание для {name} запланировано на "
            f'{send_at.astimezone(MOSCOW_TZ).strftime("%d.%m %H:%M")}.'
        )

    return f"Сообщение отправлено клиенту: {name}."


def format_verified_schedule(
    target_date,
    events,
):
    lines = [
        "Проверил Google Calendar.",
        f"Фактическое расписание на "
        f"{target_date.strftime('%d.%m.%Y')}:",
        "",
    ]

    for event in events:
        title = event.get(
            "title",
            "Без названия",
        )

        if event.get("all_day"):
            when = "весь день"
        else:
            when = (
                f'{event.get("start", "")}'
                f'–{event.get("end", "")}'
            )

        lines.append(
            f"• {when} — {title}"
        )

    return "\n".join(lines)



def simple_schedule_read_target_date(
    text: str,
):
    """
    Возвращает дату только для простого чтения расписания.

    Примеры:
    - Что у меня сегодня?
    - Что у меня завтра?
    - Что запланировано послезавтра?
    - Покажи расписание на сегодня.
    - Что в календаре завтра?

    Изменения календаря и планирование сюда не попадают.
    """

    value = " ".join(
        (text or "")
        .lower()
        .replace("ё", "е")
        .split()
    )

    if not value:
        return None

    # Всё, что похоже на изменение календаря,
    # обязательно оставляем AI.
    mutation_words = (
        "добавь",
        "создай",
        "поставь",
        "запиши",
        "перенеси",
        "перенос",
        "передвинь",
        "сдвинь",
        "измени",
        "удали",
        "убери",
        "отмени",
        "замени",
        "переставь",
        "примени",
        "применяй",
        "применить",
        "внеси",
        "зафиксируй",
        "запланируй",
        "сделай",
    )

    if any(
        word in value
        for word in mutation_words
    ):
        return None

    # Планирование оставляем AI.
    if is_planning_request(text):
        return None

    has_date_word = any(
        word in value
        for word in (
            "сегодня",
            "завтра",
            "послезавтра",
        )
    )

    calendarish = any(
        phrase in value
        for phrase in (
            "расписан",
            "календар",
            "запланирован",
            "по плану",
            "планы на",
        )
    )

    # "что у меня сегодня?" считаем запросом
    # расписания даже без слова "календарь".
    what_do_i_have = (
        "что у меня" in value
        and has_date_word
    )

    if not calendarish and not what_do_i_have:
        return None

    today = datetime.now(
        MOSCOW_TZ
    ).date()

    # Важно проверять послезавтра раньше завтра.
    if "послезавтра" in value:
        return today + timedelta(days=2)

    if "завтра" in value:
        return today + timedelta(days=1)

    if "сегодня" in value:
        return today

    # Если явно спросили календарь/расписание
    # без даты — считаем, что речь о сегодня.
    if calendarish:
        return today

    return None


def format_apply_result(result):
    applied = result.get(
        "applied",
        [],
    )

    lines = []

    if result.get("ok"):
        lines.append(
            "Изменения применены."
        )
    else:
        lines.append(
            "Не все изменения удалось применить."
        )

    for item in applied:
        lines.append(
            f"• {item}"
        )

    if not result.get("ok"):
        lines.append(
            "Ошибка: "
            + str(
                result.get(
                    "error",
                    "неизвестная ошибка",
                )
            )
        )

    return "\n".join(lines)


@dp.callback_query()
async def calendar_confirmation(
    callback: CallbackQuery,
):
    if (
        not callback.from_user
        or callback.from_user.id
        != OWNER_ID
    ):
        return

    if (
        callback.data
        or ""
    ).startswith(
        "morning_select:"
    ):
        raw_item_key = callback.data.split(
            ":",
            1,
        )[1]

        proposal_date_hint, token = _decode_morning_item_key(
            raw_item_key
        )

        found = _find_morning_item(
            token,
            proposal_date_hint,
        )

        if not found:
            await callback.answer(
                "Предложение уже не найдено",
                show_alert=True,
            )
            return

        (
            proposal_date,
            _proposal,
            item,
        ) = found

        selected = not bool(
            item.get(
                "selected"
            )
        )

        _update_morning_item(
            proposal_date,
            token,
            selected=selected,
        )

        if callback.message:
            await callback.message.edit_reply_markup(
                reply_markup=(
                    morning_approval_keyboard(
                        proposal_date
                    )
                )
            )

        await callback.answer(
            (
                "Выбрано"
                if selected
                else "Снято"
            )
        )

        return


    if (
        callback.data
        or ""
    ).startswith(
        "morning_move:"
    ):
        raw_item_key = callback.data.split(
            ":",
            1,
        )[1]

        proposal_date_hint, token = _decode_morning_item_key(
            raw_item_key
        )

        found = _find_morning_item(token, proposal_date_hint)

        if not found:
            await callback.answer(
                "Предложение уже не найдено",
                show_alert=True,
            )
            return

        if callback.message:
            await callback.message.edit_reply_markup(
                reply_markup=(
                    _morning_date_keyboard(
                        token,
                        found[0],
                    )
                )
            )

        await callback.answer(
            "Выбери день"
        )

        return


    if (
        callback.data
        or ""
    ).startswith(
        "morning_back:"
    ):
        raw_item_key = callback.data.split(
            ":",
            1,
        )[1]

        proposal_date_hint, token = _decode_morning_item_key(
            raw_item_key
        )

        found = _find_morning_item(
            token,
            proposal_date_hint,
        )

        if found and callback.message:
            proposal_date = found[0]

            await callback.message.edit_reply_markup(
                reply_markup=(
                    morning_approval_keyboard(
                        proposal_date
                    )
                )
            )

        await callback.answer()

        return


    if (
        callback.data
        or ""
    ).startswith(
        "morning_date:"
    ):
        try:
            raw_item_key, raw_offset = (
                callback.data.split(
                    ":",
                    1,
                )[1].rsplit(":", 1)
            )

            offset = int(
                raw_offset
            )

            proposal_date_hint, token = _decode_morning_item_key(
                raw_item_key
            )

        except Exception:
            await callback.answer(
                "Некорректная дата",
                show_alert=True,
            )
            return

        found = _find_morning_item(
            token,
            proposal_date_hint,
        )

        if not found:
            await callback.answer(
                "Предложение уже не найдено",
                show_alert=True,
            )
            return

        (
            proposal_date,
            proposal,
            item,
        ) = found

        target_date = (
            datetime.now(MOSCOW_TZ).date()
            + timedelta(
                days=offset
            )
        )

        reserved = []

        for other in proposal.get(
            "items",
            [],
        ):

            if (
                not isinstance(
                    other,
                    dict,
                )
                or other is item
                or other.get(
                    "target_date"
                )
                != target_date.isoformat()
            ):
                continue

            try:
                reserved.append(
                    (
                        datetime.fromisoformat(
                            other[
                                "start"
                            ]
                        ).astimezone(
                            MOSCOW_TZ
                        ),
                        datetime.fromisoformat(
                            other[
                                "end"
                            ]
                        ).astimezone(
                            MOSCOW_TZ
                        ),
                    )
                )
            except Exception:
                pass

        slot = await asyncio.to_thread(
            _morning_find_slot,
            target_date,
            int(
                item.get(
                    "estimated_minutes"
                )
                or 60
            ),
            reserved,
        )

        if not slot:
            await callback.answer(
                "На этот день свободного окна не нашла",
                show_alert=True,
            )
            return

        _update_morning_item(
            proposal_date,
            token,
            target_date=(
                target_date.isoformat()
            ),
            start=(
                slot[
                    "start"
                ].isoformat()
            ),
            end=(
                slot[
                    "end"
                ].isoformat()
            ),
            allow_ozon_overlap=(
                bool(
                    slot.get(
                        "overlaps_ozon"
                    )
                )
            ),
            selected=True,
        )

        if callback.message:
            await callback.message.edit_reply_markup(
                reply_markup=(
                    morning_approval_keyboard(
                        proposal_date
                    )
                )
            )

        await callback.answer(
            "Дата изменена"
        )

        return


    if (
        callback.data
        or ""
    ).startswith(
        "morning_apply:"
    ):
        try:
            proposal_date = (
                date.fromisoformat(
                    callback.data.split(
                        ":",
                        1,
                    )[1]
                )
            )
        except Exception:
            await callback.answer(
                "Некорректный план",
                show_alert=True,
            )
            return

        proposal = (
            _get_morning_proposal(
                proposal_date
            )
        )

        if not proposal:
            await callback.answer(
                "Утренний план уже не найден",
                show_alert=True,
            )
            return

        if proposal_date < datetime.now(MOSCOW_TZ).date():
            await callback.answer(
                (
                    "Это предложение устарело. "
                    "Запусти /test_morning ещё раз."
                ),
                show_alert=True,
            )
            return

        selected = [
            item
            for item in proposal.get(
                "items",
                [],
            )
            if (
                isinstance(
                    item,
                    dict,
                )
                and item.get(
                    "selected"
                )
            )
        ]

        if not selected:
            await callback.answer(
                "Сначала отметь хотя бы одну задачу",
                show_alert=True,
            )
            return

        if has_pending_changes():
            await callback.answer(
                "Сначала закончи другое ожидающее изменение календаря",
                show_alert=True,
            )
            return

        actions = []

        for item in selected:
            actions.append({
                "type": "create",
                "calendar_kind": (
                    "personal"
                ),
                "event_id": None,
                "title": (
                    item["title"]
                ),
                "start": (
                    item["start"]
                ),
                "end": (
                    item["end"]
                ),
                "allow_ozon_overlap": (
                    bool(
                        item.get(
                            "allow_ozon_overlap"
                        )
                    )
                ),
            })

        await asyncio.to_thread(
            clear_saved_plan_proposal
        )

        saved = await asyncio.to_thread(
            save_plan_proposal,
            proposal_date.isoformat(),
            actions,
            (
                "Утренний согласованный "
                "план Clippy."
            ),
        )

        if not saved.get(
            "ok"
        ):
            await callback.answer(
                (
                    "Не удалось сохранить план: "
                    + str(
                        saved.get(
                            "error"
                        )
                    )
                ),
                show_alert=True,
            )
            return

        prepared = await asyncio.to_thread(
            prepare_saved_plan_for_confirmation
        )

        if not prepared.get(
            "ok"
        ):
            await asyncio.to_thread(
                clear_pending_changes
            )

            await callback.answer(
                (
                    "Не удалось подготовить: "
                    + str(
                        prepared.get(
                            "error"
                        )
                    )
                ),
                show_alert=True,
            )
            return

        applied = await asyncio.to_thread(
            apply_pending_changes
        )

        if not applied.get(
            "ok"
        ):
            await callback.answer(
                "Не удалось применить план",
                show_alert=True,
            )
            return

        action_ids = [
            str(
                item.get(
                    "action_id"
                )
                or ""
            )
            for item in selected
        ]

        await asyncio.to_thread(
            mark_project_actions_planned,
            action_ids,
            proposal_date,
        )

        proposal[
            "applied"
        ] = True

        proposal[
            "applied_at"
        ] = datetime.now(
            MOSCOW_TZ
        ).isoformat()

        _save_morning_proposal(
            proposal_date,
            proposal,
        )

        await asyncio.to_thread(
            clear_saved_plan_proposal
        )

        if callback.message:
            await callback.message.edit_reply_markup(
                reply_markup=None
            )

            await callback.message.answer(
                format_apply_result(
                    applied
                )
            )

        await callback.answer(
            "План утверждён"
        )

        return


    if (callback.data or "").startswith("task_toggle:"):
        token = callback.data.split(":", 1)[1]
        is_completed = await asyncio.to_thread(
            _toggle_task_token,
            token,
        )

        if is_completed is None:
            await callback.answer(
                "Задача уже не найдена",
                show_alert=True,
            )
            return

        if callback.message:
            keyboard = _updated_task_keyboard(
                callback.message.reply_markup,
                token,
                is_completed,
            )
            await callback.message.edit_reply_markup(
                reply_markup=keyboard
            )

        await callback.answer(
            "Отмечено выполненным"
            if is_completed
            else "Галочка снята"
        )
        return

    if (
        callback.data
        or ""
    ).startswith(
        "brief_review_done:"
    ):
        try:
            review_date = (
                date.fromisoformat(
                    callback.data.split(
                        ":",
                        1,
                    )[1]
                )
            )

            open_tasks = (
                await asyncio.to_thread(
                    _brief_open_tasks,
                    review_date,
                )
            )

            if not open_tasks:
                if callback.message:
                    await callback.message.answer(
                        "Все задачи дня выполнены. "
                        "Переносить нечего."
                    )

                await callback.answer(
                    "Все выполнено"
                )
                return

            lines = [
                "Остались невыполненными:",
                "",
            ]

            for item in open_tasks:
                lines.append(
                    "☐ "
                    + str(
                        item.get(
                            "title",
                            "Без названия",
                        )
                    )
                )

            lines.extend([
                "",
                (
                    "Перенести их на другое "
                    "свободное время?"
                ),
            ])

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=(
                                "🔁 Предложить время"
                            ),
                            callback_data=(
                                "brief_reschedule:"
                                + review_date.isoformat()
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=(
                                "Оставить как есть"
                            ),
                            callback_data=(
                                "brief_keep:"
                                + review_date.isoformat()
                            ),
                        )
                    ],
                ]
            )

            if callback.message:
                await callback.message.answer(
                    "\n".join(lines),
                    reply_markup=keyboard,
                )

            await callback.answer(
                "Проверила остаток"
            )

        except Exception:
            logging.exception(
                "Evening review finish failed"
            )

            await callback.answer(
                "Не удалось проверить задачи",
                show_alert=True,
            )

        return


    if (
        callback.data
        or ""
    ).startswith(
        "brief_keep:"
    ):
        await callback.answer(
            "Оставляю без переноса"
        )

        if callback.message:
            await callback.message.answer(
                "Невыполненные задачи "
                "оставлены на текущей дате."
            )

        return


    if (
        callback.data
        or ""
    ).startswith(
        "brief_reschedule:"
    ):
        try:
            review_date = (
                date.fromisoformat(
                    callback.data.split(
                        ":",
                        1,
                    )[1]
                )
            )

            result = (
                await asyncio.to_thread(
                    _prepare_evening_replan,
                    review_date,
                )
            )

            if not result.get(
                "ok"
            ):
                error = result.get(
                    "error"
                )

                if (
                    error
                    == "no_open_tasks"
                ):
                    text = (
                        "Невыполненных задач "
                        "уже нет."
                    )

                elif (
                    error
                    == "not_enough_free_time"
                ):
                    missing = ", ".join(
                        result.get(
                            "not_placed",
                            [],
                        )
                    )

                    text = (
                        "Не удалось найти "
                        "достаточно свободного "
                        "времени для: "
                        + missing
                    )

                else:
                    text = (
                        "Не удалось подготовить "
                        "перенос: "
                        + str(error)
                    )

                if callback.message:
                    await callback.message.answer(
                        text
                    )

                await callback.answer()
                return

            lines = [
                (
                    "Предлагаю перенести "
                    "невыполненные задачи:"
                ),
                "",
            ]

            for item in result.get(
                "proposed",
                [],
            ):
                start = item[
                    "start"
                ]

                end = item[
                    "end"
                ]

                suffix = (
                    " — параллельно OZON"
                    if item.get(
                        "overlaps_ozon"
                    )
                    else ""
                )

                lines.append(
                    f'• {item["title"]}: '
                    f'{start.strftime("%d.%m %H:%M")}'
                    f'–{end.strftime("%H:%M")}'
                    f'{suffix}'
                )

            lines.extend([
                "",
                (
                    "Применить этот перенос?"
                ),
            ])

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=(
                                "✅ Перенести"
                            ),
                            callback_data=(
                                "brief_apply_plan"
                            ),
                        ),
                        InlineKeyboardButton(
                            text="❌ Отмена",
                            callback_data=(
                                "brief_cancel_plan"
                            ),
                        ),
                    ]
                ]
            )

            if callback.message:
                await callback.message.answer(
                    "\n".join(lines),
                    reply_markup=keyboard,
                )

            await callback.answer(
                "Время найдено"
            )

        except Exception:
            logging.exception(
                "Evening reschedule failed"
            )

            await callback.answer(
                "Ошибка поиска времени",
                show_alert=True,
            )

        return


    if callback.data == "brief_apply_plan":
        try:
            prepared = (
                await asyncio.to_thread(
                    prepare_saved_plan_for_confirmation
                )
            )

            if not prepared.get(
                "ok"
            ):
                if callback.message:
                    await callback.message.answer(
                        "Перенос не подготовлен: "
                        + str(
                            prepared.get(
                                "error",
                                "неизвестная ошибка",
                            )
                        )
                    )

                await callback.answer()
                return

            applied = (
                await asyncio.to_thread(
                    apply_pending_changes
                )
            )

            if callback.message:
                await callback.message.answer(
                    format_apply_result(
                        applied
                    )
                )

            await callback.answer(
                (
                    "Перенесено"
                    if applied.get("ok")
                    else "Есть ошибка"
                )
            )

        except Exception:
            logging.exception(
                "Evening plan apply failed"
            )

            await callback.answer(
                "Не удалось применить перенос",
                show_alert=True,
            )

        return


    if callback.data == "brief_cancel_plan":
        await asyncio.to_thread(
            clear_saved_plan_proposal
        )

        await asyncio.to_thread(
            clear_pending_changes
        )

        if callback.message:
            await callback.message.answer(
                "Перенос отменён."
            )

        await callback.answer(
            "Отменено"
        )
        return


    if callback.data == "client_message_cancel":
        await asyncio.to_thread(clear_pending_client_message)
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer("Сообщение клиенту отменено.")
        await callback.answer("Отменено")
        return

    if callback.data == "client_message_confirm":
        result = await asyncio.to_thread(
            confirm_pending_client_message
        )
        if callback.message:
            if result.get("ok"):
                await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer(
                format_client_message_result(result)
            )
        await callback.answer(
            "Готово" if result.get("ok") else "Не отправлено",
            show_alert=not result.get("ok"),
        )
        return

    if callback.data == "calendar_cancel":
        clear_pending_changes()
        clear_saved_plan_proposal()

        await callback.message.edit_reply_markup(
            reply_markup=None
        )

        await callback.message.answer(
            "Изменения календаря отменены."
        )

        await callback.answer(
            "Отменено"
        )
        return

    if callback.data != "calendar_confirm":
        await callback.answer()
        return

    proposal = get_saved_plan_proposal()

    if not has_pending_changes():

        if proposal:
            prepared = await asyncio.to_thread(
                prepare_saved_plan_for_confirmation
            )

            if not prepared.get("ok"):
                await callback.answer(
                    prepared.get(
                        "error",
                        "Не удалось подготовить изменения",
                    ),
                    show_alert=True,
                )
                return

    if not has_pending_changes():
        await callback.answer(
            "Нет изменений для подтверждения",
            show_alert=True,
        )
        return

    target_dates = await asyncio.to_thread(
        get_pending_target_dates
    )
    result = await asyncio.to_thread(
        apply_pending_changes
    )
    reply_text = format_apply_result(result)

    await callback.message.edit_reply_markup(
        reply_markup=None
    )

    await callback.message.answer(
        reply_text
    )

    await asyncio.to_thread(
        save_message,
        "assistant",
        "Подтверждённое состояние календаря:\n"
        + reply_text,
        "general",
    )

    if result.get("ok"):
        clear_saved_plan_proposal()
        clear_booking_context()

    if result.get("ok"):
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(
                "Europe/Moscow"
            )

            if not target_dates and (
                proposal
                and proposal.get("target_date")
            ):
                target_dates = [
                    date.fromisoformat(
                        proposal["target_date"]
                    )
                ]

            if not target_dates:
                target_dates = [datetime.now(tz).date()]

            for target_date in target_dates[:3]:
                events = await asyncio.to_thread(
                    get_day_schedule,
                    target_date,
                )
                verified_text = format_verified_schedule(
                    target_date,
                    events,
                )
                await callback.message.answer(
                    verified_text
                )
                await asyncio.to_thread(
                    save_message,
                    "assistant",
                    verified_text,
                    "general",
                )

        except Exception:
            logging.exception(
                "Не удалось проверить календарь "
                "после применения"
            )

    await callback.answer(
        "Готово"
        if result.get("ok")
        else "Есть ошибка"
    )





def _normalize_schedule_title(
    value: str,
) -> str:
    return " ".join(
        re.sub(
            r"[^а-яёa-z0-9]+",
            " ",
            str(value or "")
            .casefold()
            .replace("ё", "е"),
        ).split()
    )


def _exact_batch_target_date(
    text: str,
) -> date | None:
    value = (
        str(text or "")
        .casefold()
        .replace("ё", "е")
    )

    today = datetime.now(
        MOSCOW_TZ
    ).date()

    if "послезавтра" in value:
        return today + timedelta(days=2)

    if "завтра" in value:
        return today + timedelta(days=1)

    if "сегодня" in value:
        return today

    iso_match = re.search(
        r"\b(20\d{2})-(\d{2})-(\d{2})\b",
        value,
    )

    if iso_match:
        try:
            return date(
                int(iso_match.group(1)),
                int(iso_match.group(2)),
                int(iso_match.group(3)),
            )
        except ValueError:
            return None

    numeric = re.search(
        r"\b(\d{1,2})[./](\d{1,2})"
        r"(?:[./](20\d{2}))?\b",
        value,
    )

    if numeric:
        try:
            return date(
                int(
                    numeric.group(3)
                    or today.year
                ),
                int(numeric.group(2)),
                int(numeric.group(1)),
            )
        except ValueError:
            return None

    months = {
        "января": 1,
        "февраля": 2,
        "марта": 3,
        "апреля": 4,
        "мая": 5,
        "июня": 6,
        "июля": 7,
        "августа": 8,
        "сентября": 9,
        "октября": 10,
        "ноября": 11,
        "декабря": 12,
    }

    month_names = "|".join(
        months
    )

    match = re.search(
        rf"\b(\d{{1,2}})\s+"
        rf"({month_names})"
        rf"(?:\s+(20\d{{2}}))?\b",
        value,
    )

    if not match:
        return None

    try:
        return date(
            int(
                match.group(3)
                or today.year
            ),
            months[match.group(2)],
            int(match.group(1)),
        )
    except ValueError:
        return None


def _parse_exact_batch_schedule(
    text: str,
) -> dict | None:

    target_date = (
        _exact_batch_target_date(
            text
        )
    )

    if target_date is None:
        return None

    pattern = re.compile(
        r"(?m)^\s*"
        r"[•☐□\-]*\s*"
        r"((?:[01]?\d|2[0-3])"
        r"[:.][0-5]\d)"
        r"\s*[-–—]\s*"
        r"((?:[01]?\d|2[0-3])"
        r"[:.][0-5]\d)"
        r"\s*(?:[-–—:])\s*"
        r"(.+?)\s*$"
    )

    blocks = []

    for match in pattern.finditer(
        text
    ):
        start_text = (
            match.group(1)
            .replace(".", ":")
        )

        end_text = (
            match.group(2)
            .replace(".", ":")
        )

        title = (
            match.group(3)
            .strip()
            .strip("*")
            .strip()
        )

        if not title:
            continue

        blocks.append({
            "start": start_text,
            "end": end_text,
            "title": title,
        })

    if len(blocks) < 2:
        return None

    return {
        "target_date": target_date,
        "blocks": blocks,
    }


def _schedule_match_score(
    requested: str,
    existing: str,
) -> float:

    req = _normalize_schedule_title(
        requested
    )

    cur = _normalize_schedule_title(
        existing
    )

    if not req or not cur:
        return 0.0

    if req == cur:
        return 100.0

    if req in cur:
        return 90.0

    if cur in req:
        return 85.0

    req_tokens = set(
        req.split()
    )

    cur_tokens = set(
        cur.split()
    )

    if (
        req_tokens
        and req_tokens.issubset(
            cur_tokens
        )
    ):
        return 80.0

    overlap = len(
        req_tokens
        & cur_tokens
    )

    if not overlap:
        return 0.0

    return (
        50.0
        * overlap
        / max(
            len(req_tokens),
            len(cur_tokens),
        )
    )


def _build_exact_batch_actions(
    target_date: date,
    blocks: list[dict],
) -> dict:

    events = get_day_overview(
        target_date
    )

    candidates = []

    for event in events:

        # Отдельные all-day строки Google Tasks
        # не являются Calendar blocks.
        if event.get("source") == "google_tasks":
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

        try:
            current_start = (
                datetime.fromisoformat(
                    start_raw
                )
                .astimezone(
                    MOSCOW_TZ
                )
            )

            current_end = (
                datetime.fromisoformat(
                    end_raw
                )
                .astimezone(
                    MOSCOW_TZ
                )
            )

        except Exception:
            continue

        # Carry-over предыдущего дня не участвует
        # в сопоставлении графика нового дня.
        if (
            current_start.date()
            < target_date
        ):
            continue

        candidates.append({
            "event": event,
            "start": current_start,
            "end": current_end,
        })

    used_event_ids = set()
    actions = []
    unchanged = []
    matches = []

    for block in blocks:

        requested_title = (
            block["title"]
        )

        ranked = []

        for candidate in candidates:

            event = candidate[
                "event"
            ]

            event_id = str(
                event.get(
                    "event_id",
                    "",
                )
            )

            if (
                not event_id
                or event_id
                in used_event_ids
            ):
                continue

            score = _schedule_match_score(
                requested_title,
                event.get(
                    "title",
                    "",
                ),
            )

            if score > 0:
                ranked.append(
                    (
                        score,
                        candidate,
                    )
                )

        ranked.sort(
            key=lambda item:
            item[0],
            reverse=True,
        )

        if not ranked:
            return {
                "ok": False,
                "error": (
                    "Не найден существующий "
                    "календарный блок для "
                    f"«{requested_title}»"
                ),
            }

        best_score, candidate = (
            ranked[0]
        )

        if (
            len(ranked) > 1
            and best_score < 100
            and (
                best_score
                - ranked[1][0]
            ) < 10
        ):
            return {
                "ok": False,
                "error": (
                    "Неоднозначно, какой блок "
                    f"соответствует «{requested_title}»"
                ),
            }

        event = candidate[
            "event"
        ]

        event_id = str(
            event["event_id"]
        )

        used_event_ids.add(
            event_id
        )

        start_h, start_m = map(
            int,
            block["start"].split(":"),
        )

        end_h, end_m = map(
            int,
            block["end"].split(":"),
        )

        new_start = datetime.combine(
            target_date,
            time(
                start_h,
                start_m,
            ),
            tzinfo=MOSCOW_TZ,
        )

        new_end = datetime.combine(
            target_date,
            time(
                end_h,
                end_m,
            ),
            tzinfo=MOSCOW_TZ,
        )

        if new_end <= new_start:
            new_end += timedelta(
                days=1
            )

        current_start = candidate[
            "start"
        ]

        current_end = candidate[
            "end"
        ]

        actual_title = event.get(
            "title",
            requested_title,
        )

        matches.append({
            "requested": requested_title,
            "actual": actual_title,
            "source": event.get(
                "source"
            ),
            "event_id": event_id,
        })

        # Уже стоит ровно так —
        # изменение не требуется.
        if (
            current_start == new_start
            and current_end == new_end
        ):
            unchanged.append(
                actual_title
            )
            continue

        # Фиксированные обычные Calendar events
        # пакетный planner не двигает.
        if (
            event.get("source")
            != "linked_google_task"
        ):
            return {
                "ok": False,
                "error": (
                    f"«{actual_title}» — фиксированное "
                    "событие. Сейчас оно стоит "
                    f"{current_start.strftime('%H:%M')}"
                    f"–{current_end.strftime('%H:%M')}, "
                    "и пакетный график не может "
                    "автоматически его переносить."
                ),
            }

        actions.append({
            "type": "update",
            "calendar_id": (
                event.get(
                    "calendar_id"
                )
            ),
            "event_id": event_id,
            "title": actual_title,
            "description": "",
            "allow_ozon_overlap": False,
            "start": (
                new_start.isoformat()
            ),
            "end": (
                new_end.isoformat()
            ),
        })

    return {
        "ok": True,
        "actions": actions,
        "unchanged": unchanged,
        "matches": matches,
    }


async def _handle_exact_batch_schedule(
    message: Message,
    text: str,
) -> str | None:

    parsed = _parse_exact_batch_schedule(
        text
    )

    if not parsed:
        return None

    target_date = parsed[
        "target_date"
    ]

    built = await asyncio.to_thread(
        _build_exact_batch_actions,
        target_date,
        parsed["blocks"],
    )

    if not built.get("ok"):
        reply = (
            "Не удалось подготовить пакетный "
            "график: "
            + str(
                built.get(
                    "error",
                    "неизвестная ошибка",
                )
            )
        )

        await message.answer(
            reply
        )

        return reply

    actions = built.get(
        "actions",
        []
    )

    if not actions:
        reply = (
            "Этот график уже стоит "
            f"на {target_date.strftime('%d.%m.%Y')}. "
            "Изменения не требуются."
        )

        await message.answer(
            reply
        )

        return reply

    await asyncio.to_thread(
        clear_pending_changes
    )

    await asyncio.to_thread(
        clear_saved_plan_proposal
    )

    summary = (
        "Точный пакетный график на "
        + target_date.strftime(
            "%d.%m.%Y"
        )
    )

    saved = await asyncio.to_thread(
        save_plan_proposal,
        target_date.isoformat(),
        actions,
        summary,
    )

    if not saved.get("ok"):
        reply = (
            "Не удалось сохранить пакетный "
            "план: "
            + str(
                saved.get(
                    "error",
                    "неизвестная ошибка",
                )
            )
        )

        await message.answer(
            reply
        )

        return reply

    prepared = await asyncio.to_thread(
        prepare_saved_plan_for_confirmation
    )

    if not prepared.get("ok"):
        await asyncio.to_thread(
            clear_pending_changes
        )

        await asyncio.to_thread(
            clear_saved_plan_proposal
        )

        reply = (
            "Пакет распознан, но не прошёл "
            "проверку календаря: "
            + str(
                prepared.get(
                    "error",
                    "неизвестная ошибка",
                )
            )
        )

        await message.answer(
            reply
        )

        return reply

    lines = [
        (
            "Пакетный график подготовлен "
            f"на {target_date.strftime('%d.%m.%Y')}:"
        ),
        "",
    ]

    for action in actions:
        start = datetime.fromisoformat(
            action["start"]
        )

        end = datetime.fromisoformat(
            action["end"]
        )

        lines.append(
            "• "
            + str(
                action["title"]
            )
            + " → "
            + start.strftime(
                "%H:%M"
            )
            + "–"
            + end.strftime(
                "%H:%M"
            )
        )

    unchanged = built.get(
        "unchanged",
        []
    )

    if unchanged:
        lines.extend([
            "",
            "Без изменений: "
            + ", ".join(
                unchanged
            ),
        ])

    lines.extend([
        "",
        (
            "Все существующие linked-задачи "
            "сохраняют свои Google Task и event_id."
        ),
        "Для применения напиши «применяй».",
    ])

    reply = "\n".join(
        lines
    )

    await message.answer(
        reply,
        reply_markup=calendar_confirm_kb(),
    )

    return reply




def is_planning_request(
    text: str,
) -> bool:
    """
    Определяет запрос именно на построение /
    перепланирование расписания.

    Это не запрос простого чтения Calendar
    и не конкретная команда переноса одного события.
    """

    value = " ".join(
        (text or "")
        .lower()
        .replace("ё", "е")
        .split()
    )

    if not value:
        return False

    # Явные глаголы планирования.
    if any(
        stem in value
        for stem in (
            "распланир",
            "перепланир",
            "спланир",
        )
    ):
        return True

    # "Составь новый план", "составь мне план" и т.п.
    if (
        "составь" in value
        and "план" in value
    ):
        return True

    # "Предложи новый план / расписание".
    if (
        "предложи" in value
        and any(
            marker in value
            for marker in (
                "план",
                "расписан",
            )
        )
    ):
        return True

    # Распределение реальных задач.
    if (
        any(
            verb in value
            for verb in (
                "распредели",
                "раскидай",
            )
        )
        and any(
            noun in value
            for noun in (
                "задач",
                "дела",
                "дел",
                "работ",
            )
        )
    ):
        return True

    # Более разговорные команды на перестройку дня.
    if any(
        phrase in value
        for phrase in (
            "как лучше распределить",
            "что теперь делать сегодня",
            "что делать дальше сегодня",
            "составь новый график",
            "предложи новый график",
        )
    ):
        return True

    # Запрос самостоятельно найти новое время.
    if (
        "найди" in value
        and any(
            marker in value
            for marker in (
                "время",
                "окно",
                "когда",
            )
        )
    ):
        return True

    # --------------------------------------------------
    # BATCH SCHEDULE
    #
    # Если пользователь задаёт сразу несколько точных
    # временных блоков, это перестройка расписания,
    # а не серия независимых direct-update.
    #
    # Пример:
    # 10:30–11:30 — тренировка
    # 12:30–15:00 — визитки
    # 16:00–17:00 — стикерпак
    #
    # Такой пакет должен сохраняться одним proposal,
    # чтобы старые позиции переносимых задач считались
    # vacating и не конфликтовали с новыми.
    # --------------------------------------------------

    explicit_time_ranges = re.findall(
        r"(?<!\d)"
        r"(?:[01]?\d|2[0-3])[:.][0-5]\d"
        r"\s*(?:[-–—]|до)\s*"
        r"(?:[01]?\d|2[0-3])[:.][0-5]\d"
        r"(?!\d)",
        value,
    )

    if len(explicit_time_ranges) >= 2:
        return True

    # Точное время означает прямую команду:
    # «перенеси Clippy на 18:00–19:00».
    has_exact_clock = bool(
        re.search(
            r"(?<!\d)"
            r"(?:[01]?\d|2[0-3])"
            r"[:.]"
            r"[0-5]\d"
            r"(?!\d)",
            value,
        )
    )

    # Если точного времени нет, а пользователь
    # просит перенести на широкое окно/другой день,
    # Clippy должна сначала сама подобрать слот.
    if (
        not has_exact_clock
        and any(
            stem in value
            for stem in (
                "перенес",
                "сдвин",
            )
        )
        and any(
            marker in value
            for marker in (
                "на вечер",
                "вечером",
                "на утро",
                "утром",
                "днем",
                "днём",
                "сегодня",
                "завтра",
                "позже",
                "другое время",
                "свободное время",
                "свободное окно",
            )
        )
    ):
        return True

    return False




def is_new_tattoo_booking_request(
    text: str,
) -> bool:

    value = (text or "").lower().strip()

    action_words = (
        "поставь",
        "запиши",
        "создай",
        "добавь",
        "забронируй",
    )

    booking_words = (
        "сеанс",
        "тату-сеанс",
        "тату сеанс",
        "запись",
        "запись клиента",
        "клиента",
        "клиенту",
        "бронь",
        "бронирование",
        "татуировку",
        "тату",
    )

    return (
        any(
            word in value
            for word in action_words
        )
        and any(
            word in value
            for word in booking_words
        )
    )


def is_calendar_action_followup(
    text: str,
) -> bool:

    value = (text or "").lower().strip()

    phrases = (
        "имя клиента",
        "клиента зовут",
        "клиент будет",
        "клиент —",
        "клиент -",
        "делать будем",
        "делаем ему",
        "делаем ей",
        "работа будет",
        "задача будет",
        "по проекту",
        "стоимость",
        "цена сеанса",
        "цена будет",
        "город будет",
        "город —",
        "город -",
    )

    return any(
        phrase in value
        for phrase in phrases
    )


def is_booking_details_followup(
    last_assistant_reply: str,
) -> bool:
    """Detect a terse answer to Clippy's request for booking details."""

    value = (
        last_assistant_reply
        or ""
    ).lower().strip()

    markers = (
        "дай короткий бриф",
        "короткий бриф:",
        "как зовут клиента",
        "уточни имя клиента",
        "нужно уточнить имя",
        "добавь стоимость",
        "какая стоимость",
        "нужна стоимость",
    )

    return any(
        marker in value
        for marker in markers
    )


def calendar_changes_allowed_for_text(
    text: str,
) -> bool:

    value = (text or "").lower().strip()

    # Планирование всегда READ ONLY.
    # Используем единый классификатор, чтобы routing
    # и разрешение календарных изменений не расходились.
    if is_planning_request(text):
        return False

    # Только явно выраженное действие
    # получает доступ к подготовке изменений.
    mutation_words = (
        "поставь",
        "добавь",
        "создай",
        "запиши",
        "перенеси",
        "перемести",
        "сдвинь",
        "удали",
        "убери",
        "измени",
        "переименуй",
        "отредактируй",
        "напиши",
        "отправь",
        "напомни",
        "запланируй",
        "запланировать",
        "примени",
        "зафиксируй",
        "сделай",
    )

    return any(
        word in value
        for word in mutation_words
    )


def saved_plan_apply_followup(
    text: str,
) -> bool:
    value = (text or "").lower().strip()

    approval_phrases = (
        "можешь изменять",
        "можно изменять",
        "всё правильно",
        "все правильно",
        "делай как предложила",
        "сделай как предложила",
        "примени предложение",
        "примени предложенный план",
        "применяй предложенный план",
        "примени сохранённый план",
        "применяй сохранённый план",
        "да, делай",
        "да делай",
    )
    problem_markers = (
        "не изменилось",
        "не изменилась",
        "не изменился",
        "не исчезла",
        "не исчезло",
        "не перенеслась",
        "не перенеслось",
        "не удалилась",
        "не удалилось",
        "всё ещё отображается",
        "все еще отображается",
        "всё ещё стоит",
        "все еще стоит",
        "не вижу, чтобы она исчезла в календаре",
        "не вижу чтобы она исчезла в календаре",
        "событие всё ещё отображается в календаре",
    )

    return any(
        phrase in value
        for phrase in approval_phrases + problem_markers
    )


async def apply_saved_plan_from_followup(message: Message) -> str:
    prepared = await asyncio.to_thread(
        prepare_saved_plan_for_confirmation
    )

    if not prepared.get("ok"):
        reply_text = (
            "Не удалось подготовить сохранённый план: "
            + prepared.get("error", "неизвестная ошибка")
        )
        await message.answer(reply_text)
        return reply_text

    if prepared.get("requires_confirmation"):
        reply_text = (
            "План содержит изменение OZON. "
            "Подтверди применение всего пакета."
        )
        await message.answer(
            reply_text,
            reply_markup=calendar_confirm_kb(),
        )
        return reply_text

    result = await asyncio.to_thread(apply_pending_changes)
    reply_text = format_apply_result(result)
    await message.answer(reply_text)

    if result.get("ok"):
        clear_saved_plan_proposal()
        clear_booking_context()

    return reply_text


async def process_owner_text(
    message: Message,
    text: str,
):
    text = (text or "").strip()

    if not text:
        await message.answer(
            "Не удалось получить текст команды."
        )
        return

    normalized = re.sub(
        r"[^а-яёa-z0-9\s]+",
        " ",
        text.lower(),
    )

    normalized = " ".join(
        normalized.split()
    )

    confirm_words = {
        "подтверждаю",
        "подтвердить",
        "да подтверждаю",
        "подтверждаю изменения",
        "подтверждаю действие",
        "подтверждаю план",
        "применяй",
        "применить",
        "примени",
        "применяй план",
        "примени план",
        "примени этот план",
        "да применяй",
        "да делаем",
        "делаем",
    }

    cancel_words = {
        "отмена",
        "отменить",
        "не надо",
    }

    if normalized in confirm_words:

        if has_pending_client_message():
            result = await asyncio.to_thread(
                confirm_pending_client_message
            )
            reply_text = format_client_message_result(result)
            await message.answer(reply_text)
            return reply_text

        # Если pending ещё не существует, но AI уже
        # сохранил подтверждаемое предложение —
        # сначала превращаем предложение в реальные
        # pending-операции.
        if not has_pending_changes():

            proposal = (
                get_saved_plan_proposal()
            )

            if proposal:
                prepared = await asyncio.to_thread(
                    prepare_saved_plan_for_confirmation
                )

                if not prepared.get("ok"):
                    reply_text = (
                        "Не удалось подготовить изменения: "
                        + prepared.get(
                            "error",
                            "неизвестная ошибка",
                        )
                    )

                    await message.answer(
                        reply_text
                    )

                    return reply_text

        if has_pending_changes():

            result = await asyncio.to_thread(
                apply_pending_changes
            )

            reply_text = format_apply_result(
                result
            )

            await message.answer(
                reply_text
            )

            await asyncio.to_thread(
                save_message,
                "assistant",
                "Подтверждённое состояние календаря:\n"
                + reply_text,
                "general",
            )

            # Сохранённый план после успешного
            # применения больше не нужен.
            if result.get("ok"):
                clear_saved_plan_proposal()
                clear_booking_context()

            return reply_text

        reply_text = (
            "Сейчас нет изменений для подтверждения."
        )

        await message.answer(
            reply_text
        )

        return reply_text

    if (
        get_saved_plan_proposal()
        and saved_plan_apply_followup(text)
    ):
        return await apply_saved_plan_from_followup(
            message
        )


    if normalized in cancel_words:
        clear_pending_changes()
        clear_saved_plan_proposal()
        clear_pending_client_message()
        clear_booking_context()

        reply_text = (
            "Изменения календаря отменены."
        )

        await message.answer(
            reply_text
        )

        return reply_text

    # Точный многострочный график обрабатываем
    # детерминированно, без генерации большого JSON моделью.
    exact_batch = _parse_exact_batch_schedule(
        text
    )

    if exact_batch is not None:
        result = await _handle_exact_batch_schedule(
            message,
            text,
        )

        if result is not None:
            return result

    # ECONOMY: простое чтение расписания напрямую
    # из Google Calendar, без OpenAI Responses API.
    direct_schedule_date = (
        simple_schedule_read_target_date(
            text
        )
    )

    if (
        direct_schedule_date is not None
        and not is_planning_request(text)
        and not calendar_changes_allowed_for_text(text)
    ):
        try:
            events = await asyncio.to_thread(
                get_day_overview,
                direct_schedule_date,
            )

            reply_text = (
                format_verified_schedule(
                    direct_schedule_date,
                    events,
                )
            )

            # Если день пустой, говорим об этом явно.
            if not events:
                reply_text += (
                    "\n\nСобытий на этот день нет."
                )

            await asyncio.to_thread(
                save_message,
                "user",
                text,
                "general",
            )

            await asyncio.to_thread(
                save_message,
                "assistant",
                reply_text,
                "general",
            )

            await message.answer(
                reply_text
            )

            logging.info(
                "Economy direct calendar read: %s",
                direct_schedule_date.isoformat(),
            )

            return reply_text

        except Exception:
            # Если прямое чтение Google Calendar
            # неожиданно сломалось — обычный AI-маршрут
            # остаётся резервным.
            logging.exception(
                "Direct calendar read failed; "
                "falling back to AI"
            )

    # Новая команда делает старое неподтверждённое
    # изменение недействительным. Исключение — ответ
    # на запрос недостающих данных текущей записи.
    calendar_action_followup = (
        is_calendar_action_followup(
            text
        )
    )

    new_tattoo_booking = (
        is_new_tattoo_booking_request(
            text
        )
    )

    last_booking_reply = await asyncio.to_thread(
        get_last_message,
        "booking",
        "assistant",
    )

    booking_followup = (
        not new_tattoo_booking
        and bool(last_booking_reply)
        and await asyncio.to_thread(
            has_booking_context
        )
        and (
            calendar_action_followup
            or is_booking_details_followup(
                last_booking_reply
            )
        )
    )

    if new_tattoo_booking:
        # Каждая новая запись получает чистый,
        # изолированный клиентский контекст.
        await asyncio.to_thread(
            clear_booking_context
        )

    if has_pending_client_message():
        clear_pending_client_message()

    if (
        (
            has_pending_changes()
            or get_saved_plan_proposal()
        )
        and not booking_followup
    ):
        clear_pending_changes()
        clear_saved_plan_proposal()

    planning_request = (
        is_planning_request(
            text
        )
    )

    if planning_request:
        clear_saved_plan_proposal()

    allow_calendar_changes = (
        calendar_changes_allowed_for_text(
            text
        )
        or calendar_action_followup
        or booking_followup
    )

    if new_tattoo_booking:
        conversation_scope = "booking"
        conversation_context = ""
    elif booking_followup:
        conversation_scope = "booking"
        booking_context = await asyncio.to_thread(
            get_booking_context,
            20,
        )
        conversation_context = (
            "Контекст только текущей клиентской записи:\n"
            + booking_context
        )
    else:
        conversation_scope = "general"
        conversation_context = await asyncio.to_thread(
            get_recent_context,
            8,
            "general",
        )

    await asyncio.to_thread(
        save_message,
        "user",
        text,
        conversation_scope,
    )

    try:
        answer = await asyncio.wait_for(
            ask_assistant(
                text,
                context=conversation_context,
                allow_calendar_changes=(
                    allow_calendar_changes
                ),
                require_plan_proposal=(
                    planning_request
                ),
            ),
            timeout=90,
        )

    except Exception:
        logging.exception(
            "Ошибка OpenAI Assistant"
        )

        reply_text = (
            "Не удалось обработать команду. "
            "Ассистент продолжает работать."
        )

        await message.answer(
            reply_text
        )

        return reply_text

    await asyncio.to_thread(
        save_message,
        "assistant",
        answer,
        conversation_scope,
    )

    if has_pending_client_message():
        await message.answer(
            answer,
            reply_markup=client_message_confirm_kb(),
        )
    elif (
        allow_calendar_changes
        and has_pending_changes()
    ):
        await message.answer(
            answer,
            reply_markup=calendar_confirm_kb(),
        )
    else:
        await message.answer(
            answer
        )

    return answer


@dp.message(F.voice)
async def private_voice(
    message: Message,
):
    if not is_owner(message):
        return

    try:
        file_info = await message.bot.get_file(
            message.voice.file_id
        )

        buffer = await message.bot.download_file(
            file_info.file_path
        )

        audio_bytes = buffer.read()

        transcript = await asyncio.wait_for(
            transcribe_voice(
                audio_bytes
            ),
            timeout=45,
        )

    except Exception:
        logging.exception(
            "Ошибка распознавания voice"
        )

        await message.answer(
            "Не удалось распознать голосовое сообщение."
        )
        return

    if not transcript:
        await message.answer(
            "Не удалось разобрать голосовую команду."
        )
        return

    await message.answer(
        "🎙 Распознал:\n"
        + transcript
    )

    reply_text = await process_owner_text(
        message,
        transcript,
    )

    if reply_text:
        try:
            voice_bytes = await asyncio.wait_for(
                synthesize_voice(
                    reply_text
                ),
                timeout=60,
            )

            await message.answer_voice(
                voice=BufferedInputFile(
                    voice_bytes,
                    filename="assistant_reply.ogg",
                )
            )

        except Exception:
            # Ошибка TTS не должна ломать
            # основной текстовый ответ ассистента.
            logging.exception(
                "Ошибка голосового ответа TTS"
            )


@dp.message(F.photo)
async def private_photo(
    message: Message,
):
    if not is_owner(message):
        return

    photo = message.photo[-1]

    if photo.file_size and photo.file_size > 12 * 1024 * 1024:
        await message.answer(
            "Изображение слишком большое. Пришли файл до 12 МБ."
        )
        return

    try:
        file_info = await message.bot.get_file(
            photo.file_id
        )
        buffer = await message.bot.download_file(
            file_info.file_path
        )
        image_bytes = buffer.read()
    except Exception:
        logging.exception("Не удалось скачать изображение")
        await message.answer(
            "Не удалось получить изображение. Попробуй отправить ещё раз."
        )
        return

    prompt = (message.caption or "").strip()
    if not prompt:
        prompt = (
            "Проанализируй это изображение и расскажи, "
            "что на нём важно."
        )

    context = await asyncio.to_thread(
        get_recent_context,
        8,
        "general",
    )
    memory_text = f"[Изображение] {prompt}"
    await asyncio.to_thread(
        save_message,
        "user",
        memory_text,
        "general",
    )

    try:
        answer = await asyncio.wait_for(
            ask_assistant(
                prompt,
                context=context,
                allow_calendar_changes=False,
                image_bytes=image_bytes,
                image_mime_type="image/jpeg",
            ),
            timeout=120,
        )
    except Exception:
        logging.exception("Ошибка анализа изображения")
        await message.answer(
            "Не удалось проанализировать изображение. "
            "Clippy продолжает работать."
        )
        return

    await asyncio.to_thread(
        save_message,
        "assistant",
        answer,
        "general",
    )
    await message.answer(answer)



# === CLIPPY DOCUMENT HANDLER ===

DOCUMENT_DIR = Path(
    "data/uploads/documents"
)

DOCUMENT_MAX_BYTES = 20 * 1024 * 1024
DOCUMENT_TEXT_MAX_CHARS = 60000

DOCUMENT_TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".sql",
}

DOCUMENT_SQLITE_EXTENSIONS = {
    ".sqlite",
    ".sqlite3",
    ".db",
}

DOCUMENT_SUPPORTED_EXTENSIONS = (
    DOCUMENT_TEXT_EXTENSIONS
    | DOCUMENT_SQLITE_EXTENSIONS
    | {".zip"}
)


def _safe_document_filename(filename: str) -> str:
    filename = Path(filename).name.strip()

    if not filename:
        filename = "document"

    filename = re.sub(
        r"[^0-9A-Za-zА-Яа-яЁё._()\- ]+",
        "_",
        filename,
    )

    return filename[:180]


def _decode_document_text(data: bytes) -> str:
    for encoding in (
        "utf-8-sig",
        "utf-8",
        "cp1251",
    ):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass

    return data.decode(
        "utf-8",
        errors="replace",
    )


def _sqlite_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sqlite_preview(path: Path) -> str:
    uri = (
        "file:"
        + path.resolve().as_posix()
        + "?mode=ro&immutable=1"
    )

    conn = sqlite3.connect(
        uri,
        uri=True,
        timeout=5,
    )

    try:
        tables = conn.execute(
            """
            SELECT name, sql
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            LIMIT 50
            """
        ).fetchall()

        out = [
            f"SQLite файл: {path.name}",
            f"Таблиц: {len(tables)}",
            "",
        ]

        for table_name, create_sql in tables:
            ident = _sqlite_identifier(
                table_name
            )

            out.append(
                f"=== TABLE: {table_name} ==="
            )

            columns = conn.execute(
                f"PRAGMA table_info({ident})"
            ).fetchall()

            if columns:
                out.append(
                    "Колонки: "
                    + ", ".join(
                        f"{row[1]}"
                        + (
                            f" ({row[2]})"
                            if row[2]
                            else ""
                        )
                        for row in columns
                    )
                )

            if create_sql:
                out.append(
                    "Schema: "
                    + str(create_sql)
                )

            try:
                rows = conn.execute(
                    f"SELECT * FROM {ident} LIMIT 3"
                ).fetchall()

                if rows:
                    out.append(
                        "Первые строки:"
                    )

                    for row in rows:
                        rendered = json.dumps(
                            list(row),
                            ensure_ascii=False,
                            default=str,
                        )

                        if len(rendered) > 1500:
                            rendered = (
                                rendered[:1500]
                                + "…"
                            )

                        out.append(rendered)

                else:
                    out.append(
                        "Таблица пустая."
                    )

            except Exception as exc:
                out.append(
                    "Не удалось прочитать строки: "
                    + str(exc)
                )

            out.append("")

            if sum(map(len, out)) > 50000:
                out.append(
                    "[Вывод SQLite сокращён]"
                )
                break

        return "\n".join(out)

    finally:
        conn.close()



def _process_zip_document(
    data: bytes,
    saved_zip_path: Path,
) -> dict:
    result = {
        "files": [],
        "creative_install": None,
    }

    total_size = 0

    with zipfile.ZipFile(
        io.BytesIO(data),
        "r",
    ) as archive:

        members = [
            item
            for item in archive.infolist()
            if not item.is_dir()
        ]

        if len(members) > 100:
            raise ValueError(
                "В ZIP больше 100 файлов"
            )

        extract_dir = (
            saved_zip_path.parent
            / (
                saved_zip_path.stem
                + "_contents"
            )
        )

        extract_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        for item in members:
            total_size += int(
                item.file_size or 0
            )

            if total_size > 50 * 1024 * 1024:
                raise ValueError(
                    "ZIP больше 50 МБ после распаковки"
                )

            raw_name = item.filename or ""

            if (
                raw_name.startswith("/")
                or ".." in Path(raw_name).parts
            ):
                result["files"].append({
                    "name": raw_name,
                    "status": "unsafe",
                })
                continue

            filename = Path(
                raw_name
            ).name

            if not filename:
                continue

            safe_name = _safe_document_filename(
                filename
            )

            extension = Path(
                safe_name
            ).suffix.lower()

            if extension not in (
                DOCUMENT_TEXT_EXTENSIONS
                | DOCUMENT_SQLITE_EXTENSIONS
            ):
                result["files"].append({
                    "name": safe_name,
                    "status": "unsupported",
                })
                continue

            file_data = archive.read(
                item
            )

            target = (
                extract_dir
                / safe_name
            )

            if target.exists():
                stem = target.stem
                suffix = target.suffix
                index = 2

                while target.exists():
                    target = (
                        extract_dir
                        / f"{stem}_{index}{suffix}"
                    )
                    index += 1

            target.write_bytes(
                file_data
            )

            row = {
                "name": safe_name,
                "status": "saved",
                "size": len(file_data),
            }

            if (
                extension
                in DOCUMENT_SQLITE_EXTENSIONS
                and is_creative_database_filename(
                    safe_name
                )
            ):
                install_result = (
                    install_creative_database(
                        target
                    )
                )

                result[
                    "creative_install"
                ] = install_result

                row[
                    "creative_database"
                ] = True

            result["files"].append(
                row
            )

    return result


@dp.message(F.document)
async def private_document(
    message: Message,
):
    if not is_owner(message):
        return

    document = message.document

    filename = (
        document.file_name
        or f"document_{document.file_unique_id}"
    )

    safe_filename = _safe_document_filename(
        filename
    )

    extension = Path(
        safe_filename
    ).suffix.lower()

    if extension not in DOCUMENT_SUPPORTED_EXTENSIONS:
        await message.answer(
            "Этот тип файла пока не поддерживается.\n"
            "Можно прислать: TXT, MD, CSV, JSON, SQL, "
            "SQLite, SQLite3, DB или ZIP."
        )
        return

    if (
        document.file_size
        and document.file_size
        > DOCUMENT_MAX_BYTES
    ):
        await message.answer(
            "Файл слишком большой. Максимум 20 МБ."
        )
        return

    try:
        file_info = await message.bot.get_file(
            document.file_id
        )

        buffer = await message.bot.download_file(
            file_info.file_path
        )

        data = buffer.read()

        if len(data) > DOCUMENT_MAX_BYTES:
            await message.answer(
                "Файл слишком большой. Максимум 20 МБ."
            )
            return

        DOCUMENT_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

        timestamp = datetime.now(
            MOSCOW_TZ
        ).strftime("%Y%m%d_%H%M%S")

        saved_path = (
            DOCUMENT_DIR
            / f"{timestamp}_{safe_filename}"
        )

        saved_path.write_bytes(data)

        if extension == ".zip":

            if await asyncio.to_thread(
                is_chatgpt_export_zip,
                saved_path,
            ):
                try:
                    archive_result = await asyncio.to_thread(
                        import_chatgpt_export_zip,
                        saved_path,
                    )

                    if archive_result.get("ok"):
                        logging.info(
                            "CHATGPT_ARCHIVE_IMPORTED conversations=%s messages=%s",
                            archive_result.get(
                                "conversation_count",
                                0,
                            ),
                            archive_result.get(
                                "message_count",
                                0,
                            ),
                        )

                        await message.answer(
                            "🧠 Архив ChatGPT импортирован. "
                            f"Чатов: "
                            f"{archive_result.get('conversation_count', 0)}. "
                            f"Сообщений: "
                            f"{archive_result.get('message_count', 0)}."
                        )

                    else:
                        await message.answer(
                            "ZIP похож на экспорт ChatGPT, "
                            "но импорт завершился ошибкой: "
                            + str(
                                archive_result.get(
                                    "error",
                                    "неизвестная ошибка",
                                )
                            )
                        )

                except Exception:
                    logging.exception(
                        "Ошибка импорта архива ChatGPT"
                    )

                    await message.answer(
                        "Не удалось импортировать архив ChatGPT."
                    )

                return

            try:
                zip_result = await asyncio.to_thread(
                    _process_zip_document,
                    data,
                    saved_path,
                )

                files = zip_result.get(
                    "files",
                    [],
                )

                saved_count = sum(
                    1
                    for item in files
                    if item.get("status") == "saved"
                )

                creative_install = (
                    zip_result.get(
                        "creative_install"
                    )
                )

                if (
                    creative_install
                    and creative_install.get("ok")
                ):
                    await message.answer(
                        "🧠 ZIP разобран. "
                        "Творческая база Clippy установлена. "
                        f"Проектов: "
                        f"{creative_install.get('project_count', 0)}. "
                        f"Файлов сохранено: {saved_count}."
                    )
                else:
                    await message.answer(
                        "📦 ZIP разобран. "
                        f"Файлов сохранено: {saved_count}. "
                        "Творческая SQLite-база внутри не найдена."
                    )

                logging.info(
                    "ZIP_PACKAGE_PROCESSED files=%s",
                    saved_count,
                )

            except Exception:
                logging.exception(
                    "Ошибка обработки ZIP"
                )

                await message.answer(
                    "ZIP получен, но не удалось "
                    "безопасно разобрать архив."
                )

            return

        if (
            extension in DOCUMENT_SQLITE_EXTENSIONS
            and is_creative_database_filename(
                safe_filename
            )
        ):
            install_result = await asyncio.to_thread(
                install_creative_database,
                saved_path,
            )

            if not install_result.get("ok"):
                await message.answer(
                    "Файл сохранён, но как творческую базу "
                    "установить его не удалось: "
                    + str(
                        install_result.get(
                            "error",
                            "неизвестная ошибка",
                        )
                    )
                )
                return

            logging.info(
                "CREATIVE_DATABASE_INSTALLED projects=%s",
                install_result.get(
                    "project_count",
                    0,
                ),
            )

            await message.answer(
                "🧠 Творческая база Clippy установлена. "
                f"Проектов: "
                f"{install_result.get('project_count', 0)}."
            )

    except Exception:
        logging.exception(
            "Не удалось сохранить документ"
        )

        await message.answer(
            "Не удалось получить файл."
        )
        return

    await message.answer(
        "📎 Получил и сохранил: "
        + safe_filename
    )

    try:
        if extension in DOCUMENT_SQLITE_EXTENSIONS:
            file_content = await asyncio.to_thread(
                _sqlite_preview,
                saved_path,
            )

            content_description = (
                "Безопасный read-only просмотр "
                "структуры SQLite и первых строк."
            )

        else:
            file_content = await asyncio.to_thread(
                _decode_document_text,
                data,
            )

            original_length = len(
                file_content
            )

            if original_length > DOCUMENT_TEXT_MAX_CHARS:
                file_content = (
                    file_content[
                        :DOCUMENT_TEXT_MAX_CHARS
                    ]
                    + "\n\n"
                    "[Файл сокращён для анализа. "
                    f"Исходный размер: "
                    f"{original_length} символов.]"
                )

            content_description = (
                "Текстовое содержимое файла."
            )

        caption = (
            message.caption
            or ""
        ).strip()

        user_request = (
            caption
            if caption
            else (
                "Проанализируй этот файл. "
                "Объясни, что в нём находится "
                "и что здесь важно."
            )
        )

        prompt = (
            f"Пользователь прислал файл "
            f"`{safe_filename}`.\n\n"
            f"Запрос пользователя:\n"
            f"{user_request}\n\n"
            f"{content_description}\n\n"
            "=== FILE CONTENT ===\n"
            f"{file_content}"
        )

        context = await asyncio.to_thread(
            get_recent_context,
            8,
            "general",
        )

        await asyncio.to_thread(
            save_message,
            "user",
            (
                f"[Файл: {safe_filename}] "
                + (
                    caption
                    if caption
                    else "без подписи"
                )
            ),
            "general",
        )

        answer = await asyncio.wait_for(
            ask_assistant(
                prompt,
                context=context,
                allow_calendar_changes=False,
            ),
            timeout=120,
        )

        await asyncio.to_thread(
            save_message,
            "assistant",
            answer,
            "general",
        )

        await message.answer(
            answer
        )

    except Exception:
        logging.exception(
            "Ошибка анализа документа"
        )

        await message.answer(
            "Файл сохранён, но сейчас "
            "не удалось его проанализировать."
        )



def _is_test_morning_request(
    text: str,
) -> bool:

    normalized = (
        str(text or "")
        .strip()
        .casefold()
        .replace("ё", "е")
    )

    if normalized.startswith((
        "/test_morning",
        "/testmorning",
    )):
        return True

    has_morning = (
        "утрен" in normalized
    )

    has_message = (
        "сообщ" in normalized
        or "бриф" in normalized
        or "план" in normalized
    )

    has_action = any(
        token in normalized
        for token in (
            "покажи",
            "показать",
            "тест",
            "превью",
            "как будет",
            "как выглядит",
        )
    )

    return bool(
        has_morning
        and has_message
        and has_action
    )


def _test_morning_target_date(
    text: str,
    now: datetime,
) -> date:

    normalized = (
        str(text or "")
        .strip()
        .casefold()
        .replace("ё", "е")
    )

    if (
        "завтра" in normalized
        or " tomorrow" in (
            " " + normalized
        )
    ):
        return (
            now.date()
            + timedelta(days=1)
        )

    # Тест без даты проверяет сегодняшнее утро.
    return now.date()


def _test_morning_datetime(
    target_date: date,
    events: list[dict],
) -> datetime:
    """
    Используем штатное время morning brief,
    насколько это возможно.

    Если определить его не удалось —
    безопасный fallback 10:00.
    """

    try:
        value = _morning_time(
            events,
            target_date,
        )

        if isinstance(
            value,
            datetime,
        ):
            if value.tzinfo is None:
                return value.replace(
                    tzinfo=MOSCOW_TZ
                )

            return value.astimezone(
                MOSCOW_TZ
            )

        if isinstance(
            value,
            time,
        ):
            return datetime.combine(
                target_date,
                value,
                tzinfo=MOSCOW_TZ,
            )

        if isinstance(
            value,
            str,
        ):
            parsed_time = (
                time.fromisoformat(
                    value
                )
            )

            return datetime.combine(
                target_date,
                parsed_time,
                tzinfo=MOSCOW_TZ,
            )

    except Exception:
        logging.exception(
            "Test morning time resolve failed"
        )

    return datetime.combine(
        target_date,
        time(
            hour=10,
            minute=0,
        ),
        tzinfo=MOSCOW_TZ,
    )


def build_test_morning_payload(
    target_date: date,
) -> tuple[str, list[dict]]:

    events = get_day_overview(
        target_date
    )

    preview_now = (
        _test_morning_datetime(
            target_date,
            events,
        )
    )

    weather = get_weather_summary(
        target_date
    )

    daylight = get_daylight_summary(
        target_date
    )

    moon = get_moon_phase_summary(
        target_date
    )

    text = build_morning_plan(
        preview_now,
        events,
        weather=weather,
        daylight=daylight,
        moon=moon,
    budget=get_openai_budget_summary(),
    )

    keyboard_events = (
        _morning_display_events(
            events,
            target_date,
        )
    )

    return (
        text,
        keyboard_events,
    )


async def _send_test_morning(
    message: Message,
    request_text: str,
) -> None:
    """
    Ручной тест настоящего morning brief.

    ВАЖНО:
    - morning_sent не меняем;
    - save_message не вызываем;
    - voice notification не вызываем.
    """

    now = datetime.now(
        MOSCOW_TZ
    )

    target_date = (
        _test_morning_target_date(
            request_text,
            now,
        )
    )

    text, keyboard_events = (
        await asyncio.to_thread(
            build_test_morning_payload,
            target_date,
        )
    )

    proposal = (
        await asyncio.to_thread(
            _build_morning_project_proposal,
            target_date,
            keyboard_events,
        )
    )

    keyboard = (
        morning_approval_keyboard(
            target_date
        )
    )

    if proposal.get(
        "items"
    ):
        text += (
            "\n\n🧭 Следующие шаги по проектам"
            "\nClippy подобрала конкретные действия и свободное время."
            "\nОтметь то, что хочешь добавить в план."
            "\n📅 При необходимости выбери другой день."
            "\nДо «Утвердить выбранное» ничего не создаётся."
        )

    await message.answer(
        text,
        reply_markup=keyboard,
    )


@dp.message()
async def private_message(
    message: Message,
):
    if not is_owner(message):
        return

    if not message.text:
        return

    if _is_test_morning_request(
        message.text
    ):
        await _send_test_morning(
            message,
            message.text,
        )
        return

    await process_owner_text(
        message,
        message.text,
    )



async def client_message_delivery_loop() -> None:
    while True:
        try:
            result = await asyncio.to_thread(
                send_due_client_messages
            )
            if result.get("failed"):
                logging.warning(
                    "Client message delivery has %s failed attempt(s)",
                    result["failed"],
                )
        except Exception:
            logging.exception("Client message delivery loop failed")
        await asyncio.sleep(30)


async def main():
    logging.basicConfig(level=logging.INFO)

    bot = Bot(token=TOKEN)

    logging.info(
        "Clippy Assistant starting for owner %s",
        OWNER_ID,
    )

    brief_task = asyncio.create_task(
        evening_brief_loop(bot)
    )
    routine_task = asyncio.create_task(
        daily_routine_loop(bot)
    )
    client_delivery_task = asyncio.create_task(
        client_message_delivery_loop()
    )
    project_sync_task = asyncio.create_task(
        nightly_project_sync_loop()
    )

    try:
        await dp.start_polling(bot)
    finally:
        tasks = (
            brief_task,
            routine_task,
            client_delivery_task,
            project_sync_task,
        )
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task


if __name__ == "__main__":
    asyncio.run(main())
