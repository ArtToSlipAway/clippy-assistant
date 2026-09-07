import ast
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


def load_functions(filename, names):
    tree = ast.parse(Path(__file__).with_name(filename).read_text())
    nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    namespace = {"date": date, "timedelta": timedelta, "os": os}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), filename, "exec"), namespace)
    return namespace


bot_functions = load_functions(
    "bot.py",
    {
        "missing_confirmation_button_followup",
        "contextual_confirmation",
        "calendar_confirmation_markup_needed",
        "_morning_candidate_dates",
    },
)
calendar_functions = load_functions(
    "calendar_tools.py",
    {
        "classify_planning_event",
        "_linked_google_task_meta",
        "classify_existing_calendar_event",
        "standalone_google_tasks",
        "_plan_action_calendar_id",
        "save_plan_proposal",
    },
)
google_task_functions = load_functions(
    "google_tasks_tools.py",
    {"get_day_overview"},
)


class CalendarConfirmationTests(unittest.TestCase):
    def test_saved_plan_keeps_explicit_personal_calendar_kind(self):
        save = calendar_functions["save_plan_proposal"]
        namespace = save.__globals__

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "GOOGLE_PERSONAL_CALENDAR_ID": "personal",
                "GOOGLE_CALENDAR_ID": "tattoo",
            },
        ):
            proposal_path = Path(tmp) / "proposal.json"
            namespace.update({
                "json": json,
                "datetime": datetime,
                "TZ": ZoneInfo("Europe/Moscow"),
                "PLAN_PROPOSAL_FILE": proposal_path,
                "has_technical_ai_plan_prefix": lambda _title: False,
                "clean_calendar_title": lambda title: title.strip(),
                "_parse_input_datetime": datetime.fromisoformat,
                "_plan_action_calendar_id": calendar_functions[
                    "_plan_action_calendar_id"
                ],
            })

            result = save(
                "2099-09-08",
                [{
                    "type": "create",
                    "calendar_kind": "personal",
                    "title": "Отрисовать эскиз к тату сеансу Кирилла",
                    "start": "2099-09-08T17:00:00+03:00",
                    "end": "2099-09-08T19:00:00+03:00",
                    "allow_ozon_overlap": True,
                }],
            )

            saved = json.loads(proposal_path.read_text())

        self.assertTrue(result["ok"])
        self.assertEqual(saved["actions"][0]["calendar_kind"], "personal")

    def test_explicit_personal_calendar_wins_over_tattoo_words_in_title(self):
        route = calendar_functions["_plan_action_calendar_id"]

        with patch.dict(
            os.environ,
            {
                "GOOGLE_PERSONAL_CALENDAR_ID": "personal",
                "GOOGLE_CALENDAR_ID": "tattoo",
            },
        ):
            result = route({
                "type": "create",
                "calendar_kind": "personal",
                "title": "Отрисовать эскиз к тату сеансу Кирилла",
            })

        self.assertEqual(result, "personal")

    def test_tattoo_sketch_searches_until_preferred_date(self):
        candidate_dates = bot_functions["_morning_candidate_dates"]
        namespace = candidate_dates.__globals__
        namespace["TATTOO_ACTION_SOURCE"] = "tattoo-sketches"

        result = candidate_dates(
            {
                "source_chat": "tattoo-sketches",
                "preferred_date": "2026-09-10",
            },
            date(2026, 9, 7),
        )

        self.assertEqual(
            result,
            [
                date(2026, 9, 7),
                date(2026, 9, 8),
                date(2026, 9, 9),
                date(2026, 9, 10),
            ],
        )

    def test_regular_action_stays_on_proposal_date(self):
        candidate_dates = bot_functions["_morning_candidate_dates"]
        candidate_dates.__globals__["TATTOO_ACTION_SOURCE"] = (
            "tattoo-sketches"
        )

        self.assertEqual(
            candidate_dates(
                {
                    "source_chat": "ChatGPT project",
                    "preferred_date": "2026-09-10",
                },
                date(2026, 9, 7),
            ),
            [date(2026, 9, 7)],
        )

    def test_plain_yes_is_contextual_confirmation(self):
        confirm = bot_functions["contextual_confirmation"]
        self.assertTrue(confirm("да", True))
        self.assertFalse(confirm("да", False))

    def test_missing_button_followup_is_recognized(self):
        detect = bot_functions["missing_confirmation_button_followup"]
        self.assertTrue(detect("Нет кнопки"))
        self.assertTrue(detect("Не вижу кнопку"))

    def test_saved_plan_alone_gets_confirmation_buttons(self):
        should_show = bot_functions["calendar_confirmation_markup_needed"]
        self.assertTrue(should_show(False, True))
        self.assertTrue(should_show(True, False))
        self.assertFalse(should_show(False, False))

    def test_linked_google_task_is_movable_regardless_of_title(self):
        event = {
            "extendedProperties": {
                "private": {
                    "google_task_list_id": "list-1",
                    "google_task_id": "task-1",
                }
            }
        }
        self.assertEqual(
            calendar_functions["classify_existing_calendar_event"](
                "Личный",
                "Т-Старт — снять видео и завершить анкету",
                event,
            ),
            "flexible",
        )

    def test_unlinked_unknown_personal_event_remains_fixed(self):
        self.assertEqual(
            calendar_functions["classify_existing_calendar_event"](
                "Личный",
                "Встреча с Александром",
                {},
            ),
            "fixed",
        )

    def test_legacy_t_start_task_is_movable_without_metadata(self):
        classify = calendar_functions["classify_existing_calendar_event"]
        self.assertEqual(
            classify(
                "Личный",
                "Т-Старт — снять видео и завершить анкету",
                {},
            ),
            "flexible",
        )
        self.assertEqual(
            classify(
                "Личный",
                "Встреча с Александром — съёмка видео Т-Старт",
                {},
            ),
            "fixed",
        )

    def test_linked_google_task_is_not_repeated_as_all_day_task(self):
        standalone = calendar_functions["standalone_google_tasks"]
        tasks = [
            {"task_list_id": "list-1", "task_id": "task-1"},
            {"task_list_id": "list-1", "task_id": "task-2"},
        ]
        events = [{
            "source": "linked_google_task",
            "task_list_id": "list-1",
            "task_id": "task-1",
        }]
        self.assertEqual(standalone(tasks, events), [tasks[1]])

    def test_day_overview_uses_combined_schedule_once(self):
        combined = [
            {"title": "Timed", "all_day": False, "start_iso": "12:00"},
            {"title": "Task", "all_day": True, "start_iso": ""},
        ]
        calendar_module = types.ModuleType("calendar_tools")
        calendar_module.get_day_schedule = lambda _target: list(combined)

        with patch.dict(sys.modules, {"calendar_tools": calendar_module}):
            result = google_task_functions["get_day_overview"](
                date(2026, 9, 8)
            )

        self.assertEqual(
            [item["title"] for item in result],
            ["Task", "Timed"],
        )


if __name__ == "__main__":
    unittest.main()
