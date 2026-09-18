import json
import shutil
import unittest
import uuid
from pathlib import Path

from argparse import Namespace

from neumooc_login import (
    DEFAULT_BUSINESS_BASE,
    LEGACY_BUSINESS_BASE,
    ApiError,
    NeumoocClient,
    _attendance_publish_payload,
)

# 不用 tempfile.mkdtemp/TemporaryDirectory：其 0700 限制性 ACL
# 在部分受控环境（沙箱）下无法写入，改用普通权限的独立目录
_TEMP_ROOT = Path(__file__).resolve().parent / "_tmp_run"


class FakeResponse:
    def __init__(self, payload=None, *, status=200, code=0, msg="", content=b""):
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


class NeumoocClientTests(unittest.TestCase):
    def make_temp_dir(self):
        _TEMP_ROOT.mkdir(exist_ok=True)
        path = _TEMP_ROOT / uuid.uuid4().hex[:12]
        path.mkdir()
        self.addCleanup(shutil.rmtree, path, True)
        return path

    def make_client(self, *, saved=None, credentials=None):
        directory = self.make_temp_dir()
        token_file = directory / "token.json"
        if saved is not None:
            token_file.write_text(json.dumps(saved), encoding="utf-8")
        credential_file = directory / "credentials.json"
        if credentials is not None:
            text = credentials if isinstance(credentials, str) else json.dumps(credentials)
            credential_file.write_text(text, encoding="utf-8")
        client = NeumoocClient(
            token_file=str(token_file), credential_file=str(credential_file)
        )
        client.http = FakeSession()
        return client

    def test_default_domain_and_legacy_cache_migration(self):
        client = self.make_client(saved={"base": LEGACY_BUSINESS_BASE})
        self.assertEqual(DEFAULT_BUSINESS_BASE, "https://study.neusoft.edu.cn")
        self.assertEqual(client.base, DEFAULT_BUSINESS_BASE)

    def test_custom_cached_domain_is_preserved(self):
        client = self.make_client(saved={"base": "https://custom.example"})
        self.assertEqual(client.base, "https://custom.example")

    def test_message_array_body_and_common_headers(self):
        client = self.make_client()
        client.access_token = "access"
        client.tenant_id = 12
        client.mark_messages_read([1, 2])
        method, url, kwargs = client.http.calls[-1]
        self.assertEqual(method, "PUT")
        self.assertEqual(url, DEFAULT_BUSINESS_BASE + "/web-api/system/notify-target/update-read")
        self.assertEqual(kwargs["json"], [1, 2])
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer access")
        self.assertEqual(kwargs["headers"]["Tenant-Id"], "12")
        # Id-Code = base64(AES-CBC(key=iv="neuedu_nse_12345", 零填充("{userId}_{uuid}_{path}")))
        self.assert_id_code(
            kwargs["headers"]["Id-Code"],
            "/web-api/system/notify-target/update-read",
        )

    def assert_id_code(self, code: str, path: str) -> None:
        """解密 Id-Code 回验：明文应为 {用户ID}_{uuid}_{请求路径}。"""
        import base64 as _b64

        from Crypto.Cipher import AES  # type: ignore

        raw = AES.new(
            b"neuedu_nse_12345", AES.MODE_CBC, b"neuedu_nse_12345"
        ).decrypt(_b64.b64decode(code))
        text = raw.rstrip(b"\x00").decode("utf-8")
        self.assertTrue(text.startswith("0_"), text)
        self.assertTrue(text.endswith(path), text)
        self.assertEqual(len(text.split("_")[1]), 36)   # uuid

    def test_representative_routes_from_all_groups(self):
        client = self.make_client()
        checks = [
            (lambda: client.check_forget_password({"mobile": "1"}), "POST", "/web-api/system/auth/check/forgetPassword"),
            (lambda: client.update_profile({"nickname": "n"}), "PUT", "/web-api/system/user/profile/app/update"),
            (lambda: client.bind_device({"deviceId": "d"}), "POST", "/web-api/system/user-device/bind"),
            (client.get_unread_message_count, "GET", "/web-api/system/notify-target/get-unread-count"),
            (lambda: client.report_app_crash({"message": "x"}), "POST", "/web-api/infra/app-crash-log/add"),
            (client.get_term_options, "GET", "/web-api/teachmanager/teach-dropdown/getTeachTermDropDown"),
            (lambda: client.get_student_attendance_detail("a/b"), "GET", "/web-api/teachmanager/teach-course-attendance-detail/getAppStuAttendanceDetail/a%2Fb"),
            (lambda: client.start_study({"resourceId": 1}), "POST", "/web-api/teachmanager/teach-course-res-stu-record/startStudy"),
            (lambda: client.get_video_path("eid"), "GET", "/content/search/getVideoPath"),
        ]
        for invoke, expected_method, expected_path in checks:
            with self.subTest(path=expected_path):
                invoke()
                method, url, _ = client.http.calls[-1]
                self.assertEqual(method, expected_method)
                self.assertEqual(url, DEFAULT_BUSINESS_BASE + expected_path)

    def test_qr_validation_requires_literal_true(self):
        client = self.make_client()
        client.http.responses = [FakeResponse(True), FakeResponse(1)]
        self.assertTrue(client.check_qr_code_valid(1, "q"))
        self.assertFalse(client.check_qr_code_valid(1, "q"))

    def test_binary_download(self):
        client = self.make_client()
        client.http.responses = [FakeResponse(content=b"abc")]
        self.assertEqual(client.get_file_download("e1"), b"abc")
        _, _, kwargs = client.http.calls[-1]
        self.assertEqual(kwargs["params"], {"eid": "e1", "source": "NEUNSE"})

    def test_batch_upload_uses_multipart_and_closes_files(self):
        client = self.make_client()
        directory = self.make_temp_dir()
        source = directory / "a.txt"
        source.write_bytes(b"hello")
        client.batch_upload_files([source])
        method, url, kwargs = client.http.calls[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(url, DEFAULT_BUSINESS_BASE + "/content/s3/batchUpload")
        self.assertEqual(kwargs["data"]["source"], "NEUNSE")
        self.assertEqual(kwargs["files"][0][0], "files")
        self.assertTrue(kwargs["files"][0][1][1].closed)

    def test_publish_attendance_route_and_qr_payload(self):
        client = self.make_client()
        args = Namespace(
            duration=10, type="qr", title="测试签到", dir_id="",
            course_id="course-1", class_id="class-1", location_type=0,
            longitude=None, latitude=None, address=None, radius=300,
            scheduled_at=None, qr_refresh=10,
        )
        payload = _attendance_publish_payload(args)
        self.assertEqual(payload["type"], 1)
        self.assertEqual(payload["qrCodeRefreshFrequency"], "10")
        self.assertEqual(payload["finishTime"] - payload["openTime"], 600_000)
        client.publish_attendance(payload)
        method, url, kwargs = client.http.calls[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(
            url,
            DEFAULT_BUSINESS_BASE + "/web-api/teachmanager/teach-course-attendance/create",
        )
        self.assertEqual(kwargs["json"]["teachClassId"], "class-1")

    def test_save_and_load_credentials(self):
        client = self.make_client()
        client.tenant_id = "123"
        client.save_credentials("stu-1", "pwd")
        creds = client.load_credentials()
        self.assertEqual(
            creds, {"username": "stu-1", "password": "pwd", "tenantId": "123"}
        )

    def test_401_refresh_failure_relogins_from_credentials(self):
        client = self.make_client(
            credentials={"username": "stu-1", "password": "pwd", "tenantId": "123"}
        )
        client.access_token = "expired"
        client.refresh_token = "refresh"
        client.tenant_id = "123"
        client.http.responses = [
            FakeResponse(code=401, msg="token 过期"),      # 原始请求 401
            FakeResponse(code=401, msg="refresh 失败"),    # 刷新 401
            FakeResponse({                                 # 凭据重新登录
                "accessToken": "new-access",
                "refreshToken": "new-refresh",
                "userId": "stu-1",
            }),
            FakeResponse({"name": "张三"}),                # 重试成功
        ]
        result = client.get_profile()
        self.assertEqual(result, {"name": "张三"})
        self.assertEqual(client.access_token, "new-access")
        self.assertEqual(client.user_id, "stu-1")
        urls = [u for _, u, _ in client.http.calls]
        self.assertIn("/system/auth/app/refresh-token", urls[1])
        self.assertIn("/system/auth/app/login", urls[2])

    def test_401_without_credentials_clears_session_and_raises(self):
        client = self.make_client()  # 无凭据
        client.access_token = "expired"
        client.refresh_token = None
        client.http.responses = [FakeResponse(code=401, msg="token 过期")]
        with self.assertRaises(ApiError):
            client.get_profile()
        self.assertIsNone(client.access_token)

    def test_ensure_logged_in_relogins_when_no_token(self):
        client = self.make_client(
            credentials={"username": "stu-1", "password": "pwd", "tenantId": "123"}
        )
        client.http.responses = [
            FakeResponse({"accessToken": "a", "refreshToken": "r", "userId": "u"})
        ]
        self.assertTrue(client.ensure_logged_in())
        self.assertEqual(client.access_token, "a")
        self.assertEqual(client.user_id, "u")


if __name__ == "__main__":
    unittest.main()
