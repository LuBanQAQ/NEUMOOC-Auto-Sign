// Command neumooc 是东软智慧教育 App 的 Go 命令行客户端。
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strings"

	"neumooc"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(1)
	}
	cmd, args := os.Args[1], os.Args[2:]
	switch cmd {
	case "login":
		cmdLogin(args)
	case "auto-checkin":
		cmdAutoCheckin(args)
	case "force-checkin":
		cmdForceCheckin(args)
	case "courses":
		cmdCourses(args)
	case "profile":
		cmdProfile(args)
	case "refresh":
		cmdRefresh(args)
	case "call":
		cmdCall(args)
	default:
		usage()
		os.Exit(1)
	}
}

func usage() {
	fmt.Println(`用法：
  neumooc login -u 学号 -p 密码 -t 租户ID [--save-credentials] [--website 域名] [--debug]
  neumooc auto-checkin [--interval 30] [--once] [--dry-run] [--qr-sign-type 0|1] ...
  neumooc force-checkin [--course-id c1,c2] [--once] [--dry-run] [--include-ended] ...
  neumooc courses [--term-id 学期ID]        # 列出课程及 teachCourseId
  neumooc profile
  neumooc refresh
  neumooc call METHOD PATH [--params '{"id":1}'] [--body '...']`)
}

func cmdLogin(args []string) {
	fs := flag.NewFlagSet("login", flag.ExitOnError)
	username := fs.String("u", "", "学号/账号")
	password := fs.String("p", "", "登录密码（不填则交互输入）")
	tenant := fs.String("t", "", "租户/学校 ID")
	website := fs.String("website", "", "覆盖业务域名")
	debug := fs.Bool("debug", false, "打印原始请求/响应")
	insecure := fs.Bool("insecure", false, "忽略 TLS 证书校验")
	saveCred := fs.Bool("save-credentials", false, "保存账号密码用于令牌失效后自动重登")
	fs.Parse(args)

	if *username == "" {
		fmt.Fprintln(os.Stderr, "请提供 -u 学号")
		os.Exit(1)
	}
	if *password == "" {
		fmt.Print("登录密码：")
		_, _ = fmt.Scanln(password)
	}
	if *password == "" {
		fmt.Fprintln(os.Stderr, "未输入密码")
		os.Exit(1)
	}

	client := neumooc.NewClient(*website, *debug, *insecure)
	if _, err := client.LoginWithPassword(*username, *password, *tenant); err != nil {
		fmt.Fprintf(os.Stderr, "登录失败：%v\n", err)
		os.Exit(1)
	}
	fmt.Println("[OK] 登录成功（AUTH-02）")
	if *saveCred {
		_ = client.SaveCredentials(*username, *password)
	}
	printSession(client)
}

func cmdAutoCheckin(args []string) {
	fs := flag.NewFlagSet("auto-checkin", flag.ExitOnError)
	interval := fs.Int("interval", 30, "轮询间隔秒数（最小 5）")
	once := fs.Bool("once", false, "只扫描一轮即退出")
	maxRounds := fs.Int("max-rounds", 0, "最多轮询轮数（0 不限）")
	termID := fs.String("term-id", "", "学期 ID（默认自动识别）")
	courseID := fs.String("course-id", "", "只关注指定课程")
	longitude := fs.String("longitude", "", "定位签到经度（缺省默认学校坐标）")
	latitude := fs.String("latitude", "", "定位签到纬度（缺省默认学校坐标）")
	address := fs.String("address", "", "定位签到地址")
	qrSignType := fs.Int("qr-sign-type", neumooc.QRBypassSignType, "二维码考勤直签的 type 值（默认 1）")
	includeTeacher := fs.Bool("include-teacher", false, "教师考勤也尝试签到")
	anyStatus := fs.Bool("any-status", false, "不过滤考勤状态")
	dryRun := fs.Bool("dry-run", false, "只打印提交体，不实际提交")
	maxAttempts := fs.Int("max-attempts", 3, "每场考勤失败重试上限")
	quiet := fs.Bool("quiet", false, "无考勤数据的轮次不打印日志")
	pageSize := fs.Int("page-size", 0, "ATT-01 分页大小（0 用服务端默认）")
	website := fs.String("website", "", "覆盖业务域名")
	debug := fs.Bool("debug", false, "打印原始请求/响应")
	insecure := fs.Bool("insecure", false, "忽略 TLS 证书校验")
	fs.Parse(args)

	client := neumooc.NewClient(*website, *debug, *insecure)
	cfg := neumooc.DefaultCheckinConfig()
	cfg.Interval = *interval
	cfg.Once = *once
	cfg.MaxRounds = *maxRounds
	cfg.CourseID = *courseID
	cfg.Longitude = *longitude
	cfg.Latitude = *latitude
	cfg.Address = *address
	cfg.QRBypassSignType = *qrSignType
	cfg.IncludeTeacher = *includeTeacher
	cfg.AnyStatus = *anyStatus
	cfg.DryRun = *dryRun
	cfg.MaxAttempts = *maxAttempts
	cfg.Quiet = *quiet
	cfg.PageSize = *pageSize
	if *termID != "" {
		cfg.TermID = *termID
	}

	bot := neumooc.NewAutoCheckinBot(client, cfg)
	if err := bot.Run(); err != nil {
		fmt.Fprintf(os.Stderr, "错误：%v\n", err)
		os.Exit(1)
	}
}

func cmdForceCheckin(args []string) {
	fs := flag.NewFlagSet("force-checkin", flag.ExitOnError)
	interval := fs.Int("interval", 5, "扫描间隔秒数（最小 1）")
	once := fs.Bool("once", false, "只扫描一轮即退出")
	maxRounds := fs.Int("max-rounds", 0, "最多扫描轮数（0 不限）")
	termID := fs.String("term-id", "", "学期 ID（默认自动识别）")
	courses := fs.String("course-id", "", "只补签指定课程，多个用逗号分隔；留空=该学期全部课程")
	longitude := fs.String("longitude", "", "签到经度（缺省默认学校坐标）")
	latitude := fs.String("latitude", "", "签到纬度（缺省默认学校坐标）")
	address := fs.String("address", "", "签到地址")
	signType := fs.Int("sign-type", neumooc.ForceSignTypeDefault, "提交体 type（默认 1）")
	refreshSeed := fs.String("refresh-seed", "", "伪造 refreshSeed（默认 \"0\"）")
	signRole := fs.Int("sign-role", neumooc.SignRoleStudent,
		"提交体 signRole：4=学生签到形态（默认），3=教师手动修改形态（可改已结束的场次）")
	noTeacherFallback := fs.Bool("no-teacher-fallback", false,
		"学生形态被拒时不要自动改用教师形态重试")
	teacherUserID := fs.String("teacher-user-id", "",
		"教师形态（signRole=3）用的 signUserId，即教师 user id；留空则从考勤行字段推断")
	reopenEnded := fs.Bool("reopen-ended", false,
		"已结束场次补签：临时重开考勤窗口 → 补签 → 立即恢复（重开的几十秒里该场对全班显示为进行中）")
	reopenSeconds := fs.Int("reopen-seconds", neumooc.ForceReopenSecondsDefault,
		"临时重开的窗口秒数（默认 180）")
	signStatus := fs.Int("sign-status", neumooc.SignStatusPresent,
		"补签写入的考勤状态：1=出勤（默认）2=缺勤 3=事假 4=病假 5=迟到 6=早退")
	signTime := fs.String("sign-time", "",
		"补签写回的 signTime（毫秒时间戳或 'YYYY-MM-DD HH:MM:SS'）；留空沿用该场原有签到时间")
	noSkipSigned := fs.Bool("no-skip-signed", false,
		"不先拉本人考勤列表过滤已签场次（默认会跳过已签，避免重复提交）")
	payloadStyle := fs.String("payload-style", neumooc.PayloadStyleFull,
		"提交体形态：full=App 直签（type+refreshSeed+坐标）；teacher=Web 教师端补签最小字段集")
	extraFields := fs.String("extra-fields", "",
		`额外并入提交体的字段 JSON，例如 {"signTime":"2026-09-14 08:00:00"}`)
	submitPath := fs.String("submit-path", "",
		"覆盖提交路由（默认 ATT-05 detail/update）")
	includeEnded := fs.Bool("include-ended", false,
		"也尝试补签最近已结束的场次（服务端直签接口会返回 1020065005「考勤已结束」，命中后自动跳过）")
	endedWithin := fs.Int("ended-within", neumooc.ForceEndedWindowDefault, "--include-ended 的回看分钟数")
	pageSize := fs.Int("page-size", neumooc.ForcePageSizeDefault, "考勤列表分页大小")
	maxAttempts := fs.Int("max-attempts", 3, "每场失败重试上限")
	noVerify := fs.Bool("no-verify", false, "提交成功后不回读 ATT-02 校验")
	dryRun := fs.Bool("dry-run", false, "只打印提交体，不实际提交")
	quiet := fs.Bool("quiet", false, "无待补场次时不打印每轮日志")
	website := fs.String("website", "", "覆盖业务域名")
	debug := fs.Bool("debug", false, "打印原始请求/响应")
	insecure := fs.Bool("insecure", false, "忽略 TLS 证书校验")
	fs.Parse(args)

	client := neumooc.NewClient(*website, *debug, *insecure)
	cfg := neumooc.DefaultForceConfig()
	cfg.Interval = *interval
	cfg.MaxRounds = *maxRounds
	if *once {
		cfg.MaxRounds = 1
	}
	cfg.Longitude = *longitude
	cfg.Latitude = *latitude
	cfg.Address = *address
	cfg.SignType = *signType
	cfg.RefreshSeed = *refreshSeed
	cfg.SignRole = *signRole
	cfg.TeacherFallback = !*noTeacherFallback
	if *teacherUserID != "" {
		cfg.TeacherUserID = *teacherUserID
	}
	cfg.PayloadStyle = *payloadStyle
	cfg.SkipSigned = !*noSkipSigned
	cfg.ReopenEnded = *reopenEnded
	cfg.ReopenSeconds = *reopenSeconds
	cfg.SignStatus = *signStatus
	if *signTime != "" {
		cfg.SignTime = *signTime
	}
	if *extraFields != "" {
		var m map[string]interface{}
		if err := json.Unmarshal([]byte(*extraFields), &m); err != nil {
			fmt.Fprintf(os.Stderr, "--extra-fields 不是有效 JSON 对象：%v\n", err)
			os.Exit(1)
		}
		cfg.ExtraFields = m
	}
	cfg.SubmitPath = *submitPath
	cfg.IncludeEnded = *includeEnded
	cfg.EndedWithinMinutes = *endedWithin
	cfg.PageSize = *pageSize
	cfg.MaxAttempts = *maxAttempts
	cfg.Verify = !*noVerify
	cfg.DryRun = *dryRun
	cfg.Quiet = *quiet
	if *termID != "" {
		cfg.TermID = *termID
	}
	cfg.CourseIDs = splitCourseIDs(*courses)

	bot := neumooc.NewForceCheckinBot(client, cfg)
	if err := bot.Run(); err != nil {
		fmt.Fprintf(os.Stderr, "错误：%v\n", err)
		os.Exit(1)
	}
}

func cmdCourses(args []string) {
	fs := flag.NewFlagSet("courses", flag.ExitOnError)
	termID := fs.String("term-id", "", "学期 ID（默认自动识别）")
	website := fs.String("website", "", "覆盖业务域名")
	debug := fs.Bool("debug", false, "debug")
	insecure := fs.Bool("insecure", false, "insecure")
	fs.Parse(args)

	client := neumooc.NewClient(*website, *debug, *insecure)
	if client.AccessToken == "" && !client.EnsureLoggedIn() {
		fmt.Fprintln(os.Stderr, "本地没有访问令牌，请先 login")
		os.Exit(1)
	}
	var term interface{}
	if *termID != "" {
		term = *termID
	} else {
		t, err := client.CurrentTermID()
		if err != nil {
			fmt.Fprintf(os.Stderr, "错误：%v\n", err)
			os.Exit(1)
		}
		term = t
	}
	items, err := client.GetCourseOptionsByTerm(term)
	if err != nil {
		fmt.Fprintf(os.Stderr, "错误：%v\n", err)
		os.Exit(1)
	}
	if len(items) == 0 {
		fmt.Println("未查到课程（可加 --debug 查看原始返回）")
		return
	}
	fmt.Printf("学期 %v 共 %d 门课程（--course-id 填 teachCourseId 的值）：\n", term, len(items))
	for _, item := range items {
		fmt.Printf("  teachCourseId=%v\t课程=%v\n",
			firstOf(item, "teachCourseId", "courseId", "id"),
			firstOf(item, "teachCourseName", "courseName", "name"))
	}
}

func firstOf(m map[string]interface{}, keys ...string) interface{} {
	for _, k := range keys {
		if v, ok := m[k]; ok && v != nil {
			return v
		}
	}
	return nil
}

func splitCourseIDs(raw string) []interface{} {
	var out []interface{}
	for _, part := range strings.Split(raw, ",") {
		if part = strings.TrimSpace(part); part != "" {
			out = append(out, part)
		}
	}
	return out
}

func cmdProfile(args []string) {
	fs := flag.NewFlagSet("profile", flag.ExitOnError)
	website := fs.String("website", "", "覆盖业务域名")
	debug := fs.Bool("debug", false, "debug")
	insecure := fs.Bool("insecure", false, "insecure")
	fs.Parse(args)
	client := neumooc.NewClient(*website, *debug, *insecure)
	data, err := client.GetProfile()
	if err != nil {
		fmt.Fprintf(os.Stderr, "错误：%v\n", err)
		os.Exit(1)
	}
	printJSON(data)
}

func cmdRefresh(args []string) {
	fs := flag.NewFlagSet("refresh", flag.ExitOnError)
	website := fs.String("website", "", "覆盖业务域名")
	debug := fs.Bool("debug", false, "debug")
	insecure := fs.Bool("insecure", false, "insecure")
	fs.Parse(args)
	client := neumooc.NewClient(*website, *debug, *insecure)
	if _, err := client.RefreshAccessToken(); err != nil {
		fmt.Fprintf(os.Stderr, "错误：%v\n", err)
		os.Exit(1)
	}
	fmt.Println("[OK] 令牌刷新成功（AUTH-05）")
	printSession(client)
}

func cmdCall(args []string) {
	fs := flag.NewFlagSet("call", flag.ExitOnError)
	paramsRaw := fs.String("params", "", "查询参数 JSON")
	bodyRaw := fs.String("body", "", "请求体 JSON")
	website := fs.String("website", "", "覆盖业务域名")
	debug := fs.Bool("debug", false, "debug")
	insecure := fs.Bool("insecure", false, "insecure")
	fs.Parse(args)
	if fs.NArg() < 2 {
		fmt.Fprintln(os.Stderr, "用法：neumooc call METHOD PATH [--params ...] [--body ...]")
		os.Exit(1)
	}
	method := strings.ToUpper(fs.Arg(0))
	path := fs.Arg(1)

	client := neumooc.NewClient(*website, *debug, *insecure)
	var params map[string]string
	if *paramsRaw != "" {
		var m map[string]interface{}
		if json.Unmarshal([]byte(*paramsRaw), &m) == nil {
			params = map[string]string{}
			for k, v := range m {
				params[k] = fmt.Sprint(v)
			}
		}
	}
	var body interface{}
	if *bodyRaw != "" {
		_ = json.Unmarshal([]byte(*bodyRaw), &body)
	}
	data, err := client.Call(method, path, params, body)
	if err != nil {
		fmt.Fprintf(os.Stderr, "错误：%v\n", err)
		os.Exit(1)
	}
	printJSON(data)
}

func printSession(c *neumooc.Client) {
	fmt.Printf("  业务域名 : %s\n", c.Base)
	fmt.Printf("  租户 ID  : %s\n", c.TenantID)
	fmt.Printf("  用户 ID  : %v\n", c.UserID)
	fmt.Printf("  访问令牌 : %s\n", mask(c.AccessToken))
	fmt.Printf("  刷新令牌 : %s\n", mask(c.RefreshToken))
}

func mask(s string) string {
	if s == "" {
		return "<空>"
	}
	if len(s) <= 4 {
		return s + "****"
	}
	if len(s) <= 16 {
		return s[:4] + "****"
	}
	return s[:10] + "..." + s[len(s)-6:]
}

func printJSON(v interface{}) {
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		fmt.Println(v)
		return
	}
	fmt.Println(string(b))
}
