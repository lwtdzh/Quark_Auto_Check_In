from pathlib import Path
import os
import subprocess
import tempfile
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

    def test_cache_hit_outputs_saved_accounts_with_current_notification_status(self):
        parsed = yaml.safe_load((ROOT / ".github/workflows/quark_signin.yml").read_text(encoding="utf-8"))
        step = next(s for s in parsed["jobs"]["sign-in"]["steps"] if s["name"] == "今日已完成签到")
        for with_report in (True, False):
            with self.subTest(with_report=with_report), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                summary = root / "step-summary.md"
                if with_report:
                    cache = root / ".cache" / "quark-signin"
                    cache.mkdir(parents=True)
                    (cache / "summary.md").write_text(
                        "## 夸克自动签到\n\n```text\n账号一 ✅ 签到成功\n账号二 ✅ 今日已签到\n```\n\n",
                        encoding="utf-8",
                    )
                result = subprocess.run(
                    ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
                    cwd=root,
                    env={**os.environ, "GITHUB_STEP_SUMMARY": str(summary)},
                    capture_output=True,
                    text=True,
                    check=True,
                )
                report = summary.read_text(encoding="utf-8")
                self.assertEqual(report, result.stdout)
                self.assertIn("本次未发送（命中今日成功缓存，跳过重复执行）", report)
                self.assertNotIn("发送成功", report)
                if with_report:
                    self.assertIn("账号一", report)
                    self.assertIn("账号二", report)
                else:
                    self.assertIn("缓存中没有账号摘要", report)


if __name__ == "__main__":
    unittest.main()
