import base64
import json
import logging
from clippy_task_classifier import classify_task
import os

from datetime import date, datetime
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI

from memory_store import (
    delete_fact,
    get_facts,
    save_fact,
)

from chatgpt_archive import (
    get_archive_conversation,
    get_archive_status,
    search_archive,
)

from creative_knowledge import (
    get_creative_project,
    get_creative_status,
    search_creative_projects,
)

from calendar_tools import (
    apply_pending_changes,
    get_day_schedule,
    get_managed_calendars,
    prepare_calendar_changes,
    prepare_managed_event_create,
    prepare_personal_event_create,
    prepare_tattoo_session_create,
    prepare_tattoo_session_changes,
    get_saved_plan_proposal,
    save_plan_proposal,
    prepare_saved_plan_for_confirmation,
    prepare_linked_task_reschedule,
    find_linked_task_slot,
)
from google_tasks_tools import (
    get_google_tasks_for_date,
    get_google_task,
)

from bot_tools import (
    get_client_bot_status,
    get_lead_stats,
    get_recent_completed_leads,
    get_recent_client_bot_errors,
    prepare_client_message,
)


MODEL = os.environ.get(
    "OPENAI_ASSISTANT_MODEL",
    "gpt-5.6-luna",
).strip()

REASONING_EFFORT = os.environ.get(
    "OPENAI_REASONING_EFFORT",
    "low",
).strip().lower()

if REASONING_EFFORT not in {
    "none",
    "minimal",
    "low",
    "medium",
    "high",
}:
    REASONING_EFFORT = "low"

try:
    MAX_OUTPUT_TOKENS = int(os.environ.get(
        "OPENAI_MAX_OUTPUT_TOKENS",
        "1200",
    ))
except ValueError:
    MAX_OUTPUT_TOKENS = 1200
MAX_OUTPUT_TOKENS = min(max(MAX_OUTPUT_TOKENS, 400), 3000)

TZ = ZoneInfo(
    "Europe/Moscow"
)


INSTRUCTIONS = """
You are Clippy, a personal productivity assistant.

Primary responsibilities:
- help the user plan days and weeks;
- work with calendar events and tasks;
- prioritize projects and next actions;
- provide concise general-purpose assistance;
- use connected tools only when they are required.

Safety and reliability:
- never claim an external action succeeded unless a tool confirms it;
- never expose credentials, tokens or private user data;
- do not execute arbitrary shell commands;
- distinguish facts from assumptions;
- ask for clarification only when it materially affects the result.

Communication:
- respond clearly and concisely;
- use the user's language when possible;
- accuracy takes priority over personality.
"""



OPENAI_COST_STATUS_FILE = (
    "data/openai_cost_status.json"
)


def get_openai_cost_status() -> dict:
    """
    Read the local sanitized OpenAI cost snapshot.

    This function never reads the OpenAI Admin key
    and never calls the OpenAI Costs API directly.
    """

    try:
        with open(
            OPENAI_COST_STATUS_FILE,
            "r",
            encoding="utf-8",
        ) as handle:
            data = json.load(handle)

    except FileNotFoundError:
        return {
            "ok": False,
            "error": "cost_status_file_missing",
        }

    except Exception as exc:
        return {
            "ok": False,
            "error": "cost_status_read_failed",
            "error_type": type(exc).__name__,
        }

    if not isinstance(data, dict):
        return {
            "ok": False,
            "error": "invalid_cost_status",
        }

    # Whitelist only fields that are safe for Clippy.
    safe_fields = (
        "ok",
        "baseline_balance",
        "baseline_balance_usd",
        "baseline_at",
        "spend_since_baseline",
        "spend_since_baseline_usd",
        "estimated_balance",
        "estimated_balance_usd",
        "checked_at",
        "cost_buckets",
        "latest_bucket_end",
        "error",
    )

    result = {
        key: data.get(key)
        for key in safe_fields
        if key in data
    }

    result["source"] = "local_safe_snapshot"

    if not result:
        return {
            "ok": False,
            "error": "unknown_cost_status_schema",
            "source": "local_safe_snapshot",
        }

    return result


TOOLS = [
    {
        "type": "function",
        "name": "openai_get_cost_status",
        "description": (
            "Получить безопасный локальный снимок расходов "
            "OpenAI API и расчётного остатка. "
            "Инструмент не имеет доступа к Admin API key."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "strict": True,
    },

    {
        "type": "web_search",
    },

    {
        "type": "function",
        "name": "calendar_get_day",
        "description": (
            "Получить реальные события из Google Calendar "
            "на конкретный день. Возвращает также calendar_id "
            "и event_id для последующей подготовки изменений."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target_date": {
                    "type": "string",
                    "description": (
                        "Дата YYYY-MM-DD "
                        "в Europe/Moscow."
                    ),
                },
            },
            "required": [
                "target_date"
            ],
            "additionalProperties": False,
        },
        "strict": True,
    },

    {
        "type": "function",
        "name": "calendar_get_managed_groups",
        "description": (
            "Получить четыре разрешённые группы календаря и их реальные "
            "calendar_id для последующих подтверждаемых действий."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "strict": True,
    },

    {
        "type": "function",
        "name": "calendar_prepare_changes",
        "description": (
            "Подготовить перенос, удаление или редактирование существующих "
            "событий в Личном, Татуировках, Проектах или OZON. "
            "Личный, Татуировки и Проекты применяются сразу. "
            "OZON и смешанный пакет с OZON требуют подтверждения."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "actions": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": [
                                    "update",
                                    "delete",
                                ],
                            },
                            "calendar_id": {
                                "type": "string",
                            },
                            "event_id": {
                                "type": "string",
                            },
                            "new_start": {
                                "type": [
                                    "string",
                                    "null",
                                ],
                                "description": (
                                    "Для update: "
                                    "ISO datetime. "
                                    "Для delete: null."
                                ),
                            },
                            "new_end": {
                                "type": [
                                    "string",
                                    "null",
                                ],
                                "description": (
                                    "Для update: "
                                    "ISO datetime. "
                                    "Для delete: null."
                                ),
                            },
                            "new_title": {
                                "type": ["string", "null"],
                            },
                            "new_description": {
                                "type": ["string", "null"],
                            },
                            "new_location": {
                                "type": ["string", "null"],
                            },
                        },
                        "required": [
                            "type",
                            "calendar_id",
                            "event_id",
                            "new_start",
                            "new_end",
                            "new_title",
                            "new_description",
                            "new_location",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": [
                "actions"
            ],
            "additionalProperties": False,
        },
        "strict": True,
    },    {
        "type": "function",
        "name": "calendar_prepare_create_personal",
        "description": (
            "Создать новый личный объект. "
            "Если это работа/задача типа сайт, эскизы, картина, "
            "Clippy, Telegram, визитки, стикеры, тренировка и т.п., "
            "backend создаёт настоящий Google Task + linked "
            "Calendar time-block. "
            "Fixed/anchor события вроде встречи остаются обычными "
            "Calendar events. Не использовать для клиентских "
            "тату-записей. Явная команда применяется сразу."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                },
                "start": {
                    "type": "string",
                    "description": (
                        "Начало в ISO datetime по Europe/Moscow."
                    ),
                },
                "end": {
                    "type": "string",
                    "description": (
                        "Конец в ISO datetime по Europe/Moscow."
                    ),
                },
                "description": {
                    "type": ["string", "null"],
                },
                "allow_ozon_overlap": {
                    "type": "boolean",
                },
            },
            "required": [
                "title",
                "start",
                "end",
                "description",
                "allow_ozon_overlap",
            ],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "calendar_prepare_create_managed",
        "description": (
            "Подготовить создание обычного события в одной из четырёх "
            "управляемых групп. Личный, Татуировки и Проекты применяются "
            "сразу; OZON требует подтверждения."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "calendar_id": {
                    "type": "string",
                },
                "title": {
                    "type": "string",
                },
                "start": {
                    "type": "string",
                },
                "end": {
                    "type": "string",
                },
                "description": {
                    "type": ["string", "null"],
                },
                "location": {
                    "type": ["string", "null"],
                },
                "allow_ozon_overlap": {
                    "type": "boolean",
                },
            },
            "required": [
                "calendar_id",
                "title",
                "start",
                "end",
                "description",
                "location",
                "allow_ozon_overlap",
            ],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "calendar_prepare_create_tattoo",
        "description": (
            "Подготовить запись клиента на тату-сеанс "
            "в календарь «Татуировка и клиенты». "
            "Перед записью проверяются конфликты. "
            "По явной команде событие применяется сразу."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "client_name": {
                    "type": "string",
                },
                "start": {
                    "type": "string",
                    "description": (
                        "Начало сеанса ISO datetime "
                        "по Europe/Moscow."
                    ),
                },
                "end": {
                    "type": "string",
                    "description": (
                        "Окончание сеанса ISO datetime "
                        "по Europe/Moscow."
                    ),
                },
                "city": {
                    "type": "string",
                },
                "project_note": {
                    "type": [
                        "string",
                        "null"
                    ],
                },
                "price": {
                    "type": "string",
                    "description": (
                        "Обязательная стоимость сеанса. "
                        "Например: 20000 ₽, 100000 ₽ за сеанс."
                    ),
                },
            },
            "required": [
                "client_name",
                "start",
                "end",
                "city",
                "project_note",
                "price",
            ],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "calendar_prepare_tattoo_changes",
        "description": (
            "Подготовить перенос или удаление существующего "
            "тату-сеанса и сразу применить явную команду."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "actions": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": [
                                    "update_tattoo",
                                    "delete_tattoo",
                                ],
                            },
                            "calendar_id": {
                                "type": "string",
                            },
                            "event_id": {
                                "type": "string",
                            },
                            "new_start": {
                                "type": [
                                    "string",
                                    "null",
                                ],
                            },
                            "new_end": {
                                "type": [
                                    "string",
                                    "null",
                                ],
                            },
                        },
                        "required": [
                            "type",
                            "calendar_id",
                            "event_id",
                            "new_start",
                            "new_end",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": [
                "actions"
            ],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "archive_get_status",
        "description": (
            "Проверить состояние полного архива прошлых разговоров ChatGPT."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "archive_search",
        "description": (
            "Найти прошлые разговоры пользователя с ChatGPT по теме, "
            "проекту, сайту, серверу, боту, вопросу или ключевым словам."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 12,
                },
            },
            "required": [
                "query",
                "limit",
            ],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "archive_get_conversation",
        "description": (
            "Получить сообщения конкретного разговора из архива ChatGPT "
            "после archive_search."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "conversation_id": {
                    "type": "string",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 120,
                },
            },
            "required": [
                "conversation_id",
                "limit",
            ],
            "additionalProperties": False,
        },
        "strict": True,
    },

    {
        "type": "function",
        "name": "creative_get_status",
        "description": (
            "Проверить состояние отдельной творческой базы Clippy."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "creative_search",
        "description": (
            "Найти прошлые творческие проекты, эскизы, картины "
            "и связанные с ними ограничения или правки."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": [
                "query",
                "limit",
            ],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "creative_get_project",
        "description": (
            "Получить полный контекст конкретного творческого проекта "
            "по project_id, названию или найденной ссылке."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "project_ref": {
                    "type": "string",
                },
            },
            "required": [
                "project_ref",
            ],
            "additionalProperties": False,
        },
        "strict": True,
    },

    {
        "type": "function",
        "name": "memory_save_fact",
        "description": (
            "Сохранить устойчивый факт, правило или предпочтение "
            "пользователя в постоянную память."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string"
                },
                "value": {
                    "type": "string"
                },
            },
            "required": [
                "key",
                "value"
            ],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "memory_get_facts",
        "description": (
            "Получить постоянные правила и предпочтения пользователя."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "memory_delete_fact",
        "description": (
            "Удалить сохранённый факт или правило из памяти."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string"
                },
            },
            "required": [
                "key"
            ],
            "additionalProperties": False,
        },
        "strict": True,
    },

    {
        "type": "function",
        "name": "client_bot_get_status",
        "description": (
            "Получить read-only статус фиксированного сервиса "
            "клиентского Telegram-бота."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "client_bot_get_lead_stats",
        "description": (
            "Получить read-only агрегированную статистику "
            "из leads_log.csv клиентского бота."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "client_bot_get_recent_completed",
        "description": (
            "Получить последние завершённые заявки "
            "из leads_log.csv клиентского бота."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": ["limit"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "client_bot_get_recent_errors",
        "description": (
            "Получить последние предупреждения и ошибки "
            "фиксированного сервиса клиентского бота."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "required": ["limit"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "client_bot_prepare_message",
        "description": (
            "Подготовить одно Telegram-сообщение реальному клиенту "
            "из leads_log.csv. Сообщение отправляется или ставится "
            "в очередь только после отдельного подтверждения пользователя."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "recipient": {
                    "type": "string",
                    "description": (
                        "Точное имя, @username или номер заявки клиента."
                    ),
                },
                "message": {
                    "type": "string",
                    "description": "Готовый текст для клиента.",
                },
                "send_at": {
                    "type": ["string", "null"],
                    "description": (
                        "ISO datetime Europe/Moscow для отложенной "
                        "отправки или null для отправки сразу."
                    ),
                },
            },
            "required": [
                "recipient",
                "message",
                "send_at",
            ],
            "additionalProperties": False,
        },
        "strict": True,
    },

    {
        "type": "function",
        "name": "linked_task_find_slot",
        "description": (
            "Найти безопасное свободное время для уже "
            "существующей source=linked_google_task. "
            "Использовать когда пользователь говорит «найди время», "
            "«на вечер», «позже», «найди окно». "
            "Инструмент ничего не изменяет. Он блокирует все "
            "остальные события, включая другие linked/flexible "
            "задачи. Единственное допустимое пересечение — OZON."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                },
                "source_date": {
                    "type": "string",
                    "description": (
                        "Текущая дата задачи YYYY-MM-DD."
                    ),
                },
                "target_date": {
                    "type": "string",
                    "description": (
                        "Дата, на которой искать окно YYYY-MM-DD."
                    ),
                },
                "window_start": {
                    "type": "string",
                    "description": (
                        "Начало допустимого окна HH:MM. "
                        "Для обычного поиска используй 09:00; "
                        "для «на вечер» — 18:00."
                    ),
                },
                "window_end": {
                    "type": "string",
                    "description": (
                        "Конец допустимого окна HH:MM."
                    ),
                },
                "allow_ozon_overlap": {
                    "type": "boolean",
                    "description": (
                        "Для flexible/adjustable задач обычно true."
                    ),
                },
            },
            "required": [
                "title",
                "source_date",
                "target_date",
                "window_start",
                "window_end",
                "allow_ozon_overlap",
            ],
            "additionalProperties": False,
        },
        "strict": True,
    },

    {
        "type": "function",
        "name": "linked_task_prepare_reschedule",
        "description": (
            "Изменить точное время или длительность существующей "
            "связанной задачи Google Task + Calendar time-block. "
            "Используй ТОЛЬКО когда пользователь однозначно указал "
            "новое время/начало/длительность. "
            "Если пользователь просит найти время, перенести на вечер, "
            "позже или в свободное окно — сначала составляй proposal."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                },
                "source_date": {
                    "type": "string",
                    "description": (
                        "Текущая дата задачи YYYY-MM-DD."
                    ),
                },
                "new_start": {
                    "type": ["string", "null"],
                    "description": (
                        "Новое точное начало ISO datetime "
                        "Europe/Moscow или null."
                    ),
                },
                "new_end": {
                    "type": ["string", "null"],
                    "description": (
                        "Новое точное окончание ISO datetime "
                        "или null."
                    ),
                },
                "duration_minutes": {
                    "type": ["integer", "null"],
                    "description": (
                        "Новая длительность или null. "
                        "Если задано только новое начало, "
                        "старая длительность сохраняется."
                    ),
                },
                "allow_ozon_overlap": {
                    "type": "boolean",
                    "description": (
                        "True если гибкая задача должна идти "
                        "параллельно смене OZON."
                    ),
                },
            },
            "required": [
                "title",
                "source_date",
                "new_start",
                "new_end",
                "duration_minutes",
                "allow_ozon_overlap",
            ],
            "additionalProperties": False,
        },
        "strict": True,
    },

    {
        "type": "function",
        "name": "planner_save_proposal",
        "description": (
            "Сохранить предложенный план дня как структурированный "
            "черновик. НЕ изменяет Google Calendar. "
            "ВАЖНО: action create допустим только для задачи, "
            "которую пользователь прямо назвал в текущем сообщении, "
            "либо для реально существующей Google Task. "
            "Нельзя создавать задачи из истории диалога, "
            "технического контекста или собственных идей."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target_date": {
                    "type": "string",
                    "description": "Дата плана YYYY-MM-DD",
                },
                "summary": {
                    "type": "string",
                },
                "actions": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": [
                                    "create",
                                    "update",
                                    "delete",
                                ],
                            },
                            "calendar_kind": {
                                "type": ["string", "null"],
                                "enum": ["personal", "tattoo", None],
                            },
                            "event_id": {
                                "type": [
                                    "string",
                                    "null",
                                ],
                            },
                            "title": {
                                "type": "string",
                            },
                            "start": {
                                "type": ["string", "null"],
                                "description": (
                                    "Начало ISO datetime "
                                    "Europe/Moscow."
                                ),
                            },
                            "end": {
                                "type": ["string", "null"],
                                "description": (
                                    "Конец ISO datetime "
                                    "Europe/Moscow."
                                ),
                            },
                            "description": {
                                "type": [
                                    "string",
                                    "null",
                                ],
                            },
                            "client_name": {
                                "type": ["string", "null"],
                            },
                            "city": {
                                "type": ["string", "null"],
                            },
                            "project_note": {
                                "type": ["string", "null"],
                            },
                            "price": {
                                "type": ["string", "null"],
                            },
                            "allow_ozon_overlap": {
                                "type": "boolean",
                                "description": (
                                    "True только для подтверждённой "
                                    "гибкой личной задачи, которую "
                                    "можно поставить параллельно "
                                    "фиксированной смене OZON. "
                                    "Не разрешает пересечения с "
                                    "другими событиями."
                                ),
                            },
                        },
                        "required": [
                            "type",
                            "calendar_kind",
                            "event_id",
                            "title",
                            "start",
                            "end",
                            "description",
                            "client_name",
                            "city",
                            "project_note",
                            "price",
                            "allow_ozon_overlap",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": [
                "target_date",
                "summary",
                "actions",
            ],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "planner_prepare_saved_plan",
        "description": (
            "Взять последний сохранённый план. План без OZON "
            "применяется сразу по явной команде; план с OZON "
            "подготавливается к подтверждению целиком."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "strict": True,
    },

]


import contextvars


_PLANNING_REQUEST_ACTIVE = contextvars.ContextVar(
    "planning_request_active",
    default=False,
)

_PLANNING_REQUEST_TEXT = contextvars.ContextVar(
    "planning_request_text",
    default="",
)


CALENDAR_MUTATION_TOOL_NAMES = {
    "linked_task_prepare_reschedule",
    "planner_prepare_saved_plan",
    "calendar_prepare_changes",
    "calendar_prepare_create_personal",
    "calendar_prepare_create_managed",
    "calendar_prepare_create_tattoo",
    "calendar_prepare_tattoo_changes",
    "client_bot_prepare_message",
}


def get_available_tools_for_request(
    allow_calendar_changes: bool,
    require_plan_proposal: bool,
):
    """
    Планирование и прямое изменение календаря
    являются разными режимами.

    В plan-proposal mode модель может читать
    календарь и сохранить предложение, но
    физически не получает write-инструменты.
    """

    if require_plan_proposal:
        return [
            tool
            for tool in TOOLS
            if tool.get("name")
            not in CALENDAR_MUTATION_TOOL_NAMES
        ]

    if allow_calendar_changes:
        return TOOLS

    return [
        tool
        for tool in TOOLS
        if tool.get("name")
        not in CALENDAR_MUTATION_TOOL_NAMES
    ]


def _finalize_prepared_calendar_action(
    prepared: dict,
) -> dict:
    if not prepared.get("ok"):
        return prepared

    if prepared.get("requires_confirmation", True):
        return prepared

    applied = apply_pending_changes()

    if not applied.get("ok"):
        return {
            "ok": False,
            "applied_immediately": True,
            "error": applied.get("error", "calendar_apply_failed"),
            "applied": applied.get("applied", []),
            "remaining": applied.get("remaining", 0),
        }

    return {
        "ok": True,
        "applied_immediately": True,
        "requires_confirmation": False,
        "applied": applied.get("applied", []),
    }



def _normalize_planning_source_text(
    value: str,
) -> str:
    value = (
        value
        or ""
    ).casefold()

    return " ".join(
        "".join(
            ch
            if ch.isalnum()
            else " "
            for ch in value
        ).split()
    )


def _planning_source_mentions_title(
    source_text: str,
    title: str,
) -> bool:
    """
    Консервативное сопоставление явно названной задачи
    с текущим сообщением пользователя.

    Учитывает простые русские словоформы:
    картина/картину,
    тренировка/тренировку,
    эскиз/эскизы.

    Не является общим fuzzy matching:
    все значимые слова названия должны присутствовать
    в текущем запросе хотя бы по устойчивому префиксу.
    """

    source_norm = (
        _normalize_planning_source_text(
            source_text
        )
    )

    title_norm = (
        _normalize_planning_source_text(
            title
        )
    )

    if not source_norm or not title_norm:
        return False

    # Точное вхождение остаётся главным вариантом.
    if title_norm in source_norm:
        return True

    source_words = source_norm.split()
    title_words = title_norm.split()

    generic_words = {
        "план",
        "задача",
        "задачи",
        "дело",
        "дела",
        "работа",
        "сегодня",
        "завтра",
    }

    significant_title_words = [
        word
        for word in title_words
        if (
            len(word) >= 4
            and word not in generic_words
        )
    ]

    if not significant_title_words:
        return False

    def words_match(
        title_word: str,
        source_word: str,
    ) -> bool:

        if title_word == source_word:
            return True

        # Длинные русские словоформы обычно сохраняют
        # первые 5 символов основы.
        if (
            len(title_word) >= 5
            and len(source_word) >= 5
            and title_word[:5] == source_word[:5]
        ):
            return True

        return False

    return all(
        any(
            words_match(
                title_word,
                source_word,
            )
            for source_word in source_words
        )
        for title_word
        in significant_title_words
    )


def _task_title_from_item(
    item,
) -> str:
    if isinstance(item, str):
        return item

    if isinstance(item, dict):
        return (
            item.get("title")
            or item.get("name")
            or item.get("summary")
            or ""
        )

    return ""


def _classify_google_task_items(task_items):
    classified_tasks = []

    for source_item in task_items or []:
        item = dict(source_item)
        title = _task_title_from_item(item)

        if not title:
            continue

        classification = classify_task(title)

        item["clippy_type"] = classification.get(
            "type",
            "project",
        )
        item["clippy_tracking"] = classification.get(
            "tracking",
            False,
        )
        item["clippy_plan"] = classification.get(
            "plan",
            True,
        )
        classified_tasks.append(item)

    return classified_tasks


def _validate_planner_create_sources(
    target_date: str,
    actions: list[dict],
) -> dict:
    """
    Серверная защита от выдуманных create.

    Источники create:
    1. текущий запрос пользователя;
    2. реальная Google Task нужной даты.

    Conversation history намеренно не используется.
    """

    if not _PLANNING_REQUEST_ACTIVE.get():
        return {
            "ok": True,
        }

    request_text = (
        _PLANNING_REQUEST_TEXT.get()
        or ""
    )

    request_norm = (
        _normalize_planning_source_text(
            request_text
        )
    )

    try:
        parsed_date = date.fromisoformat(
            target_date
        )
    except Exception:
        return {
            "ok": False,
            "error": "Некорректная дата плана",
        }

    try:
        task_items = (
            get_google_tasks_for_date(
                parsed_date
            )
            or []
        )
    except Exception:
        task_items = []


    task_items = _classify_google_task_items(
        task_items
    )

    google_task_titles = {
        _normalize_planning_source_text(
            _task_title_from_item(item)
        )
        for item in task_items
        if (
            _task_title_from_item(item)
            and item.get("clippy_plan", True)
        )
    }

    forbidden = []

    for action in actions or []:

        if action.get("type") != "create":
            continue

        title = (
            action.get("title")
            or ""
        ).strip()

        title_norm = (
            _normalize_planning_source_text(
                title
            )
        )

        if not title_norm:
            forbidden.append(
                title or "Без названия"
            )
            continue

        # Пользователь прямо назвал задачу
        # именно в текущем сообщении.
        from_current_request = (
            _planning_source_mentions_title(
                request_text,
                title,
            )
        )

        # Либо это реальная Google Task.
        from_google_tasks = (
            title_norm
            in google_task_titles
        )

        if (
            from_current_request
            or from_google_tasks
        ):
            continue

        forbidden.append(title)

    if forbidden:
        return {
            "ok": False,
            "error": (
                "PLAN_CREATE_SOURCE_NOT_CONFIRMED"
            ),
            "message": (
                "План содержит новые задачи, "
                "которые пользователь не называл "
                "в текущем запросе и которых нет "
                "в Google Tasks. Удали эти create "
                "из структурированного плана."
            ),
            "forbidden_titles": forbidden,
        }

    return {
        "ok": True,
    }


def run_tool(
    name,
    arguments,
):
    if name == "openai_get_cost_status":
        return get_openai_cost_status()

    if name == "calendar_get_day":
        try:
            target_date = date.fromisoformat(
                arguments["target_date"]
            )

            events = get_day_schedule(
                target_date
            )

            # Для связанных Calendar blocks
            # состояние настоящего Google Task
            # является источником истины.
            for event in events:

                if (
                    event.get("source")
                    != "linked_google_task"
                ):
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
                    event[
                        "task_status"
                    ] = "unknown"

                    event[
                        "task_completed"
                    ] = None

                    continue

                try:
                    task_state = (
                        get_google_task(
                            task_list_id,
                            task_id,
                        )
                    )

                    status = (
                        task_state.get(
                            "status"
                        )
                        or "unknown"
                    )

                    event[
                        "task_status"
                    ] = status

                    event[
                        "task_completed"
                    ] = (
                        status
                        == "completed"
                    )

                except Exception:
                    logging.exception(
                        "Linked Google Task "
                        "status read failed"
                    )

                    event[
                        "task_status"
                    ] = "unknown"

                    event[
                        "task_completed"
                    ] = None

            try:
                tasks = get_google_tasks_for_date(
                    target_date
                )
            except Exception:
                tasks = []

            tasks = _classify_google_task_items(
                tasks
            )

            return {
                "ok": True,
                "date": (
                    target_date.isoformat()
                ),
                "events": events,
                "tasks": tasks,
            }

        except Exception as exc:
            return {
                "ok": False,
                "error": type(exc).__name__,
            }

    if name == "calendar_get_managed_groups":
        return {
            "ok": True,
            "calendars": get_managed_calendars(),
        }

    if name == "calendar_prepare_changes":
        try:
            return _finalize_prepared_calendar_action(
                prepare_calendar_changes(
                    arguments["actions"]
                )
            )

        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
            }

    if name == "calendar_prepare_create_personal":
        try:
            prepared = prepare_personal_event_create(
                title=arguments["title"],
                start_value=arguments["start"],
                end_value=arguments["end"],
                description=(
                    arguments.get("description")
                    or ""
                ),
                allow_ozon_overlap=bool(
                    arguments.get("allow_ozon_overlap", False)
                ),
            )
            return _finalize_prepared_calendar_action(
                prepared
            )

        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
            }

    if name == "calendar_prepare_create_managed":
        try:
            prepared = prepare_managed_event_create(
                calendar_id=arguments["calendar_id"],
                title=arguments["title"],
                start_value=arguments["start"],
                end_value=arguments["end"],
                description=(
                    arguments.get("description")
                    or ""
                ),
                location=(
                    arguments.get("location")
                    or ""
                ),
                allow_ozon_overlap=bool(
                    arguments.get("allow_ozon_overlap", False)
                ),
            )
            return _finalize_prepared_calendar_action(
                prepared
            )

        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
            }

    if name == "calendar_prepare_create_tattoo":
        try:
            prepared = prepare_tattoo_session_create(
                client_name=arguments[
                    "client_name"
                ],
                start_value=arguments[
                    "start"
                ],
                end_value=arguments[
                    "end"
                ],
                city=(
                    arguments.get("city")
                    or "Санкт-Петербург"
                ),
                project_note=(
                    arguments.get("project_note")
                    or ""
                ),
                price=arguments[
                    "price"
                ],
            )
            return _finalize_prepared_calendar_action(
                prepared
            )

        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
            }

    if name == "calendar_prepare_tattoo_changes":
        try:
            return _finalize_prepared_calendar_action(
                prepare_tattoo_session_changes(
                    arguments["actions"]
                )
            )

        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
            }

    if name == "client_bot_get_status":
        return get_client_bot_status()

    if name == "client_bot_get_lead_stats":
        return get_lead_stats()

    if name == "client_bot_get_recent_completed":
        try:
            return get_recent_completed_leads(
                limit=arguments["limit"]
            )
        except Exception as exc:
            return {
                "ok": False,
                "error": type(exc).__name__,
            }

    if name == "client_bot_get_recent_errors":
        try:
            return get_recent_client_bot_errors(
                limit=arguments["limit"]
            )
        except Exception as exc:
            return {
                "ok": False,
                "error": type(exc).__name__,
            }

    if name == "client_bot_prepare_message":
        try:
            return prepare_client_message(
                recipient=arguments["recipient"],
                message=arguments["message"],
                send_at=arguments.get("send_at"),
            )
        except Exception as exc:
            return {
                "ok": False,
                "error": type(exc).__name__,
            }

    if name == "archive_get_status":
        try:
            return get_archive_status()
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
            }

    if name == "archive_search":
        try:
            return search_archive(
                query=arguments["query"],
                limit=arguments["limit"],
            )
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
            }

    if name == "archive_get_conversation":
        try:
            return get_archive_conversation(
                conversation_id=arguments[
                    "conversation_id"
                ],
                limit=arguments["limit"],
            )
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
            }

    if name == "creative_get_status":
        try:
            return get_creative_status()
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
            }

    if name == "creative_search":
        try:
            return search_creative_projects(
                query=arguments["query"],
                limit=arguments["limit"],
            )
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
            }

    if name == "creative_get_project":
        try:
            return get_creative_project(
                arguments["project_ref"]
            )
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
            }

    if name == "memory_save_fact":
        try:
            save_fact(
                arguments["key"],
                arguments["value"],
            )

            return {
                "ok": True,
                "saved": {
                    "key": arguments["key"],
                    "value": arguments["value"],
                },
            }

        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
            }

    if name == "memory_get_facts":
        try:
            return {
                "ok": True,
                "facts": get_facts(),
            }

        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
            }

    if name == "memory_delete_fact":
        try:
            delete_fact(
                arguments["key"]
            )

            return {
                "ok": True,
                "deleted": arguments["key"],
            }

        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
            }

    if name == "linked_task_find_slot":
        try:
            return find_linked_task_slot(
                title=arguments[
                    "title"
                ],
                source_date=arguments[
                    "source_date"
                ],
                target_date=arguments[
                    "target_date"
                ],
                window_start=arguments[
                    "window_start"
                ],
                window_end=arguments[
                    "window_end"
                ],
                allow_ozon_overlap=bool(
                    arguments.get(
                        "allow_ozon_overlap",
                        True,
                    )
                ),
            )

        except Exception as exc:
            logging.exception(
                "Linked task slot search failed"
            )

            return {
                "ok": False,
                "error": str(exc),
            }


    if name == "linked_task_prepare_reschedule":
        try:
            prepared = (
                prepare_linked_task_reschedule(
                    title=arguments[
                        "title"
                    ],
                    source_date=arguments[
                        "source_date"
                    ],
                    new_start=(
                        arguments.get(
                            "new_start"
                        )
                        or ""
                    ),
                    new_end=(
                        arguments.get(
                            "new_end"
                        )
                        or ""
                    ),
                    duration_minutes=(
                        arguments.get(
                            "duration_minutes"
                        )
                    ),
                    allow_ozon_overlap=bool(
                        arguments.get(
                            "allow_ozon_overlap",
                            False,
                        )
                    ),
                )
            )

            return (
                _finalize_prepared_calendar_action(
                    prepared
                )
            )

        except Exception as exc:
            logging.exception(
                "Linked task reschedule failed"
            )

            return {
                "ok": False,
                "error": str(exc),
            }


    if name == "planner_save_proposal":
        logging.info(
            "PLANNER SAVE CALLED: %s",
            arguments,
        )

        try:
            source_check = (
                _validate_planner_create_sources(
                    target_date=arguments[
                        "target_date"
                    ],
                    actions=arguments[
                        "actions"
                    ],
                )
            )

            if not source_check.get(
                "ok"
            ):
                return source_check

            result = save_plan_proposal(
                target_date=arguments[
                    "target_date"
                ],
                actions=arguments[
                    "actions"
                ],
                summary=arguments.get(
                    "summary",
                    "",
                ),
            )

            if not result.get("ok"):
                logging.warning(
                    "planner_save_proposal rejected: %s",
                    result.get(
                        "error",
                        "unknown_error",
                    ),
                )

            return result

        except Exception as exc:
            logging.exception(
                "planner_save_proposal failed"
            )

            return {
                "ok": False,
                "error": str(exc),
            }

    if name == "planner_prepare_saved_plan":
        try:
            return _finalize_prepared_calendar_action(
                prepare_saved_plan_for_confirmation()
            )

        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
            }

    return {
        "ok": False,
        "error": "Неизвестный инструмент",
    }


async def ask_assistant(
    user_message: str,
    context: str = "",
    allow_calendar_changes: bool = False,
    require_plan_proposal: bool = False,
    image_bytes: bytes | None = None,
    image_mime_type: str = "image/jpeg",
) -> str:

    client = AsyncOpenAI()

    _PLANNING_REQUEST_ACTIVE.set(
        bool(require_plan_proposal)
    )

    _PLANNING_REQUEST_TEXT.set(
        (
            user_message
            or ""
        )
        if require_plan_proposal
        else ""
    )

    available_tools = (
        get_available_tools_for_request(
            allow_calendar_changes=(
                allow_calendar_changes
            ),
            require_plan_proposal=(
                require_plan_proposal
            ),
        )
    )

    now = datetime.now(TZ)

    runtime_context = (
        f"Текущая дата: "
        f"{now.strftime('%Y-%m-%d')}.\n"
        f"Текущее время: "
        f"{now.strftime('%H:%M')}.\n"
        "Часовой пояс: Europe/Moscow."
    )

    try:
        calendar_project_snapshot = get_facts().get(
            "calendar_project_snapshot",
            "",
        )
    except Exception:
        calendar_project_snapshot = ""

    if calendar_project_snapshot:
        runtime_context += (
            "\n\nАКТУАЛЬНЫЙ СНИМОК ПРОЕКТОВ И ТАТУ-СЕАНСОВ "
            "ИЗ GOOGLE CALENDAR:\n"
            + calendar_project_snapshot[:7000]
            + "\nСобытия этого снимка, включая созданные владельцем "
            "вручную, считай истинными. Не создавай их повторно и "
            "не переноси без явной команды владельца."
        )

    if require_plan_proposal:
        runtime_context += (
            "\n\nРЕЖИМ ПЛАН-ПРЕДЛОЖЕНИЕ:\n"
            "Сейчас нельзя создавать, переносить "
            "или удалять события напрямую. "
            "Сначала проверь календарь и сохрани "
            "структурированный вариант через "
            "planner_save_proposal. Реальные "
            "изменения возможны только после "
            "отдельного подтверждения пользователя. "
            "Не создавай новые задачи из истории "
            "разговора, своей памяти, технической "
            "работы над Clippy или собственных идей. "
            "Для action create источник должен быть "
            "только текущий запрос пользователя или "
            "реальная Google Task."
        )

    if (
        context
        and not require_plan_proposal
    ):
        runtime_context += (
            "\n\nДополнительный контекст:\n"
            + context
        )

    elif (
        context
        and require_plan_proposal
    ):
        runtime_context += (
            "\n\nИстория предыдущего диалога "
            "намеренно исключена из режима "
            "планирования. Она НЕ является "
            "источником задач."
        )

    first_text = (
        runtime_context
        + "\n\nСообщение пользователя:\n"
        + user_message
    )

    if image_bytes:
        encoded_image = base64.b64encode(
            image_bytes
        ).decode("ascii")
        first_content = [
            {
                "type": "input_text",
                "text": first_text,
            },
            {
                "type": "input_image",
                "image_url": (
                    f"data:{image_mime_type};base64,"
                    + encoded_image
                ),
            },
        ]
    else:
        first_content = first_text

    input_items = [
        {
            "role": "user",
            "content": first_content,
        }
    ]

    for _ in range(6):

        response = await client.responses.create(
            model=MODEL,
            instructions=INSTRUCTIONS,
            input=input_items,
            tools=available_tools,
            tool_choice="auto",
            parallel_tool_calls=False,
            store=False,
            reasoning={
                "effort": REASONING_EFFORT
            },
            max_output_tokens=MAX_OUTPUT_TOKENS,
        )

        function_calls = [
            item
            for item in response.output
            if item.type == "function_call"
        ]

        if not function_calls:
            final_text = (
                response.output_text.strip()
                or "Готово."
            )

            # forced_plan_save
            # Для команды планирования недостаточно
            # просто красивого текстового ответа.
            # План обязан быть сохранён структурированно.
            if (
                require_plan_proposal
                and not get_saved_plan_proposal()
            ):

                forced_input = list(
                    input_items
                )

                forced_input.extend(
                    item.model_dump(exclude={"status"}, exclude_none=True)
                    for item in response.output
                )

                forced_input.append({
                    "role": "user",
                    "content": (
                        "Сохрани только что предложенный "
                        "план через planner_save_proposal. "
                        "Не меняй расписание. "
                        "Не создавай события Google Calendar. "
                        "Для существующих переносимых или удаляемых "
                        "событий Google Calendar используй только реальные "
                        "event_id, полученные из calendar_get_day. "
                        "Google Tasks не являются событиями Calendar. "
                        "Если source=linked_google_task уже имеет Calendar "
                        "event_id, перенос сохраняй ТОЛЬКО как update этого "
                        "существующего event_id. Не делай create+delete. "
                        "Только отдельную Google Task без связанного "
                        "Calendar block можно сохранить как create с "
                        "calendar_kind=personal и event_id=null. "
                        "Никогда не используй Task ID как Calendar event_id. "
                        "Если существующее календарное событие убрано "
                        "из текстового плана, сохрани delete. "
                        "Не создавай action для новой задачи, которой нет "
                        "в запросе пользователя и нет в Google Tasks. "
                        "Для тату-сеанса используй calendar_kind=tattoo, "
                        "для личных блоков calendar_kind=personal."
                    ),
                })

                planner_tools = [
                    tool
                    for tool in available_tools
                    if tool.get("name")
                    == "planner_save_proposal"
                ]

                if not planner_tools:
                    return (
                        final_text
                        + "\n\n⚠️ Структурированный "
                          "черновик плана не сохранён."
                    )

                forced_response = (
                    await client.responses.create(
                        model=MODEL,
                        instructions=INSTRUCTIONS,
                        input=forced_input,
                        tools=planner_tools,
                        tool_choice={
                            "type": "function",
                            "name": (
                                "planner_save_proposal"
                            ),
                        },
                        parallel_tool_calls=False,
                        store=False,
                        reasoning={
                            "effort": REASONING_EFFORT
                        },
                        max_output_tokens=MAX_OUTPUT_TOKENS,
                    )
                )

                forced_calls = [
                    item
                    for item
                    in forced_response.output
                    if item.type
                    == "function_call"
                ]

                if not forced_calls:
                    return (
                        final_text
                        + "\n\n⚠️ Не удалось "
                          "сохранить черновик плана."
                    )

                call = forced_calls[0]

                try:
                    args = json.loads(
                        call.arguments or "{}"
                    )

                    save_result = run_tool(
                        call.name,
                        args,
                    )

                except Exception as exc:
                    save_result = {
                        "ok": False,
                        "error": str(exc),
                    }

                if not save_result.get("ok"):
                    return (
                        final_text
                        + "\n\n⚠️ План предложен, "
                          "но черновик не сохранён: "
                        + str(
                            save_result.get(
                                "error",
                                "неизвестная ошибка",
                            )
                        )
                    )

            return final_text

        input_items.extend(
            item.model_dump(exclude={"status"}, exclude_none=True)
            for item in response.output
        )

        for call in function_calls:
            try:
                args = json.loads(
                    call.arguments
                    or "{}"
                )

                if (
                    call.name
                    in CALENDAR_MUTATION_TOOL_NAMES
                    and not allow_calendar_changes
                ):
                    result = {
                        "ok": False,
                        "error": (
                            "Изменение календаря "
                            "запрещено в режиме планирования"
                        ),
                    }
                else:
                    result = run_tool(
                        call.name,
                        args,
                    )

            except Exception as exc:
                result = {
                    "ok": False,
                    "error": (
                        type(exc).__name__
                    ),
                }

            input_items.append({
                "type": (
                    "function_call_output"
                ),
                "call_id": call.call_id,
                "output": json.dumps(
                    result,
                    ensure_ascii=False,
                ),
            })

    return (
        "Не удалось завершить запрос "
        "за допустимое число шагов."
    )
