import json
import shutil
import time
import unittest
import uuid
from datetime import date
from pathlib import Path

from neumooc_checkin import (
    DEFAULT_SIGN_ADDRESS,
    DEFAULT_SIGN_LATITUDE,
    DEFAULT_SIGN_LONGITUDE,
    DIRECT_SIGN_REFRESH_SEED,
    AutoCheckinBot,
    ForceCheckinBot,
    SignTask,
    build_force_sign_payload,
    build_sign_payload,
    extract_page_items,
    resolve_current_term,
)
from neumooc_login import ApiError, NeumoocClient, _teacher_makeup_payload, build_parser

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

    def test_scan_fetches_all_statuses_to_include_null_status(self):
        bot = self.make_bot([])
        bot.scan_once()
        self.assertIsNone(bot.client.http.last_json()["status"])
        self.assertEqual(bot.client.http.last_json()["termId"], "term-1")

        bot_any = self.make_bot([], any_status=True)
        bot_any.scan_once()
        self.assertIsNone(bot_any.client.http.last_json()["status"])

    def test_scan_accepts_pending_row_with_null_status(self):
        bot = self.make_bot([{**ROW_QR, "status": None}])
        counts = bot.scan_once()
        self.assertEqual(counts["ok"], 1)
        self.assertEqual(bot.client.http.urls()[-1], ("PUT", UPDATE_URL))

    def test_scan_skips_explicit_not_started_and_ended_rows(self):
        bot = self.make_bot([
            {**ROW_NORMAL, "id": "detail-0", "status": 0},
            {**ROW_NORMAL, "id": "detail-2", "status": 2},
        ])
        counts = bot.scan_once()
        self.assertEqual(counts["skip"], 2)
        self.assertNotIn(("PUT", UPDATE_URL), bot.client.http.urls())

    def test_any_status_allows_explicit_ended_row(self):
        bot = self.make_bot([{**ROW_NORMAL, "status": 2}], any_status=True)
        counts = bot.scan_once()
        self.assertEqual(counts["ok"], 1)

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

    def test_location_signs_with_default_or_explicit_coords(self):
        # 不传坐标：使用默认学校坐标直签（不再跳过）
        bot = self.make_bot([ROW_LOCATION])
        counts = bot.scan_once()
        self.assertEqual(counts["ok"], 1)
        payload = bot.client.http.last_json()
        self.assertEqual(payload["signLongitude"], DEFAULT_SIGN_LONGITUDE)
        self.assertEqual(payload["signLatitude"], DEFAULT_SIGN_LATITUDE)
        self.assertEqual(payload["signAddressName"], DEFAULT_SIGN_ADDRESS)

        # 传了坐标：使用传入值
        bot2 = self.make_bot(
            [ROW_LOCATION], longitude="121.5", latitude="38.9", address="教学楼"
        )
        counts = bot2.scan_once()
        self.assertEqual(counts["ok"], 1)
        payload = bot2.client.http.last_json()
        self.assertEqual(payload["signLongitude"], "121.5")
        self.assertEqual(payload["signLatitude"], "38.9")
        self.assertEqual(payload["signAddressName"], "教学楼")

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

    def test_teacher_makeup_payload_and_parser(self):
        args = build_parser().parse_args([
            "teacher-makeup",
            "--attendance-id", "att-1",
            "--detail-id", "detail-1",
            "--student-id", "stu-1",
            "--type", "1",
            "--dry-run",
        ])
        self.assertEqual(
            _teacher_makeup_payload(args),
            {
                "attendanceId": "att-1",
                "id": "detail-1",
                "status": 1,
                "type": 1,
                "signRole": 3,
                "signUserId": "stu-1",
            },
        )
        self.assertTrue(args.dry_run)

    def test_teacher_makeup_requires_complete_location(self):
        args = build_parser().parse_args([
            "teacher-makeup",
            "--attendance-id", "att-1",
            "--detail-id", "detail-1",
            "--student-id", "stu-1",
            "--longitude", "121.5",
        ])
        with self.assertRaises(ApiError):
            _teacher_makeup_payload(args)

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

    def test_teacher_makeup_uses_attendance_update_route(self):
        client = self.make_client([FakeResponse({})])
        payload = {
            "attendanceId": "att-1", "id": "detail-1", "status": 1,
            "type": 0, "signRole": 3, "signUserId": "stu-1",
        }
        client.teacher_makeup_attendance(payload)
        self.assertEqual(client.http.urls()[-1], ("PUT", UPDATE_URL))
        self.assertEqual(client.http.last_json(), payload)


TEACHER_PAGE_URL = (
    "https://study.neusoft.edu.cn/web-api/teachmanager/teach-course-attendance/page"
)
COURSE_OPTION_URL = (
    "https://study.neusoft.edu.cn/web-api/teachmanager/"
    "teach-course/get-option/by-term-id"
)


class ForceCheckinTests(CheckinTestCase):
    """教师接口强制补签（学生账号 + 教师端考勤列表）。"""

    def live_row(self, attendance_id="att-9", **overrides):
        now = int(time.time() * 1000)
        row = {
            "id": attendance_id,
            "title": "强制补签场次",
            "type": 0,
            "openTime": now - 60_000,
            "finishTime": now + 600_000,
        }
        row.update(overrides)
        return row

    def make_force_bot(self, client, **kwargs):
        kwargs.setdefault("term_id", "term-1")
        kwargs.setdefault("course_ids", ["course-1"])
        kwargs.setdefault("max_rounds", 1)
        kwargs.setdefault("quiet", True)
        kwargs.setdefault("verify", False)
        kwargs.setdefault("skip_signed", False)   # 默认关掉，避免每条用例都要多喂一份 ATT-01
        return ForceCheckinBot(client, **kwargs)

    def test_force_payload_shape(self):
        payload = build_force_sign_payload("att-1", "detail-1", "stu-1")
        self.assertEqual(payload["status"], 1)
        self.assertEqual(payload["type"], 1)
        self.assertEqual(payload["signRole"], 4)
        self.assertEqual(payload["signUserId"], "stu-1")
        self.assertEqual(payload["refreshSeed"], DIRECT_SIGN_REFRESH_SEED)
        self.assertEqual(payload["signLongitude"], DEFAULT_SIGN_LONGITUDE)
        self.assertEqual(payload["signLatitude"], DEFAULT_SIGN_LATITUDE)
        self.assertEqual(payload["signAddressName"], DEFAULT_SIGN_ADDRESS)
        self.assertNotIn("qrCodeId", payload)

        plain = build_force_sign_payload("att-1", "detail-1", "stu-1", sign_type=0)
        self.assertEqual(plain["type"], 0)
        self.assertNotIn("refreshSeed", plain)
        self.assertEqual(plain["signLongitude"], DEFAULT_SIGN_LONGITUDE)

    def test_force_signs_live_session_in_selected_course(self):
        client = self.make_client([
            FakeResponse({"list": [self.live_row()], "total": 1}),  # 教师端考勤列表
            FakeResponse("detail-9"),                                # ATT-03 明细 ID
            FakeResponse(None),                                      # ATT-05 直签
        ])
        bot = self.make_force_bot(client)
        counts = bot.scan_once()
        self.assertEqual(counts["courses"], 1)
        self.assertEqual(counts["sessions"], 1)
        self.assertEqual(counts["ok"], 1)
        urls = [u for _, u in client.http.urls()]
        self.assertIn(TEACHER_PAGE_URL, urls)
        self.assertIn(UPDATE_URL, urls)
        # 指定了课程时不应再调用课程选项接口
        self.assertNotIn(COURSE_OPTION_URL, urls)
        page_call = [c for c in client.http.calls if c[1] == TEACHER_PAGE_URL][0]
        self.assertEqual(page_call[2]["params"]["teachCourseId"], "course-1")
        payload = client.http.last_json()
        self.assertEqual(payload["attendanceId"], "att-9")
        self.assertEqual(payload["id"], "detail-9")
        self.assertEqual(payload["type"], 1)
        self.assertEqual(payload["refreshSeed"], DIRECT_SIGN_REFRESH_SEED)

    def test_force_skips_when_student_not_in_class(self):
        client = self.make_client([
            FakeResponse({"list": [self.live_row()], "total": 1}),
            FakeResponse(""),   # ATT-03 返回空 = 本人不在该班
        ])
        bot = self.make_force_bot(client)
        counts = bot.scan_once()
        self.assertEqual(counts["ok"], 0)
        self.assertEqual(counts["skip"], 1)
        self.assertNotIn(("PUT", UPDATE_URL), client.http.urls())

    def test_force_scans_all_courses_without_filter(self):
        client = self.make_client([
            FakeResponse([                                    # EDU-03 课程选项
                {"teachCourseId": "c-1", "teachCourseName": "数学"},
                {"teachCourseId": "c-2", "teachCourseName": "英语"},
            ]),
            FakeResponse({"list": [self.live_row("att-1")]}),  # c-1
            FakeResponse("detail-1"),
            FakeResponse(None),
            FakeResponse({"list": [self.live_row("att-2")]}),  # c-2
            FakeResponse("detail-2"),
            FakeResponse(None),
        ])
        bot = self.make_force_bot(client, course_ids=None)
        counts = bot.scan_once()
        self.assertEqual(counts["courses"], 2)
        self.assertEqual(counts["ok"], 2)
        self.assertIn(COURSE_OPTION_URL, [u for _, u in client.http.urls()])

    def test_force_excludes_ended_sessions_by_default(self):
        now = int(time.time() * 1000)
        ended = self.live_row(
            "att-old", openTime=now - 7_200_000, finishTime=now - 3_600_000
        )
        client = self.make_client([FakeResponse({"list": [ended], "total": 1})])
        bot = self.make_force_bot(client)
        counts = bot.scan_once()
        self.assertEqual(counts["sessions"], 0)
        self.assertNotIn(("PUT", UPDATE_URL), client.http.urls())

        # --include-ended 时补签最近结束的场次
        client2 = self.make_client([
            FakeResponse({"list": [ended], "total": 1}),
            FakeResponse("detail-old"),
            FakeResponse(None),
        ])
        bot2 = self.make_force_bot(
            client2, include_ended=True, ended_within_minutes=120
        )
        counts = bot2.scan_once()
        self.assertEqual(counts["ok"], 1)
        self.assertEqual(client2.http.last_json()["id"], "detail-old")

    def test_force_dry_run_never_submits(self):
        client = self.make_client([
            FakeResponse({"list": [self.live_row()], "total": 1}),
            FakeResponse("detail-9"),
        ])
        bot = self.make_force_bot(client, dry_run=True)
        counts = bot.scan_once()
        self.assertEqual(counts["skip"], 1)
        self.assertNotIn(("PUT", UPDATE_URL), client.http.urls())

    def test_force_does_not_resubmit_done_session(self):
        client = self.make_client([
            FakeResponse({"list": [self.live_row()], "total": 1}),
            FakeResponse("detail-9"),
            FakeResponse(None),
            FakeResponse({"list": [self.live_row()], "total": 1}),  # 第二轮
        ])
        bot = self.make_force_bot(client)
        bot.scan_once()
        counts = bot.scan_once()
        self.assertEqual(counts["sessions"], 0)
        self.assertEqual(
            len([1 for _, u in client.http.urls() if u == UPDATE_URL]), 1
        )

    def test_force_checkin_flags_parse(self):
        args = build_parser().parse_args([
            "force-checkin",
            "--once", "--dry-run", "--interval", "3",
            "--course-id", "c-1", "--course-id", "c-2",
            "--term-id", "t-1", "--include-ended", "--ended-within", "60",
            "--sign-type", "0", "--no-verify", "--quiet",
        ])
        self.assertTrue(args.once)
        self.assertTrue(args.dry_run)
        self.assertEqual(args.interval, 3)
        self.assertEqual(args.course_id, ["c-1", "c-2"])
        self.assertEqual(args.term_id, "t-1")
        self.assertTrue(args.include_ended)
        self.assertEqual(args.ended_within, 60)
        self.assertEqual(args.sign_type, 0)
        self.assertTrue(args.no_verify)
        self.assertTrue(args.quiet)
        self.assertTrue(callable(args.func))

    def test_is_unsignable_error(self):
        from neumooc_checkin import is_unsignable_error

        self.assertTrue(is_unsignable_error(ApiError(1020065005, "考勤已结束")))
        self.assertTrue(is_unsignable_error(ApiError(-1, "考勤已结束")))
        self.assertTrue(is_unsignable_error(ApiError(-1, "考勤未开始")))
        self.assertFalse(is_unsignable_error(ApiError(500, "服务器开小差")))

    def test_force_payload_extra_fields(self):
        payload = build_force_sign_payload(
            "a", "d", "s", extra={"signTime": "2026-09-14 08:00:00"}
        )
        self.assertEqual(payload["signTime"], "2026-09-14 08:00:00")
        self.assertEqual(payload["signRole"], 4)
        self.assertNotIn("signTime", build_force_sign_payload("a", "d", "s"))

    def test_force_extra_fields_and_submit_path_override(self):
        client = self.make_client([
            FakeResponse({"list": [self.live_row()], "total": 1}),
            FakeResponse("detail-9"),
            FakeResponse(None),
        ])
        bot = self.make_force_bot(
            client,
            extra_fields={"signTime": "2026-09-14 08:00:00"},
            submit_path="/web-api/teachmanager/teach-course-attendance-detail/teacher/update",
        )
        counts = bot.scan_once()
        self.assertEqual(counts["ok"], 1)
        method, url = client.http.urls()[-1]
        self.assertEqual(method, "PUT")
        self.assertTrue(url.endswith("/teacher/update"))
        payload = client.http.last_json()
        self.assertEqual(payload["signTime"], "2026-09-14 08:00:00")
        self.assertEqual(payload["id"], "detail-9")

    def test_force_checkin_extra_flags_parse(self):
        args = build_parser().parse_args([
            "force-checkin",
            "--extra-fields", '{"signTime":"x"}',
            "--submit-path", "/x/y",
        ])
        self.assertEqual(args.extra_fields, '{"signTime":"x"}')
        self.assertEqual(args.submit_path, "/x/y")

    def test_force_teacher_fallback_on_ended_session(self):
        # 学生形态被拒（考勤已结束）→ 改用 signRole=3 + signUserId=教师ID 重试并成功
        client = self.make_client([
            FakeResponse({"list": [self.live_row(teacherId="teacher-1")], "total": 1}),
            FakeResponse("detail-9"),
            FakeResponse(None, code=1020065005, msg="考勤已结束"),  # 学生形态被拒
            FakeResponse(None),                                      # 教师形态成功
        ])
        bot = self.make_force_bot(client)
        counts = bot.scan_once()
        self.assertEqual(counts["ok"], 1)
        self.assertEqual(counts["skip"], 0)
        self.assertEqual(bot.attempted["force:att-9"], "done")
        puts = [c for c in client.http.calls if c[1] == UPDATE_URL]
        self.assertEqual(len(puts), 2)
        # 学生形态：signRole=4 + signUserId=学生
        self.assertEqual(puts[0][2]["json"]["signRole"], 4)
        self.assertEqual(puts[0][2]["json"]["signUserId"], "stu-1")
        self.assertEqual(puts[0][2]["json"]["id"], "detail-9")
        # 教师形态：Web 教师端补签的最小字段集（去掉 type/refreshSeed/坐标）
        self.assertEqual(puts[1][2]["json"], {
            "attendanceId": "att-9",
            "id": "detail-9",
            "status": 1,
            "signRole": 3,
            "signUserId": "teacher-1",
        })

    def test_teacher_style_payload_is_minimal(self):
        from neumooc_checkin import build_teacher_style_payload

        self.assertEqual(
            build_teacher_style_payload("att-1", "detail-1", "teacher-1"),
            {
                "attendanceId": "att-1",
                "id": "detail-1",
                "status": 1,
                "signRole": 3,
                "signUserId": "teacher-1",
            },
        )

    def test_force_payload_style_teacher_from_start(self):
        client = self.make_client([
            FakeResponse({"list": [self.live_row(teacherId="t-1")], "total": 1}),
            FakeResponse("detail-9"),
            FakeResponse(None),
        ])
        bot = self.make_force_bot(client, payload_style="teacher")
        counts = bot.scan_once()
        self.assertEqual(counts["ok"], 1)
        payload = client.http.last_json()
        self.assertEqual(payload, {
            "attendanceId": "att-9",
            "id": "detail-9",
            "status": 1,
            "signRole": 3,
            "signUserId": "t-1",
        })
        self.assertNotIn("type", payload)
        self.assertNotIn("refreshSeed", payload)

    def test_force_skips_already_signed_sessions(self):
        # 本人考勤列表里已签（signRole=4 + signTime）→ 不重复提交
        signed_row = {
            "id": "detail-9", "attendanceId": "att-9", "title": "已签过的场次",
            "status": 1, "signRole": 4, "signUserId": "stu-1",
            "signTime": 1789350930000, "signTimeString": "2026-09-14 10:00:00",
        }
        client = self.make_client([
            FakeResponse({"list": [signed_row], "total": 1}),        # ATT-01（已签过滤）
            FakeResponse({"list": [self.live_row("att-9")], "total": 1}),  # 教师端列表
        ])
        bot = self.make_force_bot(client, skip_signed=True)
        counts = bot.scan_once()
        self.assertEqual(counts["signed"], 1)
        self.assertEqual(counts["sessions"], 0)
        self.assertEqual(counts["ok"], 0)
        self.assertNotIn(("PUT", UPDATE_URL), client.http.urls())
        self.assertEqual(bot.attempted["force:att-9"], "done")

    def test_force_no_skip_signed_flag_parse(self):
        args = build_parser().parse_args(["force-checkin", "--no-skip-signed"])
        self.assertTrue(args.no_skip_signed)
        self.assertFalse(
            build_parser().parse_args(["force-checkin"]).no_skip_signed
        )

    def test_force_payload_style_flag_parse(self):
        args = build_parser().parse_args(
            ["force-checkin", "--payload-style", "teacher"]
        )
        self.assertEqual(args.payload_style, "teacher")
        self.assertEqual(
            build_parser().parse_args(["force-checkin"]).payload_style, "full"
        )

    def test_force_teacher_fallback_uses_explicit_teacher_user_id(self):
        client = self.make_client([
            FakeResponse({"list": [self.live_row()], "total": 1}),  # 行里没有教师字段
            FakeResponse("detail-9"),
            FakeResponse(None, code=1020065005, msg="考勤已结束"),
            FakeResponse(None),
        ])
        bot = self.make_force_bot(client, teacher_user_id="teacher-9")
        counts = bot.scan_once()
        self.assertEqual(counts["ok"], 1)
        puts = [c for c in client.http.calls if c[1] == UPDATE_URL]
        self.assertEqual(puts[1][2]["json"]["signUserId"], "teacher-9")
        self.assertEqual(puts[1][2]["json"]["signRole"], 3)

    def test_force_teacher_fallback_defaults_to_current_sign_user_id(self):
        # 拿不到教师 ID 时不跳过：signUserId 默认沿用当前用户 ID，直接提交教师形态
        client = self.make_client([
            FakeResponse({"list": [self.live_row()], "total": 1}),
            FakeResponse("detail-9"),
            FakeResponse(None, code=1020065005, msg="考勤已结束"),
            FakeResponse({}),   # teach-course/get：没有 teacherId
            FakeResponse(None),
        ])
        bot = self.make_force_bot(client)
        counts = bot.scan_once()
        self.assertEqual(counts["ok"], 1)
        self.assertEqual(bot.attempted["force:att-9"], "done")
        puts = [c for c in client.http.calls if c[1] == UPDATE_URL]
        self.assertEqual(len(puts), 2)
        self.assertEqual(puts[1][2]["json"]["signRole"], 3)
        self.assertEqual(puts[1][2]["json"]["signUserId"], "stu-1")

    def test_force_reopen_ended_signs_and_restores(self):
        # 已结束场次：临时重开考勤窗口 → signRole=3 补签 → 立即还原原 status/finishTime
        from unittest.mock import patch

        client = self.make_client([
            FakeResponse({"list": [], "total": 0}),                  # 本人考勤列表
            FakeResponse({"list": [self.live_row(teacherId="t-1")], "total": 1}),  # 教师端列表
            FakeResponse("detail-9"),                                # ATT-03
            FakeResponse(None, code=1020065005, msg="考勤已结束"),    # 学生形态被拒
            FakeResponse({"id": "att-9", "status": 2,               # 考勤主表详情
                          "finishTime": 1789350930000}),
            FakeResponse(None),                                      # 重开（status=1）
            FakeResponse(None),                                      # signRole=3 补签
            FakeResponse(None),                                      # 还原（status=2）
        ])
        bot = self.make_force_bot(client, reopen_ended=True, reopen_seconds=30)
        with patch("neumooc_checkin.time.sleep"):
            counts = bot.scan_once()
        self.assertEqual(counts["ok"], 1)
        self.assertEqual(bot.attempted["force:att-9"], "done")

        att_updates = [
            c[2]["json"] for c in client.http.calls
            if c[1].endswith("/teach-course-attendance/update")
        ]
        self.assertEqual(len(att_updates), 2)
        self.assertEqual(att_updates[0]["status"], 1)        # 重开
        self.assertEqual(att_updates[0]["id"], "att-9")
        self.assertEqual(att_updates[1]["status"], 2)        # 还原原 status
        self.assertEqual(att_updates[1]["finishTime"], 1789350930000)

        sign_payloads = [c[2]["json"] for c in client.http.calls if c[1] == UPDATE_URL]
        self.assertEqual(sign_payloads[-1], {
            "attendanceId": "att-9", "id": "detail-9", "status": 1,
            "signRole": 3, "signUserId": "t-1",     # signUserId 用教师 id
        })

    def test_force_reopen_writes_back_original_sign_time(self):
        # 补签要写回该场原有的 signTime（别把老师那次的修改时间改成"现在"）
        from unittest.mock import patch

        own_row = {
            "id": "detail-9", "attendanceId": "att-9", "title": "教师考勤",
            "status": 2, "signRole": None, "signUserId": None,
            "signTime": 1789344729000, "signTimeString": "2026-09-14 08:12:09",
        }
        client = self.make_client([
            FakeResponse({"list": [own_row], "total": 1}),            # 本人考勤列表
            FakeResponse({"list": [self.live_row(teacherId="t-1")], "total": 1}),
            FakeResponse("detail-9"),
            FakeResponse(None, code=1020065005, msg="考勤已结束"),
            FakeResponse({"id": "att-9", "status": 2, "finishTime": 1789350930000}),
            FakeResponse(None),                                      # 重开
            FakeResponse(None),                                      # 补签
            FakeResponse(None),                                      # 还原
        ])
        # skip_signed=False（不因"已有 signTime"跳过），但 reopen 仍会拉本人列表取原 signTime
        bot = self.make_force_bot(client, reopen_ended=True, skip_signed=False)
        with patch("neumooc_checkin.time.sleep"):
            counts = bot.scan_once()
        self.assertEqual(counts["sessions"], 1)
        sign_payloads = [c[2]["json"] for c in client.http.calls if c[1] == UPDATE_URL]
        self.assertEqual(sign_payloads[-1]["signTime"], 1789344729000)
        self.assertEqual(sign_payloads[-1]["signUserId"], "t-1")

    def test_force_reopen_restores_even_when_sign_fails(self):
        from unittest.mock import patch

        client = self.make_client([
            FakeResponse({"list": [], "total": 0}),                  # 本人考勤列表
            FakeResponse({"list": [self.live_row(teacherId="t-1")], "total": 1}),
            FakeResponse("detail-9"),
            FakeResponse(None, code=1020065005, msg="考勤已结束"),
            FakeResponse({"id": "att-9", "status": 2, "finishTime": 1789350930000}),
            FakeResponse(None),                                      # 重开
            FakeResponse(None, code=500, msg="服务器开小差"),           # 补签失败
            FakeResponse(None),                                      # 仍要还原
        ])
        bot = self.make_force_bot(client, reopen_ended=True)
        with patch("neumooc_checkin.time.sleep"):
            counts = bot.scan_once()
        self.assertEqual(counts["fail"], 1)
        att_updates = [
            c[2]["json"] for c in client.http.calls
            if c[1].endswith("/teach-course-attendance/update")
        ]
        self.assertEqual(len(att_updates), 2)                     # 重开 + 还原
        self.assertEqual(att_updates[1]["status"], 2)
        self.assertEqual(att_updates[1]["finishTime"], 1789350930000)

    def test_force_reopen_flags_parse(self):
        args = build_parser().parse_args([
            "force-checkin", "--reopen-ended",
            "--reopen-seconds", "30", "--sign-status", "2",
        ])
        self.assertTrue(args.reopen_ended)
        self.assertEqual(args.reopen_seconds, 30)
        self.assertEqual(args.sign_status, 2)
        defaults = build_parser().parse_args(["force-checkin"])
        self.assertFalse(defaults.reopen_ended)
        self.assertEqual(defaults.reopen_seconds, 180)
        self.assertEqual(defaults.sign_status, 1)

    def test_force_teacher_fallback_can_be_disabled(self):
        client = self.make_client([
            FakeResponse({"list": [self.live_row()], "total": 1}),
            FakeResponse("detail-9"),
            FakeResponse(None, code=1020065005, msg="考勤已结束"),
        ])
        bot = self.make_force_bot(client, teacher_fallback=False)
        counts = bot.scan_once()
        self.assertEqual(counts["ok"], 0)
        self.assertEqual(counts["fail"], 0)
        self.assertEqual(counts["skip"], 1)
        self.assertEqual(bot.attempted["force:att-9"], "unsignable")
        self.assertEqual(
            len([1 for _, u in client.http.urls() if u == UPDATE_URL]), 1
        )

    def test_force_sign_role_three_used_directly(self):
        client = self.make_client([
            FakeResponse({"list": [self.live_row()], "total": 1}),
            FakeResponse("detail-9"),
            FakeResponse(None),
        ])
        bot = self.make_force_bot(
            client, sign_role=3, teacher_user_id="teacher-9"
        )
        counts = bot.scan_once()
        self.assertEqual(counts["ok"], 1)
        payload = client.http.last_json()
        self.assertEqual(payload["signRole"], 3)
        self.assertEqual(payload["signUserId"], "teacher-9")
        self.assertEqual(payload["id"], "detail-9")

    def test_force_marks_ended_session_unsignable_and_stops_retrying(self):
        # 教师形态也失败时：标记为不可签，不计失败、不再重试
        client = self.make_client([
            FakeResponse({"list": [self.live_row()], "total": 1}),
            FakeResponse("detail-9"),
            FakeResponse(None, code=1020065005, msg="考勤已结束"),
            FakeResponse(None, code=1020065005, msg="考勤已结束"),
        ])
        bot = self.make_force_bot(client, teacher_user_id="teacher-9")
        counts = bot.scan_once()
        self.assertEqual(counts["ok"], 0)
        self.assertEqual(counts["fail"], 0)
        self.assertEqual(counts["skip"], 1)
        self.assertEqual(bot.attempted["force:att-9"], "unsignable")

        # 下一轮同一场次直接跳过，不再提交
        client.http.responses.append(
            FakeResponse({"list": [self.live_row()], "total": 1})
        )
        counts = bot.scan_once()
        self.assertEqual(counts["sessions"], 0)
        # 学生形态 + 教师形态各一次，共 2 次 PUT，且后续不再增加
        self.assertEqual(
            len([1 for _, u in client.http.urls() if u == UPDATE_URL]), 2
        )

    def test_courses_flags_parse(self):
        args = build_parser().parse_args(["courses", "--term-id", "t-1"])
        self.assertEqual(args.term_id, "t-1")
        self.assertTrue(callable(args.func))

    def test_script_mode_keeps_single_api_error_class(self):
        """以 `python neumooc_login.py ...` 运行时不能出现两份 ApiError。

        脚本以 __main__ 运行，而 neumooc_checkin 会 `from neumooc_login import
        ApiError`；若不把 __main__ 注册成 neumooc_login，就会加载出第二份模块，
        导致 neumooc_checkin 里的 `except ApiError` 永远匹配不上（业务错误直接
        冒泡成“接口错误：[code] msg”）。
        """
        import subprocess
        import sys as _sys

        root = Path(__file__).resolve().parent.parent
        script = (
            "import sys\n"
            "sys.argv = ['neumooc_login.py', 'no-such-command']\n"
            "import runpy\n"
            "try:\n"
            "    runpy.run_path(r'%s', run_name='__main__')\n"
            "except SystemExit:\n"
            "    pass\n"
            "main_mod = sys.modules.get('neumooc_login')\n"
            "import neumooc_checkin\n"
            "ok = (\n"
            "    main_mod is not None\n"
            "    and getattr(main_mod, '__name__', '') == '__main__'\n"
            "    and neumooc_checkin.ApiError is main_mod.ApiError\n"
            ")\n"
            "print('ALIASED' if ok else 'SPLIT')\n"
        ) % (root / "neumooc_login.py")
        out_file = self.make_temp_dir() / "out.txt"
        with open(out_file, "w", encoding="utf-8") as fh:
            subprocess.run(
                [_sys.executable, "-c", script],
                stdout=fh, stderr=subprocess.STDOUT, cwd=str(root),
            )
        self.assertIn("ALIASED", out_file.read_text(encoding="utf-8"))

    def test_print_courses_lists_teach_course_id(self):
        import contextlib
        import io

        from neumooc_login import _print_courses

        client = self.make_client([
            FakeResponse([
                {"teachCourseId": "c-1", "teachCourseName": "数据结构"},
                {"teachCourseId": "c-2", "teachCourseName": "操作系统"},
            ]),
        ])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _print_courses(client, term_id="t-1")
        out = buf.getvalue()
        self.assertIn("c-1", out)
        self.assertIn("数据结构", out)
        self.assertIn("c-2", out)
        # 指定学期时不应再调用学期下拉接口
        self.assertEqual(len(client.http.calls), 1)


if __name__ == "__main__":
    unittest.main()
