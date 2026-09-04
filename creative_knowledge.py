import os
import shutil
import sqlite3

from datetime import datetime
from pathlib import Path


DB_PATH = Path(
    "data/creative_knowledge.db"
)

SQLITE_EXTENSIONS = {
    ".db",
    ".sqlite",
    ".sqlite3",
}


def is_creative_database_filename(
    filename: str,
) -> bool:
    name = (
        Path(filename)
        .name
        .lower()
    )

    suffix = Path(name).suffix

    if suffix not in SQLITE_EXTENSIONS:
        return False

    return (
        "clippy_creative_knowledge" in name
        or "creative_knowledge" in name
    )


def _quote_identifier(
    value: str,
) -> str:
    return (
        '"'
        + value.replace('"', '""')
        + '"'
    )


def _safe_value(value):
    if value is None:
        return None

    if isinstance(
        value,
        (
            str,
            int,
            float,
            bool,
        ),
    ):
        if (
            isinstance(value, str)
            and len(value) > 4000
        ):
            return value[:4000] + "…"

        return value

    if isinstance(value, bytes):
        return (
            f"<bytes:{len(value)}>"
        )

    return str(value)


def _table_names(
    conn,
) -> list[str]:
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type='table'
          AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()

    return [
        row[0]
        for row in rows
    ]


def _columns(
    conn,
    table: str,
) -> list[str]:
    ident = _quote_identifier(
        table
    )

    rows = conn.execute(
        f"PRAGMA table_info({ident})"
    ).fetchall()

    return [
        row[1]
        for row in rows
    ]


def _find_column(
    columns: list[str],
    candidates: tuple[str, ...],
) -> str | None:
    mapping = {
        column.lower(): column
        for column in columns
    }

    for candidate in candidates:
        if candidate in mapping:
            return mapping[candidate]

    return None


def _rows_as_dicts(
    conn,
    table: str,
    limit: int = 5000,
) -> list[dict]:
    ident = _quote_identifier(
        table
    )

    cursor = conn.execute(
        f"SELECT * FROM {ident} LIMIT ?",
        (limit,),
    )

    columns = [
        item[0]
        for item in cursor.description
    ]

    result = []

    for row in cursor.fetchall():
        result.append({
            column: _safe_value(value)
            for column, value
            in zip(columns, row)
        })

    return result


def _open_readonly():
    if not DB_PATH.exists():
        raise FileNotFoundError(
            "Творческая база ещё не установлена"
        )

    uri = (
        "file:"
        + DB_PATH.resolve().as_posix()
        + "?mode=ro"
    )

    return sqlite3.connect(
        uri,
        uri=True,
        timeout=5,
    )


def validate_creative_database(
    path: Path,
) -> dict:
    path = Path(path)

    if not path.exists():
        return {
            "ok": False,
            "error": "file_not_found",
        }

    try:
        uri = (
            "file:"
            + path.resolve().as_posix()
            + "?mode=ro"
        )

        conn = sqlite3.connect(
            uri,
            uri=True,
            timeout=5,
        )

        try:
            check = conn.execute(
                "PRAGMA quick_check"
            ).fetchone()

            if (
                not check
                or check[0] != "ok"
            ):
                return {
                    "ok": False,
                    "error": "sqlite_check_failed",
                    "detail": (
                        check[0]
                        if check
                        else "no_result"
                    ),
                }

            tables = _table_names(
                conn
            )

            if "projects" not in tables:
                return {
                    "ok": False,
                    "error": (
                        "projects_table_missing"
                    ),
                    "tables": tables,
                }

            project_count = conn.execute(
                "SELECT COUNT(*) FROM projects"
            ).fetchone()[0]

            return {
                "ok": True,
                "tables": tables,
                "project_count": (
                    project_count
                ),
            }

        finally:
            conn.close()

    except Exception as exc:
        return {
            "ok": False,
            "error": type(exc).__name__,
            "detail": str(exc),
        }


def install_creative_database(
    source_path: Path,
) -> dict:
    source_path = Path(
        source_path
    )

    validation = (
        validate_creative_database(
            source_path
        )
    )

    if not validation.get("ok"):
        return validation

    DB_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temp_path = DB_PATH.with_name(
        DB_PATH.name + ".new"
    )

    try:
        shutil.copy2(
            source_path,
            temp_path,
        )

        second_validation = (
            validate_creative_database(
                temp_path
            )
        )

        if not second_validation.get("ok"):
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

            return second_validation

        os.chmod(
            temp_path,
            0o600,
        )

        os.replace(
            temp_path,
            DB_PATH,
        )

        return {
            "ok": True,
            "installed": True,
            "path": str(DB_PATH),
            "project_count": (
                second_validation.get(
                    "project_count",
                    0,
                )
            ),
            "tables": (
                second_validation.get(
                    "tables",
                    [],
                )
            ),
        }

    except Exception as exc:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass

        return {
            "ok": False,
            "error": type(exc).__name__,
            "detail": str(exc),
        }


def get_creative_status() -> dict:
    if not DB_PATH.exists():
        return {
            "ok": True,
            "installed": False,
            "path": str(DB_PATH),
            "project_count": 0,
            "tables": [],
        }

    validation = (
        validate_creative_database(
            DB_PATH
        )
    )

    if not validation.get("ok"):
        return {
            **validation,
            "installed": True,
            "path": str(DB_PATH),
        }

    stat = DB_PATH.stat()

    return {
        "ok": True,
        "installed": True,
        "path": str(DB_PATH),
        "size_bytes": stat.st_size,
        "modified_at": (
            datetime.fromtimestamp(
                stat.st_mtime
            ).isoformat(
                timespec="seconds"
            )
        ),
        "project_count": (
            validation.get(
                "project_count",
                0,
            )
        ),
        "tables": (
            validation.get(
                "tables",
                [],
            )
        ),
    }


def _row_text(
    row: dict,
) -> str:
    return " ".join(
        str(value)
        for value in row.values()
        if value is not None
    ).casefold()


def search_creative_projects(
    query: str,
    limit: int = 8,
) -> dict:
    query = (
        query
        or ""
    ).strip()

    limit = min(
        max(int(limit), 1),
        10,
    )

    try:
        conn = _open_readonly()

        try:
            tables = _table_names(
                conn
            )

            if "projects" not in tables:
                return {
                    "ok": False,
                    "error": (
                        "projects_table_missing"
                    ),
                }

            project_columns = _columns(
                conn,
                "projects",
            )

            project_id_column = (
                _find_column(
                    project_columns,
                    (
                        "project_id",
                        "id",
                        "code",
                        "slug",
                    ),
                )
            )

            title_column = (
                _find_column(
                    project_columns,
                    (
                        "title",
                        "name",
                        "project_name",
                    ),
                )
            )

            projects = _rows_as_dicts(
                conn,
                "projects",
            )

            if not query:
                return {
                    "ok": True,
                    "query": query,
                    "results": (
                        projects[:limit]
                    ),
                }

            needle = query.casefold()

            scores = {}

            for index, row in enumerate(
                projects
            ):
                text = _row_text(
                    row
                )

                score = 0

                if needle in text:
                    score = 2

                if title_column:
                    title = str(
                        row.get(
                            title_column,
                            "",
                        )
                    ).casefold()

                    if needle in title:
                        score = max(
                            score,
                            4,
                        )

                if project_id_column:
                    project_id = str(
                        row.get(
                            project_id_column,
                            "",
                        )
                    ).casefold()

                    if needle == project_id:
                        score = max(
                            score,
                            6,
                        )
                    elif needle in project_id:
                        score = max(
                            score,
                            4,
                        )

                if score:
                    scores[index] = score

            if project_id_column:
                project_index_by_id = {
                    str(
                        row.get(
                            project_id_column,
                            "",
                        )
                    ): index
                    for index, row
                    in enumerate(projects)
                }

                for table in tables:
                    if table == "projects":
                        continue

                    columns = _columns(
                        conn,
                        table,
                    )

                    relation_column = (
                        _find_column(
                            columns,
                            (
                                "project_id",
                                "project",
                            ),
                        )
                    )

                    if not relation_column:
                        continue

                    for row in _rows_as_dicts(
                        conn,
                        table,
                    ):
                        if needle not in _row_text(
                            row
                        ):
                            continue

                        relation_id = str(
                            row.get(
                                relation_column,
                                "",
                            )
                        )

                        index = (
                            project_index_by_id.get(
                                relation_id
                            )
                        )

                        if index is not None:
                            scores[index] = max(
                                scores.get(
                                    index,
                                    0,
                                ),
                                3,
                            )

            ranked = sorted(
                scores.items(),
                key=lambda item: (
                    -item[1],
                    item[0],
                ),
            )

            results = [
                projects[index]
                for index, _
                in ranked[:limit]
            ]

            return {
                "ok": True,
                "query": query,
                "count": len(results),
                "results": results,
            }

        finally:
            conn.close()

    except FileNotFoundError:
        return {
            "ok": False,
            "error": (
                "creative_database_not_installed"
            ),
        }

    except Exception as exc:
        return {
            "ok": False,
            "error": type(exc).__name__,
            "detail": str(exc),
        }


def get_creative_project(
    project_ref: str,
) -> dict:
    project_ref = (
        project_ref
        or ""
    ).strip()

    if not project_ref:
        return {
            "ok": False,
            "error": "empty_project_ref",
        }

    try:
        conn = _open_readonly()

        try:
            tables = _table_names(
                conn
            )

            if "projects" not in tables:
                return {
                    "ok": False,
                    "error": (
                        "projects_table_missing"
                    ),
                }

            columns = _columns(
                conn,
                "projects",
            )

            id_column = _find_column(
                columns,
                (
                    "project_id",
                    "id",
                    "code",
                    "slug",
                ),
            )

            title_column = _find_column(
                columns,
                (
                    "title",
                    "name",
                    "project_name",
                ),
            )

            projects = _rows_as_dicts(
                conn,
                "projects",
            )

            needle = (
                project_ref.casefold()
            )

            selected = None

            for row in projects:
                if id_column:
                    value = str(
                        row.get(
                            id_column,
                            "",
                        )
                    ).casefold()

                    if value == needle:
                        selected = row
                        break

            if selected is None:
                for row in projects:
                    if title_column:
                        value = str(
                            row.get(
                                title_column,
                                "",
                            )
                        ).casefold()

                        if value == needle:
                            selected = row
                            break

            if selected is None:
                search_result = (
                    search_creative_projects(
                        project_ref,
                        limit=1,
                    )
                )

                results = search_result.get(
                    "results",
                    [],
                )

                if results:
                    selected = results[0]

            if selected is None:
                return {
                    "ok": True,
                    "found": False,
                    "project_ref": (
                        project_ref
                    ),
                }

            result = {
                "ok": True,
                "found": True,
                "project": selected,
                "related": {},
            }

            if not id_column:
                return result

            project_id = str(
                selected.get(
                    id_column,
                    "",
                )
            )

            for table in tables:
                if table == "projects":
                    continue

                relation_columns = (
                    _columns(
                        conn,
                        table,
                    )
                )

                relation_column = (
                    _find_column(
                        relation_columns,
                        (
                            "project_id",
                            "project",
                        ),
                    )
                )

                if not relation_column:
                    continue

                rows = _rows_as_dicts(
                    conn,
                    table,
                )

                matched = [
                    row
                    for row in rows
                    if str(
                        row.get(
                            relation_column,
                            "",
                        )
                    ) == project_id
                ]

                if matched:
                    result[
                        "related"
                    ][table] = matched[:100]

            return result

        finally:
            conn.close()

    except FileNotFoundError:
        return {
            "ok": False,
            "error": (
                "creative_database_not_installed"
            ),
        }

    except Exception as exc:
        return {
            "ok": False,
            "error": type(exc).__name__,
            "detail": str(exc),
        }
