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
  * 定位签到（type=0 且 attendanceLocationType=1）：需要提供 --longitude/--latitude
    （可选 --address），否则跳过并提示；提交体使用 sign* 前缀字段；
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

常量区字段名/取值若与实际服务端返回不一致（ATT-01 行结构为“对象透传”，
文档未给出明细），请用 --debug 观察原始数据后调整常量。
"""

from __future__ import annotations

import json
import time
from datetime import date
from typing import Any, Dict, List, Optional

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
        sign_role = _to_int(_first(self.raw, SIGN_ROLE_KEYS))
        if sign_role in SIGNED_SIGN_ROLES:
            return True
        return any(_first(self.raw, (k,)) for k in SIGN_TIME_KEYS)

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
        options = {"longitude": longitude, "latitude": latitude, "address": address}
        if is_qr_sign:
            options["longitude"] = options["longitude"] or DEFAULT_SIGN_LONGITUDE
            options["latitude"] = options["latitude"] or DEFAULT_SIGN_LATITUDE
            options["address"] = options["address"] or DEFAULT_SIGN_ADDRESS
        for option, field in LOCATION_SIGN_FIELDS.items():
            value = options.get(option)
            if value is not None:
                payload[field] = value
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
            # 新发布且进行中的考勤可能返回 status=null，服务端按 status=1
            # 过滤会漏掉它，因此拉取全部记录后在本地过滤明确的 0/2。
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
        if task.location_type == 1 and not (self.longitude and self.latitude):
            self._skip(task, "定位签到需要 --longitude/--latitude，未提供则跳过")
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
