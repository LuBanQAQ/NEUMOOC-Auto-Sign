#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
东软智慧教育 App 学生端自动签到
==============================

轮询“进行中”的课程考勤（签到），发现未签到的考勤后自动调用 ATT-05 提交签到。

接口事实依据（《东软智慧教育 App 全量接口文档》APK v1.0.74 静态分析，第 5 节）：

  * ATT-05 提交签到 PUT /web-api/teachmanager/teach-course-attendance-detail/update，
    请求体字段（文档 5/ATT-05）：
        {
          "attendanceId":  <考勤活动ID>,
          "id":            <当前学生的考勤明细ID>,
          "status":        1,              // 客户端签到固定传 1
          "type":          0 或 1,         // 二维码签到=1，普通/定位签到=0
          "signRole":      4,              // 学生端固定传 4（3=教师手动修改）
          "signUserId":    <学生ID>,
          "signLongitude": "<经度>",        // 定位签到
          "signLatitude":  "<纬度>",        // 定位签到
          "signAddressName": "<地址>",      // 定位签到
          "refreshSeed":   "<刷新种子>"     // 二维码流程携带；普通/定位流程可能为空
        }
    客户端最终请求体中没有 qrCodeId / qrCodeValid——二维码有效性由 ATT-04
    在提交前单独校验（服务端自行绑定校验状态）。
  * 考勤场次状态 status：0 未开始 / 1 进行中 / 2 已结束（Web 端映射佐证）；
    ATT-01 请求体中 attendanceStatus 为“签到结果筛选”、status 为“考勤活动状态筛选”。
  * ATT-04 校验体 {attendanceId, qrCodeId}；二维码原始内容为 JSON：
        {"type": "qrCodeAttendance", "id": "<qrCodeId>",
         "attendanceId": "<场次ID>", "refreshSeed": "<刷新种子>"}
    （Web 教师端 QrCodeAttendancePopup 源码与此一致。）
  * 文档 6.2 二维码签到流程：解析二维码 JSON → ATT-04 校验 →
    ATT-03 取明细 ID → ATT-02 详情 → ATT-05 提交（type=1 + refreshSeed）。

自动签到策略：
  * 普通签到（type=0 且非定位）：自动提交；
  * 定位签到（type=0 且 attendanceLocationType=1）：坐标缺省时使用默认学校坐标，
    也可用 --longitude/--latitude（可选 --address）覆盖；提交体使用 sign* 前缀字段；
  * 二维码签到（type=1）：二维码签到已下线，改为直接发包签到——不再扫描二维码、
    不再调用 ATT-04 校验、不携带 qrCodeId；提交体 type 默认 1，附带伪造的
    refreshSeed="0" 与默认坐标（可用 --longitude/--latitude/--address 覆盖）；
    仍被拒绝时可用 --qr-sign-type 0 按普通签到(type=0)提交；
  * 教师考勤（type=2）：默认跳过，可用 --include-teacher 开启。

快速上手（先登录保存令牌）：
    python neumooc_login.py login -u 学号 -p 密码 -t 租户ID
    python neumooc_login.py auto-checkin                  # 每 30 秒扫描一轮
    python neumooc_login.py auto-checkin --once --dry-run # 只看会提交什么
    python neumooc_login.py auto-checkin --interval 15 \
        --longitude 121.5 --latitude 38.9 --address 某教学楼

强制补签（教师端接口 + 学生账号，见文末 ForceCheckinBot）：
    python neumooc_login.py force-checkin                          # 该学期全部课程
    python neumooc_login.py force-checkin --course-id 课程ID        # 只补签指定课程
    python neumooc_login.py force-checkin --once --dry-run         # 演练

常量区字段名/取值若与实际服务端返回不一致（ATT-01 行结构为“对象透传”，
文档未给出明细），请用 --debug 观察原始数据后调整常量。
"""

from __future__ import annotations

import json
import time
from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import requests

from neumooc_login import ApiError, NeumoocClient

# ============================================================
# 常量区（联调时按需调整）
# ============================================================
# 考勤场次状态（Web 学生端映射）
SESSION_STATUS_NOT_STARTED = 0
SESSION_STATUS_IN_PROGRESS = 1
SESSION_STATUS_ENDED = 2

# 签到类型
ATTENDANCE_TYPE_NORMAL = 0
ATTENDANCE_TYPE_QR = 1
ATTENDANCE_TYPE_TEACHER = 2

# 签到提交体字段值/字段名（文档 5/ATT-05）
SIGN_STATUS_PRESENT = 1   # status：客户端签到固定传 1
SIGN_ROLE_STUDENT = 4     # signRole：学生端固定传 4
TYPE_FIELD = "type"                        # 二维码=1，普通/定位=0
REFRESH_SEED_FIELD = "refreshSeed"         # 二维码直签时伪造的刷新种子字段
QR_CODE_JSON_TYPE = "qrCodeAttendance"     # 二维码原始内容 JSON 的 type 值（已下线，保留仅作参考）
# 二维码考勤(type=1)改为直接发包签到后，ATT-05 提交体的 type 默认值：
# 1 = 与考勤活动类型保持一致；0 = 按普通签到提交（服务端仍校验刷新种子时可试 0）
QR_BYPASS_SIGN_TYPE = 1
# 直签二维码考勤时提交的伪造 refreshSeed（参考已验证的直签通道：该通道不校验凭证）
DIRECT_SIGN_REFRESH_SEED = "0"
# 直签二维码考勤时使用的默认坐标（与 App 内置兜底值一致，可用 --longitude/--latitude/--address 覆盖）
DEFAULT_SIGN_LONGITUDE = "121.614682"
DEFAULT_SIGN_LATITUDE = "38.914003"
DEFAULT_SIGN_ADDRESS = "大连东软信息学院"

# 已签到判定：signRole 取值 3（教师手动修改）/ 4（学生已签）视为已签
SIGNED_SIGN_ROLES = (3, 4)
# 服务端提示“已签到”的报错关键字（命中则视为签到完成，不再重试）
DUPLICATE_SIGN_MARKERS = ("已签", "签到过", "重复", "已经签到")

# ATT-01 返回行的字段名兼容列表（文档 5/ATT-01 已确认核心字段）
DETAIL_ID_KEYS = ("id", "attendanceDetailId", "detailId")
ATTENDANCE_ID_KEYS = ("attendanceId", "courseAttendanceId")
TYPE_KEYS = ("type", "attendanceType")
LOCATION_TYPE_KEYS = ("attendanceLocationType", "locationType")
SESSION_STATUS_KEYS = ("status",)
SIGN_ROLE_KEYS = ("signRole",)
SIGN_TIME_KEYS = ("signTime", "signTimeString", "signInTime", "signTimeStr")
TITLE_KEYS = ("title", "attendanceName", "name")

# 定位签到需要额外并入提交体的字段（option 名 -> ATT-05 请求体字段名）
# 文档 5/ATT-05：signLongitude / signLatitude / signAddressName
LOCATION_SIGN_FIELDS = {
    "longitude": "signLongitude",
    "latitude": "signLatitude",
    "address": "signAddressName",
}

# 需要额外并入所有签到提交体的字段（联调时按需填写）
EXTRA_SIGN_FIELDS: Dict[str, Any] = {}


class CheckinError(Exception):
    """自动签到流程无法继续的致命错误（未登录、学期无法识别等）"""


# ============================================================
# 工具函数
# ============================================================
def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def _first(source: Dict[str, Any], keys) -> Any:
    for key in keys:
        if key in source and source[key] is not None:
            return source[key]
    return None


def _to_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def row_is_signed(raw: Mapping[str, Any]) -> bool:
    """行内 signRole 为 3（教师修改）/4（学生已签）或存在签到时间即视为已签。"""
    if _to_int(_first(raw, SIGN_ROLE_KEYS)) in SIGNED_SIGN_ROLES:
        return True
    return any(_first(raw, (k,)) for k in SIGN_TIME_KEYS)


def _term_date(value: Any) -> Optional[date]:
    """学期边界可能是 [年,月,日] 数组、ISO 字符串等，统一解析为 date。"""
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        try:
            return date(int(value[0]), int(value[1]), int(value[2]))
        except (TypeError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()[:10]
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    return None


def resolve_current_term(terms: List[Dict[str, Any]], today: Optional[date] = None) -> Optional[Dict[str, Any]]:
    """从 EDU-01 学期选项里挑出当前学期。

    实测服务端 isCurrentTerm 标志不可靠（多条为 1，按日期的当前学期
    反而为 0），因此优先按“今天落在学期起止日期内”判定；都不满足时
    依次回退到 isCurrentTerm=1 中创建时间最新、结束日期不早于今天、
    创建时间最新的学期。
    """
    today = today or date.today()
    if not terms:
        return None
    in_range = [
        t for t in terms
        if (s := _term_date(t.get("termStartTime"))) and (e := _term_date(t.get("termEndTime")))
        and s <= today <= e
    ]
    if in_range:
        current = [t for t in in_range if _to_int(t.get("isCurrentTerm"), 0) == 1]
        pool = current or in_range
        return max(pool, key=lambda t: t.get("createTime") or 0)
    current = [t for t in terms if _to_int(t.get("isCurrentTerm"), 0) == 1]
    if current:
        return max(current, key=lambda t: t.get("createTime") or 0)
    upcoming = [t for t in terms if (e := _term_date(t.get("termEndTime"))) and e >= today]
    if upcoming:
        return max(upcoming, key=lambda t: t.get("createTime") or 0)
    return max(terms, key=lambda t: t.get("createTime") or 0)


def extract_page_items(page: Any) -> List[Dict[str, Any]]:
    """从分页返回中提取考勤行（兼容 {list}/{records}/{rows} 或裸数组）。"""
    if isinstance(page, list):
        return [item for item in page if isinstance(item, dict)]
    if not isinstance(page, dict):
        return []
    for key in ("list", "records", "rows", "data"):
        inner = page.get(key)
        if isinstance(inner, list):
            return [item for item in inner if isinstance(item, dict)]
    return []


class SignTask:
    """一场考勤中学生维度的签到任务（ATT-01 行归一化结果）。"""

    def __init__(self, raw: Dict[str, Any]):
        self.raw = raw
        self.detail_id = _first(raw, DETAIL_ID_KEYS)
        self.attendance_id = _first(raw, ATTENDANCE_ID_KEYS)
        self.title = str(_first(raw, TITLE_KEYS) or "未命名考勤")
        self.type = _to_int(_first(raw, TYPE_KEYS), ATTENDANCE_TYPE_NORMAL) or 0
        self.location_type = _to_int(_first(raw, LOCATION_TYPE_KEYS), 0) or 0
        self.session_status = _to_int(_first(raw, SESSION_STATUS_KEYS))
        self.signed = self._detect_signed()
        if self.detail_id is None and self.attendance_id is None:
            self.key = "raw:" + json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str)[:120]
        else:
            self.key = f"{self.attendance_id}:{self.detail_id}"

    def _detect_signed(self) -> bool:
        return row_is_signed(self.raw)

    def __repr__(self) -> str:  # 便于调试与日志
        return f"<SignTask {self.title} type={self.type} loc={self.location_type} key={self.key}>"


def build_sign_payload(
    task: SignTask,
    student_id: Any,
    *,
    longitude: Optional[str] = None,
    latitude: Optional[str] = None,
    address: Optional[str] = None,
    refresh_seed: Optional[str] = None,
    qr_sign_type: int = QR_BYPASS_SIGN_TYPE,
) -> Dict[str, Any]:
    """按文档 5/ATT-05 构建 ATT-05 签到提交体。

    二维码签到已改为直接发包：不调用 ATT-04、不携带 qrCodeId；二维码考勤
    (type=1)的 type 由 qr_sign_type 决定（默认 1），并提交伪造的
    refreshSeed（默认 "0"，该直签通道不校验凭证），坐标缺省时用默认值。
    """
    if task.type == ATTENDANCE_TYPE_QR:
        sign_type = qr_sign_type
    else:
        sign_type = task.type if task.type == ATTENDANCE_TYPE_NORMAL else 0
    is_qr_sign = sign_type == ATTENDANCE_TYPE_QR
    payload: Dict[str, Any] = {
        "attendanceId": task.attendance_id,
        "id": task.detail_id,
        "status": SIGN_STATUS_PRESENT,
        "type": sign_type,
        "signRole": SIGN_ROLE_STUDENT,
        "signUserId": student_id,
    }
    if is_qr_sign:
        payload[REFRESH_SEED_FIELD] = (
            refresh_seed if refresh_seed is not None else DIRECT_SIGN_REFRESH_SEED
        )
    if task.location_type == 1 or is_qr_sign:
        options = {
            "longitude": longitude or DEFAULT_SIGN_LONGITUDE,
            "latitude": latitude or DEFAULT_SIGN_LATITUDE,
            "address": address or DEFAULT_SIGN_ADDRESS,
        }
        for option, field in LOCATION_SIGN_FIELDS.items():
            payload[field] = options[option]
    payload.update(EXTRA_SIGN_FIELDS)
    return payload


# ============================================================
# 自动签到机器人
# ============================================================
class AutoCheckinBot:
    """轮询进行中考勤并自动提交签到。

    :param client: 已登录（或令牌可刷新）的 NeumoocClient
    :param interval: 轮询间隔秒数（下限 5 秒）
    :param term_id: 学期 ID；不填则自动识别当前学期
    :param course_id: 只关注指定课程（teachCourseId）
    :param longitude/latitude/address: 定位签到坐标与地址
    :param qr_sign_type: 二维码考勤(type=1)直接发包签到时的提交体 type
        （默认 1，附带 refreshSeed="0" 与默认坐标；仍被拒绝时可改 0）
    :param include_teacher: 教师考勤（type=2）也尝试签到
    :param any_status: 处理所有场次状态（默认跳过明确未开始/已结束的场次）
    :param dry_run: 只打印将要提交的数据
    :param max_attempts: 每场考勤失败重试上限
    :param max_rounds: 最大轮询轮数；None 表示不限（Ctrl+C 停止）
    :param page_size: ATT-01 分页大小（默认不传）
    :param quiet: 安静模式（无考勤数据时不打印每轮日志）
    """

    def __init__(
        self,
        client: NeumoocClient,
        *,
        interval: int = 30,
        term_id: Optional[Any] = None,
        course_id: Optional[Any] = None,
        longitude: Optional[str] = None,
        latitude: Optional[str] = None,
        address: Optional[str] = None,
        qr_sign_type: int = QR_BYPASS_SIGN_TYPE,
        include_teacher: bool = False,
        any_status: bool = False,
        dry_run: bool = False,
        max_attempts: int = 3,
        max_rounds: Optional[int] = None,
        page_size: Optional[int] = None,
        quiet: bool = False,
    ):
        self.client = client
        self.interval = max(5, int(interval))
        self.term_id = term_id
        self.course_id = course_id
        self.longitude = longitude
        self.latitude = latitude
        self.address = address
        self.qr_sign_type = qr_sign_type
        self.include_teacher = include_teacher
        self.any_status = any_status
        self.dry_run = dry_run
        self.max_attempts = max(1, int(max_attempts))
        self.max_rounds = max_rounds
        self.page_size = page_size
        self.quiet = quiet

        self.student_id: Optional[Any] = None
        # key -> "done" 或 "fail:次数"
        self.attempted: Dict[str, str] = {}
        self._skip_logged: set = set()
        self._fatal: Optional[str] = None
        self._totals = {"rounds": 0, "ok": 0, "fail": 0, "skip": 0}

    # --------------------------------------------------------
    # 准备
    # --------------------------------------------------------
    def prepare(self) -> None:
        if not self.client.access_token:
            if not self.client.ensure_logged_in():
                raise CheckinError(
                    "本地没有访问令牌，且没有可自动登录的凭据；"
                    "请先执行 login（可用 --save-credentials 保存密码）"
                )
            log("已通过保存的凭据自动登录")
        self._ensure_student()
        if self.term_id is None:
            terms = self.client.get_term_options()
            items = terms if isinstance(terms, list) else []
            term = resolve_current_term([t for t in items if isinstance(t, dict)])
            if term is None:
                raise CheckinError("未能识别当前学期，请用 --term-id 手动指定")
            self.term_id = term.get("id")
            log(f"当前学期：{term.get('name')}（id={self.term_id}）")

    def _ensure_student(self) -> None:
        if self.student_id is None:
            self.student_id = self.client.user_id
        if not self.student_id:
            raise CheckinError("本地没有用户 ID，请重新登录")

    # --------------------------------------------------------
    # 单轮扫描
    # --------------------------------------------------------
    def scan_once(self) -> Dict[str, int]:
        self._ensure_student()
        body: Dict[str, Any] = {
            "studentId": self.student_id,
            "termId": self.term_id,
            "courseId": self.course_id,
            "attendanceStatus": None,
            # 生产接口会把正在进行的新考勤返回为 status=null。如果在服务端
            # 按 status=1 过滤，这类考勤会完全消失，因此拉取全部记录后再在
            # 本地跳过明确的“未开始/已结束”场次。
            "status": None,
        }
        if self.page_size:
            body.update({"pageNo": 1, "pageSize": self.page_size})
        page = self.client.get_student_attendance_page(body)
        tasks = [SignTask(row) for row in extract_page_items(page)]

        counts = {"rows": len(tasks), "signed": 0, "ok": 0, "fail": 0, "skip": 0}
        for task in tasks:
            try:
                self._process(task, counts)
            except CheckinError:
                raise
            if self._fatal:
                raise CheckinError(self._fatal)
        return counts

    def _process(self, task: SignTask, counts: Dict[str, int]) -> None:
        if task.signed:
            counts["signed"] += 1
            return
        if not self.any_status and task.session_status in (
            SESSION_STATUS_NOT_STARTED,
            SESSION_STATUS_ENDED,
        ):
            self._skip(task, f"考勤状态为 {task.session_status}，不是进行中场次")
            counts["skip"] += 1
            return
        state = self.attempted.get(task.key)
        if state == "done":
            counts["signed"] += 1
            return
        failures = int(state.split(":", 1)[1]) if state else 0
        if failures >= self.max_attempts:
            self._skip(task, f"失败已达 {failures} 次，本轮不再重试")
            counts["skip"] += 1
            return
        if task.attendance_id is None and task.detail_id is None:
            self._skip(
                task,
                "无法识别考勤 ID，请加 --debug 查看返回行结构并调整常量区字段名",
            )
            self._dump_unknown_row(task)
            counts["skip"] += 1
            return
        if task.attendance_id is None:
            self._skip(task, "缺少 attendanceId，无法提交签到")
            counts["skip"] += 1
            return
        if task.detail_id is None:
            # 行内没有明细 ID 时尝试 ATT-03 反查
            if not self._resolve_detail_id(task):
                self._fail(task, counts)
                return
        if task.type == ATTENDANCE_TYPE_TEACHER and not self.include_teacher:
            self._skip(task, "教师考勤默认不签（--include-teacher 可开启）")
            counts["skip"] += 1
            return
        payload = build_sign_payload(
            task, self.student_id,
            longitude=self.longitude, latitude=self.latitude,
            address=self.address,
            qr_sign_type=self.qr_sign_type,
        )
        if self.dry_run:
            log(f"[DRY-RUN] {task.title}：将提交签到 {json.dumps(payload, ensure_ascii=False)}")
            counts["skip"] += 1
            return
        try:
            self.client.submit_attendance(payload)
        except ApiError as exc:
            # 能走到这里说明自动刷新令牌也没能救回 401（无 refreshToken
            # 或刷新失败已清空会话），继续轮询没有意义
            if exc.code == 401 or self.client.access_token is None:
                self._fatal = f"登录态已失效：{exc}"
                return
            message = str(exc.msg or "")
            if any(marker in message for marker in DUPLICATE_SIGN_MARKERS):
                log(f"[OK] {task.title}：服务端提示已签到（{exc}）")
                self.attempted[task.key] = "done"
                counts["signed"] += 1
                return
            log(f"[失败] {task.title}：{exc}")
            self._bump_failure(task)
            counts["fail"] += 1
            return
        except requests.RequestException as exc:
            log(f"[失败] {task.title}：网络异常 {exc}")
            self._bump_failure(task)
            counts["fail"] += 1
            return
        log(f"[OK] {task.title}：签到提交成功")
        self.attempted[task.key] = "done"
        counts["ok"] += 1
        self._totals["ok"] += 1

    # --------------------------------------------------------
    # 辅助
    # --------------------------------------------------------
    def _resolve_detail_id(self, task: SignTask) -> bool:
        """ATT-01 行缺少明细 ID 时，用 ATT-03 反查 getAttendanceDetailId。"""
        try:
            data = self.client.get_attendance_detail_id(task.attendance_id, self.student_id)
        except (ApiError, requests.RequestException) as exc:
            log(f"[失败] {task.title}：反查考勤明细 ID 失败（{exc}）")
            if isinstance(exc, ApiError) and (
                exc.code == 401 or self.client.access_token is None
            ):
                self._fatal = f"登录态已失效：{exc}"
            return False
        if isinstance(data, dict):
            task.detail_id = _first(data, DETAIL_ID_KEYS)
        else:
            task.detail_id = data
        if task.detail_id is None:
            log(f"[失败] {task.title}：ATT-03 未返回明细 ID（data={data!r}）")
            return False
        task.key = f"{task.attendance_id}:{task.detail_id}"
        return True

    def _bump_failure(self, task: SignTask) -> None:
        failures = 1
        state = self.attempted.get(task.key)
        if state and state.startswith("fail"):
            failures = int(state.split(":", 1)[1]) + 1
        self.attempted[task.key] = f"fail:{failures}"
        self._totals["fail"] += 1

    def _fail(self, task: SignTask, counts: Dict[str, int]) -> None:
        self._bump_failure(task)
        counts["fail"] += 1

    def _skip(self, task: SignTask, reason: str) -> None:
        if task.key in self._skip_logged:
            return
        self._skip_logged.add(task.key)
        log(f"[跳过] {task.title}：{reason}")
        self._totals["skip"] += 1

    def _dump_unknown_row(self, task: SignTask) -> None:
        text = json.dumps(task.raw, ensure_ascii=False, default=str)
        log(f"       原始行数据：{text[:400]}{'...' if len(text) > 400 else ''}")

    # --------------------------------------------------------
    # 主循环
    # --------------------------------------------------------
    def run(self) -> int:
        try:
            self.prepare()
        except CheckinError as exc:
            log(f"[错误] {exc}")
            return 1
        mode = "（DRY-RUN 演练）" if self.dry_run else ""
        log(f"自动签到已启动{mode}：间隔 {self.interval}s，Ctrl+C 停止")
        round_no = 0
        try:
            while True:
                round_no += 1
                try:
                    counts = self.scan_once()
                except (ApiError, requests.RequestException) as exc:
                    # 查询接口偶发失败不应终止监视（登录态失效会以
                    # CheckinError 形式抛出并终止）
                    log(f"[失败] 第 {round_no} 轮扫描失败：{exc}")
                    counts = None
                self._totals["rounds"] = round_no
                if counts is not None and (not self.quiet or counts["rows"]):
                    log(
                        f"第 {round_no} 轮：考勤 {counts['rows']} 场，"
                        f"已签 {counts['signed']}，本轮成功 {counts['ok']}，"
                        f"失败 {counts['fail']}，跳过 {counts['skip']}"
                    )
                if self.max_rounds is not None and round_no >= self.max_rounds:
                    break
                time.sleep(self.interval)
        except KeyboardInterrupt:
            log("收到 Ctrl+C，停止自动签到")
        except CheckinError as exc:
            log(f"[错误] {exc}")
            return 1
        totals = self._totals
        log(
            f"结束：共 {totals['rounds']} 轮，签到成功 {totals['ok']} 次，"
            f"失败 {totals['fail']} 次，跳过 {totals['skip']} 项"
        )
        return 0


# ============================================================
# 教师接口强制补签（学生账号 + 教师端考勤列表）
# ============================================================
# 参考已验证的直签通道：学生 token 轮询教师端考勤列表（垂直越权）→
# ATT-03 取本人明细 → PUT detail/update 直签（type=1 + refreshSeed="0" + 坐标）。
# 该直签通道不校验二维码凭证，教师端二维码刷新多快都无关。
FORCE_SIGN_TYPE_DEFAULT = ATTENDANCE_TYPE_QR   # 统一按 type=1 走直签通道
FORCE_SIGN_REFRESH_SEED = DIRECT_SIGN_REFRESH_SEED
FORCE_ENDED_WINDOW_DEFAULT = 120               # --include-ended 的回看分钟数
FORCE_PAGE_SIZE_DEFAULT = 100
FORCE_MIN_INTERVAL = 1                         # 强制补签扫描较快，允许 1 秒
# 已结束场次补签：临时重开考勤窗口的秒数（参考实现用 180s + 每学生 0.4s）
FORCE_REOPEN_SECONDS_DEFAULT = 180
# signRole：4 = 学生本人签到（走“签到”校验，已结束会被拒）；3 = 教师手动修改
# （走教师补签通道，实测可改已结束的场次，与 CLI 的 teacher-makeup 同形态）。
FORCE_SIGN_ROLE_STUDENT = SIGN_ROLE_STUDENT    # 4
FORCE_SIGN_ROLE_TEACHER = 3
# 提交体形态。抓自 Web 前端 courseTeaAttendanceDetail 源码：
#   教师端补签只发 {attendanceId, id, status, signRole:3, signUserId}，
#   不带 type / refreshSeed / 坐标 —— 带这些字段会被服务端当成"学生签到"路径校验，
#   已结束的场次就会返回 1020065005「考勤已结束」。
FORCE_PAYLOAD_STYLE_FULL = "full"        # type=1 + refreshSeed="0" + 坐标（App 直签形态）
FORCE_PAYLOAD_STYLE_TEACHER = "teacher"  # 教师端网页补签形态（最小字段集）
# 教师形态要用教师的 user id 作为 signUserId（id 仍是学生的明细 id）。
# 优先用 --teacher-user-id 显式指定，否则从考勤行里按下列字段名猜。
TEACHER_ID_KEYS = (
    "teacherUserId", "teacherId", "createUserId", "createBy", "creatorId", "userId",
)
# 服务端明确不允许补签的错误码/文案（实测：学生形态对已结束场次返回
# code=1020065005 msg="考勤已结束"）。命中后按配置改教师形态重试，仍失败则跳过。
UNSIGNABLE_SIGN_CODES = (1020065005,)
UNSIGNABLE_SIGN_MARKERS = (
    "考勤已结束", "考勤未开始", "签到时间已过", "不在签到时间内", "已过签到时间",
)


def is_unsignable_error(exc: ApiError) -> bool:
    """服务端是否明确表示该场次无法补签（已结束 / 未开始 / 不在签到时间内）。"""
    if _to_int(exc.code) in UNSIGNABLE_SIGN_CODES:
        return True
    message = str(exc.msg or "")
    return any(marker in message for marker in UNSIGNABLE_SIGN_MARKERS)


def build_force_sign_payload(
    attendance_id: Any,
    detail_id: Any,
    student_id: Any,
    *,
    sign_type: int = FORCE_SIGN_TYPE_DEFAULT,
    refresh_seed: Optional[str] = None,
    longitude: Optional[str] = None,
    latitude: Optional[str] = None,
    address: Optional[str] = None,
    sign_role: int = FORCE_SIGN_ROLE_STUDENT,
    sign_user_id: Optional[Any] = None,
    extra: Optional[Mapping[str, Any]] = None,
    style: str = FORCE_PAYLOAD_STYLE_FULL,
) -> Dict[str, Any]:
    """教师接口强制补签的提交体。

    与普通签到不同：无论考勤原本是什么类型，默认都按 type=1（二维码直签通道）
    提交，且始终携带坐标（缺省用默认学校坐标）；type=1 时附带伪造 refreshSeed。
    sign_role=4 为学生签到形态（signUserId=学生），sign_role=3 为教师手动修改形态
    （signUserId 应传教师 id，可用 sign_user_id 覆盖）。
    extra 里的字段会在最后覆盖/追加，便于联调时补 signTime 等未知字段。
    """
    payload: Dict[str, Any] = {
        "attendanceId": attendance_id,
        "id": detail_id,
        "status": SIGN_STATUS_PRESENT,
        "type": sign_type,
        "signRole": sign_role,
        "signUserId": sign_user_id if sign_user_id is not None else student_id,
        "signLongitude": longitude or DEFAULT_SIGN_LONGITUDE,
        "signLatitude": latitude or DEFAULT_SIGN_LATITUDE,
        "signAddressName": address or DEFAULT_SIGN_ADDRESS,
    }
    if sign_type == ATTENDANCE_TYPE_QR:
        payload[REFRESH_SEED_FIELD] = (
            refresh_seed if refresh_seed is not None else FORCE_SIGN_REFRESH_SEED
        )
    payload.update(EXTRA_SIGN_FIELDS)
    if extra:
        payload.update(extra)
    return payload


def build_teacher_style_payload(
    attendance_id: Any,
    detail_id: Any,
    sign_user_id: Any,
    *,
    status: int = SIGN_STATUS_PRESENT,
    sign_role: int = FORCE_SIGN_ROLE_TEACHER,
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """教师端网页补签形态的最小提交体（抓包自 courseTeaAttendanceDetail）。

    只发 {attendanceId, id, status, signRole, signUserId}；不带 type /
    refreshSeed / 坐标，避免被服务端按"学生签到"路径校验时间窗口。
    """
    payload: Dict[str, Any] = {
        "attendanceId": attendance_id,
        "id": detail_id,
        "status": status,
        "signRole": sign_role,
        "signUserId": sign_user_id,
    }
    payload.update(EXTRA_SIGN_FIELDS)
    if extra:
        payload.update(extra)
    return payload


class ForceCheckinBot:
    """使用教师端考勤列表接口（仍用学生账号）强制补签。

    流程：
      1. EDU-03 按学期取课程列表（--course-id 指定时直接用指定课程）；
      2. 逐课程 GET 教师端 teach-course-attendance/page，挑出
         openTime<=now<finishTime 的场次（--include-ended 时追加最近结束的场次）；
      3. ATT-03 getAttendanceDetailId/{attendanceId}/{studentId} 取本人明细，
         为空说明不在该班，跳过；
      4. ATT-05 detail/update 直签（type=1 + refreshSeed="0" + 坐标）；
      5. 可选回读 ATT-02 确认 status=1。

    :param client: 已登录（或凭据可自动登录）的 NeumoocClient（学生账号）
    :param interval: 扫描间隔秒数（下限 1）
    :param term_id: 学期 ID；不填则自动识别当前学期
    :param course_ids: 只补签这些课程（teachCourseId）；为空则扫描该学期全部课程
    :param longitude/latitude/address: 签到坐标，缺省用默认学校坐标
    :param sign_type: 提交体 type（默认 1，走已验证的直签通道）
    :param refresh_seed: 伪造的 refreshSeed（默认 "0"）
    :param sign_role: 提交体 signRole：4=学生签到形态（默认），3=教师手动修改形态
    :param teacher_fallback: 学生形态被拒（如“考勤已结束”）时，自动改用教师形态
        （signRole=3 + signUserId=教师 id）再试一次；默认开启
    :param teacher_user_id: 教师形态用的 signUserId（教师 user id）。不填时先尝试从
        考勤行的 teacherUserId/teacherId/createBy 等字段自动取，仍拿不到就沿用
        当前 signUserId（默认即登录用户自己）
    :param include_ended: 是否也补签最近已结束的场次
    :param ended_within_minutes: include_ended 时的回看窗口（分钟）
    :param dry_run: 只打印提交体，不实际提交
    :param max_attempts: 每场失败重试上限
    :param max_rounds: 最大轮数；None 表示不限（Ctrl+C 停止）
    :param page_size: 教师端考勤列表分页大小
    :param quiet: 无待补场次时不打印每轮日志
    :param verify: 提交成功后回读 ATT-02 校验
    """

    def __init__(
        self,
        client: NeumoocClient,
        *,
        interval: int = 5,
        term_id: Optional[Any] = None,
        course_ids: Optional[Sequence[Any]] = None,
        longitude: Optional[str] = None,
        latitude: Optional[str] = None,
        address: Optional[str] = None,
        sign_type: int = FORCE_SIGN_TYPE_DEFAULT,
        refresh_seed: Optional[str] = None,
        sign_role: int = FORCE_SIGN_ROLE_STUDENT,
        teacher_fallback: bool = True,
        teacher_user_id: Optional[Any] = None,
        payload_style: str = FORCE_PAYLOAD_STYLE_FULL,
        skip_signed: bool = True,
        signed_page_size: int = 200,
        reopen_ended: bool = False,
        reopen_seconds: int = FORCE_REOPEN_SECONDS_DEFAULT,
        sign_status: int = SIGN_STATUS_PRESENT,
        sign_time: Optional[Any] = None,
        omit_sign_time: bool = False,
        extra_fields: Optional[Mapping[str, Any]] = None,
        submit_path: Optional[str] = None,
        include_ended: bool = False,
        ended_within_minutes: int = FORCE_ENDED_WINDOW_DEFAULT,
        dry_run: bool = False,
        max_attempts: int = 3,
        max_rounds: Optional[int] = None,
        page_size: int = FORCE_PAGE_SIZE_DEFAULT,
        quiet: bool = False,
        verify: bool = True,
    ):
        self.client = client
        self.interval = max(FORCE_MIN_INTERVAL, int(interval))
        self.term_id = term_id
        self.course_ids = [c for c in (course_ids or []) if c not in (None, "")]
        self.longitude = longitude
        self.latitude = latitude
        self.address = address
        self.sign_type = sign_type
        self.refresh_seed = refresh_seed
        self.sign_role = sign_role
        self.teacher_fallback = teacher_fallback
        self.teacher_user_id = teacher_user_id
        self.payload_style = payload_style
        self.skip_signed = skip_signed
        self.signed_page_size = max(1, int(signed_page_size))
        self.reopen_ended = reopen_ended
        self.reopen_seconds = max(1, int(reopen_seconds))
        self.sign_status = int(sign_status)
        self.sign_time = sign_time
        self.omit_sign_time = omit_sign_time
        self.extra_fields = dict(extra_fields or {})
        self.submit_path = submit_path
        self.include_ended = include_ended
        self.ended_within_minutes = max(0, int(ended_within_minutes))
        self.dry_run = dry_run
        self.max_attempts = max(1, int(max_attempts))
        self.max_rounds = max_rounds
        self.page_size = max(1, int(page_size))
        self.quiet = quiet
        self.verify = verify

        self.student_id: Optional[Any] = None
        self.attempted: Dict[str, str] = {}
        self._skip_logged: set = set()
        self._fatal: Optional[str] = None
        self._ended_hint_shown = False
        # course_id -> 教师 user id（teach-course/get 的 teacherId）
        self._teacher_id_cache: Dict[str, Any] = {}
        # attendanceId -> 本人该场原有的 signTime（补签时原样写回，不记成"现在"）
        self._own_sign_times: Dict[str, Any] = {}
        self._totals = {"rounds": 0, "ok": 0, "fail": 0, "skip": 0}

    # --------------------------------------------------------
    # 准备
    # --------------------------------------------------------
    def prepare(self) -> None:
        if not self.client.access_token:
            if not self.client.ensure_logged_in():
                raise CheckinError(
                    "本地没有访问令牌，且没有可自动登录的凭据；"
                    "请先执行 login（可用 --save-credentials 保存密码）"
                )
            log("已通过保存的凭据自动登录")
        self._ensure_student()
        if self.term_id is None:
            terms = self.client.get_term_options()
            items = terms if isinstance(terms, list) else []
            term = resolve_current_term([t for t in items if isinstance(t, dict)])
            if term is None:
                raise CheckinError("未能识别当前学期，请用 --term-id 手动指定")
            self.term_id = term.get("id")
            log(f"当前学期：{term.get('name')}（id={self.term_id}）")

    def _ensure_student(self) -> None:
        if self.student_id is None:
            self.student_id = self.client.user_id
        if not self.student_id:
            raise CheckinError("本地没有用户 ID，请重新登录")

    # --------------------------------------------------------
    # 课程与场次
    # --------------------------------------------------------
    def _courses(self) -> List[Tuple[Any, str]]:
        """返回待扫描的 (teachCourseId, 课程名) 列表（--course-id 优先）。"""
        if self.course_ids:
            return [(cid, str(cid)) for cid in self.course_ids]
        data = self.client.get_course_options_by_term({"termId": self.term_id})
        out: List[Tuple[Any, str]] = []
        for item in extract_page_items(data):
            cid = _first(item, ("teachCourseId", "courseId", "id"))
            if cid is None:
                continue
            name = _first(item, ("teachCourseName", "courseName", "name"))
            out.append((cid, str(name or cid)))
        return out

    def _sessions(
        self, course_id: Any, now_ms: int
    ) -> Tuple[int, List[Dict[str, Any]]]:
        """教师端考勤列表里挑出待补签的场次。

        返回 (该课程考勤记录总数, 待补签场次列表)。
        """
        params = {
            "teachCourseId": course_id,
            "searchReq": "",
            "pageNo": 1,
            "pageSize": self.page_size,
        }
        page = self.client.get_teacher_attendance_page(params)
        rows = extract_page_items(page)
        window_ms = self.ended_within_minutes * 60_000
        out: List[Dict[str, Any]] = []
        for row in rows:
            open_ms = _to_int(row.get("openTime"), 0) or 0
            finish_ms = _to_int(row.get("finishTime"), 0) or 0
            if not finish_ms:
                continue
            if open_ms <= now_ms < finish_ms:
                out.append(row)
            elif (
                self.include_ended
                and finish_ms <= now_ms
                and (window_ms == 0 or finish_ms >= now_ms - window_ms)
            ):
                out.append(row)
        return len(rows), out

    # --------------------------------------------------------
    # 单轮扫描
    # --------------------------------------------------------
    def _own_attendance_index(self) -> Tuple[set, Dict[str, Any]]:
        """拉一份本人的考勤列表：已签的 attendanceId 集合 + 各场原有的 signTime。

        signTime 用于补签时原样写回（不要把老师那次的修改时间改成"现在"）。
        """
        signed: set = set()
        sign_times: Dict[str, Any] = {}
        try:
            page = self.client.get_student_attendance_page({
                "studentId": self.student_id,
                "termId": self.term_id,
                "courseId": None,
                "attendanceStatus": None,
                "status": None,
                "pageNo": 1,
                "pageSize": self.signed_page_size,
            })
        except (ApiError, requests.RequestException) as exc:
            log(f"[!] 拉取本人考勤列表失败（{exc}），本轮不做已签过滤")
            return signed, sign_times
        for row in extract_page_items(page):
            aid = _first(row, ATTENDANCE_ID_KEYS)
            if aid is None:
                continue
            key = str(aid)
            sign_time = _first(row, SIGN_TIME_KEYS)
            if sign_time is not None:
                sign_times[key] = sign_time
            if row_is_signed(row):
                signed.add(key)
        return signed, sign_times

    def scan_once(self) -> Dict[str, int]:
        self._ensure_student()
        now_ms = int(time.time() * 1000)
        # 拉本人考勤列表：用于「跳过已签」，也用于补签时写回该场原有的 signTime
        need_index = self.skip_signed or (
            self.reopen_ended and self.sign_time is None
        )
        signed_ids: set = set()
        if need_index:
            signed_ids, self._own_sign_times = self._own_attendance_index()
            if not self.skip_signed:
                signed_ids = set()
        counts = {
            "courses": 0, "records": 0, "sessions": 0, "signed": 0,
            "ok": 0, "fail": 0, "skip": 0,
        }
        for course_id, course_name in self._courses():
            counts["courses"] += 1
            total, rows = self._sessions(course_id, now_ms)
            counts["records"] += total
            for row in rows:
                if self._fatal:
                    raise CheckinError(self._fatal)
                attendance_id = _first(row, ("id", "attendanceId"))
                key = f"force:{attendance_id}"
                if self.attempted.get(key) in ("done", "unsignable"):
                    continue
                if str(attendance_id) in signed_ids:
                    # 本人已签（含教师已改）：不重复提交
                    self.attempted[key] = "done"
                    counts["signed"] += 1
                    continue
                counts["sessions"] += 1
                self._process(row, course_id, course_name, counts)
        # 有考勤记录但一条都不在时间窗口内：说明只能等下一场开始
        if (
            counts["records"]
            and not counts["sessions"]
            and not self._ended_hint_shown
        ):
            self._ended_hint_shown = True
            log(
                "    （该范围查到考勤记录，但当前没有“进行中”的场次；"
                "服务端不允许补签已结束的场次，等下一场开始后会立刻签上）"
            )
        if self._fatal:
            raise CheckinError(self._fatal)
        return counts

    def _process(
        self,
        row: Dict[str, Any],
        course_id: Any,
        course_name: str,
        counts: Dict[str, int],
    ) -> None:
        attendance_id = _first(row, ("id", "attendanceId"))
        title = str(_first(row, ("title", "attendanceName", "name")) or "未命名考勤")
        label = f"{title}（{course_name}）"
        if attendance_id is None:
            log(f"[跳过] {label}：无法识别 attendanceId")
            counts["skip"] += 1
            return
        key = f"force:{attendance_id}"
        state = self.attempted.get(key)
        failures = int(state.split(":", 1)[1]) if state and state.startswith("fail") else 0
        if failures >= self.max_attempts:
            self._skip(key, label, f"失败已达 {failures} 次，不再重试")
            counts["skip"] += 1
            return

        detail_id = self._detail_id(attendance_id)
        if not detail_id:
            # ATT-03 返回空：本人不在该考勤班级
            if self._fatal:
                return
            self._skip(key, label, "无本人明细（不在该考勤班级），跳过")
            counts["skip"] += 1
            return

        teacher_id: Optional[Any] = None
        if (
            self.payload_style == FORCE_PAYLOAD_STYLE_TEACHER
            or self.sign_role == FORCE_SIGN_ROLE_TEACHER
        ):
            # 只有走教师形态时才去解析教师 ID（会多一次 teach-course/get，按课程缓存）
            teacher_id = self._teacher_id_for(row, course_id)
        if self.payload_style == FORCE_PAYLOAD_STYLE_TEACHER:
            # 教师端网页补签形态：最小字段集，不带 type/refreshSeed/坐标
            payload = build_teacher_style_payload(
                attendance_id, detail_id, teacher_id or self.student_id,
                extra=self.extra_fields,
            )
        else:
            if self.sign_role == FORCE_SIGN_ROLE_TEACHER and teacher_id is None:
                log(f"[!] {label}：未提供 --teacher-user-id 且考勤行没有教师字段，"
                    "signUserId 默认沿用当前用户 ID")
            payload = build_force_sign_payload(
                attendance_id, detail_id, self.student_id,
                sign_type=self.sign_type, refresh_seed=self.refresh_seed,
                longitude=self.longitude, latitude=self.latitude, address=self.address,
                sign_role=self.sign_role,
                sign_user_id=(teacher_id if self.sign_role == FORCE_SIGN_ROLE_TEACHER
                              else self.student_id),
                extra=self.extra_fields,
            )
        if self.dry_run:
            log(f"[DRY-RUN] {label}：将强制补签 {json.dumps(payload, ensure_ascii=False)}")
            counts["skip"] += 1
            return
        try:
            self._submit(payload)
        except ApiError as exc:
            if exc.code == 401 or self.client.access_token is None:
                self._fatal = f"登录态已失效：{exc}"
                return
            if is_unsignable_error(exc):
                # 已结束场次：临时重开考勤窗口 → 补签 → 立即恢复（参考已验证做法）
                if self.reopen_ended:
                    if self._sign_with_reopen(
                        row, course_id, attendance_id, detail_id, label, key, counts
                    ):
                        return
                    if self._fatal:
                        return
                    # 重开路径已失败，窗口也已还原，再试普通教师形态没有意义
                    self._bump(key, failures)
                    counts["fail"] += 1
                    return
                # 学生形态被拒（如“考勤已结束”）：改教师手动修改形态再试一次
                if (
                    self.sign_role != FORCE_SIGN_ROLE_TEACHER
                    and self.payload_style != FORCE_PAYLOAD_STYLE_TEACHER
                    and self.teacher_fallback
                    and self._retry_as_teacher(
                        payload, detail_id, label, key, counts,
                        self._teacher_id_for(row, course_id),
                    )
                ):
                    return
                log(f"[跳过] {label}：{exc}（服务端不允许该场次补签，不再重试）")
                self.attempted[key] = "unsignable"
                counts["skip"] += 1
                self._totals["skip"] += 1
                return
            message = str(exc.msg or "")
            if any(marker in message for marker in DUPLICATE_SIGN_MARKERS):
                log(f"[OK] {label}：服务端提示已签到（{exc}）")
                self.attempted[key] = "done"
                counts["ok"] += 1
                self._totals["ok"] += 1
                return
            log(f"[失败] {label}：{exc}")
            self._bump(key, failures)
            counts["fail"] += 1
            return
        except requests.RequestException as exc:
            log(f"[失败] {label}：网络异常 {exc}")
            self._bump(key, failures)
            counts["fail"] += 1
            return

        log(f"[OK] {label}：强制补签提交成功")
        if self.verify:
            self._verify(detail_id, label)
        self.attempted[key] = "done"
        counts["ok"] += 1
        self._totals["ok"] += 1

    # --------------------------------------------------------
    # 辅助
    # --------------------------------------------------------
    def _submit(self, payload: Dict[str, Any]) -> Any:
        """提交补签；submit_path 非空时改用指定路由（联调时指向抓包得到的教师端路由）。"""
        if self.submit_path:
            return self.client.request_api("PUT", self.submit_path, body=payload)
        return self.client.submit_attendance(payload)

    def _detail_id(self, attendance_id: Any) -> Optional[Any]:
        """ATT-03 反查本人明细 ID；None 表示不在该班或查询失败。"""
        try:
            data = self.client.get_attendance_detail_id(attendance_id, self.student_id)
        except (ApiError, requests.RequestException) as exc:
            log(f"[失败] 反查考勤明细失败（attendanceId={attendance_id}）：{exc}")
            if isinstance(exc, ApiError) and (
                exc.code == 401 or self.client.access_token is None
            ):
                self._fatal = f"登录态已失效：{exc}"
            return None
        if isinstance(data, dict):
            return _first(data, DETAIL_ID_KEYS)
        return data

    def _teacher_id_from_row(self, row: Dict[str, Any]) -> Optional[Any]:
        """从考勤行里猜教师 user id（不同环境字段名可能不同）。"""
        return _first(row, TEACHER_ID_KEYS)

    def _teacher_id_for(self, row: Dict[str, Any], course_id: Any) -> Optional[Any]:
        """解析补签用的 signUserId（教师 id）：显式指定 → 考勤行教师字段 → 课程 teacherId。

        教师 id 取自 teach-course/get 的 teacherId；都拿不到才回退到当前用户 id。
        """
        return (
            self.teacher_user_id
            or self._teacher_id_from_row(row)
            or self._teacher_id_for_course(course_id)
        )

    def _teacher_id_for_course(self, course_id: Any) -> Optional[Any]:
        """从课程信息里取教师 user id：teach-course/get 的 teacherId（按课程缓存）。"""
        key = str(course_id)
        if key in self._teacher_id_cache:
            return self._teacher_id_cache[key]
        teacher_id: Optional[Any] = None
        try:
            data = self.client.get_course({"id": course_id})
            if isinstance(data, dict):
                teacher_id = _first(data, TEACHER_ID_KEYS)
        except (ApiError, requests.RequestException) as exc:
            log(f"[!] 取课程 {course_id} 的教师 ID 失败（{exc}）")
        self._teacher_id_cache[key] = teacher_id
        return teacher_id

    def _sign_time_for(self, attendance_id: Any) -> Optional[Any]:
        """补签要写回的 signTime：显式 --sign-time 优先，否则沿用该场原有的签到时间。"""
        if self.omit_sign_time:
            return None
        if self.sign_time is not None:
            return self.sign_time
        return self._own_sign_times.get(str(attendance_id))

    def _teacher_payload(
        self, attendance_id: Any, detail_id: Any, row: Dict[str, Any], course_id: Any
    ) -> Dict[str, Any]:
        """教师端补签形态：signRole=3 + signUserId=教师 id + 原 signTime。"""
        payload = build_teacher_style_payload(
            attendance_id,
            detail_id,
            self._teacher_id_for(row, course_id) or self.student_id,
            status=self.sign_status,
            extra=self.extra_fields,
        )
        sign_time = self._sign_time_for(attendance_id)
        if sign_time is not None:
            payload.setdefault("signTime", sign_time)
        return payload

    def _reopen_attendance(
        self, attendance_id: Any, status: int, finish_time: Any
    ) -> None:
        """教师端「考勤主表更新」——用于临时重开/恢复结束。"""
        self.client.update_teacher_attendance({
            "id": attendance_id, "status": status, "finishTime": finish_time,
        })

    def _retry_as_teacher(
        self,
        payload: Dict[str, Any],
        detail_id: Any,
        label: str,
        key: str,
        counts: Dict[str, int],
        teacher_id: Optional[Any] = None,
    ) -> bool:
        """学生形态被拒时，改用教师手动修改形态（signRole=3 + signUserId=教师ID）再试一次。

        返回 True 表示已补签成功（调用方直接 return）。
        """
        # 改用 Web 教师端补签的最小字段集：只有 attendanceId/id/status/signRole/signUserId，
        # 去掉 type / refreshSeed / 坐标（这些会让服务端走"学生签到"校验路径）。
        teacher_payload = build_teacher_style_payload(
            payload.get("attendanceId"),
            detail_id,
            teacher_id if teacher_id not in (None, "") else payload.get("signUserId"),
            extra=self.extra_fields,
        )
        sign_time = self._sign_time_for(payload.get("attendanceId"))
        if sign_time is not None:
            teacher_payload.setdefault("signTime", sign_time)
        log(f"[..] {label}：学生形态被拒，改用教师端补签形态"
            f"（signRole=3, signUserId={teacher_payload.get('signUserId')}，"
            f"去掉 type/refreshSeed/坐标）重试")
        try:
            self._submit(teacher_payload)
        except ApiError as exc:
            if exc.code == 401 or self.client.access_token is None:
                self._fatal = f"登录态已失效：{exc}"
                return False
            message = str(exc.msg or "")
            if any(marker in message for marker in DUPLICATE_SIGN_MARKERS):
                log(f"[OK] {label}：服务端提示已签到（{exc}）")
                self.attempted[key] = "done"
                counts["ok"] += 1
                self._totals["ok"] += 1
                return True
            log(f"[跳过] {label}：教师补签形态也被拒（{exc}）")
            return False
        except requests.RequestException as exc:
            log(f"[失败] {label}：教师补签形态网络异常 {exc}")
            return False
        log(f"[OK] {label}：教师补签形态（signRole=3）提交成功")
        if self.verify:
            self._verify(detail_id, label)
        self.attempted[key] = "done"
        counts["ok"] += 1
        self._totals["ok"] += 1
        return True

    def _sign_with_reopen(
        self,
        row: Dict[str, Any],
        course_id: Any,
        attendance_id: Any,
        detail_id: Any,
        label: str,
        key: str,
        counts: Dict[str, int],
    ) -> bool:
        """临时重开已结束的考勤 → 补签 → 立即恢复结束。

        与参考实现（neumooc-checkin/dashboard.py batch_modify）一致：
          1. GET teach-course-attendance/get 读原 status / finishTime；
          2. PUT teach-course-attendance/update {id, status:1, finishTime:now+窗口}；
          3. PUT detail/update 补签（signRole=3，signUserId=当前用户，status=sign_status）；
          4. finally PUT teach-course-attendance/update 还原原 status / finishTime。
        """
        try:
            att = self.client.get_teacher_attendance({"id": attendance_id})
        except (ApiError, requests.RequestException) as exc:
            log(f"[失败] {label}：读取考勤主表失败（{exc}）")
            return False
        if not isinstance(att, dict):
            log(f"[失败] {label}：考勤主表返回异常（{att!r}）")
            return False
        orig_status = att.get("status")
        orig_finish = att.get("finishTime")
        buf_ms = int(time.time() * 1000) + self.reopen_seconds * 1000

        reopened = False
        try:
            try:
                self._reopen_attendance(attendance_id, 1, buf_ms)
                reopened = True
                time.sleep(0.6)
            except (ApiError, requests.RequestException) as exc:
                log(f"[失败] {label}：临时重开考勤失败（{exc}）")
                return False

            payload = self._teacher_payload(attendance_id, detail_id, row, course_id)
            log(f"[..] {label}：已临时重开考勤，改用 signRole=3 补签"
                f"（signUserId={payload['signUserId']}, status={self.sign_status}"
                f"{', signTime=' + str(payload['signTime']) if 'signTime' in payload else ''}）")
            try:
                self._submit(payload)
            except ApiError as exc:
                if exc.code == 401 or self.client.access_token is None:
                    self._fatal = f"登录态已失效：{exc}"
                    return False
                message = str(exc.msg or "")
                if not any(m in message for m in DUPLICATE_SIGN_MARKERS):
                    log(f"[失败] {label}：重开后补签仍被拒（{exc}）")
                    return False
                log(f"[OK] {label}：服务端提示已签到（{exc}）")
            except requests.RequestException as exc:
                log(f"[失败] {label}：重开后补签网络异常（{exc}）")
                return False

            log(f"[OK] {label}：补签提交成功（signRole=3, status={self.sign_status}）")
            if self.verify:
                self._verify(detail_id, label)
            self.attempted[key] = "done"
            counts["ok"] += 1
            self._totals["ok"] += 1
            return True
        finally:
            if reopened:
                try:
                    self._reopen_attendance(attendance_id, orig_status, orig_finish)
                    log(f"      已恢复考勤原状态（status={orig_status}, "
                        f"finishTime={orig_finish}）")
                except (ApiError, requests.RequestException) as exc:
                    log(f"[!] 恢复考勤原状态失败：{exc}"
                        f"（请手动检查 attendanceId={attendance_id}）")

    def _verify(self, detail_id: Any, label: str) -> None:
        """回读 ATT-02 确认签到是否真的落库（失败不影响成功计数）。"""
        try:
            data = self.client.get_student_attendance_detail(detail_id)
        except (ApiError, requests.RequestException) as exc:
            log(f"      回读失败：{exc}")
            return
        if not isinstance(data, dict):
            return
        status = _to_int(data.get("status"))
        sign_time = _first(data, SIGN_TIME_KEYS)
        if status == 1 or sign_time:
            log(f"      ✅ 回读确认已签：status={status} signTime={sign_time}")
        else:
            log(f"      ⚠ 回读未确认：status={status} signTime={sign_time}（可下一轮复查）")

    def _skip(self, key: str, label: str, reason: str) -> None:
        if key in self._skip_logged:
            return
        self._skip_logged.add(key)
        log(f"[跳过] {label}：{reason}")
        self._totals["skip"] += 1

    def _bump(self, key: str, failures: int) -> None:
        self.attempted[key] = f"fail:{failures + 1}"
        self._totals["fail"] += 1

    # --------------------------------------------------------
    # 主循环
    # --------------------------------------------------------
    def run(self) -> int:
        try:
            self.prepare()
        except CheckinError as exc:
            log(f"[错误] {exc}")
            return 1
        mode = "（DRY-RUN 演练）" if self.dry_run else ""
        scope = (
            "指定课程 " + ", ".join(str(c) for c in self.course_ids)
            if self.course_ids else "该学期全部课程"
        )
        ended = (
            f"，含最近 {self.ended_within_minutes} 分钟内结束的场次"
            if self.include_ended else ""
        )
        log(
            f"教师接口强制补签已启动{mode}：范围={scope}{ended}，"
            f"间隔 {self.interval}s，Ctrl+C 停止"
        )
        round_no = 0
        try:
            while True:
                round_no += 1
                try:
                    counts = self.scan_once()
                except (ApiError, requests.RequestException) as exc:
                    log(f"[失败] 第 {round_no} 轮扫描失败：{exc}")
                    counts = None
                self._totals["rounds"] = round_no
                if counts is not None and (not self.quiet or counts["sessions"]):
                    log(
                        f"第 {round_no} 轮：课程 {counts['courses']} 门，"
                        f"考勤记录 {counts['records']} 条，已签 {counts['signed']}，"
                        f"待补场次 {counts['sessions']}，成功 {counts['ok']}，"
                        f"失败 {counts['fail']}，跳过 {counts['skip']}"
                    )
                if self.max_rounds is not None and round_no >= self.max_rounds:
                    break
                time.sleep(self.interval)
        except KeyboardInterrupt:
            log("收到 Ctrl+C，停止强制补签")
        except CheckinError as exc:
            log(f"[错误] {exc}")
            return 1
        totals = self._totals
        log(
            f"结束：共 {totals['rounds']} 轮，强制补签成功 {totals['ok']} 次，"
            f"失败 {totals['fail']} 次，跳过 {totals['skip']} 项"
        )
        return 0
