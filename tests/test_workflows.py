from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class WorkflowSyntaxTests(unittest.TestCase):
    def test_all_workflows_are_valid_yaml_mappings(self):
        workflow_dir = ROOT / ".github" / "workflows"
        for path in workflow_dir.glob("*.yml"):
            with self.subTest(path=path.name):
                parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
                self.assertIsInstance(parsed, dict)
                self.assertIn("jobs", parsed)

    def test_restored_cache_does_not_skip_reading_current_secret(self):
        parsed = yaml.safe_load((ROOT / ".github/workflows/quark_signin.yml").read_text(encoding="utf-8"))
        steps = parsed["jobs"]["sign-in"]["steps"]
        restore = next(s for s in steps if s.get("id") == "cache-success")
        checkout = next(s for s in steps if s.get("uses", "").startswith("actions/checkout@"))
        signin = next(s for s in steps if s["name"] == "执行签到脚本")
        save = next(s for s in steps if s.get("uses", "").startswith("actions/cache/save@"))
        # Checkout must precede restore; its cleanup must not remove restored records.
        self.assertLess(steps.index(checkout), steps.index(restore))
        for step in steps[:steps.index(signin) + 1]:
            self.assertNotIn("if", step)
        self.assertEqual(signin["env"]["COOKIE_QUARK"], "${{ secrets.COOKIE_QUARK }}")
        self.assertEqual(signin["env"]["QUARK_SUCCESS_CACHE"], restore["with"]["path"])
        self.assertEqual(save["with"]["path"], restore["with"]["path"])
        # Immutable caches need a new snapshot for each run, restored only from today.
        self.assertIn("${{ github.run_id }}", restore["with"]["key"])
        self.assertIn("${{ github.run_attempt }}", restore["with"]["key"])
        self.assertEqual(
            restore["with"]["restore-keys"].strip(),
            "quark-accounts-v1-${{ steps.date.outputs.today }}-",
        )
        self.assertIn("always()", save["if"])
        self.assertNotIn("success()", save["if"])


if __name__ == "__main__":
    unittest.main()
