import ast
import unittest
from pathlib import Path


def load_functions(filename, names):
    tree = ast.parse(Path(__file__).with_name(filename).read_text())
    nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    namespace = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), filename, "exec"), namespace)
    return namespace


bot_functions = load_functions(
    "bot.py",
    {
        "missing_confirmation_button_followup",
        "contextual_confirmation",
        "calendar_confirmation_markup_needed",
    },
)
calendar_functions = load_functions(
    "calendar_tools.py",
    {
        "classify_planning_event",
        "_linked_google_task_meta",
        "classify_existing_calendar_event",
    },
)


class CalendarConfirmationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
