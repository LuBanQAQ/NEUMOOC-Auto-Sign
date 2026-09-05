import json
import shutil
import unittest
import uuid
from datetime import date
from pathlib import Path

from neumooc_checkin import (
    DEFAULT_SIGN_LATITUDE,
    DEFAULT_SIGN_LONGITUDE,
    DIRECT_SIGN_REFRESH_SEED,
    AutoCheckinBot,
    SignTask,
    build_sign_payload,
    extract_page_items,
    resolve_current_term,
)
from neumooc_login import NeumoocClient, build_parser

# 不用 tempfile.mkdtemp：其 0700 限制性 ACL 在部分受控环境下无法写入
_TEMP_ROOT = Path(__file__).resolve().parent / "_tmp_run"


class FakeResponse:
    def __init__(self, payload=None, *, code=0, msg="", status=200, content=b""):
        self.status_code = status
        self._payload = {"code": code, "msg": msg, "data": payload}
        self.content = content
        self.text = json.dumps(self._payload, ensure_ascii=False)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses=None):
        self.headers = {}
        self.verify = True
        self.trust_env = True
        self.calls = []
        self.responses = list(responses or [])

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse({"ok": True})

    # 便捷断言辅助
    def urls(self):
        return [(m, u) for m, u, _ in self.calls]

    def last_json(self):
        return self.calls[-1][2]["json"]


class CheckinTestCase(unittest.TestCase):
    """公共基类：提供可写的临时目录与测试客户端工厂。"""

    def make_temp_dir(self):
        _TEMP_ROOT.mkdir(exist_ok=True)
        path = _TEMP_ROOT / uuid.uuid4().hex[:12]
        path.mkdir()
        self.addCleanup(shutil.rmtree, path, True)
        return path

    def make_client(self, responses=None):
        directory = self.make_temp_dir()
        client = NeumoocClient(
            token_file=str(directory / "token.json"),
            credential_file=str(directory / "credentials.json"),
        )
        client.http = FakeSession(responses)
        client.access_token = "access"
        client.user_id = "stu-1"
        return client


def page_response(rows):
    return FakeResponse({"list": rows, "total": len(rows)})


ROW_NORMAL = {
    "id": "detail-1",
    "attendanceId": "att-1",
    "title": "随堂签到",
    "type": 0,
    "attendanceLocationType": 0,
    "status": 1,
}
ROW_SIGNED = {
    "id": "detail-2",
    "attendanceId": "att-2",
    "title": "已签过的考勤",
    "type": 0,
    "attendanceLocationType": 0,
    "status": 1,
    "signRole": 4,
    "signTimeString": "2026-01-01 08:00:00",
}
ROW_QR = {
    "id": "detail-3",
    "attendanceId": "att-3",
    "title": "二维码签到",
    "type": 1,
    "attendanceLocationType": 0,
    "status": 1,
}
ROW_LOCATION = {
    "id": "detail-4",
    "attendanceId": "att-4",
    "title": "定位签到",
    "type": 0,
    "attendanceLocationType": 1,
    "status": 1,
}
ROW_NO_ID = {"title": "字段不明的行", "type": 0, "status": 1}

UPDATE_URL = (
    "https://study.neusoft.edu.cn/web-api/teachmanager/"
    "teach-course-attendance-detail/update"
)
PAGE_URL = (
    "https://study.neusoft.edu.cn/web-api/teachmanager/"
    "teach-course-attendance-detail/getAppStuAttendancePage"
)


class HelperTests(CheckinTestCase):
    def test_resolve_current_term_prefers_date_range_over_flag(self):
        # 服务端 isCurrentTerm 标志不可靠：按日期落在区间内的学期优先，
        # 即使它 isCurrentTerm=0、而标志为 1 的学期是未来的
        terms = [
            {"id": "real-now", "name": "本学期(标志0)", "isCurrentTerm": 0,
             "termStartTime": [2026, 8, 31], "termEndTime": [2027, 1, 4],
             "createTime": 1},
            {"id": "flagged-future", "name": "未来学期(标志1)", "isCurrentTerm": 1,
             "termStartTime": [2027, 6, 21], "termEndTime": [2027, 7, 19],
             "createTime": 2},
        ]
        term = resolve_current_term(terms, today=date(2026, 9, 5))
        self.assertEqual(term["id"], "real-now")

    def test_resolve_current_term_tiebreak_and_fallback(self):
        # 多个区间内学期：优先 isCurrentTerm=1，再按 createTime 最新
        terms = [
            {"id": "a", "isCurrentTerm": 0, "createTime": 9,
             "termStartTime": [2026, 2, 1], "termEndTime": [2026, 7, 1]},
            {"id": "b", "isCurrentTerm": 1, "createTime": 5,
             "termStartTime": [2026, 2, 1], "termEndTime": [2026, 7, 1]},
        ]
        self.assertEqual(resolve_current_term(terms, today=date(2026, 3, 1))["id"], "b")
        # 无日期区间信息时回退到 isCurrentTerm=1 / createTime 最新
        self.assertEqual(
            resolve_current_term([
                {"id": "x", "isCurrentTerm": 0, "createTime": 5},
                {"id": "y", "isCurrentTerm": 1, "createTime": 9},
            ])["id"],
            "y",
        )
        self.assertIsNone(resolve_current_term([]))

    def test_extract_page_items_shapes(self):
        self.assertEqual(extract_page_items({"list": [ROW_NORMAL]}), [ROW_NORMAL])
        self.assertEqual(extract_page_items({"records": [ROW_SIGNED]}), [ROW_SIGNED])
        self.assertEqual(extract_page_items([ROW_QR]), [ROW_QR])
        self.assertEqual(extract_page_items(None), [])
        self.assertEqual(extract_page_items({"total": 0}), [])

    def test_sign_task_detects_signed_rows(self):
        self.assertTrue(SignTask(dict(ROW_SIGNED)).signed)
        unsigned = dict(ROW_SIGNED)
        unsigned.pop("signRole")
        unsigned.pop("signTimeString")
        self.assertFalse(SignTask(unsigned).signed)
        # 字符串类型的 signRole / signTime 也能识别
        self.assertTrue(SignTask({**ROW_NORMAL, "signRole": "4"}).signed)
        self.assertTrue(SignTask({**ROW_NORMAL, "signTime": "2026-01-01"}).signed)

    def test_sign_task_defaults(self):
        task = SignTask({"title": "仅标题"})
        self.assertEqual(task.type, 0)
        self.assertEqual(task.location_type, 0)
        self.assertFalse(task.signed)


class PayloadTests(CheckinTestCase):
    def test_normal_payload_matches_apk_doc(self):
        task = SignTask(dict(ROW_NORMAL))
        payload = build_sign_payload(task, "stu-1")
        self.assertEqual(
            payload,
            {
                "attendanceId": "att-1",
                "id": "detail-1",
                "status": 1,
                "type": 0,
                "signRole": 4,
                "signUserId": "stu-1",
            },
        )

    def test_location_payload_uses_sign_prefixed_fields(self):
        task = SignTask(dict(ROW_LOCATION))
        payload = build_sign_payload(
            task, "stu-1", longitude="121.5", latitude="38.9", address="教学楼"
        )
        # 文档 5/ATT-05：signLongitude / signLatitude / signAddressName
        self.assertEqual(payload["signLongitude"], "121.5")
        self.assertEqual(payload["signLatitude"], "38.9")
        self.assertEqual(payload["signAddressName"], "教学楼")
        self.assertNotIn("longitude", payload)
        self.assertNotIn("qrCodeId", payload)

    def test_qr_payload_carries_type_and_refresh_seed(self):
        task = SignTask(dict(ROW_QR))
        payload = build_sign_payload(task, "stu-1", refresh_seed="seed-1")
        self.assertEqual(payload["type"], 1)
        self.assertEqual(payload["refreshSeed"], "seed-1")
        # 文档明确：提交体中没有 qrCodeId / qrCodeValid
        self.assertNotIn("qrCodeId", payload)
        self.assertNotIn("qrCodeValid", payload)

    def test_qr_payload_type_is_configurable(self):
        task = SignTask(dict(ROW_QR))
        self.assertEqual(build_sign_payload(task, "stu-1")["type"], 1)
        self.assertEqual(
            build_sign_payload(task, "stu-1")["refreshSeed"], DIRECT_SIGN_REFRESH_SEED
        )
        self.assertEqual(build_sign_payload(task, "stu-1", qr_sign_type=0)["type"], 0)
        self.assertNotIn(
            "refreshSeed", build_sign_payload(task, "stu-1", qr_sign_type=0)
        )


class BotTests(CheckinTestCase):
    def make_bot(self, rows, *, responses=None, **kwargs):
        client = self.make_client([page_response(rows)] + list(responses or []))
        kwargs.setdefault("term_id", "term-1")
        bot = AutoCheckinBot(client, **kwargs)
        return bot

    def test_prepare_requires_token_and_user(self):
        client = self.make_client()
        client.access_token = None
        bot = AutoCheckinBot(client)
        with self.assertRaises(Exception):
            bot.prepare()

    def test_prepare_auto_login_from_credentials(self):
        directory = self.make_temp_dir()
        cred_file = directory / "credentials.json"
        cred_file.write_text(
            json.dumps({"username": "u", "password": "p", "tenantId": "123"}),
            encoding="utf-8",
        )
        client = NeumoocClient(
            token_file=str(directory / "token.json"),
            credential_file=str(cred_file),
        )
        client.http = FakeSession(
            [FakeResponse({"accessToken": "a", "refreshToken": "r", "userId": "stu-1"})]
        )
        bot = AutoCheckinBot(client, term_id="term-1")
        bot.prepare()
        self.assertEqual(client.access_token, "a")
        self.assertEqual(bot.student_id, "stu-1")

    def test_scan_signs_pending_normal_task(self):
        bot = self.make_bot([ROW_NORMAL])
        counts = bot.scan_once()
        self.assertEqual(counts["ok"], 1)
        method, url = bot.client.http.urls()[-1]
        self.assertEqual((method, url), ("PUT", UPDATE_URL))
        self.assertEqual(
            bot.client.http.last_json(),
            {
                "attendanceId": "att-1",
                "id": "detail-1",
                "status": 1,
                "type": 0,
                "signRole": 4,
                "signUserId": "stu-1",
            },
        )
        self.assertEqual(bot.attempted["att-1:detail-1"], "done")

        # 第二轮同一场考勤不会重复提交
        bot.client.http.responses.append(page_response([ROW_NORMAL]))
        counts = bot.scan_once()
        self.assertEqual(counts["ok"], 0)
        self.assertEqual(counts["signed"], 1)
        self.assertEqual(len([1 for m, u in bot.client.http.urls() if u == UPDATE_URL]), 1)

    def test_scan_skips_signed_rows(self):
        bot = self.make_bot([ROW_SIGNED])
        counts = bot.scan_once()
        self.assertEqual(counts["signed"], 1)
        self.assertEqual(counts["ok"], 0)
        self.assertNotIn(("PUT", UPDATE_URL), bot.client.http.urls())

    def test_scan_filters_in_progress_status(self):
        bot = self.make_bot([])
        bot.scan_once()
        self.assertEqual(bot.client.http.last_json()["status"], 1)
        self.assertEqual(bot.client.http.last_json()["termId"], "term-1")

        bot_any = self.make_bot([], any_status=True)
        bot_any.scan_once()
        self.assertIsNone(bot_any.client.http.last_json()["status"])

    def test_dry_run_never_submits(self):
        bot = self.make_bot([ROW_NORMAL], dry_run=True)
        counts = bot.scan_once()
        self.assertEqual(counts["skip"], 1)
        urls = bot.client.http.urls()
        self.assertEqual(len(urls), 1)  # 只有 ATT-01 查询
        self.assertEqual(urls[0][1], PAGE_URL)

    def test_qr_signs_directly_without_code_or_validation(self):
        # 二维码签到已改为直接发包：不需要 --qr-code，也不调用 ATT-04，
        # 直接提交 ATT-05（type=1 + 伪造 refreshSeed="0" + 默认坐标）
        bot = self.make_bot([ROW_QR])
        counts = bot.scan_once()
        self.assertEqual(counts["ok"], 1)
        method, url = bot.client.http.urls()[-1]
        self.assertEqual((method, url), ("PUT", UPDATE_URL))
        payload = bot.client.http.last_json()
        self.assertEqual(payload["type"], 1)
        self.assertEqual(payload["refreshSeed"], DIRECT_SIGN_REFRESH_SEED)
        self.assertEqual(payload["signLongitude"], DEFAULT_SIGN_LONGITUDE)
        self.assertEqual(payload["signLatitude"], DEFAULT_SIGN_LATITUDE)
        self.assertNotIn("qrCodeId", payload)
        self.assertFalse(
            any("check-qrCode-is-valid" in u for _, u in bot.client.http.urls())
        )

    def test_qr_sign_type_zero_submits_as_normal(self):
        bot = self.make_bot([ROW_QR], qr_sign_type=0)
        counts = bot.scan_once()
        self.assertEqual(counts["ok"], 1)
        payload = bot.client.http.last_json()
        self.assertEqual(payload["type"], 0)
        self.assertNotIn("refreshSeed", payload)

    def test_location_requires_coordinates(self):
        bot = self.make_bot([ROW_LOCATION])
        counts = bot.scan_once()
        self.assertEqual(counts["skip"], 1)
        self.assertNotIn(("PUT", UPDATE_URL), bot.client.http.urls())

        bot2 = self.make_bot([ROW_LOCATION], longitude="121.5", latitude="38.9")
        counts = bot2.scan_once()
        self.assertEqual(counts["ok"], 1)
        payload = bot2.client.http.last_json()
        self.assertEqual(payload["signLongitude"], "121.5")
        self.assertEqual(payload["signLatitude"], "38.9")

    def test_missing_detail_id_resolved_via_att03(self):
        row = {k: v for k, v in ROW_NORMAL.items() if k != "id"}
        bot = self.make_bot([row], responses=[FakeResponse("detail-9")])
        counts = bot.scan_once()
        self.assertEqual(counts["ok"], 1)
        self.assertEqual(bot.client.http.last_json()["id"], "detail-9")

    def test_unknown_row_is_skipped_without_crash(self):
        bot = self.make_bot([ROW_NO_ID])
        counts = bot.scan_once()
        self.assertEqual(counts["skip"], 1)
        self.assertNotIn(("PUT", UPDATE_URL), bot.client.http.urls())

    def test_server_duplicate_message_marks_done(self):
        bot = self.make_bot(
            [ROW_NORMAL],
            responses=[FakeResponse(None, code=1001, msg="该考勤已签到")],
        )
        counts = bot.scan_once()
        self.assertEqual(counts["signed"], 1)
        self.assertEqual(bot.attempted["att-1:detail-1"], "done")

    def test_failure_counts_and_gives_up(self):
        # 服务端持续报错（非“已签”）：每轮重试直至 max_attempts 后跳过
        bot = self.make_bot(
            [ROW_NORMAL],
            responses=[
                FakeResponse(None, code=500, msg="服务器开小差"),
                FakeResponse(None, code=500, msg="服务器开小差"),
            ],
            max_attempts=2,
        )
        first = bot.scan_once()
        self.assertEqual(first["fail"], 1)
        # 第二轮：队列头插入新的页面返回，再跟一个失败响应
        bot.client.http.responses.insert(0, FakeResponse(None, code=500, msg="服务器开小差"))
        bot.client.http.responses.insert(0, page_response([ROW_NORMAL]))
        second = bot.scan_once()
        self.assertEqual(second["fail"], 1)
        # 第三轮：失败次数已达上限，只查询不再提交
        bot.client.http.responses.insert(0, page_response([ROW_NORMAL]))
        third = bot.scan_once()
        self.assertEqual(third["skip"], 1)  # 达到上限不再请求提交
        put_count = len([1 for _, u in bot.client.http.urls() if u == UPDATE_URL])
        self.assertEqual(put_count, 2)

    def test_run_survives_page_fetch_failure(self):
        # 第 1 轮查询接口 500，第 2 轮恢复：run() 不应中断
        from unittest.mock import patch

        client = self.make_client(
            [FakeResponse(None, code=500, msg="服务器开小差"), page_response([])]
        )
        bot = AutoCheckinBot(client, term_id="term-1", max_rounds=2, quiet=True)
        with patch("neumooc_checkin.time.sleep"):
            self.assertEqual(bot.run(), 0)
        self.assertEqual(bot._totals["rounds"], 2)

    def test_auth_loss_stops_with_fatal_error(self):
        from neumooc_checkin import CheckinError

        bot = self.make_bot(
            [ROW_NORMAL], responses=[FakeResponse(None, code=401, msg="账号未登录")]
        )
        # 模拟自动刷新失败后会话被清空
        bot.client.refresh_token = None
        with self.assertRaises(CheckinError):
            bot.scan_once()

    def test_run_respects_max_rounds(self):
        bot = self.make_bot(
            [], responses=[page_response([])], max_rounds=1, quiet=True
        )
        self.assertEqual(bot.run(), 0)
        self.assertEqual(bot._totals["rounds"], 1)

    def test_teacher_attendance_skipped_unless_enabled(self):
        row = {**ROW_NORMAL, "type": 2}
        bot = self.make_bot([row])
        counts = bot.scan_once()
        self.assertEqual(counts["skip"], 1)

        bot2 = self.make_bot([row], include_teacher=True)
        counts = bot2.scan_once()
        self.assertEqual(counts["ok"], 1)


class ParserTests(CheckinTestCase):
    def test_auto_checkin_flags_parse(self):
        args = build_parser().parse_args(
            [
                "auto-checkin",
                "--once", "--dry-run", "--interval", "15",
                "--term-id", "t1", "--course-id", "c1",
                "--longitude", "121.5", "--latitude", "38.9",
                "--qr-sign-type", "0",
                "--include-teacher", "--any-status", "--quiet",
                "--max-attempts", "5",
            ]
        )
        self.assertTrue(args.once)
        self.assertTrue(args.dry_run)
        self.assertEqual(args.interval, 15)
        self.assertEqual(args.term_id, "t1")
        self.assertEqual(args.course_id, "c1")
        self.assertEqual(args.longitude, "121.5")
        self.assertEqual(args.latitude, "38.9")
        self.assertEqual(args.qr_sign_type, 0)
        self.assertTrue(args.include_teacher)
        self.assertTrue(args.any_status)
        self.assertTrue(args.quiet)
        self.assertEqual(args.max_attempts, 5)
        self.assertTrue(callable(args.func))

    def test_default_interval_and_flags(self):
        args = build_parser().parse_args(["auto-checkin"])
        self.assertEqual(args.interval, 30)
        self.assertFalse(args.once)
        self.assertIsNone(args.max_rounds)

    def test_client_sign_record_methods_use_web_routes(self):
        client = self.make_client()
        client.get_sign_num("stu-1", "cls-1")
        client.get_sign_record("stu-1", "cls-1")
        urls = [u for _, u in client.http.urls()]
        self.assertIn(
            "https://study.neusoft.edu.cn/web-api/teachmanager/"
            "teach-course-attendance-detail/getSignNum",
            urls,
        )
        self.assertIn(
            "https://study.neusoft.edu.cn/web-api/teachmanager/"
            "teach-course-attendance-detail/getSignRecord",
            urls,
        )
        params = client.http.calls[0][2]["params"]
        self.assertEqual(params, {"studentId": "stu-1", "teachClassId": "cls-1"})


if __name__ == "__main__":
    unittest.main()
