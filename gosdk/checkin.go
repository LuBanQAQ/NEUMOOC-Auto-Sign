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

// 签到常量（与 neumooc_checkin.py 对齐）
const (
	SessionStatusInProgress = 1

	AttendanceTypeNormal  = 0
	AttendanceTypeQR      = 1
	AttendanceTypeTeacher = 2

	SignStatusPresent     = 1
	SignRoleStudent       = 4
	QRBypassSignType      = 1
	DirectSignRefreshSeed = "0"

	DefaultSignLongitude = "121.614682"
	DefaultSignLatitude  = "38.914003"
	DefaultSignAddress   = "大连东软信息学院"
)

var signedSignRoles = map[int]bool{3: true, 4: true}

var (
	detailIDKeys     = []string{"id", "attendanceDetailId", "detailId"}
	attendanceIDKeys = []string{"attendanceId", "courseAttendanceId"}
	typeKeys         = []string{"type", "attendanceType"}
	locationTypeKeys = []string{"attendanceLocationType", "locationType"}
	signRoleKeys     = []string{"signRole"}
	signTimeKeys     = []string{"signTime", "signTimeString", "signInTime", "signTimeStr"}
	titleKeys        = []string{"title", "attendanceName", "name"}
)

func toInt(v interface{}, def int) int {
	switch n := v.(type) {
	case float64:
		return int(n)
	case int:
		return n
	case string:
		if i, err := strconv.Atoi(n); err == nil {
			return i
		}
	}
	return def
}

// SignTask 是一场考勤中学生维度的签到任务（ATT-01 行归一化结果）。
type SignTask struct {
	Raw           map[string]interface{}
	DetailID      interface{}
	AttendanceID  interface{}
	Title         string
	Type          int
	LocationType  int
	SessionStatus int
	Signed        bool
	Key           string
}

func newSignTask(raw map[string]interface{}) *SignTask {
	t := &SignTask{Raw: raw}
	t.DetailID = first(raw, detailIDKeys...)
	t.AttendanceID = first(raw, attendanceIDKeys...)
	if v := first(raw, titleKeys...); v != nil {
		t.Title = fmt.Sprint(v)
	} else {
		t.Title = "未命名考勤"
	}
	t.Type = toInt(first(raw, typeKeys...), AttendanceTypeNormal)
	t.LocationType = toInt(first(raw, locationTypeKeys...), 0)
	t.SessionStatus = toInt(first(raw, []string{"status"}...), 0)
	t.Signed = detectSigned(raw)
	if t.DetailID == nil && t.AttendanceID == nil {
		if b, err := json.Marshal(raw); err == nil {
			t.Key = "raw:" + string(b)
		} else {
			t.Key = "raw:?"
		}
	} else {
		t.Key = fmt.Sprintf("%v:%v", t.AttendanceID, t.DetailID)
	}
	return t
}

func detectSigned(raw map[string]interface{}) bool {
	if signedSignRoles[toInt(first(raw, signRoleKeys...), -1)] {
		return true
	}
	for _, k := range signTimeKeys {
		if first(raw, k) != nil {
			return true
		}
	}
	return false
}

func termDate(v interface{}) (time.Time, bool) {
	switch x := v.(type) {
	case []interface{}:
		if len(x) >= 3 {
			return time.Date(toInt(x[0], 0), time.Month(toInt(x[1], 0)), toInt(x[2], 0), 0, 0, 0, 0, time.Local), true
		}
	case string:
		s := x
		if len(s) > 10 {
			s = s[:10]
		}
		if t, err := time.Parse("2006-01-02", s); err == nil {
			return t, true
		}
	}
	return time.Time{}, false
}

func dateOnly(t time.Time) time.Time {
	y, m, d := t.Date()
	return time.Date(y, m, d, 0, 0, 0, 0, time.Local)
}

func maxByCreate(items []map[string]interface{}) map[string]interface{} {
	var best map[string]interface{}
	var bestC float64 = -1
	for _, t := range items {
		c := float64(toInt(t["createTime"], 0))
		if best == nil || c > bestC {
			best, bestC = t, c
		}
	}
	return best
}

// resolveCurrentTerm 从 EDU-01 学期选项里挑出当前学期。
func resolveCurrentTerm(items []map[string]interface{}, today time.Time) (map[string]interface{}, bool) {
	if len(items) == 0 {
		return nil, false
	}
	today = dateOnly(today)

	var inRange []map[string]interface{}
	for _, t := range items {
		s, ok1 := termDate(t["termStartTime"])
		e, ok2 := termDate(t["termEndTime"])
		if ok1 && ok2 && !today.Before(s) && !today.After(e) {
			inRange = append(inRange, t)
		}
	}
	if len(inRange) > 0 {
		var current []map[string]interface{}
		for _, t := range inRange {
			if toInt(t["isCurrentTerm"], 0) == 1 {
				current = append(current, t)
			}
		}
		pool := current
		if len(pool) == 0 {
			pool = inRange
		}
		return maxByCreate(pool), true
	}
	var current []map[string]interface{}
	for _, t := range items {
		if toInt(t["isCurrentTerm"], 0) == 1 {
			current = append(current, t)
		}
	}
	if len(current) > 0 {
		return maxByCreate(current), true
	}
	var upcoming []map[string]interface{}
	for _, t := range items {
		if e, ok := termDate(t["termEndTime"]); ok && !e.Before(today) {
			upcoming = append(upcoming, t)
		}
	}
	if len(upcoming) > 0 {
		return maxByCreate(upcoming), true
	}
	return maxByCreate(items), true
}

// CurrentTermID 识别当前学期 ID（基于 EDU-01 学期选项）。
func (c *Client) CurrentTermID() (interface{}, error) {
	terms, err := c.GetTermOptions()
	if err != nil {
		return nil, err
	}
	term, ok := resolveCurrentTerm(terms, time.Now())
	if !ok {
		return nil, fmt.Errorf("未能识别当前学期，请用 --term-id 手动指定")
	}
	return term["id"], nil
}

func buildSignPayload(task *SignTask, studentID interface{}, longitude, latitude, address, refreshSeed string, qrSignType int) map[string]interface{} {
	signType := 0
	if task.Type == AttendanceTypeQR {
		signType = qrSignType
	}
	isQR := signType == AttendanceTypeQR

	payload := map[string]interface{}{
		"attendanceId": task.AttendanceID,
		"id":           task.DetailID,
		"status":       SignStatusPresent,
		"type":         signType,
		"signRole":     SignRoleStudent,
		"signUserId":   studentID,
	}
	if isQR {
		seed := refreshSeed
		if seed == "" {
			seed = DirectSignRefreshSeed
		}
		payload["refreshSeed"] = seed
	}
	if task.LocationType == 1 || isQR {
		lon, lat, addr := longitude, latitude, address
		if lon == "" {
			lon = DefaultSignLongitude
		}
		if lat == "" {
			lat = DefaultSignLatitude
		}
		if addr == "" {
			addr = DefaultSignAddress
		}
		payload["signLongitude"] = lon
		payload["signLatitude"] = lat
		payload["signAddressName"] = addr
	}
	return payload
}

// CheckinConfig 是自动签到机器人的配置。
type CheckinConfig struct {
	Interval        int
	TermID          interface{}
	CourseID        interface{}
	Longitude       string
	Latitude        string
	Address         string
	QRBypassSignType int
	IncludeTeacher  bool
	AnyStatus       bool
	DryRun          bool
	MaxAttempts     int
	MaxRounds       int // 0 表示不限
	Once            bool
	Quiet           bool
	PageSize        int
}

// DefaultCheckinConfig 返回与 Python 版 CLI 一致的默认配置。
func DefaultCheckinConfig() CheckinConfig {
	return CheckinConfig{
		Interval:        30,
		QRBypassSignType: QRBypassSignType,
		MaxAttempts:     3,
	}
}

// AutoCheckinBot 轮询进行中考勤并直接发包提交签到。
type AutoCheckinBot struct {
	Client *Client
	Cfg    CheckinConfig

	studentID  interface{}
	attempted  map[string]string
	skipLogged map[string]struct{}
	fatal      string
	totals     struct{ rounds, ok, fail, skip int }
}

func NewAutoCheckinBot(client *Client, cfg CheckinConfig) *AutoCheckinBot {
	if cfg.Interval < 5 {
		cfg.Interval = 5
	}
	if cfg.MaxAttempts < 1 {
		cfg.MaxAttempts = 1
	}
	return &AutoCheckinBot{
		Client:     client,
		Cfg:        cfg,
		attempted:  map[string]string{},
		skipLogged: map[string]struct{}{},
	}
}

func (b *AutoCheckinBot) logf(format string, a ...interface{}) {
	fmt.Printf("[%s] %s\n", time.Now().Format("15:04:05"), fmt.Sprintf(format, a...))
}

// Prepare 确保登录态可用并识别当前学期。
func (b *AutoCheckinBot) Prepare() error {
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

// ScanOnce 扫描一轮并处理所有待签任务。
func (b *AutoCheckinBot) ScanOnce() (map[string]int, error) {
	status := interface{}(nil)
	if !b.Cfg.AnyStatus {
		status = SessionStatusInProgress
	}
	body := map[string]interface{}{
		"studentId":        b.studentID,
		"termId":           b.Cfg.TermID,
		"courseId":         b.Cfg.CourseID,
		"attendanceStatus": nil,
		"status":           status,
	}
	if b.Cfg.PageSize > 0 {
		body["pageNo"] = 1
		body["pageSize"] = b.Cfg.PageSize
	}
	page, err := b.Client.GetStudentAttendancePage(body)
	if err != nil {
		return nil, err
	}
	var tasks []*SignTask
	for _, row := range toMapList(page) {
		tasks = append(tasks, newSignTask(row))
	}
	counts := map[string]int{"rows": len(tasks), "signed": 0, "ok": 0, "fail": 0, "skip": 0}
	for _, task := range tasks {
		b.process(task, counts)
		if b.fatal != "" {
			return counts, fmt.Errorf("%s", b.fatal)
		}
	}
	return counts, nil
}

func (b *AutoCheckinBot) process(task *SignTask, counts map[string]int) {
	if task.Signed {
		counts["signed"]++
		return
	}
	state := b.attempted[task.Key]
	if state == "done" {
		counts["signed"]++
		return
	}
	failures := 0
	if strings.HasPrefix(state, "fail:") {
		failures, _ = strconv.Atoi(strings.TrimPrefix(state, "fail:"))
	}
	if failures >= b.Cfg.MaxAttempts {
		b.skip(task, fmt.Sprintf("失败已达 %d 次，本轮不再重试", failures))
		counts["skip"]++
		return
	}
	if task.AttendanceID == nil && task.DetailID == nil {
		b.skip(task, "无法识别考勤 ID，请加 --debug 查看返回行结构")
		counts["skip"]++
		return
	}
	if task.AttendanceID == nil {
		b.skip(task, "缺少 attendanceId，无法提交签到")
		counts["skip"]++
		return
	}
	if task.DetailID == nil {
		if !b.resolveDetailID(task) {
			b.fail(task, counts)
			return
		}
	}
	if task.Type == AttendanceTypeTeacher && !b.Cfg.IncludeTeacher {
		b.skip(task, "教师考勤默认不签（--include-teacher 可开启）")
		counts["skip"]++
		return
	}

	payload := buildSignPayload(task, b.studentID, b.Cfg.Longitude, b.Cfg.Latitude, b.Cfg.Address, "", b.Cfg.QRBypassSignType)
	if b.Cfg.DryRun {
		buf, _ := json.Marshal(payload)
		b.logf("[DRY-RUN] %s：将提交签到 %s", task.Title, string(buf))
		counts["skip"]++
		return
	}
	if _, err := b.Client.SubmitAttendance(payload); err != nil {
		if ae, ok := err.(*ApiError); ok && (codeEquals(ae.Code, 401) || b.Client.AccessToken == "") {
			b.fatal = fmt.Sprintf("登录态已失效：%v", err)
			return
		}
		msg := err.Error()
		if strings.Contains(msg, "已签") || strings.Contains(msg, "签到过") || strings.Contains(msg, "重复") || strings.Contains(msg, "已经签到") {
			b.logf("[OK] %s：服务端提示已签到（%v）", task.Title, err)
			b.attempted[task.Key] = "done"
			counts["signed"]++
			return
		}
		b.logf("[失败] %s：%v", task.Title, err)
		b.bumpFailure(task)
		counts["fail"]++
		return
	}
	b.logf("[OK] %s：签到提交成功", task.Title)
	b.attempted[task.Key] = "done"
	counts["ok"]++
	b.totals.ok++
}

func (b *AutoCheckinBot) resolveDetailID(task *SignTask) bool {
	data, err := b.Client.GetAttendanceDetailID(task.AttendanceID, b.studentID)
	if err != nil {
		b.logf("[失败] %s：反查考勤明细 ID 失败（%v）", task.Title, err)
		if ae, ok := err.(*ApiError); ok && (codeEquals(ae.Code, 401) || b.Client.AccessToken == "") {
			b.fatal = fmt.Sprintf("登录态已失效：%v", err)
		}
		return false
	}
	if m, ok := data.(map[string]interface{}); ok {
		task.DetailID = first(m, detailIDKeys...)
	} else {
		task.DetailID = data
	}
	if task.DetailID == nil {
		b.logf("[失败] %s：ATT-03 未返回明细 ID（data=%v）", task.Title, data)
		return false
	}
	task.Key = fmt.Sprintf("%v:%v", task.AttendanceID, task.DetailID)
	return true
}

func (b *AutoCheckinBot) skip(task *SignTask, reason string) {
	if _, ok := b.skipLogged[task.Key]; ok {
		return
	}
	b.skipLogged[task.Key] = struct{}{}
	b.logf("[跳过] %s：%s", task.Title, reason)
	b.totals.skip++
}

func (b *AutoCheckinBot) bumpFailure(task *SignTask) {
	failures := 1
	if state := b.attempted[task.Key]; strings.HasPrefix(state, "fail:") {
		if n, err := strconv.Atoi(strings.TrimPrefix(state, "fail:")); err == nil {
			failures = n + 1
		}
	}
	b.attempted[task.Key] = fmt.Sprintf("fail:%d", failures)
	b.totals.fail++
}

func (b *AutoCheckinBot) fail(task *SignTask, counts map[string]int) {
	b.bumpFailure(task)
	counts["fail"]++
}

// Run 启动主循环，直到 maxRounds / --once / Ctrl+C。
func (b *AutoCheckinBot) Run() error {
	if err := b.Prepare(); err != nil {
		return err
	}
	mode := ""
	if b.Cfg.DryRun {
		mode = "（DRY-RUN 演练）"
	}
	b.logf("自动签到已启动%s：间隔 %ds，Ctrl+C 停止", mode, b.Cfg.Interval)

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
		} else if counts != nil && (!b.Cfg.Quiet || counts["rows"] > 0) {
			b.logf("第 %d 轮：考勤 %d 场，已签 %d，本轮成功 %d，失败 %d，跳过 %d",
				round, counts["rows"], counts["signed"], counts["ok"], counts["fail"], counts["skip"])
		}
		b.totals.rounds = round
		if b.Cfg.Once || (b.Cfg.MaxRounds > 0 && round >= b.Cfg.MaxRounds) {
			break
		}
		select {
		case <-sig:
			b.logf("收到 Ctrl+C，停止自动签到")
			break loop
		case <-time.After(time.Duration(b.Cfg.Interval) * time.Second):
		}
	}
	b.logf("结束：共 %d 轮，签到成功 %d 次，失败 %d 次，跳过 %d 项",
		b.totals.rounds, b.totals.ok, b.totals.fail, b.totals.skip)
	return nil
}
