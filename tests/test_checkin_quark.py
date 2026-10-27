import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from checkIn_Quark import (
    ConfigError,
    Quark,
    QuarkAPIError,
    extract_params,
    main,
    parse_account,
    push_server_chan,
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
            patch("checkIn_Quark.requests.post", return_value=FakeResponse({})) as post,
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
            patch("checkIn_Quark.requests.post", return_value=FakeResponse({})) as post,
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
            patch("checkIn_Quark.requests.post", return_value=FakeResponse({})) as post,
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


if __name__ == "__main__":
    unittest.main()
