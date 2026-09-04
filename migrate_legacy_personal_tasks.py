import argparse
import fcntl
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import calendar_tools
from google_tasks_tools import (
    create_google_task,
    delete_google_task,
)


DAYS_AHEAD = 45
MAX_CANDIDATES = 60

LOCK_PATH = Path(
    "/tmp/clippy_legacy_task_migration.lock"
)

BACKUP_DIR = Path(
    "data/backups"
)


def acquire_lock():
    LOCK_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    handle = LOCK_PATH.open(
        "w",
        encoding="utf-8",
    )

    try:
        fcntl.flock(
            handle.fileno(),
            fcntl.LOCK_EX
            | fcntl.LOCK_NB,
        )
    except BlockingIOError:
        raise SystemExit(
            "MIGRATION_ALREADY_RUNNING"
        )

    handle.write(
        str(os.getpid())
    )
    handle.flush()

    return handle


def collect_candidates():
    today = datetime.now(
        calendar_tools.TZ
    ).date()

    seen = set()
    result = []

    for offset in range(
        DAYS_AHEAD
    ):
        day = (
            today
            + timedelta(
                days=offset
            )
        )

        events = (
            calendar_tools
            .get_day_schedule(
                day
            )
        )

        for event in events:

            if not (
                event.get("calendar")
                == "Личный"
                and not event.get(
                    "all_day"
                )
                and event.get(
                    "movable"
                )
                and event.get(
                    "source"
                )
                == "calendar"
                and not event.get(
                    "task_id"
                )
                and event.get(
                    "event_id"
                )
            ):
                continue

            calendar_id = (
                event.get(
                    "calendar_id"
                )
                or os.environ[
                    "GOOGLE_PERSONAL_CALENDAR_ID"
                ]
            )

            event_id = event[
                "event_id"
            ]

            key = (
                calendar_id,
                event_id,
            )

            if key in seen:
                continue

            seen.add(key)

            result.append({
                "day": day,
                "calendar_id": (
                    calendar_id
                ),
                "event_id": (
                    event_id
                ),
                "schedule_event": (
                    event
                ),
            })

    return result


def clean_title(value):
    raw = (
        value
        or ""
    ).strip()

    cleaned = (
        calendar_tools
        .clean_calendar_title(
            raw
        )
    )

    return (
        cleaned
        or raw
    ).strip()


def get_start_end(raw_event):
    start_raw = (
        raw_event
        .get("start", {})
        .get("dateTime")
    )

    end_raw = (
        raw_event
        .get("end", {})
        .get("dateTime")
    )

    if (
        not start_raw
        or not end_raw
    ):
        raise ValueError(
            "event_has_no_datetime"
        )

    start_dt = (
        datetime
        .fromisoformat(
            start_raw
        )
        .astimezone(
            calendar_tools.TZ
        )
    )

    end_dt = (
        datetime
        .fromisoformat(
            end_raw
        )
        .astimezone(
            calendar_tools.TZ
        )
    )

    return (
        start_dt,
        end_dt,
    )


def main(apply_changes):
    lock_handle = acquire_lock()

    service = (
        calendar_tools
        .get_calendar_service(
            write=apply_changes
        )
    )

    candidates = (
        collect_candidates()
    )

    print(
        "CANDIDATES=",
        len(candidates),
        flush=True,
    )

    if (
        len(candidates)
        > MAX_CANDIDATES
    ):
        raise SystemExit(
            "ABORT_TOO_MANY_CANDIDATES"
        )

    raw_candidates = []

    for item in candidates:
        raw = (
            service.events()
            .get(
                calendarId=(
                    item[
                        "calendar_id"
                    ]
                ),
                eventId=(
                    item[
                        "event_id"
                    ]
                ),
            )
            .execute()
        )

        start_dt, end_dt = (
            get_start_end(
                raw
            )
        )

        raw_title = (
            raw.get(
                "summary",
                "",
            )
        )

        title = clean_title(
            raw_title
        )

        row = {
            **item,
            "raw": raw,
            "raw_title": (
                raw_title
            ),
            "title": title,
            "start_dt": start_dt,
            "end_dt": end_dt,
        }

        raw_candidates.append(
            row
        )

        print(
            "DRY:",
            start_dt.strftime(
                "%Y-%m-%d %H:%M"
            ),
            "-",
            end_dt.strftime(
                "%H:%M"
            ),
            "|",
            repr(raw_title),
            "=>",
            repr(title),
            flush=True,
        )

    if not apply_changes:
        print(
            "DRY_RUN_ONLY=TRUE",
            flush=True,
        )
        return

    BACKUP_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    stamp = datetime.now(
        calendar_tools.TZ
    ).strftime(
        "%Y%m%d_%H%M%S"
    )

    backup_path = (
        BACKUP_DIR
        / (
            "legacy_personal_tasks_"
            + stamp
            + ".json"
        )
    )

    backup_payload = []

    for row in raw_candidates:
        backup_payload.append({
            "calendar_id": (
                row[
                    "calendar_id"
                ]
            ),
            "event_id": (
                row[
                    "event_id"
                ]
            ),
            "raw": (
                row[
                    "raw"
                ]
            ),
        })

    backup_path.write_text(
        json.dumps(
            backup_payload,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(
        "BACKUP=",
        backup_path,
        flush=True,
    )

    migrated = 0
    failed = 0

    for index, row in enumerate(
        raw_candidates,
        start=1,
    ):
        title = row["title"]
        start_dt = row["start_dt"]
        end_dt = row["end_dt"]
        raw = row["raw"]

        description = (
            raw.get(
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
            + start_dt.strftime(
                "%H:%M"
            )
            + "–"
            + end_dt.strftime(
                "%H:%M"
            )
        )

        notes_parts.append(
            "Связано с существующим "
            "Calendar block."
        )

        task_result = (
            create_google_task(
                title=title,
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
            failed += 1

            print(
                f"[{index}/"
                f"{len(raw_candidates)}]"
                " TASK_CREATE_FAILED",
                repr(title),
                task_result.get(
                    "error"
                ),
                flush=True,
            )

            continue

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

        # PATCH, а не INSERT:
        # существующий Calendar event
        # остаётся тем же event_id.
        patch_body = {
            "summary": title,
            "transparency": (
                "transparent"
            ),
        }

        patch_body = (
            calendar_tools
            ._mark_linked_task_event(
                patch_body,
                task_list_id,
                task_id,
            )
        )

        try:
            (
                service.events()
                .patch(
                    calendarId=(
                        row[
                            "calendar_id"
                        ]
                    ),
                    eventId=(
                        row[
                            "event_id"
                        ]
                    ),
                    body=patch_body,
                )
                .execute()
            )

        except Exception as exc:
            # Если Calendar linkage
            # не удалось записать,
            # не оставляем сиротский Task.
            try:
                delete_google_task(
                    task_list_id,
                    task_id,
                )
            except Exception:
                pass

            failed += 1

            print(
                f"[{index}/"
                f"{len(raw_candidates)}]"
                " CALENDAR_PATCH_FAILED",
                repr(title),
                type(exc).__name__,
                str(exc),
                flush=True,
            )

            continue

        migrated += 1

        print(
            f"[{index}/"
            f"{len(raw_candidates)}]"
            " MIGRATED",
            repr(title),
            start_dt.strftime(
                "%Y-%m-%d %H:%M"
            ),
            flush=True,
        )

    print(
        "MIGRATED=",
        migrated,
        flush=True,
    )

    print(
        "FAILED=",
        failed,
        flush=True,
    )

    print(
        "BACKUP=",
        backup_path,
        flush=True,
    )

    # --------------------------------------------------
    # FINAL VERIFY
    # --------------------------------------------------

    remaining = (
        collect_candidates()
    )

    print(
        "LEGACY_REMAINING=",
        len(remaining),
        flush=True,
    )

    linked_count = 0
    ai_plan_raw = []

    today = datetime.now(
        calendar_tools.TZ
    ).date()

    for offset in range(
        DAYS_AHEAD
    ):
        day = (
            today
            + timedelta(
                days=offset
            )
        )

        for event in (
            calendar_tools
            .get_day_schedule(
                day
            )
        ):
            if (
                event.get(
                    "calendar"
                )
                == "Личный"
                and event.get(
                    "source"
                )
                == "linked_google_task"
                and event.get(
                    "task_id"
                )
            ):
                linked_count += 1

    print(
        "LINKED_BLOCKS_FOUND=",
        linked_count,
        flush=True,
    )

    print(
        "DONE=TRUE",
        flush=True,
    )

    # keep lock handle alive
    _ = lock_handle


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--apply",
        action="store_true",
    )

    args = parser.parse_args()

    main(
        apply_changes=args.apply
    )
