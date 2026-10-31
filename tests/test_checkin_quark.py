import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import requests

from checkIn_Quark import (
    ConfigError,
    DailySuccessCache,
    Quark,
    QuarkAPIError,
    extract_params,
    load_env_file,
    main,
    parse_account,
    push_server_chan,
    send,
    split_account_entries,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise QuarkAPIError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class ParsingTests(unittest.TestCase):
    def test_split_accounts_supports_newlines_crlf_and_double_ampersand(self):
        entries = split_account_entries(" one \r\n\r\n two && three ")
        self.assertEqual(entries, ["one", "two", "three"])

    def test_extract_params_from_captured_url(self):
        params = extract_params(
            "https://drive-m.quark.cn/path?foo=1&kps=a%2Bb&sign=s&vcode=v"
        )
        self.assertEqual(params, {"kps": "a+b", "sign": "s", "vcode": "v"})


    def test_extract_params_preserves_literal_plus_in_new_captured_url(self):
        params = extract_params(
            "https://drive-m.quark.cn/1/clouddrive/act/growth/reward"
            "?mt=token+part&kps=AASx+abc%2Bencoded%3D"
            "&sign=AAQH+sig%2Bencoded%3D&vcode=1790667254217&app=clouddrive"
        )
        self.assertEqual(params["kps"], "AASx+abc+encoded=")
        self.assertEqual(params["sign"], "AAQH+sig+encoded=")
        self.assertEqual(params["vcode"], "1790667254217")

    def test_parse_account_supports_legacy_format(self):
        account = parse_account("user=张三; kps=k; sign=s; vcode=v;", 1)
        self.assertEqual(account["user"], "张三")
        self.assertEqual(account["kps"], "k")

    def test_parse_account_supports_captured_url_format(self):
        account = parse_account(
            "user=李四; url=https://example.test/reward?kps=k&sign=s&vcode=v;",
            1,
        )
        self.assertEqual(account["sign"], "s")

    def test_parse_account_reports_missing_parameters(self):
        with self.assertRaisesRegex(ConfigError, "sign, vcode"):
            parse_account("user=张三; kps=k;", 1)


class EnvFileTests(unittest.TestCase):
    def test_load_env_file_reads_simple_quoted_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "# comment",
                        "COOKIE_QUARK='user=账号; url=https://example.test/?a=1&b=2'",
                        'SERVER_CHAN_SEND_KEY=\"server-key\"',
                    ]
                ),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                load_env_file(env_path)
                self.assertEqual(
                    os.environ["COOKIE_QUARK"],
                    "user=账号; url=https://example.test/?a=1&b=2",
                )
                self.assertEqual(os.environ["SERVER_CHAN_SEND_KEY"], "server-key")

    def test_load_env_file_does_not_override_existing_environment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text("COOKIE_QUARK=file-value", encoding="utf-8")
            with patch.dict("os.environ", {"COOKIE_QUARK": "existing-value"}):
                load_env_file(env_path)
                self.assertEqual(os.environ["COOKIE_QUARK"], "existing-value")


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.account = {"user": "测试", "kps": "k", "sign": "s", "vcode": "v"}

    def test_already_signed_is_successful(self):
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "data": {
                            "88VIP": False,
                            "total_capacity": 1024,
                            "cap_composition": {"sign_reward": 512},
                            "cap_sign": {
                                "sign_daily": True,
                                "sign_daily_reward": 256,
                                "sign_progress": 2,
                                "sign_target": 7,
                            },
                        }
                    }
                )
            ]
        )
        quark = Quark(self.account, session=session)
        result = quark.do_sign()
        self.assertIn("今日已签到", result)
        self.assertFalse(quark.checked_in_by_this_run)
        self.assertEqual(len(session.calls), 1)

    def test_unsigned_account_posts_sign_request(self):
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "data": {
                            "88VIP": True,
                            "total_capacity": 2048,
                            "cap_composition": {},
                            "cap_sign": {
                                "sign_daily": False,
                                "sign_progress": 2,
                                "sign_target": 7,
                            },
                        }
                    }
                ),
                FakeResponse({"data": {"sign_daily_reward": 1024}}),
            ]
        )
        quark = Quark(self.account, session=session)
        result = quark.do_sign()
        self.assertIn("签到成功", result)
        self.assertTrue(quark.checked_in_by_this_run)
        self.assertEqual([call[0] for call in session.calls], ["GET", "POST"])

    def test_api_error_is_not_treated_as_success(self):
        session = FakeSession([FakeResponse({"code": 401, "message": "凭证失效"})])
        with self.assertRaisesRegex(QuarkAPIError, "凭证失效"):
            Quark(self.account, session=session).do_sign()


class MainTests(unittest.TestCase):
    def setUp(self):
        # Tests must never use real notification credentials or Actions outputs.
        env = patch.dict("os.environ", {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_multi_account_continues_after_one_failure_and_returns_nonzero(self):
        seen = []

        class StubQuark:
            def __init__(self, account):
                self.account = account

            def do_sign(self):
                seen.append(self.account["user"])
                if self.account["user"] == "坏账号":
                    raise QuarkAPIError("凭证失效")
                return "✅ 签到成功"

        raw = (
            "user=坏账号;kps=1;sign=1;vcode=1;\n"
            "user=好账号;kps=2;sign=2;vcode=2;"
        )
        with redirect_stdout(io.StringIO()):
            exit_code = main(raw, quark_factory=StubQuark)

        self.assertEqual(exit_code, 1)
        self.assertEqual(seen, ["坏账号", "好账号"])

    def test_all_accounts_success_returns_zero(self):
        class StubQuark:
            def __init__(self, account):
                self.account = account
                self.checked_in_by_this_run = True

            def do_sign(self):
                return "✅ 签到成功"

        with redirect_stdout(io.StringIO()):
            exit_code = main(
                "user=账号;kps=1;sign=1;vcode=1;", quark_factory=StubQuark
            )
        self.assertEqual(exit_code, 0)

    def test_success_pushes_server_chan_when_server_chan_send_key_is_configured(self):
        class StubQuark:
            def __init__(self, account):
                self.account = account
                self.checked_in_by_this_run = True

            def do_sign(self):
                return "✅ 签到成功"

        with (
            patch.dict("os.environ", {"SERVER_CHAN_SEND_KEY": "test-sendkey"}),
            patch("checkIn_Quark.requests.post", return_value=FakeResponse({"code": 0})) as post,
            redirect_stdout(io.StringIO()),
        ):
            exit_code = main(
                "user=账号;kps=1;sign=1;vcode=1;", quark_factory=StubQuark
            )

        self.assertEqual(exit_code, 0)
        post.assert_called_once()
        args, kwargs = post.call_args
        self.assertEqual(
            args[0], "https://sctapi.ftqq.com/test-sendkey.send"
        )
        self.assertEqual(kwargs["data"]["title"], "夸克自动签到")

    def test_all_accounts_already_checked_in_skips_server_chan_push(self):
        class StubQuark:
            def __init__(self, account):
                self.account = account
                self.checked_in_by_this_run = False

            def do_sign(self):
                return f"{self.account['user']}\n✅ 今日已签到"

        raw = (
            "user=账号一;kps=1;sign=1;vcode=1;\n"
            "user=账号二;kps=2;sign=2;vcode=2;"
        )
        with (
            patch.dict("os.environ", {"SERVER_CHAN_SEND_KEY": "test-sendkey"}),
            patch("checkIn_Quark.requests.post") as post,
            redirect_stdout(io.StringIO()),
        ):
            exit_code = main(raw, quark_factory=StubQuark)

        self.assertEqual(exit_code, 0)
        post.assert_not_called()

    def test_mixed_account_results_push_one_summary(self):
        class StubQuark:
            def __init__(self, account):
                self.account = account
                self.checked_in_by_this_run = account["user"] == "账号二"

            def do_sign(self):
                if self.checked_in_by_this_run:
                    return f"{self.account['user']}\n✅ 签到成功"
                return f"{self.account['user']}\n✅ 今日已签到"

        raw = (
            "user=账号一;kps=1;sign=1;vcode=1;\n"
            "user=账号二;kps=2;sign=2;vcode=2;"
        )
        with (
            patch.dict("os.environ", {"SERVER_CHAN_SEND_KEY": "test-sendkey"}),
            patch("checkIn_Quark.requests.post", return_value=FakeResponse({"code": 0})) as post,
            redirect_stdout(io.StringIO()),
        ):
            exit_code = main(raw, quark_factory=StubQuark)

        self.assertEqual(exit_code, 0)
        post.assert_called_once()
        message = post.call_args.kwargs["data"]["desp"]
        self.assertIn("账号一", message)
        self.assertIn("账号二", message)
        self.assertIn("今日已签到", message)
        self.assertIn("签到成功", message)

    def test_missing_server_chan_send_key_skips_server_chan_push(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("checkIn_Quark.requests.post") as post,
        ):
            pushed = push_server_chan("标题", "内容")

        self.assertFalse(pushed)
        post.assert_not_called()

    def test_failed_run_pushes_one_summary(self):
        class StubQuark:
            def __init__(self, account):
                self.account = account
                self.checked_in_by_this_run = False

            def do_sign(self):
                if self.account["user"] == "坏账号":
                    raise QuarkAPIError("凭证失效")
                return f"{self.account['user']}\n✅ 今日已签到"

        raw = (
            "user=坏账号;kps=1;sign=1;vcode=1;\n"
            "user=好账号;kps=2;sign=2;vcode=2;"
        )
        with (
            patch.dict("os.environ", {"SERVER_CHAN_SEND_KEY": "test-sendkey"}),
            patch("checkIn_Quark.requests.post", return_value=FakeResponse({"code": 0})) as post,
            redirect_stdout(io.StringIO()),
        ):
            exit_code = main(raw, quark_factory=StubQuark)

        self.assertEqual(exit_code, 1)
        post.assert_called_once()
        message = post.call_args.kwargs["data"]["desp"]
        self.assertIn("坏账号", message)
        self.assertIn("凭证失效", message)
        self.assertIn("好账号", message)

    def test_missing_environment_returns_configuration_error(self):
        with redirect_stdout(io.StringIO()):
            exit_code = main("")
        self.assertEqual(exit_code, 2)


class ActionsSummaryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.summary = Path(temp.name) / "step-summary.md"
        env = patch.dict(
            "os.environ",
            {
                "GITHUB_STEP_SUMMARY": str(self.summary),
                "SERVER_CHAN_SEND_KEY": "test-sendkey",
            },
            clear=True,
        )
        env.start()
        self.addCleanup(env.stop)

    def test_success_summary_matches_notification(self):
        class StubQuark:
            checked_in_by_this_run = True

            def __init__(self, account):
                self.account = account

            def do_sign(self):
                return f"{self.account['user']}\n✅ 签到成功 +20.00 MB"

        raw = "user=账号一;kps=1;sign=1;vcode=1;\nuser=账号二;kps=2;sign=2;vcode=2;"
        output = io.StringIO()
        with (
            patch("checkIn_Quark.requests.post", return_value=FakeResponse({"code": 0})) as post,
            redirect_stdout(output),
        ):
            self.assertEqual(main(raw, quark_factory=StubQuark), 0)

        post.assert_called_once()
        report = self.summary.read_text(encoding="utf-8")
        account_message = report.split("```text\n", 1)[1].split("\n```", 1)[0]
        self.assertEqual(post.call_args.kwargs["data"]["desp"], account_message.replace("\n", "\n\n"))
        self.assertIn("账号一", report)
        self.assertIn("账号二", report)
        self.assertIn("Server 酱：本次已发送，发送成功。", report)
        self.assertIn("Server 酱：本次已发送，发送成功。", output.getvalue())

    def test_all_already_signed_reports_skip_without_sending(self):
        with patch("checkIn_Quark.requests.post") as post, redirect_stdout(io.StringIO()):
            send("夸克自动签到", "账号一：今日已签到\n账号二：今日已签到", push=False)
        post.assert_not_called()
        self.assertIn(
            "Server 酱：本次未发送（所有账号在执行前已签到）。",
            self.summary.read_text(encoding="utf-8"),
        )

    def test_missing_key_reports_skip_without_sending(self):
        os.environ.pop("SERVER_CHAN_SEND_KEY")
        with patch("checkIn_Quark.requests.post") as post, redirect_stdout(io.StringIO()):
            send("夸克自动签到", "✅ 签到成功")
        post.assert_not_called()
        self.assertIn(
            "本次未发送（未配置 SERVER_CHAN_SEND_KEY）",
            self.summary.read_text(encoding="utf-8"),
        )

    def test_failed_account_summary_includes_all_accounts_and_push_status(self):
        class StubQuark:
            checked_in_by_this_run = False

            def __init__(self, account):
                self.account = account

            def do_sign(self):
                if self.account["user"] == "坏账号":
                    raise QuarkAPIError("凭证失效")
                return f"{self.account['user']}\n✅ 今日已签到"

        raw = "user=坏账号;kps=1;sign=1;vcode=1;\nuser=好账号;kps=2;sign=2;vcode=2;"
        with (
            patch("checkIn_Quark.requests.post", return_value=FakeResponse({"code": 0})) as post,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(raw, quark_factory=StubQuark), 1)
        post.assert_called_once()
        report = self.summary.read_text(encoding="utf-8")
        self.assertIn("坏账号", report)
        self.assertIn("凭证失效", report)
        self.assertIn("好账号", report)
        self.assertIn("本次已发送，发送成功", report)

    def test_notification_failures_are_reported_without_failing_signin(self):
        class StubQuark:
            checked_in_by_this_run = True

            def __init__(self, account):
                pass

            def do_sign(self):
                return "✅ 签到成功"

        cases = [
            {"return_value": FakeResponse({"code": 40001})},
            {"return_value": FakeResponse({})},
            {"return_value": FakeResponse([])},
            {"side_effect": requests.ConnectionError("https://sctapi.ftqq.com/test-sendkey.send")},
            {"side_effect": ValueError("invalid json")},
        ]
        for response in cases:
            with self.subTest(response=response):
                self.summary.write_text("", encoding="utf-8")
                output = io.StringIO()
                with patch("checkIn_Quark.requests.post", **response), redirect_stdout(output):
                    self.assertEqual(
                        main("user=账号;kps=1;sign=1;vcode=1;", quark_factory=StubQuark),
                        0,
                    )
                self.assertIn("本次已尝试发送，发送失败", self.summary.read_text(encoding="utf-8"))
                self.assertNotIn("test-sendkey", output.getvalue())

    def test_missing_accounts_still_produce_summary_without_sending(self):
        with patch("checkIn_Quark.requests.post") as post, redirect_stdout(io.StringIO()):
            self.assertEqual(main(""), 2)
        post.assert_not_called()
        report = self.summary.read_text(encoding="utf-8")
        self.assertIn("未配置 COOKIE_QUARK", report)
        self.assertIn("本次未发送（账号配置错误，未执行签到）", report)


class AccountCacheTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cache_path = Path(temp.name) / "cache" / "accounts.json"
        self.summary_path = Path(temp.name) / "step-summary.md"
        env = patch.dict(
            "os.environ",
            {
                "QUARK_SUCCESS_CACHE": str(self.cache_path),
                "GITHUB_STEP_SUMMARY": str(self.summary_path),
                "SERVER_CHAN_SEND_KEY": "test-sendkey",
            },
            clear=True,
        )
        env.start()
        self.addCleanup(env.stop)
        clock = patch("checkIn_Quark.datetime")
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        self.clock.now.return_value = datetime(2026, 10, 31, 12)
        self.first = "user=账号一;kps=first-secret-kps;sign=first-secret-sign;vcode=111;"
        self.second = "user=账号二;kps=second-secret-kps;sign=second-secret-sign;vcode=222;"
        self.third = "user=账号三;kps=third-secret-kps;sign=third-secret-sign;vcode=333;"

    def run_accounts(self, raw, outcomes=None, notification_failure=False):
        outcomes = outcomes or {}
        seen = []

        class StubQuark:
            def __init__(self, account):
                self.account = account
                seen.append(account["user"])
                self.checked_in_by_this_run = outcomes.get(account["user"]) != "already"

            def do_sign(self):
                user = self.account["user"]
                if outcomes.get(user) == "failure":
                    raise QuarkAPIError("凭证失效")
                status = "今日已签到" if outcomes.get(user) == "already" else "签到成功"
                return f"{user}\n✅ {status} +20.00 MB"

        self.summary_path.write_text("", encoding="utf-8")
        with (
            patch(
                "checkIn_Quark.requests.post",
                return_value=FakeResponse({"code": 40001 if notification_failure else 0}),
            ) as post,
            redirect_stdout(io.StringIO()),
        ):
            exit_code = main(raw, quark_factory=StubQuark)
        return exit_code, seen, post

    def test_added_account_runs_while_original_accounts_are_skipped(self):
        raw = self.first + "\n" + self.second
        code, seen, post = self.run_accounts(raw)
        self.assertEqual(code, 0)
        self.assertEqual(seen, ["账号一", "账号二"])
        post.assert_called_once()

        code, seen, post = self.run_accounts(raw + "\n" + self.third)
        self.assertEqual(code, 0)
        self.assertEqual(seen, ["账号三"])
        post.assert_called_once()
        report = self.summary_path.read_text(encoding="utf-8")
        message = post.call_args.kwargs["data"]["desp"]
        for user in ("账号一", "账号二", "账号三"):
            self.assertIn(user, report)
            self.assertIn(user, message)
        self.assertEqual(report.count("⏭️ 今日已确认成功"), 2)

        code, seen, post = self.run_accounts(raw + "\n" + self.third)
        self.assertEqual(code, 0)
        self.assertEqual(seen, [])
        post.assert_not_called()
        report = self.summary_path.read_text(encoding="utf-8")
        self.assertEqual(report.count("⏭️ 今日已确认成功"), 3)
        self.assertIn("本次未发送（所有账号均命中今日成功记录）", report)

    def test_only_changed_credentials_are_rechecked(self):
        self.run_accounts(self.first + "\n" + self.second)
        for old, new in (
            ("first-secret-kps", "updated-kps"),
            ("first-secret-sign", "updated-sign"),
            ("vcode=111", "vcode=444"),
        ):
            with self.subTest(credential=old):
                # Reset the baseline so each individual credential change is checked.
                self.cache_path.unlink()
                self.run_accounts(self.first + "\n" + self.second)
                code, seen, post = self.run_accounts(
                    self.first.replace(old, new) + "\n" + self.second,
                    outcomes={"账号一": "already"},
                )
                self.assertEqual(code, 0)
                self.assertEqual(seen, ["账号一"])
                post.assert_not_called()

    def test_reordering_and_url_format_do_not_recheck_same_accounts(self):
        self.run_accounts(self.first + "\n" + self.second)
        url_entry = (
            "user=账号一;url=https://example.test/other?foo=changed"
            "&vcode=111&sign=first-secret-sign&kps=first-secret-kps;"
        )
        code, seen, post = self.run_accounts(self.second + " && " + url_entry)
        self.assertEqual(code, 0)
        self.assertEqual(seen, [])
        post.assert_not_called()
        report = self.summary_path.read_text(encoding="utf-8")
        self.assertLess(report.index("账号二"), report.index("账号一"))

    def test_partial_failure_retains_success_and_retries_only_failed_account(self):
        raw = self.first + "\n" + self.second
        code, seen, post = self.run_accounts(raw, {"账号二": "failure"})
        self.assertEqual(code, 1)
        self.assertEqual(seen, ["账号一", "账号二"])
        post.assert_called_once()
        payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["accounts"]), 1)

        code, seen, post = self.run_accounts(raw)
        self.assertEqual(code, 0)
        self.assertEqual(seen, ["账号二"])
        post.assert_called_once()
        message = post.call_args.kwargs["data"]["desp"]
        self.assertIn("账号一", message)
        self.assertIn("账号二", message)

    def test_already_signed_accounts_are_saved_without_notification(self):
        self.run_accounts(self.first, {"账号一": "already"})
        self.assertTrue(self.cache_path.exists())
        code, seen, post = self.run_accounts(self.first)
        self.assertEqual(code, 0)
        self.assertEqual(seen, [])
        post.assert_not_called()

    def test_notification_failure_does_not_discard_success_records(self):
        code, seen, post = self.run_accounts(self.first, notification_failure=True)
        self.assertEqual(code, 0)
        post.assert_called_once()
        code, seen, post = self.run_accounts(self.first)
        self.assertEqual(seen, [])
        post.assert_not_called()

    def test_removed_account_does_not_appear_in_report(self):
        self.run_accounts(self.first + "\n" + self.second)
        code, seen, post = self.run_accounts(self.second)
        self.assertEqual(code, 0)
        self.assertEqual(seen, [])
        self.assertNotIn("账号一", self.summary_path.read_text(encoding="utf-8"))

    def test_next_beijing_date_rechecks_all_accounts(self):
        self.run_accounts(self.first + "\n" + self.second)
        self.clock.now.return_value = datetime(2026, 11, 1, 0)
        code, seen, post = self.run_accounts(self.first + "\n" + self.second)
        self.assertEqual(code, 0)
        self.assertEqual(seen, ["账号一", "账号二"])
        self.assertEqual(json.loads(self.cache_path.read_text())["date"], "2026-11-01")
        self.assertEqual(str(self.clock.now.call_args.args[0]), "Asia/Shanghai")

    def test_invalid_cache_never_skips_accounts(self):
        self.cache_path.parent.mkdir()
        for value in ('not json', '[]', '{"version":1,"date":"2026-10-31","accounts":[]}'):
            with self.subTest(cache=value):
                self.cache_path.write_text(value)
                code, seen, post = self.run_accounts(self.first)
                self.assertEqual(code, 0)
                self.assertEqual(seen, ["账号一"])

    def test_cache_contains_no_raw_credentials_or_notification_status(self):
        self.run_accounts(self.first)
        saved = self.cache_path.read_text()
        self.assertNotIn("first-secret-kps", saved)
        self.assertNotIn("first-secret-sign", saved)
        self.assertNotIn("vcode", saved)
        self.assertNotIn("test-sendkey", saved)
        self.assertNotIn("Server 酱", saved)
        payload = json.loads(saved)
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["date"], "2026-10-31")
        key = next(iter(payload["accounts"]))
        self.assertRegex(key, r"^[a-f0-9]{64}$")
        cache = DailySuccessCache(str(self.cache_path))
        self.assertIn("签到成功", cache.get(parse_account(self.first, 1)))


if __name__ == "__main__":
    unittest.main()
