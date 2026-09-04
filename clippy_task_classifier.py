import json
from pathlib import Path


RULES = Path(
    "clippy_task_rules.json"
)


def classify_task(title: str) -> dict:
    title_lower = title.lower()

    replacements = {
        "сна": "сон",
        "сну": "сон",
        "сном": "сон",
        "сне": "сон",
    }

    for old_word, new_word in replacements.items():
        title_lower = title_lower.replace(
            old_word,
            new_word,
        )

    if not RULES.exists():
        return {
            "type": "project",
            "plan": True,
        }

    data = json.loads(
        RULES.read_text()
    )

    for rule in data.get("rules", []):
        if rule["match"] in title_lower:
            return {
                "type": rule.get(
                    "type",
                    "project",
                ),
                "plan": rule.get(
                    "plan",
                    True,
                ),
                "tracking": rule.get(
                    "tracking",
                    False,
                ),
            }

    return {
        "type": "project",
        "plan": True,
    }
