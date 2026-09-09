import ast
import re
import tempfile
import unittest
from datetime import date
from pathlib import Path


def load_ai_classification_helpers(classify_task, task_items):
    source = Path("ai_agent.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {
        "_normalize_planning_source_text",
        "_planning_source_mentions_title",
        "_task_title_from_item",
        "_classify_google_task_items",
        "_validate_planner_create_sources",
    }
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in names
    ]
    module = ast.fix_missing_locations(
        ast.Module(body=functions, type_ignores=[])
    )

    class Value:
        def __init__(self, value):
            self.value = value

        def get(self):
            return self.value

    namespace = {
        "date": date,
        "classify_task": classify_task,
        "get_google_tasks_for_date": lambda _day: task_items,
        "_PLANNING_REQUEST_ACTIVE": Value(True),
        "_PLANNING_REQUEST_TEXT": Value("Распланируй мой день"),
    }
    exec(compile(module, "ai_agent.py", "exec"), namespace)
    return namespace


def load_morning_visibility_helpers(classify_task):
    source = Path("bot.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {
        "_normalize_morning_title",
        "_morning_task_identity",
        "_morning_visible_events",
    }
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in names
    ]
    module = ast.fix_missing_locations(
        ast.Module(body=functions, type_ignores=[])
    )
    namespace = {
        "classify_task": classify_task,
        "re": re,
    }
    exec(compile(module, "bot.py", "exec"), namespace)
    return namespace


class TaskClassificationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.rules_path = Path(self.temp_dir.name) / "rules.json"
        self.rules_path.write_text(
            Path("clippy_task_rules.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        import clippy_task_classifier

        self.classifier = clippy_task_classifier
        self.original_rules = self.classifier.RULES
        self.classifier.RULES = self.rules_path

    def tearDown(self):
        self.classifier.RULES = self.original_rules
        self.temp_dir.cleanup()

    def test_tracking_categories_never_enter_automatic_plan(self):
        for title in (
            "Тренировка дома",
            "Подготовка ко сну",
            "Проверить финансы",
            "Анализ целей за неделю",
        ):
            with self.subTest(title=title):
                result = self.classifier.classify_task(title)
                self.assertEqual(result["type"], "tracking")
                self.assertTrue(result["tracking"])
                self.assertFalse(result["plan"])
                self.assertFalse(result["ignore"])

    def test_cigarette_tasks_are_ignored(self):
        for title in (
            "Купить сигареты",
            "Учёт курения",
            "Перерыв на вейп",
        ):
            with self.subTest(title=title):
                result = self.classifier.classify_task(title)
                self.assertEqual(result["type"], "ignore")
                self.assertTrue(result["ignore"])
                self.assertFalse(result["tracking"])
                self.assertFalse(result["plan"])

    def test_non_plannable_google_task_is_rejected_as_auto_create(self):
        helpers = load_ai_classification_helpers(
            self.classifier.classify_task,
            [{"title": "Тренировка дома"}],
        )

        result = helpers["_validate_planner_create_sources"](
            "2026-09-10",
            [{"type": "create", "title": "Тренировка дома"}],
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "PLAN_CREATE_SOURCE_NOT_CONFIRMED")

    def test_task_metadata_exposes_tracking_and_ignore_modes(self):
        helpers = load_ai_classification_helpers(
            self.classifier.classify_task,
            [],
        )
        classified = helpers["_classify_google_task_items"]([
            {"title": "Сон"},
            {"title": "Купить сигареты"},
        ])

        self.assertTrue(classified[0]["clippy_tracking"])
        self.assertFalse(classified[0]["clippy_plan"])
        self.assertTrue(classified[1]["clippy_ignore"])
        self.assertFalse(classified[1]["clippy_plan"])

    def test_morning_view_hides_ignore_and_keeps_tracking(self):
        helpers = load_morning_visibility_helpers(
            self.classifier.classify_task
        )
        visible = helpers["_morning_visible_events"]([
            {
                "title": "Купить сигареты",
                "source": "google_tasks",
                "task_id": "ignore-1",
                "all_day": True,
            },
            {
                "title": "Тренировка дома",
                "source": "google_tasks",
                "task_id": "track-1",
                "all_day": True,
            },
        ])

        self.assertEqual(
            [item["title"] for item in visible],
            ["Тренировка дома"],
        )


if __name__ == "__main__":
    unittest.main()
