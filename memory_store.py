import sqlite3
from datetime import datetime
from pathlib import Path


DB_PATH = Path(
    "data/assistant_memory.db"
)


def get_connection():
    connection = sqlite3.connect(DB_PATH)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS conversation (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    connection.commit()
    return connection



def _ensure_conversation_scope(db):
    columns = {
        row[1]
        for row in db.execute(
            "PRAGMA table_info(conversation)"
        ).fetchall()
    }

    if "scope" not in columns:
        db.execute(
            """
            ALTER TABLE conversation
            ADD COLUMN scope TEXT
            NOT NULL DEFAULT 'general'
            """
        )

        # Вся старая переписка сохраняется,
        # но больше не участвует в активном контексте.
        db.execute(
            """
            UPDATE conversation
            SET scope = 'legacy'
            """
        )

        db.commit()


def save_message(
    role: str,
    content: str,
    scope: str = "general",
):
    content = (content or "").strip()
    scope = (scope or "general").strip()

    if not content:
        return

    if scope not in {
        "general",
        "booking",
        "legacy",
    }:
        scope = "general"

    with get_connection() as db:
        _ensure_conversation_scope(db)

        db.execute(
            """
            INSERT INTO conversation (
                role,
                content,
                created_at,
                scope
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                role,
                content,
                datetime.now().isoformat(
                    timespec="seconds"
                ),
                scope,
            ),
        )

        db.commit()


def get_recent_context(
    limit: int = 16,
    scope: str = "general",
) -> str:

    with get_connection() as db:
        _ensure_conversation_scope(db)

        rows = db.execute(
            """
            SELECT role, content
            FROM conversation
            WHERE scope = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (
                scope,
                limit,
            ),
        ).fetchall()

    rows.reverse()

    if not rows:
        return ""

    lines = []

    for role, content in rows:
        label = (
            "пользователь"
            if role == "user"
            else "Ассистент"
        )

        lines.append(
            f"{label}: {content}"
        )

    return "\n".join(lines)


def get_last_message(
    scope: str = "general",
    role: str | None = None,
) -> str:
    """Return the latest message from one isolated context scope."""

    scope = (scope or "general").strip()

    if scope not in {
        "general",
        "booking",
        "legacy",
    }:
        scope = "general"

    with get_connection() as db:
        _ensure_conversation_scope(db)

        if role:
            row = db.execute(
                """
                SELECT content
                FROM conversation
                WHERE scope = ? AND role = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (scope, role),
            ).fetchone()
        else:
            row = db.execute(
                """
                SELECT content
                FROM conversation
                WHERE scope = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (scope,),
            ).fetchone()

    if not row:
        return ""

    return (row[0] or "").strip()


def save_booking_message(
    role: str,
    content: str,
):
    save_message(
        role,
        content,
        scope="booking",
    )


def get_booking_context(
    limit: int = 20,
) -> str:
    return get_recent_context(
        limit=limit,
        scope="booking",
    )


def has_booking_context() -> bool:
    with get_connection() as db:
        _ensure_conversation_scope(db)

        row = db.execute(
            """
            SELECT 1
            FROM conversation
            WHERE scope = 'booking'
            LIMIT 1
            """
        ).fetchone()

    return bool(row)


def clear_booking_context():
    with get_connection() as db:
        _ensure_conversation_scope(db)

        db.execute(
            """
            DELETE FROM conversation
            WHERE scope = 'booking'
            """
        )

        db.commit()


def clear_conversation():
    with get_connection() as db:
        db.execute(
            "DELETE FROM conversation"
        )

        db.commit()


def save_fact(
    key: str,
    value: str,
):
    key = (key or "").strip()
    value = (value or "").strip()

    if not key or not value:
        raise ValueError(
            "Факт должен содержать название и значение"
        )

    with get_connection() as db:

        db.execute("""
            CREATE TABLE IF NOT EXISTS facts (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        db.execute(
            """
            INSERT INTO facts (
                key,
                value,
                updated_at
            )
            VALUES (?, ?, ?)

            ON CONFLICT(key)
            DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
            """,
            (
                key,
                value,
                datetime.now().isoformat(
                    timespec="seconds"
                ),
            ),
        )

        db.commit()


def get_facts() -> dict:
    with get_connection() as db:

        db.execute("""
            CREATE TABLE IF NOT EXISTS facts (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        rows = db.execute(
            """
            SELECT key, value
            FROM facts
            ORDER BY key
            """
        ).fetchall()

    return {
        key: value
        for key, value in rows
    }


def delete_fact(
    key: str,
):
    key = (key or "").strip()

    with get_connection() as db:
        db.execute(
            "DELETE FROM facts WHERE key = ?",
            (key,),
        )

        db.commit()
