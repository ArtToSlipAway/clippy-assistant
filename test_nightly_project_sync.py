import json
import os
import sys
import types
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo


calendar_tools = types.ModuleType("calendar_tools")
calendar_tools.classify_planning_event = lambda *_args, **_kwargs: "flexible"
calendar_tools.clean_calendar_title = lambda value: str(value or "").strip()
calendar_tools.get_calendar_service = lambda **_kwargs: None
sys.modules["calendar_tools"] = calendar_tools

memory_store = types.ModuleType("memory_store")
memory_store.get_facts = lambda: {}
memory_store.save_fact = lambda *_args, **_kwargs: None
sys.modules["memory_store"] = memory_store

project_next_actions = types.ModuleType("project_next_actions")
project_next_actions.sync_project_actions = (
    lambda **_kwargs: {"ok": True}
)
sys.modules["project_next_actions"] = project_next_actions

import nightly_project_sync as sync


TZ = ZoneInfo("Europe/Moscow")


def _event(event_id, summary, start, end, description=""):
    return {
        "id": event_id,
        "summary": summary,
        "description": description,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }


class NightlyProjectSyncTests(unittest.TestCase):
    def setUp(self):
        os.environ["GOOGLE_PROJECTS_CALENDAR_ID"] = "projects"
        os.environ["GOOGLE_PERSONAL_CALENDAR_ID"] = "personal"
        os.environ["GOOGLE_CALENDAR_ID"] = "tattoos"

    def test_sync_uses_calendar_truth_and_creates_only_missing_sketch(self):
        project_event = _event(
            "project-1",
            "Project: manually scheduled task",
            "2026-09-08T09:30:00+03:00",
            "2026-09-08T11:30:00+03:00",
        )
        tattoo_missing = _event(
            "tattoo-1",
            "Тату-сеанс — Тестовый клиент",
            "2026-09-10T13:00:00+03:00",
            "2026-09-10T17:00:00+03:00",
            "Проект: тестовый сюжет",
        )
        tattoo_ready = _event(
            "tattoo-2",
            "Тату-сеанс — Второй клиент",
            "2026-09-11T13:00:00+03:00",
            "2026-09-11T17:00:00+03:00",
            "Эскиз готов",
        )
        unrelated = _event(
            "unrelated-1",
            "Стрижка",
            "2026-09-06T14:00:00+03:00",
            "2026-09-06T15:00:00+03:00",
        )

        def fake_events(calendar_id, _start, _end):
            return {
                "projects": [project_event],
                "personal": [],
                "tattoos": [tattoo_missing, tattoo_ready, unrelated],
            }[calendar_id]

        captured = {}

        def fake_sync(**kwargs):
            captured["actions"] = kwargs["actions"]
            return {"ok": True}

        def fake_save_fact(key, value):
            captured[key] = value

        now = datetime(2026, 9, 6, 3, 20, tzinfo=TZ)

        with (
            patch.object(sync, "_calendar_events", fake_events),
            patch.object(sync, "sync_project_actions", fake_sync),
            patch.object(sync, "save_fact", fake_save_fact),
        ):
            result = sync.sync_calendar_projects(now)

        self.assertEqual(result["project_events"], 1)
        self.assertEqual(result["tattoo_sessions"], 2)
        self.assertEqual(result["sketch_actions"], 1)
        self.assertEqual(len(captured["actions"]), 1)
        self.assertEqual(
            captured["actions"][0]["preferred_date"],
            "2026-09-07",
        )

        snapshot = json.loads(captured[sync.SNAPSHOT_FACT_KEY])
        self.assertTrue(snapshot["project_events"][0]["manual"])
        self.assertFalse(snapshot["tattoo_sessions"][0]["sketch_ready"])
        self.assertTrue(snapshot["tattoo_sessions"][1]["sketch_ready"])

    def test_due_only_once_after_nightly_time(self):
        before = datetime(2026, 9, 6, 3, 14, tzinfo=TZ)
        after = datetime(2026, 9, 6, 3, 15, tzinfo=TZ)

        with patch.object(sync, "get_facts", return_value={}):
            self.assertFalse(sync.nightly_sync_is_due(before))
            self.assertTrue(sync.nightly_sync_is_due(after))

        with patch.object(
            sync,
            "get_facts",
            return_value={sync.LAST_SYNC_FACT_KEY: "2026-09-06"},
        ):
            self.assertFalse(sync.nightly_sync_is_due(after))


if __name__ == "__main__":
    unittest.main()
