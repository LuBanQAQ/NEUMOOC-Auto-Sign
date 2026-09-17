#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
东软智慧教育 App 全量接口客户端
===============================

依据《东软智慧教育 App 全量接口文档》（APK v1.0.74，应用 ID __UNI__F603DEF
静态分析结果）实现认证、用户、消息、教学、考勤、学习记录与文件服务接口。

    AUTH-01  获取租户/学校选项  GET  http://studydev3.neuedu.com/web-api/system/tenant/app/option
    AUTH-02  账号密码登录       POST {业务域名}/web-api/system/auth/app/login
    AUTH-03  获取手机验证码     GET  {业务域名}/web-api/system/auth/app/getVerificationCode/{phoneNumber}
    AUTH-04  验证码登录         POST {业务域名}/web-api/system/auth/verificationCode/login
    AUTH-05  刷新访问令牌       POST https://studytest3.neumooc.com/web-api/system/auth/app/refresh-token
    USR-01   获取当前用户资料   GET  {业务域名}/web-api/system/user/profile/get   （登录后验证令牌）

遵循文档第 3 节公共约定：
    * 请求头 Authorization / Tenant-Id / Id-Code / Content-Type: application/json；
    * 响应结构 {code, msg, data}，code = 0 视为成功；
    * code = 401 时自动刷新令牌并重试一次，刷新失败则清空本地登录信息。

静态分析无法确认、需要联调调整的点（集中在下方“常量区”）：
    1. 登录请求体字段（APK 端为页面“对象透传”），本脚本默认 username/password、
       phoneNumber/code，与服务端不一致时直接改常量即可；
    2. Id-Code 的生成算法未公开，此处生成 32 位十六进制占位值；
    3. 登录/刷新响应中的令牌字段名按 accessToken / refreshToken / userId 兼容解析。

快速上手：
    pip install -r requirements.txt
    python neumooc_login.py                                 # 直接回车进入交互式菜单（推荐）
    python neumooc_login.py tenants                          # 查看学校/租户列表
    python neumooc_login.py login -u 学号 -p 密码 -t 租户ID  # 账号密码登录
    python neumooc_login.py sms --phone 手机号 -t 租户ID     # 发送短信验证码
    python neumooc_login.py sms-login --phone 手机号 --code 验证码 -t 租户ID
    python neumooc_login.py profile                          # 校验已保存的令牌
    python neumooc_login.py refresh                          # 手动刷新令牌
    python neumooc_login.py auto-checkin --once --dry-run    # 学生端自动签到（演练）

    全局参数需放在子命令之前，例如：
    python neumooc_login.py --debug login -u 学号 -p 密码

    说明：交互模式中租户 ID 为手动输入（AUTH-01 列表接口在部分网络环境下不可达，
    会导致卡顿）；如仍想拉取学校列表可使用子命令 tenants。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union
from urllib.parse import quote, urlsplit

import requests

try:  # --insecure 时关闭告警
    import urllib3
except ImportError:  # pragma: no cover
    urllib3 = None  # type: ignore

# ============================================================
# 常量区（联调时按需调整）
# ============================================================
DEFAULT_BUSINESS_BASE = "https://study.neusoft.edu.cn"   # 当前业务域名，可被 website 覆盖
LEGACY_BUSINESS_BASE = "https://neustudy.neumooc.com"    # 旧默认值，仅用于缓存迁移
TENANT_OPTION_BASE = "http://studydev3.neuedu.com"       # 文档：AUTH-01 使用固定域名
AUTH_REFRESH_BASE = "https://studytest3.neumooc.com"     # 文档：令牌刷新使用该域名
WEB_API = "/web-api"
DEFAULT_TOKEN_FILE = "neumooc_token.json"
DEFAULT_CREDENTIAL_FILE = "neumooc_credentials.json"

# ---- 登录请求体字段名（文档标注“对象透传”，具体以联调为准）----
USERNAME_KEY = "username"        # 密码登录账号字段，备选：account / mobile / studentNumber
PASSWORD_KEY = "password"        # 密码登录密码字段
PHONE_KEY = "phoneNumber"        # 短信登录手机号字段，备选：mobile
SMS_CODE_KEY = "code"            # 短信登录验证码字段，备选：smsCode / verificationCode

# 需要额外并入登录请求体的字段（如 grantType / clientId 等），按需填写
EXTRA_PASSWORD_LOGIN_FIELDS: Dict[str, Any] = {}
EXTRA_SMS_LOGIN_FIELDS: Dict[str, Any] = {}

# ---- 响应中令牌字段名兼容列表（文档未确认确切字段名）----
ACCESS_TOKEN_KEYS = ("accessToken", "access_token", "token", "tokenValue")
REFRESH_TOKEN_KEYS = ("refreshToken", "refresh_token")
USER_ID_KEYS = ("userId", "user_id", "id")

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36 uni-app"
)


class ApiError(Exception):
    """业务/协议错误（HTTP 非 2xx、业务 code != 0、响应结构异常等）"""

    def __init__(self, code: Any, msg: str, data: Any = None):
        super().__init__(f"[{code}] {msg}")
        self.code = code
        self.msg = msg
        self.data = data


def _first(source: Dict[str, Any], keys) -> Any:
    """返回字典中第一个存在且非 None 的键值"""
    for key in keys:
        if key in source and source[key] is not None:
            return source[key]
    return None


def _mask(value: Optional[str]) -> str:
    if not value:
        return "<空>"
    text = str(value)
    if len(text) <= 16:
        return text[:4] + "****"
    return f"{text[:10]}...{text[-6:]}"


class NeumoocClient:
    """东软智慧教育 App 全量接口客户端。"""

    def __init__(
        self,
        website: Optional[str] = None,
        tenant_id: Optional[Any] = None,
        token_file: Optional[str] = None,
        credential_file: Optional[str] = None,
        debug: bool = False,
        insecure: bool = False,
        no_proxy: bool = False,
        timeout: float = 20.0,
    ):
        self._explicit_base = website is not None
        self.base = (website or DEFAULT_BUSINESS_BASE).rstrip("/")
        self.tenant_id = tenant_id
        self.debug = debug
        # (连接超时 5 秒, 读取超时)：网络不通时快速报错，避免界面长时间“卡死”
        self.timeout = (5.0, timeout)
        self.token_file = Path(token_file or DEFAULT_TOKEN_FILE)
        self.credential_file = Path(credential_file or DEFAULT_CREDENTIAL_FILE)

        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.user_id: Optional[Any] = None

        self.http = requests.Session()
        # Content-Type 由 requests 按 JSON / 表单 / multipart 自动生成，避免上传时残留 JSON 类型。
        self.http.headers.update({"User-Agent": USER_AGENT})
        if no_proxy:
            # 绕过系统/环境变量代理：部分本地代理对明文 HTTP 或内网域名会长时间挂起
            self.http.trust_env = False
        if insecure:
            self.http.verify = False
            if urllib3 is not None:
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        self._load_session()

    # ------------------------------------------------------------
    # 会话持久化
    # ------------------------------------------------------------
    def save_session(self) -> None:
        payload = {
            "base": self.base,
            "tenantId": self.tenant_id,
            "accessToken": self.access_token,
            "refreshToken": self.refresh_token,
            "userId": self.user_id,
        }
        try:
            self.token_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            print(f"[警告] 令牌文件写入失败({exc})：{self.token_file}", file=sys.stderr)

    def _load_session(self) -> None:
        if not self.token_file.exists():
            return
        try:
            saved = json.loads(self.token_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(saved, dict):
            return
        self.access_token = saved.get("accessToken") or self.access_token
        self.refresh_token = saved.get("refreshToken") or self.refresh_token
        self.user_id = saved.get("userId") or self.user_id
        if self.tenant_id is None:
            self.tenant_id = saved.get("tenantId")
        # 未显式指定域名时沿用自定义地址；旧版默认地址自动迁移到当前业务域名。
        if not self._explicit_base and saved.get("base"):
            saved_base = str(saved["base"]).rstrip("/")
            if saved_base != LEGACY_BUSINESS_BASE:
                self.base = saved_base

    def _clear_session(self) -> None:
        """文档：刷新失败则清除本地登录信息"""
        self.access_token = None
        self.refresh_token = None
        self.user_id = None
        try:
            if self.token_file.exists():
                self.token_file.unlink()
        except OSError:
            pass

    def logout(self) -> None:
        """清除本地登录信息（仅本地，不调用服务端接口）"""
        self._clear_session()

    # ------------------------------------------------------------
    # 登录凭据（可选，用于令牌失效后自动重新登录）
    # ------------------------------------------------------------
    def load_credentials(self) -> Optional[Dict[str, Any]]:
        """读取保存的账号密码凭据（明文密码，注意文件权限）。"""
        if not self.credential_file.exists():
            return None
        try:
            data = json.loads(self.credential_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def save_credentials(
        self, username: str, password: str, tenant_id: Optional[Any] = None
    ) -> None:
        """把账号密码写入凭据文件，供登录态失效后自动重新登录。"""
        payload = {
            "username": username,
            "password": password,
            "tenantId": tenant_id if tenant_id is not None else self.tenant_id,
        }
        try:
            self.credential_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            print(f"[警告] 凭据文件写入失败({exc})：{self.credential_file}", file=sys.stderr)
            return
        print(
            f"[提示] 已保存明文密码到 {self.credential_file.resolve()}，"
            "Linux 下建议 chmod 600 收紧权限"
        )

    # ------------------------------------------------------------
    # 请求核心（文档第 3 节公共约定）
    # ------------------------------------------------------------
    def _id_code(self, path: str) -> str:
        """Id-Code：官方客户端动态生成，算法未公开。
        按文档描述与「用户 ID + 随机请求 ID + 接口路径」相关，此处生成 32 位十六进制占位值。"""
        seed = f"{self.user_id if self.user_id is not None else 0}|{uuid.uuid4()}|{path}"
        return hashlib.md5(seed.encode("utf-8")).hexdigest()

    def _url(self, path: str, *, base: Optional[str] = None) -> str:
        """把文档中的绝对路径拼接成请求地址，同时允许调用方传完整 URL。"""
        if path.startswith(("http://", "https://")):
            return path
        return f"{(base or self.base).rstrip('/')}/{path.lstrip('/')}"

    @staticmethod
    def _segment(value: Any) -> str:
        """安全编码路径参数，避免手机号、ID 等意外改变 URL 结构。"""
        return quote(str(value), safe="")

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Any = None,
        data: Any = None,
        files: Any = None,
        with_token: bool = True,
        allow_refresh: bool = True,
        _retried: bool = False,
        raw: bool = False,
    ) -> Any:
        headers: Dict[str, str] = {}
        if self.tenant_id is not None:
            tenant = str(self.tenant_id)
            if not tenant.isascii():
                # HTTP 请求头仅允许 latin-1，含中文会抛 UnicodeEncodeError
                raise ApiError(
                    -1,
                    f"Tenant-Id 含非 ASCII 字符（{tenant}），"
                    "请用菜单 3 设置数字租户 ID 后重试",
                )
            headers["Tenant-Id"] = tenant
        # 文档说明 Id-Code 与接口路径相关，避免把可变域名和查询串混入占位算法。
        headers["Id-Code"] = self._id_code(urlsplit(url).path)
        if with_token and self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        if self.debug:
            shown = dict(headers)
            if "Authorization" in shown:
                shown["Authorization"] = shown["Authorization"][:24] + "..."
            print(f"-> {method} {url}")
            if params:
                print(f"   query : {params}")
            if json_body is not None:
                body = dict(json_body) if isinstance(json_body, dict) else json_body
                if isinstance(body, dict):
                    for secret in (PASSWORD_KEY,):
                        if secret in body:
                            body[secret] = "******"
                print(f"   body  : {json.dumps(body, ensure_ascii=False)}")
            if files:
                print("   files : <multipart 文件内容已隐藏>")
            print(f"   headers: {shown}")

        response = self.http.request(
            method, url, params=params, json=json_body, data=data, files=files,
            headers=headers, timeout=self.timeout,
        )

        if self.debug:
            if raw:
                print(f"<- HTTP {response.status_code} <二进制响应 {len(response.content)} 字节>")
            else:
                print(f"<- HTTP {response.status_code} {response.text[:1500]}")

        # 文档：HTTP 状态码必须处于 200-299
        if not 200 <= response.status_code < 300:
            raise ApiError(response.status_code, f"HTTP 状态异常，body={response.text[:200]}")
        if raw:
            return response.content
        try:
            result = response.json()
        except ValueError:
            raise ApiError(-1, f"响应不是 JSON：{response.text[:200]}") from None
        if not isinstance(result, dict) or "code" not in result:
            raise ApiError(-1, f"响应缺少 code 字段：{response.text[:200]}")

        code = result.get("code")
        if code == 0:
            return result.get("data")

        # 文档：code = 401 时先尝试刷新令牌并重试一次；刷新失败则用保存的
        # 账号密码自动重新登录后再重试一次；仍失败才清除会话并抛错。
        if code == 401 and with_token and allow_refresh and not _retried:
            recovered = False
            if self.refresh_token:
                try:
                    self.refresh_access_token()
                    recovered = True
                except ApiError:
                    recovered = False
            if not recovered:
                recovered = self._relogin_with_credentials()
            if not recovered:
                self._clear_session()
                raise ApiError(
                    code, str(result.get("msg") or "未知业务错误"), result.get("data")
                )
            return self._request(
                method, url, params=params, json_body=json_body, data=data, files=files,
                with_token=with_token, allow_refresh=False, _retried=True,
            )

        raise ApiError(code, str(result.get("msg") or "未知业务错误"), result.get("data"))

    def request_api(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        body: Any = None,
        with_token: bool = True,
        raw: bool = False,
    ) -> Any:
        """调用文档路径的公共入口，适合接口联调和未来新增接口。"""
        return self._request(
            method.upper(), self._url(path), params=params, json_body=body,
            with_token=with_token, raw=raw,
        )

    # ------------------------------------------------------------
    # 令牌解析
    # ------------------------------------------------------------
    def _extract_tokens(self, data: Dict[str, Any]) -> None:
        access = _first(data, ACCESS_TOKEN_KEYS)
        refresh = _first(data, REFRESH_TOKEN_KEYS)
        user_id = _first(data, USER_ID_KEYS)
        if access:
            self.access_token = str(access)
        if refresh:
            self.refresh_token = str(refresh)
        if user_id is not None:
            self.user_id = user_id

    def _absorb_login_result(self, data: Any) -> None:
        if isinstance(data, dict):
            self._extract_tokens(data)
        elif isinstance(data, str) and data:
            self.access_token = data
        if not self.access_token:
            raise ApiError(
                -1,
                "登录返回 code=0，但未识别出访问令牌字段；"
                "请加 --debug 查看原始响应，并调整常量区的 *_TOKEN_KEYS",
            )
        self.save_session()

    # ============================================================
    # AUTH-01 获取租户/学校选项
    # ============================================================
    def get_tenant_options(self) -> List[Dict[str, Any]]:
        url = f"{TENANT_OPTION_BASE}{WEB_API}/system/tenant/app/option"
        data = self._request("GET", url, with_token=False)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("list", "records", "rows", "data"):
                inner = data.get(key)
                if isinstance(inner, list):
                    return [item for item in inner if isinstance(item, dict)]
            return [data]
        return []

    # ============================================================
    # AUTH-02 账号密码登录
    # ============================================================
    def login_with_password(
        self, username: str, password: str, tenant_id: Optional[Any] = None
    ) -> Any:
        if tenant_id is not None:
            self.tenant_id = tenant_id
        url = f"{self.base}{WEB_API}/system/auth/app/login"
        payload: Dict[str, Any] = {USERNAME_KEY: username, PASSWORD_KEY: password}
        if self.tenant_id is not None and str(self.tenant_id).isdigit():
            payload["tenantId"] = int(self.tenant_id)  # 数字形式更通用
        payload.update(EXTRA_PASSWORD_LOGIN_FIELDS)
        data = self._request("POST", url, json_body=payload, with_token=False)
        self._absorb_login_result(data)
        return data

    # ============================================================
    # AUTH-03 获取手机登录验证码
    # ============================================================
    def send_verification_code(self, phone: str) -> Any:
        url = f"{self.base}{WEB_API}/system/auth/app/getVerificationCode/{phone}"
        return self._request("GET", url, with_token=False)

    # ============================================================
    # AUTH-04 验证码登录
    # ============================================================
    def login_with_verification_code(
        self, phone: str, code: str, tenant_id: Optional[Any] = None
    ) -> Any:
        if tenant_id is not None:
            self.tenant_id = tenant_id
        url = f"{self.base}{WEB_API}/system/auth/verificationCode/login"
        payload: Dict[str, Any] = {PHONE_KEY: phone, SMS_CODE_KEY: code}
        if self.tenant_id is not None and str(self.tenant_id).isdigit():
            payload["tenantId"] = int(self.tenant_id)
        payload.update(EXTRA_SMS_LOGIN_FIELDS)
        data = self._request("POST", url, json_body=payload, with_token=False)
        self._absorb_login_result(data)
        return data

    # ============================================================
    # AUTH-05 刷新访问令牌
    # ============================================================
    def refresh_access_token(self) -> Any:
        if not self.refresh_token:
            raise ApiError(-1, "本地没有 refreshToken，请先登录")
        # 文档：刷新请求头仍包含旧访问令牌、租户 ID 和针对刷新路径生成的 Id-Code
        url = f"{AUTH_REFRESH_BASE}{WEB_API}/system/auth/app/refresh-token"
        data = self._request(
            "POST",
            url,
            params={"refreshToken": self.refresh_token},
            with_token=bool(self.access_token),
            allow_refresh=False,
        )
        if isinstance(data, dict):
            self._extract_tokens(data)
        elif isinstance(data, str) and data:
            self.access_token = data
        if not self.access_token:
            raise ApiError(
                -1, "刷新返回 code=0，但未识别出访问令牌；请加 --debug 查看原始响应"
            )
        self.save_session()
        return data

    def _relogin_with_credentials(self) -> bool:
        """用保存的账号密码重新登录，成功返回 True。"""
        creds = self.load_credentials()
        if not creds:
            return False
        username = creds.get("username")
        password = creds.get("password")
        if not username or password is None:
            return False
        tenant = creds.get("tenantId")
        try:
            self.login_with_password(str(username), str(password), tenant)
        except (ApiError, requests.RequestException):
            return False
        print("[OK] 已通过保存的凭据自动重新登录")
        return True

    def ensure_logged_in(self) -> bool:
        """确保本地存在登录态：有令牌直接用，否则先刷新，再尝试凭据自动登录。"""
        if self.access_token:
            return True
        if self.refresh_token:
            try:
                self.refresh_access_token()
                return True
            except ApiError:
                pass
        return self._relogin_with_credentials()

    # ============================================================
    # USR-01 获取当前用户资料（登录后校验令牌可用性）
    # ============================================================
    def get_profile(self) -> Any:
        url = f"{self.base}{WEB_API}/system/user/profile/get"
        return self._request("GET", url)

    # ============================================================
    # AUTH-06..08 找回密码
    # ============================================================
    def check_forget_password(self, payload: Mapping[str, Any]) -> Any:
        return self.request_api(
            "POST", f"{WEB_API}/system/auth/check/forgetPassword",
            body=dict(payload), with_token=False,
        )

    def send_forget_password_verification_code(
        self, payload: Mapping[str, Any]
    ) -> Any:
        return self.request_api(
            "POST", f"{WEB_API}/system/auth/forgetPassword/sendVerificationCode",
            body=dict(payload), with_token=False,
        )

    def update_forgotten_password(self, payload: Mapping[str, Any]) -> Any:
        return self.request_api(
            "PUT", f"{WEB_API}/system/user/profile/forgetPassword/update-password",
            body=dict(payload), with_token=False,
        )

    # ============================================================
    # USR-02..06 用户资料
    # ============================================================
    def update_profile(self, payload: Mapping[str, Any]) -> Any:
        return self.request_api(
            "PUT", f"{WEB_API}/system/user/profile/app/update", body=dict(payload)
        )

    def update_avatar(self, eid: str) -> Any:
        return self._request(
            "POST", self._url(f"{WEB_API}/system/user/profile/update-avatar"),
            data={"avatar": eid},
        )

    def send_update_mobile_verification_code(self, mobile: str) -> Any:
        return self.request_api(
            "GET",
            f"{WEB_API}/system/user/profile/updateMobile/sendVerificationCode/"
            f"{self._segment(mobile)}",
        )

    def deactivate_account(self, value: Any) -> Any:
        return self.request_api(
            "PUT",
            f"{WEB_API}/system/user/profile/deactivateAccount/{self._segment(value)}",
        )

    def update_password(self, payload: Mapping[str, Any]) -> Any:
        return self.request_api(
            "PUT", f"{WEB_API}/system/user/profile/update-password", body=dict(payload)
        )

    # ============================================================
    # DEV-01..02 设备绑定
    # ============================================================
    def bind_device(self, payload: Mapping[str, Any]) -> Any:
        return self.request_api(
            "POST", f"{WEB_API}/system/user-device/bind", body=dict(payload)
        )

    def clear_bind_device(self, payload: Mapping[str, Any]) -> Any:
        return self.request_api(
            "POST", f"{WEB_API}/system/user-device/clearBindDevice", body=dict(payload)
        )

    # ============================================================
    # MSG-01..05 消息
    # ============================================================
    def get_message_page(self, params: Mapping[str, Any]) -> Any:
        return self.request_api(
            "GET", f"{WEB_API}/system/notify-target/page", params=params
        )

    def get_unread_message_count(self) -> Any:
        return self.request_api(
            "GET", f"{WEB_API}/system/notify-target/get-unread-count"
        )

    def get_message(self, message_id: Any) -> Any:
        return self.request_api(
            "GET", f"{WEB_API}/system/notify-target/get", params={"id": message_id}
        )

    def mark_messages_read(self, ids: Sequence[Any]) -> Any:
        return self.request_api(
            "PUT", f"{WEB_API}/system/notify-target/update-read", body=list(ids)
        )

    def mark_all_messages_read(self, message_type: Any) -> Any:
        return self.request_api(
            "PUT", f"{WEB_API}/system/notify-target/update-all-read",
            params={"type": message_type},
        )

    # ============================================================
    # INFRA-01..03 版本、日志与错误上报
    # ============================================================
    def get_app_version(self, params: Mapping[str, Any]) -> Any:
        return self.request_api(
            "GET", f"{WEB_API}/system/app-version/get", params=params
        )

    def save_user_app_version_log(self, payload: Mapping[str, Any]) -> Any:
        return self.request_api(
            "POST", f"{WEB_API}/infra/user-app-version-log/save", body=dict(payload)
        )

    def report_app_crash(self, payload: Mapping[str, Any]) -> Any:
        return self.request_api(
            "POST", f"{WEB_API}/infra/app-crash-log/add", body=dict(payload)
        )

    # ============================================================
    # EDU-01..13 教学、课程与通知
    # ============================================================
    def get_term_options(self) -> Any:
        return self.request_api(
            "GET", f"{WEB_API}/teachmanager/teach-dropdown/getTeachTermDropDown"
        )

    def get_student_arrangement_page(self, params: Mapping[str, Any]) -> Any:
        return self.request_api(
            "GET", f"{WEB_API}/teachmanager/teach-arrangement-stu/page", params=params
        )

    def get_course_options_by_term(self, params: Mapping[str, Any]) -> Any:
        return self.request_api(
            "GET", f"{WEB_API}/teachmanager/teach-course/get-option/by-term-id",
            params=params,
        )

    def get_course(self, params: Mapping[str, Any]) -> Any:
        return self.request_api(
            "GET", f"{WEB_API}/teachmanager/teach-course/get", params=params
        )

    def apply_join_class(self, params: Mapping[str, Any]) -> Any:
        return self.request_api(
            "GET", f"{WEB_API}/teachmanager/teach-class-student/applyJoin",
            params=params,
        )

    def get_course_notice_read_statistics(self, params: Mapping[str, Any]) -> Any:
        return self.request_api(
            "GET",
            f"{WEB_API}/teachmanager/teach-course-notice-target/getReadStatics",
            params=params,
        )

    def get_course_notice_page(self, params: Mapping[str, Any]) -> Any:
        return self.request_api(
            "GET", f"{WEB_API}/teachmanager/teach-course-notice-target/pageList",
            params=params,
        )

    def update_course_notice_read_status(self, payload: Mapping[str, Any]) -> Any:
        return self.request_api(
            "PUT",
            f"{WEB_API}/teachmanager/teach-course-notice-target/updateBatchReadStatus",
            body=dict(payload),
        )

    def get_course_notice(self, params: Mapping[str, Any]) -> Any:
        return self.request_api(
            "GET", f"{WEB_API}/teachmanager/teach-course-notice/get", params=params
        )

    def get_course_columns(self, teach_course_id: Any) -> Any:
        return self.request_api(
            "GET", f"{WEB_API}/teachmanager/teach-course-column/listByTeachCourseId",
            params={"teachCourseId": teach_course_id},
        )

    def get_student_course_directory_tree(self, payload: Mapping[str, Any]) -> Any:
        return self.request_api(
            "POST", f"{WEB_API}/teachmanager/teach-course-directory-stu/listTree",
            body=dict(payload),
        )

    def get_course_directory_cache(self, params: Mapping[str, Any]) -> Any:
        return self.request_api(
            "GET", f"{WEB_API}/teachmanager/teach-course-directory/getDirCache",
            params=params,
        )

    def update_course_directory_cache(self, payload: Mapping[str, Any]) -> Any:
        return self.request_api(
            "PUT", f"{WEB_API}/teachmanager/teach-course-directory/updateDirCache",
            body=dict(payload),
        )

    # ============================================================
    # ATT-01..05 考勤
    # ============================================================
    def get_student_attendance_page(self, payload: Mapping[str, Any]) -> Any:
        return self.request_api(
            "POST",
            f"{WEB_API}/teachmanager/teach-course-attendance-detail/"
            "getAppStuAttendancePage",
            body=dict(payload),
        )

    def get_student_attendance_detail(self, detail_id: Any) -> Any:
        return self.request_api(
            "GET",
            f"{WEB_API}/teachmanager/teach-course-attendance-detail/"
            f"getAppStuAttendanceDetail/{self._segment(detail_id)}",
        )

    def get_attendance_detail_id(self, attendance_id: Any, student_id: Any) -> Any:
        return self.request_api(
            "GET",
            f"{WEB_API}/teachmanager/teach-course-attendance-detail/"
            f"getAttendanceDetailId/{self._segment(attendance_id)}/"
            f"{self._segment(student_id)}",
        )

    def check_qr_code_valid(self, attendance_id: Any, qr_code_id: str) -> bool:
        data = self.request_api(
            "POST",
            f"{WEB_API}/teachmanager/teach-course-attendance-detail/check-qrCode-is-valid",
            body={"attendanceId": attendance_id, "qrCodeId": qr_code_id},
        )
        return data is True

    def submit_attendance(self, payload: Mapping[str, Any]) -> Any:
        return self.request_api(
            "PUT", f"{WEB_API}/teachmanager/teach-course-attendance-detail/update",
            body=dict(payload),
        )

    def teacher_makeup_attendance(self, payload: Mapping[str, Any]) -> Any:
        """教师手动补签；服务端会校验当前账号的教师权限。"""
        return self.request_api(
            "PUT", f"{WEB_API}/teachmanager/teach-course-attendance-detail/update",
            body=dict(payload),
        )

    # Web 学生端补充接口：查询未签到数量 / 当前待签到考勤（按教学班维度）
    def get_sign_num(self, student_id: Any, teach_class_id: Any) -> Any:
        return self.request_api(
            "GET", f"{WEB_API}/teachmanager/teach-course-attendance-detail/getSignNum",
            params={"studentId": student_id, "teachClassId": teach_class_id},
        )

    def get_sign_record(self, student_id: Any, teach_class_id: Any) -> Any:
        return self.request_api(
            "GET", f"{WEB_API}/teachmanager/teach-course-attendance-detail/getSignRecord",
            params={"studentId": student_id, "teachClassId": teach_class_id},
        )

    # 当前 Web 教师端补充接口：发布/查询签到（不在 APK 学生端 56 接口清单中）
    def get_teacher_attendance_page(self, params: Mapping[str, Any]) -> Any:
        return self.request_api(
            "GET", f"{WEB_API}/teachmanager/teach-course-attendance/page", params=params
        )

    def publish_attendance(self, payload: Mapping[str, Any]) -> Any:
        """发布普通、定位或二维码签到；payload 字段由教师端页面定义。"""
        return self.request_api(
            "POST", f"{WEB_API}/teachmanager/teach-course-attendance/create",
            body=dict(payload),
        )

    def publish_teacher_attendance(self, payload: Mapping[str, Any]) -> Any:
        """发布 type=2 的教师考勤。"""
        return self.request_api(
            "POST", f"{WEB_API}/teachmanager/teach-course-attendance/teacher/create",
            body=dict(payload),
        )

    # ============================================================
    # RES-01..09 课程资源与学习记录
    # ============================================================
    def get_study_task_page(self, payload: Mapping[str, Any]) -> Any:
        return self.request_api(
            "POST", f"{WEB_API}/teachmanager/teach-course-res-stu/study-task/page",
            body=dict(payload),
        )

    def validate_student_resource(self, payload: Mapping[str, Any]) -> Any:
        return self.request_api(
            "POST",
            f"{WEB_API}/teachmanager/teach-course-res-stu-record/validateStuResInfo",
            body=dict(payload),
        )

    def get_study_record(self, payload: Mapping[str, Any]) -> Any:
        return self.request_api(
            "POST", f"{WEB_API}/teachmanager/teach-course-res-stu-record/getRecordInfo",
            body=dict(payload),
        )

    def get_app_study_record(self, payload: Mapping[str, Any]) -> Any:
        return self.request_api(
            "POST",
            f"{WEB_API}/teachmanager/teach-course-res-stu-record/app/getRecordInfo",
            body=dict(payload),
        )

    def start_study(self, payload: Mapping[str, Any]) -> Any:
        return self.request_api(
            "POST", f"{WEB_API}/teachmanager/teach-course-res-stu-record/startStudy",
            body=dict(payload),
        )

    def report_audio_video_progress(self, payload: Mapping[str, Any]) -> Any:
        return self.request_api(
            "POST",
            f"{WEB_API}/teachmanager/teach-course-res-stu-record/studyForAudioOrVideo",
            body=dict(payload),
        )

    def list_student_resources(self, payload: Mapping[str, Any]) -> Any:
        return self.request_api(
            "POST", f"{WEB_API}/teachmanager/teach-course-res-stu/listResource",
            body=dict(payload),
        )

    def query_file_index(self, eid: str) -> Any:
        return self.request_api(
            "GET", "/content/search/queryIndexById",
            params={"eid": eid, "source": "NEUNSE"},
        )

    def update_course_active_record(self, payload: Mapping[str, Any]) -> Any:
        return self.request_api(
            "POST",
            f"{WEB_API}/teachmanager/teach-course-active-stu-day-record/updateActiveRecord",
            body=dict(payload),
        )

    # ============================================================
    # FILE-01..05 文件、预览、下载与上传
    # ============================================================
    def get_file_object(self, eid: str) -> bytes:
        return self.request_api(
            "GET", "/content/s3/getObjectIO",
            params={"eid": eid, "source": "NEUNSE"}, raw=True,
        )

    def get_file_download(self, eid: str) -> bytes:
        return self.request_api(
            "GET", "/content/s3/getObjectIOForDown",
            params={"eid": eid, "source": "NEUNSE"}, raw=True,
        )

    def preview_file_object(self, eid: str) -> bytes:
        return self.request_api(
            "GET", "/content/s3/previewObject",
            params={"eid": eid, "source": "NEUNSE"}, raw=True,
        )

    def get_video_path(self, eid: str) -> Any:
        return self.request_api(
            "GET", "/content/search/getVideoPath",
            params={"eid": eid, "source": "NEUNSE"},
        )

    def batch_upload_files(
        self, paths: Sequence[Union[str, Path]], *, is_check: bool = True,
        is_slice: bool = True,
    ) -> Any:
        opened = []
        try:
            multipart = []
            for raw_path in paths:
                path = Path(raw_path)
                handle = path.open("rb")
                opened.append(handle)
                multipart.append(("files", (path.name, handle)))
            if not multipart:
                raise ValueError("至少需要一个上传文件")
            return self._request(
                "POST", self._url("/content/s3/batchUpload"),
                data={
                    "source": "NEUNSE",
                    "isCheck": str(is_check).lower(),
                    "isSlice": str(is_slice).lower(),
                },
                files=multipart,
            )
        finally:
            for handle in opened:
                handle.close()


# ============================================================
# 交互模式（不带子命令直接运行时启用）
# ============================================================
def _input(prompt: str, *, required: bool = True,
           default: Optional[str] = None) -> Optional[str]:
    """交互输入：Ctrl+C / 输入流结束返回 None（视为取消）；必填项为空时反复提示"""
    while True:
        try:
            raw = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[!] 已取消")
            return None
        if raw:
            return raw
        if default is not None:
            return default
        if not required:
            return ""
        print("[!] 该项为必填，请重新输入")


def _input_password() -> Optional[str]:
    """使用普通 input（明文回显）。
    不用 getpass：其基于 msvcrt 的不回显实现在 mintty/IDE/部分终端下
    会出现无提示挂起（看似“卡死”）。"""
    return _input("登录密码（输入会显示明文，注意遮挡屏幕）：")


def _print_status(client: NeumoocClient) -> None:
    bar = "=" * 56
    print("\n" + bar)
    print(" 东软智慧教育 App 全量接口客户端（APK v1.0.74）")
    print(bar)
    state = "已登录" if client.access_token else "未登录"
    tenant = client.tenant_id if client.tenant_id is not None else "未设置"
    user = client.user_id if client.user_id is not None else "-"
    print(f" 状态: {state} | 用户ID: {user} | 租户: {tenant}")
    print(f" 域名: {client.base}")
    print(f" 令牌: {_mask(client.access_token)}")
    print("-" * 56)
    print("  1. 账号密码登录(AUTH-02)      2. 短信验证码登录(AUTH-03/04)")
    print("  3. 设置学校/租户 ID(手动输入)")
    print("  4. 查看用户资料(USR-01)      5. 刷新访问令牌(AUTH-05)")
    print("  6. 修改业务域名              7. 清除本地登录信息")
    print("  8. 启动自动签到(学生端)")
    print("  0. 退出")


def _act_password_login(client: NeumoocClient) -> None:
    username = _input("账号（学号）：")
    if not username:
        return
    _ask_tenant(client)
    password = _input_password()
    if not password:
        return
    print(f"[..] 正在登录（AUTH-02，{client.base}）...")
    client.login_with_password(username, password)
    print("[OK] 登录成功（AUTH-02）")
    _print_session(client)
    _verify_profile(client)


def _act_sms_login(client: NeumoocClient) -> None:
    phone = _input("手机号：")
    if not phone:
        return
    _ask_tenant(client)
    print(f"[..] 正在发送验证码（AUTH-03，{client.base}）...")
    client.send_verification_code(phone)
    print("[OK] 验证码发送请求已提交（AUTH-03），请查收短信")
    code = _input("短信验证码：")
    if not code:
        return
    print(f"[..] 正在登录（AUTH-04，{client.base}）...")
    client.login_with_verification_code(phone, code)
    print("[OK] 登录成功（AUTH-04）")
    _print_session(client)
    _verify_profile(client)


def _act_set_tenant(client: NeumoocClient) -> None:
    """手动设置租户 ID（不请求网络）"""
    current = client.tenant_id if client.tenant_id is not None else "未设置"
    raw = _input(f"输入学校/租户 ID（当前 {current}，回车取消，输入 0 清除）：", required=False)
    if not raw:
        return
    if raw in ("0", "none", "None"):
        client.tenant_id = None
        client.save_session()
        print("[OK] 已清除租户 ID")
        return
    if not raw.isascii():
        print("[!] 租户 ID 不能是学校名称（Tenant-Id 是 HTTP 请求头，仅允许数字/字母），"
              "具体数值可用子命令 tenants 拉取或从官方 App 抓包确认")
        return
    if not raw.isdigit():
        print("[提示] 租户 ID 通常为纯数字，已按输入保存，若登录失败请核对")
    client.tenant_id = raw
    client.save_session()
    print(f"[OK] 已设置租户 ID: {raw}")


def _act_profile(client: NeumoocClient) -> None:
    if not client.access_token:
        print("[!] 尚未登录，请先选择 1 或 2 登录")
        return
    print(f"[..] 正在获取用户资料（USR-01，{client.base}）...")
    _verify_profile(client)


def _act_refresh(client: NeumoocClient) -> None:
    print("[..] 正在刷新令牌（AUTH-05）...")
    client.refresh_access_token()
    print("[OK] 令牌刷新成功（AUTH-05）")
    _print_session(client)


def _act_website(client: NeumoocClient) -> None:
    new = _input(f"新业务域名（当前 {client.base}，回车取消）：", required=False)
    if not new:
        return
    if not new.startswith(("http://", "https://")):
        print("[!] 域名需以 http:// 或 https:// 开头")
        return
    client.base = new.rstrip("/")
    client.save_session()
    print(f"[OK] 业务域名已更新: {client.base}")


def _act_logout(client: NeumoocClient) -> None:
    confirm = _input("确认清除本地令牌并退出登录？(y/N)：", required=False, default="n").lower()
    if confirm in ("y", "yes"):
        client.logout()
        print("[OK] 本地登录信息已清除")
    else:
        print("已取消")


def _act_auto_checkin(client: NeumoocClient) -> None:
    """菜单 8：启动学生端自动签到监视（Ctrl+C 返回菜单）"""
    from neumooc_checkin import AutoCheckinBot

    raw = _input("轮询间隔秒数（回车默认 30）：", required=False)
    interval = 30
    if raw:
        try:
            interval = max(5, int(raw))
        except ValueError:
            print("[!] 不是数字，使用默认 30 秒")
    bot = AutoCheckinBot(client, interval=interval)
    bot.run()


def run_interactive(args: argparse.Namespace) -> int:
    client = _build_client(args)
    print("已进入交互模式：输入菜单编号并回车；任意输入环节可按 Ctrl+C 取消并返回菜单。")
    print("提示：租户 ID 为手动输入（菜单 3）；若请求卡住可用 --no-proxy 重新启动。")
    if client.tenant_id is not None and not str(client.tenant_id).isascii():
        print(f"[警告] 已保存的租户 ID “{client.tenant_id}” 含中文，无法作为请求头发送，"
              "请用菜单 3 重新设置（通常为数字）")
    handlers = {
        "1": _act_password_login,
        "2": _act_sms_login,
        "3": _act_set_tenant,
        "4": _act_profile,
        "5": _act_refresh,
        "6": _act_website,
        "7": _act_logout,
        "8": _act_auto_checkin,
    }
    while True:
        _print_status(client)
        choice = _input("请选择操作：", required=False)
        if choice in (None, "", "0", "q", "Q", "exit", "quit"):
            print("再见！")
            return 0
        handler = handlers.get(choice)
        if handler is None:
            print("[!] 无效的选项，请输入菜单中的编号")
            continue
        try:
            handler(client)
        except (ApiError, requests.RequestException) as exc:
            print(f"[失败] {exc}")
        except Exception as exc:  # 兜底：菜单不因意外异常整体崩溃退出
            print(f"[异常] {type(exc).__name__}: {exc}")


# ============================================================
# 命令行入口
# ============================================================
def _build_client(args: argparse.Namespace) -> NeumoocClient:
    return NeumoocClient(
        website=getattr(args, "website", None),
        tenant_id=getattr(args, "tenant", None),
        token_file=args.token_file,
        debug=args.debug,
        insecure=args.insecure,
        no_proxy=args.no_proxy,
    )


def _print_tenant_table(items: List[Dict[str, Any]]) -> None:
    print(f"共 {len(items)} 个租户/学校：")
    for item in items:
        tid = _first(item, ("id", "tenantId", "value"))
        name = _first(item, ("name", "tenantName", "label", "schoolName"))
        code = _first(item, ("code", "tenantCode"))
        line = f"  - id={tid}\t名称={name}"
        if code is not None:
            line += f"\t编码={code}"
        print(line)


def _ask_tenant(client: NeumoocClient) -> None:
    """登录前手动输入租户 ID（不请求 AUTH-01，避免该接口在网络不佳时卡住）；
    已设置租户时直接跳过。"""
    if client.tenant_id is not None:
        return
    raw = _input("学校/租户 ID（通常为数字，回车跳过则不带 Tenant-Id 登录）：", required=False)
    if not raw:
        return
    if not raw.isascii():
        print("[!] 租户 ID 不能是学校名称（Tenant-Id 是 HTTP 请求头，仅允许数字/字母），"
              "具体数值可用子命令 tenants 拉取或从官方 App 抓包确认")
        return
    client.tenant_id = raw
    client.save_session()
    print(f"[OK] 已设置租户 ID: {raw}")


def _print_session(client: NeumoocClient) -> None:
    print(f"  业务域名 : {client.base}")
    print(f"  租户 ID  : {client.tenant_id}")
    print(f"  用户 ID  : {client.user_id}")
    print(f"  访问令牌 : {_mask(client.access_token)}")
    print(f"  刷新令牌 : {_mask(client.refresh_token)}")
    print(f"  已保存到 : {client.token_file.resolve()}")


def _verify_profile(client: NeumoocClient) -> None:
    """登录成功后调用 USR-01 验证令牌确实可用"""
    try:
        profile = client.get_profile()
    except (ApiError, requests.RequestException) as exc:
        print(f"[警告] 令牌校验（USR-01）失败：{exc}")
        return
    print("[OK] 令牌校验通过（USR-01 已获取用户资料）：")
    text = json.dumps(profile, ensure_ascii=False, indent=2)
    print(text[:1200] + ("\n  ...（截断）" if len(text) > 1200 else ""))


def cmd_tenants(args: argparse.Namespace) -> None:
    client = _build_client(args)
    print(f"[..] 正在获取学校列表（AUTH-01，{TENANT_OPTION_BASE}，连接超时 5 秒）...")
    items = client.get_tenant_options()
    if not items:
        print("未解析到租户选项，请加 --debug 查看原始返回")
        return
    _print_tenant_table(items)


def cmd_login(args: argparse.Namespace) -> None:
    password = args.password or _input("登录密码（输入会显示明文，注意遮挡屏幕）：")
    if not password:
        raise ApiError(-1, "未输入密码")
    client = _build_client(args)
    _ask_tenant(client)
    client.login_with_password(args.username, password)
    print("[OK] 登录成功（AUTH-02）")
    if args.save_credentials:
        client.save_credentials(args.username, password)
    _print_session(client)
    _verify_profile(client)


def cmd_sms(args: argparse.Namespace) -> None:
    client = _build_client(args)
    _ask_tenant(client)
    client.send_verification_code(args.phone)
    print("[OK] 验证码发送请求已提交（AUTH-03），请查收短信")


def cmd_sms_login(args: argparse.Namespace) -> None:
    client = _build_client(args)
    _ask_tenant(client)
    client.login_with_verification_code(args.phone, args.code)
    print("[OK] 登录成功（AUTH-04）")
    _print_session(client)
    _verify_profile(client)


def cmd_refresh(args: argparse.Namespace) -> None:
    client = _build_client(args)
    client.refresh_access_token()
    print("[OK] 令牌刷新成功（AUTH-05）")
    _print_session(client)


def cmd_profile(args: argparse.Namespace) -> None:
    client = _build_client(args)
    if not client.access_token:
        raise ApiError(-1, "本地没有令牌，请先执行 login 或 sms-login")
    _verify_profile(client)


def _json_argument(value: Optional[str], *, name: str, object_only: bool = False) -> Any:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except ValueError as exc:
        raise ApiError(-1, f"{name} 不是有效 JSON：{exc}") from None
    if object_only and not isinstance(parsed, dict):
        raise ApiError(-1, f"{name} 必须是 JSON 对象")
    return parsed


def _print_result(result: Any) -> None:
    if isinstance(result, bytes):
        print(f"<二进制响应：{len(result)} 字节>")
        return
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_call(args: argparse.Namespace) -> None:
    """通用 CLI：无需为 56 个透传对象重复定义大量命令行字段。"""
    client = _build_client(args)
    params = _json_argument(args.params, name="--params", object_only=True)
    body = _json_argument(args.body, name="--body")
    result = client.request_api(
        args.method, args.path, params=params, body=body,
        with_token=not args.public, raw=args.raw,
    )
    if args.output:
        if not isinstance(result, bytes):
            raise ApiError(-1, "只有 --raw 二进制响应可以写入 --output")
        output = Path(args.output)
        output.write_bytes(result)
        print(f"[OK] 已写入 {output.resolve()}（{len(result)} 字节）")
    else:
        _print_result(result)


def cmd_download(args: argparse.Namespace) -> None:
    client = _build_client(args)
    content = client.get_file_download(args.eid)
    output = Path(args.output)
    output.write_bytes(content)
    print(f"[OK] 文件已下载到 {output.resolve()}（{len(content)} 字节）")


def cmd_upload(args: argparse.Namespace) -> None:
    client = _build_client(args)
    result = client.batch_upload_files(
        args.files, is_check=not args.no_check, is_slice=not args.no_slice
    )
    print("[OK] 文件上传完成（FILE-05）")
    _print_result(result)


def _attendance_publish_payload(args: argparse.Namespace) -> Dict[str, Any]:
    duration = max(1, int(args.duration))
    attendance_type = {"normal": 0, "qr": 1, "teacher": 2}[args.type]
    if attendance_type == 2:
        return {
            "title": args.title,
            "duration": duration,
            "type": 2,
            "dirId": args.dir_id or "",
            "teachCourseId": args.course_id,
            "teachClassId": args.class_id,
        }

    location_type = int(args.location_type)
    if location_type == 1 and (
        args.longitude is None or args.latitude is None or not args.address
    ):
        raise ApiError(-1, "定位签到必须同时提供 --longitude、--latitude 和 --address")
    now_ms = int(time.time() * 1000)
    open_time = int(args.scheduled_at) if args.scheduled_at is not None else now_ms
    payload: Dict[str, Any] = {
        "title": args.title,
        "dirId": args.dir_id or "",
        "teachCourseId": args.course_id,
        "teachClassId": args.class_id,
        "duration": duration,
        "type": attendance_type,
        "openTime": open_time,
        "finishTime": open_time + duration * 60_000,
        "longitude": args.longitude if location_type == 1 else "",
        "latitude": args.latitude if location_type == 1 else "",
        "addressName": args.address if location_type == 1 else "",
        "attendanceRadius": str(args.radius) if location_type == 1 else "",
        "attendanceLocationType": location_type,
        "publishType": 1 if args.scheduled_at is not None else 0,
        "scheduledPublishTime": str(open_time) if args.scheduled_at is not None else "",
    }
    if attendance_type == 1:
        payload["qrCodeRefreshFrequency"] = str(args.qr_refresh)
    return payload


def cmd_publish_attendance(args: argparse.Namespace) -> None:
    payload = _attendance_publish_payload(args)
    if args.dry_run:
        print("[DRY-RUN] 未发送请求，生成的发布参数：")
        _print_result(payload)
        return
    client = _build_client(args)
    if args.type == "teacher":
        result = client.publish_teacher_attendance(payload)
    else:
        result = client.publish_attendance(payload)
    print("[OK] 签到发布成功")
    _print_result(result)


def _teacher_makeup_payload(args: argparse.Namespace) -> Dict[str, Any]:
    return {
        "attendanceId": args.attendance_id,
        "id": args.detail_id,
        "status": 1,
        "type": args.type,
        "signRole": 3,
        "signUserId": args.student_id,
    }


def cmd_teacher_makeup(args: argparse.Namespace) -> None:
    payload = _teacher_makeup_payload(args)
    if args.dry_run:
        print("[DRY-RUN] 未发送请求，生成的教师补签参数：")
        _print_result(payload)
        return
    client = _build_client(args)
    result = client.teacher_makeup_attendance(payload)
    print("[OK] 教师补签成功")
    _print_result(result)


def cmd_auto_checkin(args: argparse.Namespace) -> None:
    """学生端自动签到：轮询进行中的考勤并直接发包提交（详见 neumooc_checkin 模块）。"""
    from neumooc_checkin import AutoCheckinBot, CheckinError

    client = _build_client(args)
    bot = AutoCheckinBot(
        client,
        interval=args.interval,
        term_id=args.term_id,
        course_id=args.course_id,
        longitude=args.longitude,
        latitude=args.latitude,
        address=args.address,
        qr_sign_type=args.qr_sign_type,
        include_teacher=args.include_teacher,
        any_status=args.any_status,
        dry_run=args.dry_run,
        max_attempts=args.max_attempts,
        max_rounds=1 if args.once else args.max_rounds,
        page_size=args.page_size,
        quiet=args.quiet,
    )
    try:
        code = bot.run()
    except CheckinError as exc:
        raise ApiError(-1, str(exc)) from None
    if code != 0:
        raise ApiError(-1, "自动签到未能正常结束（详情见上方日志）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="neumooc_login.py",
        description="东软智慧教育 App 全量接口客户端（基于 APK v1.0.74 静态分析接口文档）；"
                    "不带子命令直接运行时进入交互式菜单",
        epilog="注意：全局参数需放在子命令之前，例如 neumooc_login.py --debug login -u 学号 -p 密码；"
               "交互模式示例：neumooc_login.py --debug",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--debug", action="store_true", help="打印原始请求/响应（令牌与密码脱敏）")
    parser.add_argument("--website", help=f"覆盖业务域名（默认 {DEFAULT_BUSINESS_BASE}）")
    parser.add_argument("--token-file", default=DEFAULT_TOKEN_FILE,
                        help=f"令牌缓存文件（默认 {DEFAULT_TOKEN_FILE}）")
    parser.add_argument("--insecure", action="store_true", help="忽略 TLS 证书校验（测试环境排查用）")
    parser.add_argument("--no-proxy", action="store_true",
                        help="绕过系统/环境变量代理（请求长时间卡住时使用）")

    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("tenants", help="AUTH-01 获取租户/学校选项")
    p.set_defaults(func=cmd_tenants)

    p = sub.add_parser("login", help="AUTH-02 账号密码登录")
    p.add_argument("-u", "--username", required=True, help="学号/账号")
    p.add_argument("-p", "--password", help="登录密码（不填则交互输入，不回显）")
    p.add_argument("-t", "--tenant", help="租户/学校 ID")
    p.add_argument("--save-credentials", action="store_true",
                   help="登录成功后把账号密码写入凭据文件，用于令牌失效后自动重新登录")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("sms", help="AUTH-03 发送手机登录验证码")
    p.add_argument("--phone", required=True, help="手机号")
    p.add_argument("-t", "--tenant", help="租户/学校 ID")
    p.set_defaults(func=cmd_sms)

    p = sub.add_parser("sms-login", help="AUTH-04 验证码登录")
    p.add_argument("--phone", required=True, help="手机号")
    p.add_argument("--code", required=True, help="短信验证码")
    p.add_argument("-t", "--tenant", help="租户/学校 ID")
    p.set_defaults(func=cmd_sms_login)

    p = sub.add_parser("refresh", help="AUTH-05 刷新访问令牌")
    p.set_defaults(func=cmd_refresh)

    p = sub.add_parser("profile", help="USR-01 获取当前用户资料（校验已保存令牌）")
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser("call", help="调用任意文档接口（JSON 查询参数/请求体）")
    p.add_argument("method", choices=("GET", "POST", "PUT", "DELETE", "PATCH"),
                   type=str.upper, help="HTTP 方法")
    p.add_argument("path", help="接口路径或完整 URL")
    p.add_argument("--params", help='查询参数 JSON，例如 {"id":1}')
    p.add_argument("--body", help="请求体 JSON（对象或数组）")
    p.add_argument("--public", action="store_true", help="不发送 Authorization")
    p.add_argument("--raw", action="store_true", help="按二进制读取响应")
    p.add_argument("-o", "--output", help="将 --raw 响应写入文件")
    p.set_defaults(func=cmd_call)

    p = sub.add_parser("download", help="FILE-02 按 EID 下载文件")
    p.add_argument("--eid", required=True, help="文件 EID")
    p.add_argument("-o", "--output", required=True, help="输出文件路径")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("upload", help="FILE-05 批量上传文件")
    p.add_argument("files", nargs="+", help="要上传的本地文件")
    p.add_argument("--no-check", action="store_true", help="上传时传 isCheck=false")
    p.add_argument("--no-slice", action="store_true", help="上传时传 isSlice=false")
    p.set_defaults(func=cmd_upload)

    p = sub.add_parser("publish-attendance", help="教师端发布普通/二维码/教师签到")
    p.add_argument("--course-id", required=True, help="授课课程 teachCourseId")
    p.add_argument("--class-id", required=True, help="教学班 teachClassId")
    p.add_argument("--title", required=True, help="签到标题（最长 20 字符）")
    p.add_argument("--duration", type=int, default=10, help="持续分钟数（默认 10）")
    p.add_argument("--type", choices=("normal", "qr", "teacher"), default="normal",
                   help="签到类型（默认 normal）")
    p.add_argument("--dir-id", default="", help="可选课程目录 ID")
    p.add_argument("--scheduled-at", type=int,
                   help="定时发布时间（Unix 毫秒）；不填则立即发布")
    p.add_argument("--location-type", type=int, choices=(0, 1), default=0,
                   help="0 不限位置，1 定位签到")
    p.add_argument("--longitude", help="定位经度")
    p.add_argument("--latitude", help="定位纬度")
    p.add_argument("--address", help="定位地址")
    p.add_argument("--radius", type=int, default=300, help="签到半径（米）")
    p.add_argument("--qr-refresh", type=int, choices=(5, 10, 20, 30), default=10,
                   help="二维码刷新秒数（默认 10）")
    p.add_argument("--dry-run", action="store_true", help="仅生成请求体，不实际发布")
    p.set_defaults(func=cmd_publish_attendance)

    p = sub.add_parser(
        "teacher-makeup",
        help="使用教师权限为指定学生补签（服务端校验教师权限）",
    )
    p.add_argument("--attendance-id", required=True, help="考勤活动 ID")
    p.add_argument("--detail-id", required=True, help="学生考勤明细 ID")
    p.add_argument("--student-id", required=True, help="目标学生用户 ID")
    p.add_argument("--type", type=int, choices=(0, 1, 2), default=0,
                   help="签到类型：0 普通，1 二维码，2 教师考勤（默认 0）")
    p.add_argument("--dry-run", action="store_true", help="只打印参数，不实际补签")
    p.set_defaults(func=cmd_teacher_makeup)

    p = sub.add_parser(
        "auto-checkin",
        help="学生端自动签到：轮询进行中的考勤并直接发包提交（ATT-01/03/05）",
    )
    p.add_argument("--interval", type=int, default=30,
                   help="轮询间隔秒数（默认 30，最小 5）")
    p.add_argument("--once", action="store_true", help="只扫描一轮即退出")
    p.add_argument("--max-rounds", type=int, help="最多轮询轮数（默认不限，Ctrl+C 停止）")
    p.add_argument("--term-id", help="学期 ID（默认自动识别当前学期）")
    p.add_argument("--course-id", help="只关注指定课程（teachCourseId）")
    p.add_argument("--longitude", help="定位签到经度（定位考勤必填）")
    p.add_argument("--latitude", help="定位签到纬度（定位考勤必填）")
    p.add_argument("--address", help="定位签到地址（可选，提交为 signAddressName）")
    p.add_argument("--qr-sign-type", type=int, choices=(0, 1), default=1,
                   help="二维码考勤(type=1)直接签到时的提交体 type 值（默认 1；"
                        "服务端仍校验刷新种子时改为 0 按普通签到提交）")
    p.add_argument("--include-teacher", action="store_true",
                   help="教师考勤(type=2)也尝试签到（默认跳过）")
    p.add_argument("--any-status", action="store_true",
                   help="处理所有考勤状态（默认跳过明确未开始/已结束的场次）")
    p.add_argument("--page-size", type=int,
                   help="ATT-01 分页大小（默认不传，使用服务端默认）")
    p.add_argument("--max-attempts", type=int, default=3,
                   help="每场考勤失败重试次数上限（默认 3）")
    p.add_argument("--dry-run", action="store_true",
                   help="只打印将要提交的签到数据，不实际提交")
    p.add_argument("--quiet", action="store_true", help="无考勤数据的轮次不打印日志")
    p.set_defaults(func=cmd_auto_checkin)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    # 防止在 cp437 等不含中文的代码页下重定向输出时抛 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    if not args.command:
        return run_interactive(args)  # 未指定子命令 → 交互式菜单
    try:
        args.func(args)
    except ApiError as exc:
        print(f"接口错误：{exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"网络错误：{exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"本地操作错误：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
