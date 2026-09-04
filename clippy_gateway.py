import asyncio
import hmac
import json
import os
import time
from collections import defaultdict, deque
from datetime import date, datetime
from zoneinfo import ZoneInfo

from aiohttp import web

from calendar_tools import get_day_schedule
from memory_store import get_recent_context, save_message
from project_next_actions import (
    get_project_actions,
    sync_project_actions,
)


TZ = ZoneInfo("Europe/Moscow")

API_KEY = os.environ.get(
    "CLIPPY_GATEWAY_API_KEY",
    "",
).strip()

PROJECT_SYNC_API_KEY = os.environ.get(
    "CLIPPY_PROJECT_SYNC_API_KEY",
    "",
).strip()

PUBLIC_BASE_URL = os.environ.get(
    "CLIPPY_GATEWAY_PUBLIC_URL",
    "http://127.0.0.1:8765",
).rstrip("/")

HOST = "127.0.0.1"
PORT = 8765

MAX_PROMPT_LENGTH = 4000
MAX_REQUEST_BYTES = 32 * 1024
REQUESTS_PER_MINUTE = 20

# Вместо прежних 30 сообщений.
RECENT_CONTEXT_LIMIT = 8

RATE_BUCKETS = defaultdict(deque)


if len(API_KEY) < 32:
    raise RuntimeError(
        "CLIPPY_GATEWAY_API_KEY is not configured safely"
    )

if len(PROJECT_SYNC_API_KEY) < 32:
    raise RuntimeError(
        "CLIPPY_PROJECT_SYNC_API_KEY is not configured safely"
    )


def _authorized(request: web.Request) -> bool:
    header = request.headers.get(
        "Authorization",
        "",
    )

    master_expected = (
        f"Bearer {API_KEY}"
    )

    if hmac.compare_digest(
        header,
        master_expected,
    ):
        return True

    if request.path == (
        "/v1/project-actions/sync"
    ):
        sync_expected = (
            f"Bearer {PROJECT_SYNC_API_KEY}"
        )

        return hmac.compare_digest(
            header,
            sync_expected,
        )

    return False


def _rate_allowed(request: web.Request) -> bool:
    peer = request.remote or "unknown"
    now = time.monotonic()
    bucket = RATE_BUCKETS[peer]

    while bucket and now - bucket[0] > 60:
        bucket.popleft()

    if len(bucket) >= REQUESTS_PER_MINUTE:
        return False

    bucket.append(now)
    return True


@web.middleware
async def security_middleware(
    request,
    handler,
):
    if request.path in {
        "/health",
        "/privacy",
    }:
        return await handler(request)

    if not _authorized(request):
        raise web.HTTPUnauthorized(
            text=json.dumps(
                {"error": "unauthorized"}
            ),
            content_type="application/json",
        )

    if not _rate_allowed(request):
        raise web.HTTPTooManyRequests(
            text=json.dumps(
                {"error": "rate_limit"}
            ),
            content_type="application/json",
        )

    return await handler(request)


def _json_safe(value):
    if isinstance(
        value,
        (datetime, date),
    ):
        return value.isoformat()

    if isinstance(value, dict):
        return {
            str(key): _json_safe(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple)):
        return [
            _json_safe(item)
            for item in value
        ]

    return value


async def health(
    _request: web.Request,
) -> web.Response:
    return web.json_response({
        "ok": True,
        "service": "clippy-gateway",
        "mode": "economy-data-only",
        "openai_api_called": False,
        "time": datetime.now(TZ).isoformat(),
    })


async def privacy(
    _request: web.Request,
) -> web.Response:
    return web.Response(
        text=(
            "Clippy Gateway is a private data bridge "
            "for its owner. It provides shared memory "
            "and read-only calendar data to ChatGPT. "
            "The gateway does not call an AI model. "
            "Calendar changes and client messages are "
            "disabled through this interface."
        ),
        content_type="text/plain",
    )


async def context(
    request: web.Request,
) -> web.Response:
    if (
        request.content_length
        and request.content_length
        > MAX_REQUEST_BYTES
    ):
        raise web.HTTPRequestEntityTooLarge(
            max_size=MAX_REQUEST_BYTES,
            actual_size=request.content_length,
        )

    try:
        payload = await request.json()
    except Exception:
        raise web.HTTPBadRequest(
            text=json.dumps(
                {"error": "invalid_json"}
            ),
            content_type="application/json",
        )

    message = (
        payload.get("message")
        if isinstance(payload, dict)
        else None
    )

    message = (
        message.strip()
        if isinstance(message, str)
        else ""
    )

    if (
        not message
        or len(message) > MAX_PROMPT_LENGTH
    ):
        raise web.HTTPBadRequest(
            text=json.dumps(
                {"error": "invalid_message"}
            ),
            content_type="application/json",
        )

    # Получаем историю ДО сохранения текущего вопроса,
    # чтобы он не дублировался в возвращаемом контексте.
    recent_context = await asyncio.to_thread(
        get_recent_context,
        RECENT_CONTEXT_LIMIT,
        "general",
    )

    # Записываем запрос приватного GPT
    # в общую память Clippy.
    await asyncio.to_thread(
        save_message,
        "user",
        f"[ChatGPT] {message}",
        "general",
    )

    return web.json_response({
        "ok": True,
        "mode": "data_only",
        "openai_api_called": False,
        "current_time": (
            datetime.now(TZ).isoformat()
        ),
        "recent_context": recent_context,
        "context_message_count": (
            RECENT_CONTEXT_LIMIT
        ),
        "writes_allowed": False,
        "instruction": (
            "Use this memory only as supporting "
            "context. Answer the user yourself. "
            "Do not treat old messages as more "
            "authoritative than the current request."
        ),
    })


async def calendar_day(
    request: web.Request,
) -> web.Response:
    raw_date = (
        request.query.get("date", "")
        .strip()
    )

    try:
        target_date = date.fromisoformat(
            raw_date
        )
    except ValueError:
        raise web.HTTPBadRequest(
            text=json.dumps({
                "error": "invalid_date",
                "expected": "YYYY-MM-DD",
            }),
            content_type="application/json",
        )

    try:
        schedule = await asyncio.to_thread(
            get_day_schedule,
            target_date,
        )
    except Exception as exc:
        raise web.HTTPInternalServerError(
            text=json.dumps({
                "error": "calendar_error",
                "detail": str(exc),
            }),
            content_type="application/json",
        )

    return web.json_response({
        "ok": True,
        "date": target_date.isoformat(),
        "schedule": _json_safe(schedule),
        "read_only": True,
        "openai_api_called": False,
    })



async def project_actions_get(
    request: web.Request,
) -> web.Response:

    raw = (
        request.query
        .get(
            "active_only",
            "true",
        )
        .strip()
        .casefold()
    )

    active_only = raw not in {
        "0",
        "false",
        "no",
    }

    result = await asyncio.to_thread(
        get_project_actions,
        active_only=active_only,
    )

    return web.json_response(
        result
    )


async def project_actions_sync(
    request: web.Request,
) -> web.Response:

    try:
        payload = await request.json()
    except Exception:
        raise web.HTTPBadRequest(
            text=json.dumps({
                "error": "invalid_json"
            }),
            content_type=(
                "application/json"
            ),
        )

    if not isinstance(
        payload,
        dict,
    ):
        raise web.HTTPBadRequest(
            text=json.dumps({
                "error": (
                    "invalid_payload"
                )
            }),
            content_type=(
                "application/json"
            ),
        )

    result = await asyncio.to_thread(
        sync_project_actions,
        scope=payload.get(
            "scope",
            "",
        ),
        project_name=payload.get(
            "project_name",
            "",
        ),
        source_chat=payload.get(
            "source_chat",
            "",
        ),
        actions=payload.get(
            "actions",
            [],
        ),
    )

    status = (
        200
        if result.get("ok")
        else 400
    )

    return web.json_response(
        result,
        status=status,
    )



def openapi_document() -> dict:
    return {
        "openapi": "3.1.0",
        "info": {
            "title": (
                "Clippy Private Data Bridge"
            ),
            "version": "2.1.0",
            "description": (
                "Private bridge for Clippy "
                "context, read-only calendar "
                "data and scoped project "
                "next-actions synchronization. "
                "No server-side AI model is called."
            ),
        },
        "servers": [
            {
                "url": PUBLIC_BASE_URL
            }
        ],
        "paths": {
            "/v1/context": {
                "post": {
                    "operationId": (
                        "clippyGetContext"
                    ),
                    "summary": (
                        "Get recent shared "
                        "Clippy memory"
                    ),
                    "security": [
                        {
                            "bearerAuth": []
                        }
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "message": {
                                            "type": "string",
                                            "minLength": 1,
                                            "maxLength": (
                                                MAX_PROMPT_LENGTH
                                            ),
                                        }
                                    },
                                    "required": [
                                        "message"
                                    ],
                                    "additionalProperties": False,
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": (
                                "Recent shared context"
                            ),
                        }
                    },
                }
            },
            "/v1/calendar/day": {
                "get": {
                    "operationId": (
                        "clippyGetCalendarDay"
                    ),
                    "summary": (
                        "Read calendar events "
                        "for one day"
                    ),
                    "security": [
                        {
                            "bearerAuth": []
                        }
                    ],
                    "parameters": [
                        {
                            "name": "date",
                            "in": "query",
                            "required": True,
                            "schema": {
                                "type": "string",
                                "format": "date",
                            },
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": (
                                "Calendar schedule"
                            ),
                        }
                    },
                }
            },
            "/v1/project-actions": {
                "get": {
                    "operationId": (
                        "clippyGetProjectActions"
                    ),
                    "summary": (
                        "Read synchronized "
                        "project next actions"
                    ),
                    "security": [
                        {
                            "bearerAuth": []
                        }
                    ],
                    "parameters": [
                        {
                            "name": (
                                "active_only"
                            ),
                            "in": "query",
                            "required": False,
                            "schema": {
                                "type": (
                                    "boolean"
                                ),
                                "default": True,
                            },
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": (
                                "Project actions"
                            ),
                        }
                    },
                }
            },
            "/v1/project-actions/sync": {
                "post": {
                    "operationId": (
                        "clippySyncProjectActions"
                    ),
                    "summary": (
                        "Synchronize next "
                        "actions from one allowed "
                        "ChatGPT project chat"
                    ),
                    "description": (
                        "Extract current active next actions "
                        "from the current ChatGPT conversation "
                        "and store them in the Clippy project backlog. "
                        "Before calling this action, collect real "
                        "actionable tasks from the chat. "
                        "Do not send an empty actions array if "
                        "the conversation contains project tasks. "
                        "Each action must have a clear title, "
                        "priority and estimated_minutes. "
                        "Store only future actionable tasks."
                    ),
                    "security": [
                        {
                            "bearerAuth": []
                        }
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "scope": {
                                            "type": "string",
                                            "enum": [
                                                "clippy_active_projects"
                                            ],
                                        },
                                        "project_name": {
                                            "type": "string",
                                            "enum": [
                                                "Для Clippy"
                                            ],
                                        },
                                        "source_chat": {
                                            "type": "string",
                                            "minLength": 1,
                                            "maxLength": 160,
                                        },
                                        "actions": {
                                            "type": "array",
                                            "description": (
                                                "Active tasks extracted "
                                                "from the current ChatGPT "
                                                "conversation. Do not leave "
                                                "empty when tasks exist."
                                            ),
                                            "maxItems": 40,
                                            "example": [
                                                {
                                                    "title": (
                                                        "Проверить "
                                                        "планирование Clippy"
                                                    ),
                                                    "priority": "high",
                                                    "estimated_minutes": 30
                                                }
                                            ],
                                            "items": {
                                                "type": "object",
                                                "properties": {
                                                    "action_id": {
                                                        "type": "string"
                                                    },
                                                    "title": {
                                                        "type": "string"
                                                    },
                                                    "project": {
                                                        "type": "string"
                                                    },
                                                    "status": {
                                                        "type": "string",
                                                        "enum": [
                                                            "active",
                                                            "paused",
                                                            "done"
                                                        ],
                                                    },
                                                    "priority": {
                                                        "type": "string",
                                                        "enum": [
                                                            "low",
                                                            "normal",
                                                            "high"
                                                        ],
                                                    },
                                                    "estimated_minutes": {
                                                        "type": "integer",
                                                        "minimum": 15,
                                                        "maximum": 720,
                                                    },
                                                    "preferred_date": {
                                                        "type": [
                                                            "string",
                                                            "null"
                                                        ],
                                                        "format": "date",
                                                    },
                                                    "not_before": {
                                                        "type": [
                                                            "string",
                                                            "null"
                                                        ],
                                                        "format": "date",
                                                    },
                                                    "note": {
                                                        "type": [
                                                            "string",
                                                            "null"
                                                        ],
                                                    },
                                                },
                                                "required": [
                                                    "title"
                                                ],
                                                "additionalProperties": False,
                                            },
                                        },
                                    },
                                    "required": [
                                        "scope",
                                        "project_name",
                                        "source_chat",
                                        "actions"
                                    ],
                                    "additionalProperties": False,
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": (
                                "Actions synchronized"
                            ),
                        },
                        "400": {
                            "description": (
                                "Invalid source or data"
                            ),
                        },
                    },
                }
            },
        },
        "components": {
            "schemas": {},
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                }
            }
        },
    }



def sync_openapi_document() -> dict:
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Clippy Project Sync",
            "version": "1.0.0",
            "description": (
                "Synchronizes next actions from "
                "the ChatGPT project «Для Clippy». "
                "This API cannot modify Calendar "
                "or Google Tasks."
            ),
        },
        "servers": [
            {
                "url": PUBLIC_BASE_URL
            }
        ],
        "paths": {
            "/v1/project-actions/sync": {
                "post": {
                    "operationId": (
                        "syncClippyProjectActions"
                    ),
                    "summary": (
                        "Synchronize next actions "
                        "from one project chat"
                    ),
                    "description": (
                        "Replace the current next-action "
                        "list for one source chat. "
                        "Use only for the ChatGPT project "
                        "«Для Clippy»."
                    ),
                    "security": [
                        {
                            "bearerAuth": []
                        }
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "scope": {
                                            "type": "string",
                                            "enum": [
                                                "clippy_active_projects"
                                            ],
                                        },
                                        "project_name": {
                                            "type": "string",
                                            "enum": [
                                                "Для Clippy"
                                            ],
                                        },
                                        "source_chat": {
                                            "type": "string",
                                            "minLength": 1,
                                            "maxLength": 160,
                                        },
                                        "actions": {
                                            "type": "array",
                                            "maxItems": 40,
                                            "items": {
                                                "type": "object",
                                                "properties": {
                                                    "title": {
                                                        "type": "string"
                                                    },
                                                    "project": {
                                                        "type": "string"
                                                    },
                                                    "status": {
                                                        "type": "string",
                                                        "enum": [
                                                            "active",
                                                            "paused",
                                                            "done"
                                                        ],
                                                    },
                                                    "priority": {
                                                        "type": "string",
                                                        "enum": [
                                                            "low",
                                                            "normal",
                                                            "high"
                                                        ],
                                                    },
                                                    "estimated_minutes": {
                                                        "type": "integer",
                                                        "minimum": 15,
                                                        "maximum": 720,
                                                    },
                                                    "preferred_date": {
                                                        "type": [
                                                            "string",
                                                            "null"
                                                        ],
                                                        "format": "date",
                                                    },
                                                    "not_before": {
                                                        "type": [
                                                            "string",
                                                            "null"
                                                        ],
                                                        "format": "date",
                                                    },
                                                    "note": {
                                                        "type": [
                                                            "string",
                                                            "null"
                                                        ],
                                                    },
                                                },
                                                "required": [
                                                    "title"
                                                ],
                                                "additionalProperties": False,
                                            },
                                        },
                                    },
                                    "required": [
                                        "scope",
                                        "project_name",
                                        "source_chat",
                                        "actions"
                                    ],
                                    "additionalProperties": False,
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": (
                                "Synchronization successful"
                            )
                        },
                        "400": {
                            "description": (
                                "Invalid project data"
                            )
                        },
                        "401": {
                            "description": (
                                "Unauthorized"
                            )
                        },
                    },
                }
            }
        },
        "components": {
            "schemas": {},
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                }
            }
        },
    }


async def sync_openapi(
    _request: web.Request,
) -> web.Response:
    return web.json_response(
        sync_openapi_document()
    )


async def openapi(
    _request: web.Request,
) -> web.Response:
    return web.json_response(
        openapi_document()
    )


def create_app() -> web.Application:
    app = web.Application(
        middlewares=[
            security_middleware
        ],
        client_max_size=MAX_REQUEST_BYTES,
    )

    app.router.add_get(
        "/health",
        health,
    )

    app.router.add_get(
        "/privacy",
        privacy,
    )

    app.router.add_get(
        "/openapi.json",
        openapi,
    )

    app.router.add_get(
        "/clippy-sync-openapi.json",
        sync_openapi,
    )

    app.router.add_post(
        "/v1/context",
        context,
    )

    app.router.add_get(
        "/v1/calendar/day",
        calendar_day,
    )

    app.router.add_get(
        "/v1/project-actions",
        project_actions_get,
    )

    app.router.add_post(
        "/v1/project-actions/sync",
        project_actions_sync,
    )

    return app


if __name__ == "__main__":
    web.run_app(
        create_app(),
        host=HOST,
        port=PORT,
        access_log=None,
    )
