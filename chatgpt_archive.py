import json
import re
import sqlite3
import zipfile

from datetime import datetime
from pathlib import Path


DB_PATH = Path(
    "data/chatgpt_archive.db"
)


def _connect():
    db = sqlite3.connect(
        DB_PATH,
        timeout=20,
    )

    db.execute(
        "PRAGMA foreign_keys=ON"
    )

    db.execute(
        "PRAGMA journal_mode=WAL"
    )

    db.execute(
        "PRAGMA synchronous=NORMAL"
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            created_at TEXT,
            updated_at TEXT,
            source_file TEXT,
            message_count INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            parent_id TEXT,
            role TEXT,
            content TEXT NOT NULL DEFAULT '',
            created_at TEXT,
            PRIMARY KEY (
                conversation_id,
                id
            ),
            FOREIGN KEY (
                conversation_id
            )
            REFERENCES conversations(id)
            ON DELETE CASCADE
        )
        """
    )

    db.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_messages_conversation
        ON messages (
            conversation_id,
            created_at
        )
        """
    )

    db.commit()

    return db


def _ensure_fts(
    db,
) -> bool:
    """
    FTS5 используется для большого архива.
    Если системный SQLite собран без FTS5,
    основной архив продолжает работать.
    """

    try:
        db.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS
            message_fts
            USING fts5(
                conversation_id UNINDEXED,
                message_id UNINDEXED,
                title,
                role UNINDEXED,
                content,
                created_at UNINDEXED,
                tokenize='unicode61 remove_diacritics 2'
            )
            """
        )

        return True

    except sqlite3.OperationalError:
        return False


def _rebuild_fts(
    db,
) -> bool:
    if not _ensure_fts(db):
        return False

    db.execute(
        "DELETE FROM message_fts"
    )

    db.execute(
        """
        INSERT INTO message_fts (
            conversation_id,
            message_id,
            title,
            role,
            content,
            created_at
        )
        SELECT
            m.conversation_id,
            m.id,
            c.title,
            m.role,
            m.content,
            m.created_at
        FROM messages AS m
        JOIN conversations AS c
          ON c.id = m.conversation_id
        """
    )

    return True


def _timestamp(value):
    if value is None:
        return None

    try:
        return datetime.fromtimestamp(
            float(value)
        ).isoformat(
            timespec="seconds"
        )
    except Exception:
        return str(value)


def _extract_text(message: dict) -> str:
    content = (
        message.get("content")
        or {}
    )

    parts = (
        content.get("parts")
        or []
    )

    result = []

    for part in parts:
        if isinstance(part, str):
            result.append(part)

        elif isinstance(part, dict):
            for key in (
                "text",
                "value",
                "content",
            ):
                value = part.get(key)

                if isinstance(
                    value,
                    str,
                ):
                    result.append(value)
                    break

    if not result:
        text = content.get("text")

        if isinstance(text, str):
            result.append(text)

    text = "\n".join(
        item.strip()
        for item in result
        if item and item.strip()
    )

    return text


def _conversation_files(
    archive: zipfile.ZipFile,
) -> list[str]:
    result = []

    for name in archive.namelist():
        base = Path(name).name.lower()

        if (
            base.startswith("conversations")
            and base.endswith(".json")
        ):
            result.append(name)

    return sorted(result)


def is_chatgpt_export_zip(
    path: Path,
) -> bool:
    try:
        with zipfile.ZipFile(
            path,
            "r",
        ) as archive:
            return bool(
                _conversation_files(
                    archive
                )
            )

    except Exception:
        return False


def _load_conversation_payload(
    raw: bytes,
):
    data = json.loads(
        raw.decode(
            "utf-8-sig"
        )
    )

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        conversations = (
            data.get("conversations")
        )

        if isinstance(
            conversations,
            list,
        ):
            return conversations

    return []


def import_chatgpt_export_zip(
    path: Path,
) -> dict:
    path = Path(path)

    if not path.exists():
        return {
            "ok": False,
            "error": "file_not_found",
        }

    try:
        with zipfile.ZipFile(
            path,
            "r",
        ) as archive:

            source_files = (
                _conversation_files(
                    archive
                )
            )

            if not source_files:
                return {
                    "ok": False,
                    "error": (
                        "conversations_json_not_found"
                    ),
                }

            conversations = []

            for source_name in source_files:
                raw = archive.read(
                    source_name
                )

                payload = (
                    _load_conversation_payload(
                        raw
                    )
                )

                for conversation in payload:
                    if isinstance(
                        conversation,
                        dict,
                    ):
                        conversations.append(
                            (
                                conversation,
                                source_name,
                            )
                        )

        db = _connect()

        imported_conversations = 0
        imported_messages = 0

        try:
            for (
                conversation,
                source_name,
            ) in conversations:

                conversation_id = str(
                    conversation.get("id")
                    or conversation.get(
                        "conversation_id"
                    )
                    or ""
                ).strip()

                if not conversation_id:
                    continue

                title = str(
                    conversation.get(
                        "title"
                    )
                    or "Без названия"
                ).strip()

                mapping = (
                    conversation.get(
                        "mapping"
                    )
                    or {}
                )

                rows = []

                if isinstance(
                    mapping,
                    dict,
                ):
                    for (
                        node_id,
                        node,
                    ) in mapping.items():

                        if not isinstance(
                            node,
                            dict,
                        ):
                            continue

                        message = (
                            node.get(
                                "message"
                            )
                        )

                        if not isinstance(
                            message,
                            dict,
                        ):
                            continue

                        text = (
                            _extract_text(
                                message
                            )
                        )

                        if not text:
                            continue

                        author = (
                            message.get(
                                "author"
                            )
                            or {}
                        )

                        role = str(
                            author.get(
                                "role"
                            )
                            or ""
                        )

                        message_id = str(
                            message.get("id")
                            or node_id
                        )

                        parent_id = (
                            node.get(
                                "parent"
                            )
                        )

                        created_at = (
                            _timestamp(
                                message.get(
                                    "create_time"
                                )
                            )
                        )

                        rows.append(
                            (
                                message_id,
                                conversation_id,
                                (
                                    str(parent_id)
                                    if parent_id
                                    else None
                                ),
                                role,
                                text,
                                created_at,
                            )
                        )

                db.execute(
                    """
                    INSERT INTO conversations (
                        id,
                        title,
                        created_at,
                        updated_at,
                        source_file,
                        message_count
                    )
                    VALUES (?, ?, ?, ?, ?, ?)

                    ON CONFLICT(id)
                    DO UPDATE SET
                        title=excluded.title,
                        created_at=excluded.created_at,
                        updated_at=excluded.updated_at,
                        source_file=excluded.source_file,
                        message_count=excluded.message_count
                    """,
                    (
                        conversation_id,
                        title,
                        _timestamp(
                            conversation.get(
                                "create_time"
                            )
                        ),
                        _timestamp(
                            conversation.get(
                                "update_time"
                            )
                        ),
                        source_name,
                        len(rows),
                    ),
                )

                db.execute(
                    """
                    DELETE FROM messages
                    WHERE conversation_id = ?
                    """,
                    (
                        conversation_id,
                    ),
                )

                if rows:
                    db.executemany(
                        """
                        INSERT INTO messages (
                            id,
                            conversation_id,
                            parent_id,
                            role,
                            content,
                            created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        rows,
                    )

                imported_conversations += 1
                imported_messages += len(
                    rows
                )

            fts_enabled = (
                _rebuild_fts(db)
            )

            db.commit()

        finally:
            db.close()

        return {
            "ok": True,
            "installed": True,
            "fts_enabled": fts_enabled,
            "path": str(DB_PATH),
            "conversation_count": (
                imported_conversations
            ),
            "message_count": (
                imported_messages
            ),
            "source_files": (
                source_files
            ),
        }

    except Exception as exc:
        return {
            "ok": False,
            "error": type(exc).__name__,
            "detail": str(exc),
        }


def get_archive_status() -> dict:
    if not DB_PATH.exists():
        return {
            "ok": True,
            "installed": False,
            "path": str(DB_PATH),
            "conversation_count": 0,
            "message_count": 0,
            "fts_enabled": False,
        }

    try:
        db = _connect()

        try:
            conversation_count = (
                db.execute(
                    """
                    SELECT COUNT(*)
                    FROM conversations
                    """
                ).fetchone()[0]
            )

            message_count = (
                db.execute(
                    """
                    SELECT COUNT(*)
                    FROM messages
                    """
                ).fetchone()[0]
            )

            fts_enabled = (
                _ensure_fts(db)
            )

            fts_count = 0

            if fts_enabled:
                try:
                    fts_count = (
                        db.execute(
                            """
                            SELECT COUNT(*)
                            FROM message_fts
                            """
                        ).fetchone()[0]
                    )
                except Exception:
                    fts_count = 0

        finally:
            db.close()

        return {
            "ok": True,
            "installed": True,
            "path": str(DB_PATH),
            "size_bytes": (
                DB_PATH.stat().st_size
            ),
            "conversation_count": (
                conversation_count
            ),
            "message_count": (
                message_count
            ),
            "fts_enabled": (
                fts_enabled
            ),
            "fts_message_count": (
                fts_count
            ),
        }

    except Exception as exc:
        return {
            "ok": False,
            "error": type(exc).__name__,
            "detail": str(exc),
        }


def _query_tokens(
    query: str,
) -> list[str]:
    return [
        token.casefold()
        for token in re.findall(
            r"[0-9A-Za-zА-Яа-яЁё_-]+",
            query or "",
        )
        if len(token) >= 2
    ]


def _score_archive_result(
    title: str,
    content: str,
    tokens: list[str],
) -> int:
    title_lower = (
        title
        or ""
    ).casefold()

    content_lower = (
        content
        or ""
    ).casefold()

    score = 0

    for token in tokens:
        if token in content_lower:
            score += 1

        if token in title_lower:
            score += 4

    return score


def _make_result(
    conversation_id,
    title,
    message_id,
    role,
    content,
    created_at,
) -> dict:
    snippet = (
        content
        or ""
    )

    if len(snippet) > 1600:
        snippet = (
            snippet[:1600]
            + "…"
        )

    return {
        "conversation_id": (
            conversation_id
        ),
        "title": title,
        "message_id": (
            message_id
        ),
        "role": role,
        "created_at": (
            created_at
        ),
        "snippet": snippet,
    }


def search_archive(
    query: str,
    limit: int = 8,
) -> dict:
    query = (
        query
        or ""
    ).strip()

    limit = min(
        max(int(limit), 1),
        12,
    )

    tokens = _query_tokens(
        query
    )

    if not tokens:
        return {
            "ok": False,
            "error": "empty_query",
        }

    if not DB_PATH.exists():
        return {
            "ok": False,
            "error": (
                "chatgpt_archive_not_installed"
            ),
        }

    try:
        db = _connect()

        try:
            # ---------------------------------
            # FAST PATH: FTS5
            # ---------------------------------

            if _ensure_fts(db):

                # Если БД была создана старой
                # версией и индекс пуст,
                # строим его автоматически.
                fts_count = (
                    db.execute(
                        """
                        SELECT COUNT(*)
                        FROM message_fts
                        """
                    ).fetchone()[0]
                )

                message_count = (
                    db.execute(
                        """
                        SELECT COUNT(*)
                        FROM messages
                        """
                    ).fetchone()[0]
                )

                if (
                    message_count
                    and fts_count != message_count
                ):
                    _rebuild_fts(db)
                    db.commit()

                escaped = []

                for token in tokens:
                    safe = token.replace(
                        '"',
                        '""',
                    )

                    escaped.append(
                        f'"{safe}"'
                    )

                fts_query = (
                    " OR ".join(
                        escaped
                    )
                )

                candidates = (
                    db.execute(
                        """
                        SELECT
                            conversation_id,
                            title,
                            message_id,
                            role,
                            content,
                            created_at,
                            bm25(message_fts)
                        FROM message_fts
                        WHERE message_fts MATCH ?
                        ORDER BY
                            bm25(message_fts)
                        LIMIT 120
                        """,
                        (
                            fts_query,
                        ),
                    ).fetchall()
                )

                ranked = []

                for row in candidates:
                    (
                        conversation_id,
                        title,
                        message_id,
                        role,
                        content,
                        created_at,
                        bm25_score,
                    ) = row

                    score = (
                        _score_archive_result(
                            title,
                            content,
                            tokens,
                        )
                    )

                    if not score:
                        continue

                    ranked.append(
                        (
                            score,
                            float(
                                bm25_score
                                or 0
                            ),
                            _make_result(
                                conversation_id,
                                title,
                                message_id,
                                role,
                                content,
                                created_at,
                            ),
                        )
                    )

                ranked.sort(
                    key=lambda item: (
                        -item[0],
                        item[1],
                        str(
                            item[2].get(
                                "created_at"
                            )
                            or ""
                        ),
                    )
                )

                results = [
                    item
                    for _, _, item
                    in ranked[:limit]
                ]

                if results:
                    return {
                        "ok": True,
                        "query": query,
                        "count": len(results),
                        "search_mode": "fts5",
                        "results": results,
                    }

            # ---------------------------------
            # SAFE FALLBACK
            # ---------------------------------

            rows = db.execute(
                """
                SELECT
                    m.conversation_id,
                    c.title,
                    m.id,
                    m.role,
                    m.content,
                    m.created_at
                FROM messages AS m
                JOIN conversations AS c
                  ON c.id = m.conversation_id
                ORDER BY
                    m.created_at DESC,
                    m.rowid DESC
                """
            ).fetchall()

        finally:
            db.close()

        scored = []

        for row in rows:
            (
                conversation_id,
                title,
                message_id,
                role,
                content,
                created_at,
            ) = row

            score = (
                _score_archive_result(
                    title,
                    content,
                    tokens,
                )
            )

            if not score:
                continue

            scored.append(
                (
                    score,
                    _make_result(
                        conversation_id,
                        title,
                        message_id,
                        role,
                        content,
                        created_at,
                    ),
                )
            )

        scored.sort(
            key=lambda item: (
                -item[0],
                str(
                    item[1].get(
                        "created_at"
                    )
                    or ""
                ),
            )
        )

        results = [
            item
            for _, item
            in scored[:limit]
        ]

        return {
            "ok": True,
            "query": query,
            "count": len(results),
            "search_mode": "fallback",
            "results": results,
        }

    except Exception as exc:
        return {
            "ok": False,
            "error": type(exc).__name__,
            "detail": str(exc),
        }


def get_archive_conversation(
    conversation_id: str,
    limit: int = 80,
) -> dict:
    conversation_id = (
        conversation_id
        or ""
    ).strip()

    limit = min(
        max(int(limit), 1),
        120,
    )

    if not conversation_id:
        return {
            "ok": False,
            "error": (
                "empty_conversation_id"
            ),
        }

    if not DB_PATH.exists():
        return {
            "ok": False,
            "error": (
                "chatgpt_archive_not_installed"
            ),
        }

    try:
        db = _connect()

        try:
            conversation = (
                db.execute(
                    """
                    SELECT
                        id,
                        title,
                        created_at,
                        updated_at,
                        message_count
                    FROM conversations
                    WHERE id = ?
                    """,
                    (
                        conversation_id,
                    ),
                ).fetchone()
            )

            if not conversation:
                return {
                    "ok": True,
                    "found": False,
                }

            messages = db.execute(
                """
                SELECT
                    id,
                    role,
                    content,
                    created_at
                FROM messages
                WHERE conversation_id = ?
                ORDER BY
                    COALESCE(created_at, ''),
                    rowid
                LIMIT ?
                """,
                (
                    conversation_id,
                    limit,
                ),
            ).fetchall()

        finally:
            db.close()

        result_messages = []

        for (
            message_id,
            role,
            content,
            created_at,
        ) in messages:

            if len(content) > 5000:
                content = (
                    content[:5000]
                    + "…"
                )

            result_messages.append({
                "message_id": message_id,
                "role": role,
                "content": content,
                "created_at": created_at,
            })

        return {
            "ok": True,
            "found": True,
            "conversation": {
                "id": conversation[0],
                "title": conversation[1],
                "created_at": (
                    conversation[2]
                ),
                "updated_at": (
                    conversation[3]
                ),
                "message_count": (
                    conversation[4]
                ),
            },
            "messages": result_messages,
        }

    except Exception as exc:
        return {
            "ok": False,
            "error": type(exc).__name__,
            "detail": str(exc),
        }
