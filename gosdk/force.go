package neumooc

import (
	"encoding/json"
	"fmt"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"time"
)

// 教师接口强制补签常量（与 neumooc_checkin.py 的 ForceCheckinBot 对齐）
const (
	ForceSignTypeDefault    = AttendanceTypeQR
	ForceSignRefreshSeed    = DirectSignRefreshSeed
	ForceEndedWindowDefault = 120
	ForcePageSizeDefault    = 100
	ForceMinInterval        = 1
	// ForceReopenSecondsDefault 已结束场次补签时临时重开窗口的秒数
	ForceReopenSecondsDefault = 180
)

// SignRoleTeacher 教师手动修改形态；SignRoleStudent（4）为学生签到形态。
// 教师形态要用教师的 user id 作为 signUserId（id 仍是学生的明细 id）。
const SignRoleTeacher = 3

// 提交体形态。抓自 Web 前端 courseTeaAttendanceDetail 源码：教师端补签只发
// {attendanceId, id, status, signRole:3, signUserId}，不带 type / refreshSeed / 坐标，
// 否则会被服务端按"学生签到"路径校验，已结束场次返回 1020065005「考勤已结束」。
const (
	PayloadStyleFull    = "full"    // App 直签形态（type=1 + refreshSeed + 坐标）
	PayloadStyleTeacher = "teacher" // Web 教师端补签形态（最小字段集）
)

// BuildTeacherStylePayload 教师端网页补签形态的最小提交体。
func BuildTeacherStylePayload(
	attendanceID, detailID, signUserID interface{},
	extra map[string]interface{},
) map[string]interface{} {
	payload := map[string]interface{}{
		"attendanceId": attendanceID,
		"id":           detailID,
		"status":       SignStatusPresent,
		"signRole":     SignRoleTeacher,
		"signUserId":   signUserID,
	}
	for k, v := range extra {
		payload[k] = v
	}
	return payload
}

// 从考勤行里猜教师 user id 的候选字段名。
var teacherIDKeys = []string{
	"teacherUserId", "teacherId", "createUserId", "createBy", "creatorId", "userId",
}

func teacherIDFromRow(row map[string]interface{}) interface{} {
	return first(row, teacherIDKeys...)
}

// SignTimeKeys 候选取自 checkin.go 的 signTimeKeys（同包）。

// teacherIDFor 解析补签用的 signUserId（教师 id）：
// 显式 --teacher-user-id → 考勤行教师字段 → 课程 teacherId（teach-course/get）。
func (b *ForceCheckinBot) teacherIDFor(row map[string]interface{}, courseID interface{}) interface{} {
	if b.Cfg.TeacherUserID != nil {
		return b.Cfg.TeacherUserID
	}
	if tid := teacherIDFromRow(row); tid != nil {
		return tid
	}
	key := fmt.Sprint(courseID)
	if v, ok := b.teacherIDCache[key]; ok {
		return v
	}
	var tid interface{}
	if course, err := b.Client.GetCourse(courseID); err == nil {
		tid = first(course, teacherIDKeys...)
	} else {
		b.logf("[!] 取课程 %v 的教师 ID 失败（%v）", courseID, err)
	}
	b.teacherIDCache[key] = tid
	return tid
}

// signTimeFor 补签要写回的 signTime：显式 Cfg.SignTime 优先，否则沿用该场原有签到时间。
func (b *ForceCheckinBot) signTimeFor(attendanceID interface{}) interface{} {
	if b.Cfg.SignTime != nil {
		return b.Cfg.SignTime
	}
	return b.ownSignTimes[fmt.Sprint(attendanceID)]
}

// teacherPayload 教师端补签形态：signRole=3 + signUserId=教师 id + 原 signTime。
func (b *ForceCheckinBot) teacherPayload(
	attendanceID, detailID interface{}, row map[string]interface{}, courseID interface{},
) map[string]interface{} {
	signUser := b.teacherIDFor(row, courseID)
	if signUser == nil {
		signUser = b.studentID
	}
	payload := BuildTeacherStylePayload(attendanceID, detailID, signUser, b.Cfg.ExtraFields)
	if b.Cfg.SignStatus != 0 {
		payload["status"] = b.Cfg.SignStatus
	}
	if st := b.signTimeFor(attendanceID); st != nil {
		if _, exists := payload["signTime"]; !exists {
			payload["signTime"] = st
		}
	}
	return payload
}

// ownAttendanceIndex 拉一份本人考勤列表：已签 attendanceId 集合 + 各场原有 signTime。
func (b *ForceCheckinBot) ownAttendanceIndex() (map[string]struct{}, map[string]interface{}) {
	signed := map[string]struct{}{}
	signTimes := map[string]interface{}{}
	page, err := b.Client.GetStudentAttendancePage(map[string]interface{}{
		"studentId":        b.studentID,
		"termId":           b.Cfg.TermID,
		"courseId":         nil,
		"attendanceStatus": nil,
		"status":           nil,
		"pageNo":           1,
		"pageSize":         b.Cfg.SignedPageSize,
	})
	if err != nil {
		b.logf("[!] 拉取本人考勤列表失败（%v），本轮不做已签过滤", err)
		return signed, signTimes
	}
	for _, row := range toMapList(page) {
		aid := first(row, attendanceIDKeys...)
		if aid == nil {
			continue
		}
		key := fmt.Sprint(aid)
		if st := first(row, signTimeKeys...); st != nil {
			signTimes[key] = st
		}
		if rowIsSigned(row) {
			signed[key] = struct{}{}
		}
	}
	return signed, signTimes
}

// UnsignableSignCode 服务端明确不允许补签的错误码
// （实测：学生形态对已结束的场次返回 code=1020065005 msg="考勤已结束"）。
const UnsignableSignCode = 1020065005

var unsignableSignMarkers = []string{
	"考勤已结束", "考勤未开始", "签到时间已过", "不在签到时间内", "已过签到时间",
}

// IsUnsignableError 判断服务端是否明确表示该场次无法补签（已结束/未开始等）。
func IsUnsignableError(err error) bool {
	ae, ok := err.(*ApiError)
	if !ok {
		return false
	}
	if codeEquals(ae.Code, UnsignableSignCode) {
		return true
	}
	msg := ae.Msg
	for _, m := range unsignableSignMarkers {
		if strings.Contains(msg, m) {
			return true
		}
	}
	return false
}

// BuildForceSignPayload 构造教师接口强制补签的提交体。
//
// 与普通签到不同：无论考勤原本是什么类型，默认都按 type=1（二维码直签通道）
// 提交，且始终携带坐标（缺省用默认学校坐标）；type=1 时附带伪造 refreshSeed。
func BuildForceSignPayload(
	attendanceID, detailID, studentID interface{},
	signType int,
	refreshSeed, longitude, latitude, address string,
	signRole int,
	signUserID interface{},
	extra map[string]interface{},
) map[string]interface{} {
	if signUserID == nil {
		signUserID = studentID
	}
	if longitude == "" {
		longitude = DefaultSignLongitude
	}
	if latitude == "" {
		latitude = DefaultSignLatitude
	}
	if address == "" {
		address = DefaultSignAddress
	}
	if signRole == 0 {
		signRole = SignRoleStudent
	}
	payload := map[string]interface{}{
		"attendanceId":    attendanceID,
		"id":              detailID,
		"status":          SignStatusPresent,
		"type":            signType,
		"signRole":        signRole,
		"signUserId":      signUserID,
		"signLongitude":   longitude,
		"signLatitude":    latitude,
		"signAddressName": address,
	}
	if signType == AttendanceTypeQR {
		seed := refreshSeed
		if seed == "" {
			seed = ForceSignRefreshSeed
		}
		payload["refreshSeed"] = seed
	}
	for k, v := range extra {
		payload[k] = v
	}
	return payload
}

// ForceConfig 是教师接口强制补签机器人的配置。
type ForceConfig struct {
	Interval           int
	TermID             interface{}
	CourseIDs          []interface{}
	Longitude          string
	Latitude           string
	Address            string
	SignType           int
	RefreshSeed        string
	SignRole           int                    // 4=学生签到形态（默认），3=教师手动修改形态
	TeacherFallback    bool                   // 学生形态被拒时自动改用 signRole=3 重试一次
	TeacherUserID      interface{}            // 教师形态用的 signUserId（教师 user id）
	PayloadStyle       string                 // full=App 直签形态；teacher=Web 教师端补签最小字段集
	SkipSigned         bool                   // 先拉本人考勤列表跳过已签场次
	SignedPageSize     int                    // 拉本人考勤列表的分页大小
	ReopenEnded        bool                   // 已结束场次：临时重开窗口→补签→还原
	ReopenSeconds      int                    // 临时重开的窗口秒数
	SignStatus         int                    // 补签写入的考勤状态（1=出勤）
	SignTime           interface{}            // 补签写回的 signTime；nil=沿用该场原有值
	ExtraFields        map[string]interface{} // 额外并入提交体的字段（联调用）
	SubmitPath         string                 // 覆盖提交路由（默认 ATT-05 detail/update）
	IncludeEnded       bool
	EndedWithinMinutes int
	DryRun             bool
	MaxAttempts        int
	MaxRounds          int // 0 表示不限
	PageSize           int
	Quiet              bool
	Verify             bool
}

// DefaultForceConfig 返回与 Python 版 CLI 一致的默认配置。
func DefaultForceConfig() ForceConfig {
	return ForceConfig{
		Interval:           ForceMinInterval * 5,
		SignType:           ForceSignTypeDefault,
		SignRole:           SignRoleStudent,
		TeacherFallback:    true,
		PayloadStyle:       PayloadStyleFull,
		SkipSigned:         true,
		SignedPageSize:     200,
		ReopenSeconds:      ForceReopenSecondsDefault,
		SignStatus:         SignStatusPresent,
		EndedWithinMinutes: ForceEndedWindowDefault,
		MaxAttempts:        3,
		PageSize:           ForcePageSizeDefault,
		Verify:             true,
	}
}

type courseRef struct {
	id   interface{}
	name string
}

// toInt64 解析毫秒时间戳等大整数（JSON 数字为 float64），避免 32 位平台溢出。
func toInt64(v interface{}) int64 {
	switch n := v.(type) {
	case float64:
		return int64(n)
	case int:
		return int64(n)
	case int64:
		return n
	case string:
		if i, err := strconv.ParseInt(n, 10, 64); err == nil {
			return i
		}
	}
	return 0
}

// ForceCheckinBot 使用教师端考勤列表接口（仍用学生账号）强制补签。
type ForceCheckinBot struct {
	Client *Client
	Cfg    ForceConfig

	studentID      interface{}
	attempted      map[string]string
	skipLogged     map[string]struct{}
	fatal          string
	endedHintShown bool
	totals         struct{ rounds, ok, fail, skip int }

	teacherIDCache map[string]interface{} // courseID -> 教师 user id
	ownSignTimes   map[string]interface{} // attendanceId -> 本人该场原有 signTime
}

// NewForceCheckinBot 创建强制补签机器人。
func NewForceCheckinBot(client *Client, cfg ForceConfig) *ForceCheckinBot {
	if cfg.Interval < ForceMinInterval {
		cfg.Interval = ForceMinInterval
	}
	if cfg.MaxAttempts < 1 {
		cfg.MaxAttempts = 1
	}
	if cfg.PageSize < 1 {
		cfg.PageSize = ForcePageSizeDefault
	}
	return &ForceCheckinBot{
		Client:         client,
		Cfg:            cfg,
		attempted:      map[string]string{},
		skipLogged:     map[string]struct{}{},
		teacherIDCache: map[string]interface{}{},
		ownSignTimes:   map[string]interface{}{},
	}
}

func (b *ForceCheckinBot) logf(format string, a ...interface{}) {
	fmt.Printf("[%s] %s\n", time.Now().Format("15:04:05"), fmt.Sprintf(format, a...))
}

// Prepare 确保登录态可用并识别当前学期。
func (b *ForceCheckinBot) Prepare() error {
	if b.Client.AccessToken == "" {
		if !b.Client.EnsureLoggedIn() {
			return fmt.Errorf("本地没有访问令牌，且没有可自动登录的凭据；请先 login --save-credentials")
		}
		b.logf("已通过保存的凭据自动登录")
	}
	if b.studentID == nil {
		b.studentID = b.Client.UserID
	}
	if b.studentID == nil {
		return fmt.Errorf("本地没有用户 ID，请重新登录")
	}
	if b.Cfg.TermID == nil {
		terms, err := b.Client.GetTermOptions()
		if err != nil {
			return err
		}
		term, ok := resolveCurrentTerm(terms, time.Now())
		if !ok {
			return fmt.Errorf("未能识别当前学期，请用 --term-id 手动指定")
		}
		b.Cfg.TermID = term["id"]
		b.logf("当前学期：%v（id=%v）", term["name"], b.Cfg.TermID)
	}
	return nil
}

func (b *ForceCheckinBot) courses() ([]courseRef, error) {
	if len(b.Cfg.CourseIDs) > 0 {
		out := make([]courseRef, 0, len(b.Cfg.CourseIDs))
		for _, id := range b.Cfg.CourseIDs {
			out = append(out, courseRef{id: id, name: fmt.Sprint(id)})
		}
		return out, nil
	}
	items, err := b.Client.GetCourseOptionsByTerm(b.Cfg.TermID)
	if err != nil {
		return nil, err
	}
	var out []courseRef
	for _, item := range items {
		cid := first(item, "teachCourseId", "courseId", "id")
		if cid == nil {
			continue
		}
		name := fmt.Sprint(cid)
		if v := first(item, "teachCourseName", "courseName", "name"); v != nil {
			name = fmt.Sprint(v)
		}
		out = append(out, courseRef{id: cid, name: name})
	}
	return out, nil
}

// rowIsSigned 行内 signRole 为 3/4 或存在签到时间即视为已签。
func rowIsSigned(row map[string]interface{}) bool {
	if signedSignRoles[toInt(first(row, signRoleKeys...), -1)] {
		return true
	}
	for _, k := range signTimeKeys {
		if first(row, k) != nil {
			return true
		}
	}
	return false
}

// signedAttendanceIDs 拉一份本人考勤列表，收集已签的 attendanceId。
func (b *ForceCheckinBot) signedAttendanceIDs() map[string]struct{} {
	out := map[string]struct{}{}
	page, err := b.Client.GetStudentAttendancePage(map[string]interface{}{
		"studentId":        b.studentID,
		"termId":           b.Cfg.TermID,
		"courseId":         nil,
		"attendanceStatus": nil,
		"status":           nil,
		"pageNo":           1,
		"pageSize":         b.Cfg.SignedPageSize,
	})
	if err != nil {
		b.logf("[!] 拉取本人考勤列表失败（%v），本轮不做已签过滤", err)
		return out
	}
	for _, row := range toMapList(page) {
		if !rowIsSigned(row) {
			continue
		}
		if aid := first(row, attendanceIDKeys...); aid != nil {
			out[fmt.Sprint(aid)] = struct{}{}
		}
	}
	return out
}

// sessions 返回 (该课程考勤记录总数, 待补签场次列表)。
func (b *ForceCheckinBot) sessions(courseID interface{}, nowMS int64) (int, []map[string]interface{}, error) {
	params := map[string]string{
		"teachCourseId": fmt.Sprint(courseID),
		"searchReq":     "",
		"pageNo":        "1",
		"pageSize":      strconv.Itoa(b.Cfg.PageSize),
	}
	page, err := b.Client.GetTeacherAttendancePage(params)
	if err != nil {
		return 0, nil, err
	}
	rows := toMapList(page)
	windowMS := int64(b.Cfg.EndedWithinMinutes) * 60_000
	var out []map[string]interface{}
	for _, row := range rows {
		openMS := toInt64(row["openTime"])
		finishMS := toInt64(row["finishTime"])
		if finishMS == 0 {
			continue
		}
		if openMS <= nowMS && nowMS < finishMS {
			out = append(out, row)
		} else if b.Cfg.IncludeEnded && finishMS <= nowMS &&
			(windowMS == 0 || finishMS >= nowMS-windowMS) {
			out = append(out, row)
		}
	}
	return len(rows), out, nil
}

// ScanOnce 扫描一轮：遍历课程 -> 待补场次 -> 直签。
func (b *ForceCheckinBot) ScanOnce() (map[string]int, error) {
	if b.studentID == nil {
		b.studentID = b.Client.UserID
	}
	if b.studentID == nil {
		return nil, fmt.Errorf("本地没有用户 ID，请重新登录")
	}
	courses, err := b.courses()
	if err != nil {
		return nil, err
	}
	// 拉本人考勤列表：用于「跳过已签」，也用于补签时写回该场原有 signTime
	var signed map[string]struct{}
	if b.Cfg.SkipSigned || (b.Cfg.ReopenEnded && b.Cfg.SignTime == nil) {
		var signTimes map[string]interface{}
		signed, signTimes = b.ownAttendanceIndex()
		b.ownSignTimes = signTimes
		if !b.Cfg.SkipSigned {
			signed = map[string]struct{}{}
		}
	} else {
		signed = map[string]struct{}{}
	}
	nowMS := time.Now().UnixMilli()
	counts := map[string]int{"courses": 0, "records": 0, "sessions": 0, "signed": 0, "ok": 0, "fail": 0, "skip": 0}
	for _, course := range courses {
		counts["courses"]++
		total, rows, err := b.sessions(course.id, nowMS)
		if err != nil {
			return counts, err
		}
		counts["records"] += total
		for _, row := range rows {
			if b.fatal != "" {
				return counts, fmt.Errorf("%s", b.fatal)
			}
			attendanceID := first(row, "id", "attendanceId")
			key := fmt.Sprintf("force:%v", attendanceID)
			if st := b.attempted[key]; st == "done" || st == "unsignable" {
				continue
			}
			if _, ok := signed[fmt.Sprint(attendanceID)]; ok {
				// 本人已签（含教师已改）：不重复提交
				b.attempted[key] = "done"
				counts["signed"]++
				continue
			}
			counts["sessions"]++
			b.process(row, course.id, course.name, counts)
		}
	}
	// 有考勤记录但一条都不在时间窗口内：说明只能等下一场开始
	if counts["records"] > 0 && counts["sessions"] == 0 && !b.endedHintShown {
		b.endedHintShown = true
		b.logf("    （该范围查到考勤记录，但当前没有“进行中”的场次；服务端不允许补签已结束的场次，等下一场开始后会立刻签上）")
	}
	if b.fatal != "" {
		return counts, fmt.Errorf("%s", b.fatal)
	}
	return counts, nil
}

func (b *ForceCheckinBot) process(
	row map[string]interface{}, courseID interface{}, courseName string, counts map[string]int,
) {
	attendanceID := first(row, "id", "attendanceId")
	title := "未命名考勤"
	if v := first(row, "title", "attendanceName", "name"); v != nil {
		title = fmt.Sprint(v)
	}
	label := fmt.Sprintf("%s（%s）", title, courseName)
	if attendanceID == nil {
		b.logf("[跳过] %s：无法识别 attendanceId", label)
		counts["skip"]++
		return
	}
	key := fmt.Sprintf("force:%v", attendanceID)
	state := b.attempted[key]
	failures := 0
	if strings.HasPrefix(state, "fail:") {
		failures, _ = strconv.Atoi(strings.TrimPrefix(state, "fail:"))
	}
	if failures >= b.Cfg.MaxAttempts {
		b.skip(key, label, fmt.Sprintf("失败已达 %d 次，不再重试", failures))
		counts["skip"]++
		return
	}

	detailID := b.attendanceDetailID(attendanceID)
	if detailID == nil || fmt.Sprint(detailID) == "" {
		// ATT-03 返回空：本人不在该考勤班级
		if b.fatal != "" {
			return
		}
		b.skip(key, label, "无本人明细（不在该考勤班级），跳过")
		counts["skip"]++
		return
	}

	teacherID := b.Cfg.TeacherUserID
	if teacherID == nil {
		teacherID = teacherIDFromRow(row)
	}
	var payload map[string]interface{}
	if b.Cfg.PayloadStyle == PayloadStyleTeacher {
		signUser := b.studentID
		if teacherID != nil {
			signUser = teacherID
		}
		payload = BuildTeacherStylePayload(
			attendanceID, detailID, signUser, b.Cfg.ExtraFields,
		)
	} else {
		signUserID := b.studentID
		if b.Cfg.SignRole == SignRoleTeacher {
			if teacherID == nil {
				b.logf("[!] %s：未提供 --teacher-user-id 且考勤行没有教师字段，signUserId 默认沿用当前用户 ID", label)
			} else {
				signUserID = teacherID
			}
		}
		payload = BuildForceSignPayload(
			attendanceID, detailID, b.studentID, b.Cfg.SignType,
			b.Cfg.RefreshSeed, b.Cfg.Longitude, b.Cfg.Latitude, b.Cfg.Address,
			b.Cfg.SignRole, signUserID, b.Cfg.ExtraFields,
		)
	}
	if b.Cfg.DryRun {
		buf, _ := json.Marshal(payload)
		b.logf("[DRY-RUN] %s：将强制补签 %s", label, string(buf))
		counts["skip"]++
		return
	}
	if _, err := b.submit(payload); err != nil {
		if ae, ok := err.(*ApiError); ok && (codeEquals(ae.Code, 401) || b.Client.AccessToken == "") {
			b.fatal = fmt.Sprintf("登录态已失效：%v", err)
			return
		}
		if IsUnsignableError(err) {
			// 已结束场次：临时重开 → 补签 → 还原（参考已验证做法）
			if b.Cfg.ReopenEnded {
				if b.signWithReopen(row, courseID, attendanceID, detailID,
					label, key, counts) {
					return
				}
				if b.fatal != "" {
					return
				}
				b.bump(key, failures)
				counts["fail"]++
				return
			}
			// 学生形态被拒（如“考勤已结束”）：改教师手动修改形态再试一次
			if b.Cfg.SignRole != SignRoleTeacher &&
				b.Cfg.PayloadStyle != PayloadStyleTeacher &&
				b.Cfg.TeacherFallback &&
				b.retryAsTeacher(payload, detailID, label, key, counts, teacherID) {
				return
			}
			b.logf("[跳过] %s：%v（服务端不允许该场次补签，不再重试）", label, err)
			b.attempted[key] = "unsignable"
			counts["skip"]++
			b.totals.skip++
			return
		}
		msg := err.Error()
		if strings.Contains(msg, "已签") || strings.Contains(msg, "签到过") ||
			strings.Contains(msg, "重复") || strings.Contains(msg, "已经签到") {
			b.logf("[OK] %s：服务端提示已签到（%v）", label, err)
			b.attempted[key] = "done"
			counts["ok"]++
			b.totals.ok++
			return
		}
		b.logf("[失败] %s：%v", label, err)
		b.bump(key, failures)
		counts["fail"]++
		return
	}
	b.logf("[OK] %s：强制补签提交成功", label)
	if b.Cfg.Verify {
		b.verify(detailID, label)
	}
	b.attempted[key] = "done"
	counts["ok"]++
	b.totals.ok++
}

func (b *ForceCheckinBot) attendanceDetailID(attendanceID interface{}) interface{} {
	data, err := b.Client.GetAttendanceDetailID(attendanceID, b.studentID)
	if err != nil {
		b.logf("[失败] 反查考勤明细失败（attendanceId=%v）：%v", attendanceID, err)
		if ae, ok := err.(*ApiError); ok && (codeEquals(ae.Code, 401) || b.Client.AccessToken == "") {
			b.fatal = fmt.Sprintf("登录态已失效：%v", err)
		}
		return nil
	}
	if m, ok := data.(map[string]interface{}); ok {
		return first(m, detailIDKeys...)
	}
	return data
}

// submit 提交补签；Cfg.SubmitPath 非空时改用该路由（联调指向抓包得到的教师端路由）。
func (b *ForceCheckinBot) submit(payload map[string]interface{}) (interface{}, error) {
	if b.Cfg.SubmitPath != "" {
		return b.Client.Call("PUT", b.Cfg.SubmitPath, nil, payload)
	}
	return b.Client.SubmitAttendance(payload)
}

// signWithReopen 临时重开已结束的考勤 → 补签 → 立即恢复结束。
//
// 与参考实现（neumooc-checkin/dashboard.py batch_modify）一致：
// 1) GET teach-course-attendance/get 读原 status/finishTime；
// 2) PUT teach-course-attendance/update {id,status:1,finishTime:now+窗口}；
// 3) PUT detail/update 补签（signRole=3 + signUserId=教师 id + 原 signTime）；
// 4) 无论成败都还原原 status/finishTime。
func (b *ForceCheckinBot) signWithReopen(
	row map[string]interface{}, courseID, attendanceID, detailID interface{},
	label, key string, counts map[string]int,
) bool {
	att, err := b.Client.GetTeacherAttendance(
		map[string]string{"id": fmt.Sprint(attendanceID)},
	)
	if err != nil {
		b.logf("[失败] %s：读取考勤主表失败（%v）", label, err)
		return false
	}
	m, ok := att.(map[string]interface{})
	if !ok {
		b.logf("[失败] %s：考勤主表返回异常（%v）", label, att)
		return false
	}
	origStatus := m["status"]
	origFinish := m["finishTime"]
	bufMS := time.Now().UnixMilli() + int64(b.Cfg.ReopenSeconds)*1000

	reopened := false
	defer func() {
		if !reopened {
			return
		}
		if _, err := b.Client.UpdateTeacherAttendance(map[string]interface{}{
			"id": attendanceID, "status": origStatus, "finishTime": origFinish,
		}); err != nil {
			b.logf("[!] 恢复考勤原状态失败：%v（请手动检查 attendanceId=%v）", err, attendanceID)
			return
		}
		b.logf("      已恢复考勤原状态（status=%v, finishTime=%v）", origStatus, origFinish)
	}()

	if _, err := b.Client.UpdateTeacherAttendance(map[string]interface{}{
		"id": attendanceID, "status": 1, "finishTime": bufMS,
	}); err != nil {
		b.logf("[失败] %s：临时重开考勤失败（%v）", label, err)
		return false
	}
	reopened = true
	time.Sleep(600 * time.Millisecond)

	payload := b.teacherPayload(attendanceID, detailID, row, courseID)
	b.logf("[..] %s：已临时重开考勤，改用 signRole=3 补签（signUserId=%v, status=%v）",
		label, payload["signUserId"], payload["status"])
	if _, err := b.submit(payload); err != nil {
		if ae, ok := err.(*ApiError); ok && (codeEquals(ae.Code, 401) || b.Client.AccessToken == "") {
			b.fatal = fmt.Sprintf("登录态已失效：%v", err)
			return false
		}
		msg := err.Error()
		if !strings.Contains(msg, "已签") && !strings.Contains(msg, "签到过") {
			b.logf("[失败] %s：重开后补签仍被拒（%v）", label, err)
			return false
		}
		b.logf("[OK] %s：服务端提示已签到（%v）", label, err)
	}
	b.logf("[OK] %s：补签提交成功（signRole=3, status=%v）", label, payload["status"])
	if b.Cfg.Verify {
		b.verify(detailID, label)
	}
	b.attempted[key] = "done"
	counts["ok"]++
	b.totals.ok++
	return true
}

// retryAsTeacher 学生形态被拒时，改用教师手动修改形态（signRole=3）再试一次。
// 返回 true 表示已补签成功（调用方直接 return）。
func (b *ForceCheckinBot) retryAsTeacher(
	payload map[string]interface{}, detailID interface{},
	label, key string, counts map[string]int, teacherID interface{},
) bool {
	// 改用 Web 教师端补签的最小字段集：只有 attendanceId/id/status/signRole/signUserId，
	// 去掉 type / refreshSeed / 坐标（这些会让服务端走"学生签到"校验路径）。
	signUser := payload["signUserId"]
	if teacherID != nil && fmt.Sprint(teacherID) != "" {
		signUser = teacherID
	}
	teacherPayload := BuildTeacherStylePayload(
		payload["attendanceId"], detailID, signUser, b.Cfg.ExtraFields,
	)
	if st := b.signTimeFor(payload["attendanceId"]); st != nil {
		if _, exists := teacherPayload["signTime"]; !exists {
			teacherPayload["signTime"] = st
		}
	}
	b.logf("[..] %s：学生形态被拒，改用教师端补签形态（signRole=3, signUserId=%v，去掉 type/refreshSeed/坐标）重试",
		label, signUser)
	if _, err := b.submit(teacherPayload); err != nil {
		if ae, ok := err.(*ApiError); ok && (codeEquals(ae.Code, 401) || b.Client.AccessToken == "") {
			b.fatal = fmt.Sprintf("登录态已失效：%v", err)
			return false
		}
		msg := err.Error()
		if strings.Contains(msg, "已签") || strings.Contains(msg, "签到过") ||
			strings.Contains(msg, "重复") || strings.Contains(msg, "已经签到") {
			b.logf("[OK] %s：服务端提示已签到（%v）", label, err)
			b.attempted[key] = "done"
			counts["ok"]++
			b.totals.ok++
			return true
		}
		b.logf("[跳过] %s：教师补签形态也被拒（%v）", label, err)
		return false
	}
	b.logf("[OK] %s：教师补签形态（signRole=3）提交成功", label)
	if b.Cfg.Verify {
		b.verify(detailID, label)
	}
	b.attempted[key] = "done"
	counts["ok"]++
	b.totals.ok++
	return true
}

func (b *ForceCheckinBot) verify(detailID interface{}, label string) {
	data, err := b.Client.GetStudentAttendanceDetail(detailID)
	if err != nil {
		b.logf("      回读失败：%v", err)
		return
	}
	m, ok := data.(map[string]interface{})
	if !ok {
		return
	}
	status := toInt(m["status"], 0)
	signTime := first(m, signTimeKeys...)
	if status == 1 || signTime != nil {
		b.logf("      ✅ 回读确认已签：status=%d signTime=%v", status, signTime)
	} else {
		b.logf("      ⚠ 回读未确认：status=%d signTime=%v（可下一轮复查）", status, signTime)
	}
}

func (b *ForceCheckinBot) skip(key, label, reason string) {
	if _, ok := b.skipLogged[key]; ok {
		return
	}
	b.skipLogged[key] = struct{}{}
	b.logf("[跳过] %s：%s", label, reason)
	b.totals.skip++
}

func (b *ForceCheckinBot) bump(key string, failures int) {
	b.attempted[key] = fmt.Sprintf("fail:%d", failures+1)
	b.totals.fail++
}

// Run 启动主循环，直到 maxRounds / Ctrl+C。
func (b *ForceCheckinBot) Run() error {
	if err := b.Prepare(); err != nil {
		return err
	}
	mode := ""
	if b.Cfg.DryRun {
		mode = "（DRY-RUN 演练）"
	}
	scope := "该学期全部课程"
	if len(b.Cfg.CourseIDs) > 0 {
		parts := make([]string, 0, len(b.Cfg.CourseIDs))
		for _, c := range b.Cfg.CourseIDs {
			parts = append(parts, fmt.Sprint(c))
		}
		scope = "指定课程 " + strings.Join(parts, ", ")
	}
	ended := ""
	if b.Cfg.IncludeEnded {
		ended = fmt.Sprintf("，含最近 %d 分钟内结束的场次", b.Cfg.EndedWithinMinutes)
	}
	b.logf("教师接口强制补签已启动%s：范围=%s%s，间隔 %ds，Ctrl+C 停止", mode, scope, ended, b.Cfg.Interval)

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, os.Interrupt)
	defer signal.Stop(sig)

	round := 0
loop:
	for {
		round++
		counts, err := b.ScanOnce()
		if err != nil {
			b.logf("[失败] 第 %d 轮扫描失败：%v", round, err)
		} else if counts != nil && (!b.Cfg.Quiet || counts["sessions"] > 0) {
			b.logf("第 %d 轮：课程 %d 门，考勤记录 %d 条，已签 %d，待补场次 %d，成功 %d，失败 %d，跳过 %d",
				round, counts["courses"], counts["records"], counts["signed"],
				counts["sessions"], counts["ok"], counts["fail"], counts["skip"])
		}
		b.totals.rounds = round
		if b.Cfg.MaxRounds > 0 && round >= b.Cfg.MaxRounds {
			break
		}
		select {
		case <-sig:
			b.logf("收到 Ctrl+C，停止强制补签")
			break loop
		case <-time.After(time.Duration(b.Cfg.Interval) * time.Second):
		}
	}
	b.logf("结束：共 %d 轮，强制补签成功 %d 次，失败 %d 次，跳过 %d 项",
		b.totals.rounds, b.totals.ok, b.totals.fail, b.totals.skip)
	return nil
}
