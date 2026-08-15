import re
import unittest
from pathlib import Path

from discourse_explorer.derive_query_guide import (
    GraphStats,
    GuideInputs,
    TopicStats,
    VerbStats,
    compose_sections_7_to_12,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOST_BINDING_PATTERN = re.compile(
    r"\b(?:AskUserQuestion|subagent_type|spawn_agent|sonnet|opus|haiku|fable|terra)\b",
    re.IGNORECASE,
)
MODEL_BINDING_PATTERN = re.compile(r'\bmodel\s*:\s*"([^"]+)"')


class QueryGuideHostCompatibilityTests(unittest.TestCase):
    def test_regeneration_instructions_cover_claude_code_and_codex(self):
        inputs = GuideInputs(
            graph=GraphStats(0, 0, [], [], 0, 0, 0),
            topics=TopicStats(0, [], []),
            verbs=VerbStats([], [], 0),
            extraction_model="test",
            query_model="test",
            vocab={},
            snapshot_date="2026-08-10",
        )

        rendered = compose_sections_7_to_12(inputs)

        self.assertIn("# Claude Code\n/create-query-guide <data-dir>", rendered)
        self.assertIn("# Codex\n$create-query-guide <data-dir>", rendered)

    def test_shared_agent_skills_keep_host_bindings_centralized(self):
        symlinks = {
            PROJECT_ROOT / "AGENTS.md": PROJECT_ROOT / "CLAUDE.md",
            PROJECT_ROOT / "sample" / "AGENTS.md": PROJECT_ROOT / "sample" / "CLAUDE.md",
            PROJECT_ROOT / ".agents" / "skills": PROJECT_ROOT / ".claude" / "skills",
        }
        for link, target in symlinks.items():
            with self.subTest(link=link):
                self.assertTrue(link.is_symlink())
                self.assertEqual(link.resolve(strict=True), target.resolve(strict=True))

        skill_paths = sorted((PROJECT_ROOT / ".claude" / "skills").glob("*/SKILL.md"))
        self.assertEqual(len(skill_paths), 5)
        workflow_paths = sorted((PROJECT_ROOT / "docs" / "workflows").glob("*.md"))
        self.assertEqual(len(workflow_paths), 3)
        for host_neutral_path in [*skill_paths, *workflow_paths]:
            body = host_neutral_path.read_text(encoding="utf-8")
            with self.subTest(host_neutral_path=host_neutral_path):
                self.assertIsNone(HOST_BINDING_PATTERN.search(body))

        for skill_path in skill_paths:
            with self.subTest(skill_contract=skill_path.parent.name):
                body = skill_path.read_text(encoding="utf-8")
                self.assertIn("../HOST-COMPATIBILITY.md", body)

        contract = (PROJECT_ROOT / ".claude" / "skills" / "HOST-COMPATIBILITY.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("## Roles", contract)
        self.assertIn("| `ROUTER` |", contract)
        self.assertIn("| `EXECUTOR` |", contract)
        for required_binding in (
            'subagent_type: "general-purpose"',
            "CLAUDE_CODE_SUBAGENT_MODEL",
            "spawn_agent",
            'fork_turns: "none"',
            "If the spawn is rejected",
        ):
            with self.subTest(binding=required_binding):
                self.assertIn(required_binding, contract)

        executor_row = next(
            line for line in contract.splitlines() if line.startswith("| `EXECUTOR` |")
        )
        executor_cells = [cell.strip() for cell in executor_row.strip("|").split("|")]
        self.assertEqual(len(executor_cells), 4)
        self.assertEqual(executor_cells[0], "`EXECUTOR`")

        configured_models = []
        for host, binding in zip(("Claude Code", "Codex"), executor_cells[2:], strict=True):
            with self.subTest(executor_host=host):
                match = MODEL_BINDING_PATTERN.search(binding)
                self.assertIsNotNone(match)
                configured_models.append(match.group(1))
        self.assertNotEqual(*configured_models)
        for model in configured_models:
            self.assertNotRegex(
                model,
                r"\d{8}",
                "executor binding must be a family alias, not a pinned version ID",
            )

    def test_readme_documents_the_host_configuration_entry_point(self):
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn(".claude/skills/HOST-COMPATIBILITY.md", readme)
        self.assertIn("another agent harness", readme)


if __name__ == "__main__":
    unittest.main()
