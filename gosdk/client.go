// Package neumooc 提供东软智慧教育 App 的 Go 客户端 SDK：登录、令牌刷新、
// 通用请求、凭据自动重登，以及自动签到机器人。接口与 Python 版
// neumooc_login.py / neumooc_checkin.py 对齐，纯标准库、无第三方依赖。
package neumooc

import (
	"bytes"
	"crypto/md5"
	"crypto/rand"
	"crypto/tls"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

const (
	DefaultBusinessBase = "https://study.neusoft.edu.cn"
	LegacyBusinessBase  = "https://neustudy.neumooc.com"
	AuthRefreshBase     = "https://studytest3.neumooc.com"
	WebAPI              = "/web-api"
	DefaultTokenFile    = "neumooc_token.json"
	DefaultCredFile     = "neumooc_credentials.json"

	UserAgent = "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 " +
		"(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36 uni-app"
)

// ApiError 表示业务/协议错误（HTTP 非 2xx、业务 code != 0、响应结构异常等）。
type ApiError struct {
	Code interface{}
	Msg  string
	Data interface{}
}

func (e *ApiError) Error() string { return fmt.Sprintf("[%v] %s", e.Code, e.Msg) }

// Client 是东软智慧教育 App 的 API 客户端。
type Client struct {
	Base         string
	TenantID     string
	AccessToken  string
	RefreshToken string
	UserID       interface{}

	TokenFile string
	CredFile  string

	Debug    bool
	Insecure bool
	Timeout  time.Duration

	http         *http.Client
	explicitBase bool
}

// NewClient 创建客户端并读取本地会话文件（neumooc_token.json）。
func NewClient(website string, debug, insecure bool) *Client {
	c := &Client{
		Base:      DefaultBusinessBase,
		TokenFile: DefaultTokenFile,
		CredFile:  DefaultCredFile,
		Debug:     debug,
		Insecure:  insecure,
		Timeout:   20 * time.Second,
	}
	if website != "" {
		c.Base = strings.TrimRight(website, "/")
		c.explicitBase = true
	}
	tr := &http.Transport{}
	if insecure {
		tr.TLSClientConfig = &tls.Config{InsecureSkipVerify: true}
	}
	c.http = &http.Client{Transport: tr, Timeout: c.Timeout}
	c.loadSession()
	return c
}

// ---- 会话持久化 ----

type sessionFile struct {
	Base         string      `json:"base"`
	TenantID     interface{} `json:"tenantId"`
	AccessToken  string      `json:"accessToken"`
	RefreshToken string      `json:"refreshToken"`
	UserID       interface{} `json:"userId"`
}

func (c *Client) loadSession() {
	b, err := os.ReadFile(c.TokenFile)
	if err != nil {
		return
	}
	var s sessionFile
	if json.Unmarshal(b, &s) != nil {
		return
	}
	if s.AccessToken != "" {
		c.AccessToken = s.AccessToken
	}
	if s.RefreshToken != "" {
		c.RefreshToken = s.RefreshToken
	}
	if s.UserID != nil {
		c.UserID = s.UserID
	}
	if c.TenantID == "" && s.TenantID != nil {
		c.TenantID = fmt.Sprint(s.TenantID)
	}
	// 未显式指定域名时沿用自定义地址；旧版默认地址自动迁移到当前业务域名。
	if !c.explicitBase && s.Base != "" {
		saved := strings.TrimRight(s.Base, "/")
		if saved != LegacyBusinessBase {
			c.Base = saved
		}
	}
}

// SaveSession 把当前令牌写回 neumooc_token.json。
func (c *Client) SaveSession() error {
	s := sessionFile{
		Base:         c.Base,
		TenantID:     c.TenantID,
		AccessToken:  c.AccessToken,
		RefreshToken: c.RefreshToken,
		UserID:       c.UserID,
	}
	b, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(c.TokenFile, b, 0o600)
}

// ClearSession 清除本地登录信息（仅本地）。
func (c *Client) ClearSession() {
	c.AccessToken = ""
	c.RefreshToken = ""
	c.UserID = nil
	_ = os.Remove(c.TokenFile)
}

// ---- 登录凭据（可选，用于令牌失效后自动重新登录） ----

// LoadCredentials 读取保存的账号密码凭据（明文密码，注意文件权限）。
func (c *Client) LoadCredentials() map[string]interface{} {
	b, err := os.ReadFile(c.CredFile)
	if err != nil {
		return nil
	}
	var m map[string]interface{}
	if json.Unmarshal(b, &m) != nil {
		return nil
	}
	return m
}

// SaveCredentials 把账号密码写入凭据文件。
func (c *Client) SaveCredentials(username, password string) error {
	m := map[string]interface{}{
		"username": username,
		"password": password,
		"tenantId": c.TenantID,
	}
	b, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		return err
	}
	if err := os.WriteFile(c.CredFile, b, 0o600); err != nil {
		return err
	}
	fmt.Printf("[提示] 已保存明文密码到 %s，Linux 下建议 chmod 600\n", c.CredFile)
	return nil
}

// ---- 请求核心 ----

func (c *Client) idCode(path string) string {
	seed := fmt.Sprintf("%v|%s|%s", c.UserID, randomHex(16), path)
	sum := md5.Sum([]byte(seed))
	return hex.EncodeToString(sum[:])
}

func randomHex(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func (c *Client) request(method, rawurl string, params map[string]string, body interface{}, withToken, allowRefresh, retried bool) (interface{}, error) {
	full := rawurl
	if !strings.HasPrefix(full, "http://") && !strings.HasPrefix(full, "https://") {
		full = strings.TrimRight(c.Base, "/") + "/" + strings.TrimLeft(rawurl, "/")
	}
	if len(params) > 0 {
		q := url.Values{}
		for k, v := range params {
			q.Set(k, v)
		}
		full += "?" + q.Encode()
	}

	var reader io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		reader = bytes.NewReader(b)
	}

	req, err := http.NewRequest(method, full, reader)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", UserAgent)
	if c.TenantID != "" {
		req.Header.Set("Tenant-Id", c.TenantID)
	}
	req.Header.Set("Id-Code", c.idCode(pathOf(full)))
	if withToken && c.AccessToken != "" {
		req.Header.Set("Authorization", "Bearer "+c.AccessToken)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}

	if c.Debug {
		fmt.Printf("-> %s %s\n", method, full)
		if body != nil {
			if b, err := json.Marshal(body); err == nil {
				fmt.Printf("   body: %s\n", string(b))
			}
		}
	}

	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, &ApiError{Code: resp.StatusCode, Msg: "HTTP 状态异常: " + truncate(string(raw), 200)}
	}

	var env struct {
		Code interface{} `json:"code"`
		Msg  string      `json:"msg"`
		Data interface{} `json:"data"`
	}
	if err := json.Unmarshal(raw, &env); err != nil {
		return nil, &ApiError{Code: -1, Msg: "响应不是 JSON: " + truncate(string(raw), 200)}
	}
	if c.Debug {
		fmt.Printf("<- HTTP %d %s\n", resp.StatusCode, truncate(string(raw), 1500))
	}

	if codeEquals(env.Code, 0) {
		return env.Data, nil
	}

	// 401：先刷新令牌并重试一次；刷新失败则用保存的账号密码自动重登再重试。
	if codeEquals(env.Code, 401) && withToken && allowRefresh && !retried {
		recovered := false
		if c.RefreshToken != "" {
			if _, err := c.RefreshAccessToken(); err == nil {
				recovered = true
			}
		}
		if !recovered {
			recovered = c.relogin()
		}
		if !recovered {
			c.ClearSession()
			return nil, &ApiError{Code: env.Code, Msg: orEmpty(env.Msg), Data: env.Data}
		}
		return c.request(method, rawurl, params, body, withToken, false, true)
	}

	return nil, &ApiError{Code: env.Code, Msg: orEmpty(env.Msg), Data: env.Data}
}

// ---- 令牌解析 ----

func (c *Client) absorbLogin(data interface{}) error {
	m, ok := data.(map[string]interface{})
	if !ok {
		if s, ok := data.(string); ok && s != "" {
			c.AccessToken = s
		}
	} else {
		if v := first(m, "accessToken", "access_token", "token", "tokenValue"); v != nil {
			c.AccessToken = fmt.Sprint(v)
		}
		if v := first(m, "refreshToken", "refresh_token"); v != nil {
			c.RefreshToken = fmt.Sprint(v)
		}
		if v := first(m, "userId", "user_id", "id"); v != nil {
			c.UserID = v
		}
	}
	if c.AccessToken == "" {
		return &ApiError{Code: -1, Msg: "登录返回 code=0 但未识别出访问令牌"}
	}
	return nil
}

// ---- AUTH ----

// LoginWithPassword 账号密码登录（AUTH-02）。
func (c *Client) LoginWithPassword(username, password, tenantID string) (interface{}, error) {
	if tenantID != "" {
		c.TenantID = tenantID
	}
	body := map[string]interface{}{"username": username, "password": password}
	if isDigits(c.TenantID) {
		if n, err := strconv.Atoi(c.TenantID); err == nil {
			body["tenantId"] = n
		}
	}
	data, err := c.request("POST", WebAPI+"/system/auth/app/login", nil, body, false, false, false)
	if err != nil {
		return nil, err
	}
	if err := c.absorbLogin(data); err != nil {
		return nil, err
	}
	_ = c.SaveSession()
	return data, nil
}

// RefreshAccessToken 刷新访问令牌（AUTH-05）。
func (c *Client) RefreshAccessToken() (interface{}, error) {
	if c.RefreshToken == "" {
		return nil, &ApiError{Code: -1, Msg: "本地没有 refreshToken，请先登录"}
	}
	u := AuthRefreshBase + WebAPI + "/system/auth/app/refresh-token"
	params := map[string]string{"refreshToken": c.RefreshToken}
	data, err := c.request("POST", u, params, nil, c.AccessToken != "", false, false)
	if err != nil {
		return nil, err
	}
	if err := c.absorbLogin(data); err != nil {
		return nil, err
	}
	_ = c.SaveSession()
	return data, nil
}

func (c *Client) relogin() bool {
	creds := c.LoadCredentials()
	if creds == nil {
		return false
	}
	u, _ := creds["username"].(string)
	p, _ := creds["password"].(string)
	if u == "" || p == "" {
		return false
	}
	tid, _ := creds["tenantId"].(string)
	if _, err := c.LoginWithPassword(u, p, tid); err != nil {
		return false
	}
	fmt.Println("[OK] 已通过保存的凭据自动重新登录")
	return true
}

// EnsureLoggedIn 确保本地存在登录态：有令牌直接用，否则先刷新，再尝试凭据自动登录。
func (c *Client) EnsureLoggedIn() bool {
	if c.AccessToken != "" {
		return true
	}
	if c.RefreshToken != "" {
		if _, err := c.RefreshAccessToken(); err == nil {
			return true
		}
	}
	return c.relogin()
}

// ---- 通用与具名接口 ----

// Call 调用文档路径的公共入口。
func (c *Client) Call(method, path string, params map[string]string, body interface{}) (interface{}, error) {
	return c.request(method, path, params, body, true, true, false)
}

// GetProfile 获取当前用户资料（USR-01）。
func (c *Client) GetProfile() (interface{}, error) {
	return c.Call("GET", WebAPI+"/system/user/profile/get", nil, nil)
}

// GetTermOptions 获取学期下拉选项（EDU-01）。
func (c *Client) GetTermOptions() ([]map[string]interface{}, error) {
	data, err := c.Call("GET", WebAPI+"/teachmanager/teach-dropdown/getTeachTermDropDown", nil, nil)
	if err != nil {
		return nil, err
	}
	return toMapList(data), nil
}

// GetStudentAttendancePage 查询学生考勤列表（ATT-01）。
func (c *Client) GetStudentAttendancePage(payload map[string]interface{}) (interface{}, error) {
	return c.Call("POST", WebAPI+"/teachmanager/teach-course-attendance-detail/getAppStuAttendancePage", nil, payload)
}

// GetAttendanceDetailID 按考勤活动与学生反查明细 ID（ATT-03）。
func (c *Client) GetAttendanceDetailID(attendanceID, studentID interface{}) (interface{}, error) {
	path := fmt.Sprintf("%s/teachmanager/teach-course-attendance-detail/getAttendanceDetailId/%v/%v", WebAPI, attendanceID, studentID)
	return c.Call("GET", path, nil, nil)
}

// GetStudentAttendanceDetail 获取学生考勤详情（ATT-02）。
func (c *Client) GetStudentAttendanceDetail(detailID interface{}) (interface{}, error) {
	path := fmt.Sprintf("%s/teachmanager/teach-course-attendance-detail/getAppStuAttendanceDetail/%v", WebAPI, detailID)
	return c.Call("GET", path, nil, nil)
}

// SubmitAttendance 提交签到（ATT-05）。
func (c *Client) SubmitAttendance(payload map[string]interface{}) (interface{}, error) {
	return c.Call("PUT", WebAPI+"/teachmanager/teach-course-attendance-detail/update", nil, payload)
}

// GetCourseOptionsByTerm 按学期获取课程选项（EDU-03）。
func (c *Client) GetCourseOptionsByTerm(termID interface{}) ([]map[string]interface{}, error) {
	params := map[string]string{"termId": fmt.Sprint(termID)}
	data, err := c.Call("GET", WebAPI+"/teachmanager/teach-course/get-option/by-term-id", params, nil)
	if err != nil {
		return nil, err
	}
	return toMapList(data), nil
}

// GetTeacherAttendancePage 查询教师端考勤列表（学生 token 亦可调用）。
func (c *Client) GetTeacherAttendancePage(params map[string]string) (interface{}, error) {
	return c.Call("GET", WebAPI+"/teachmanager/teach-course-attendance/page", params, nil)
}

// GetCourse 获取课程信息（EDU-04）：其中的 teacherId 即课程教师 user id。
func (c *Client) GetCourse(courseID interface{}) (map[string]interface{}, error) {
	data, err := c.Call("GET", WebAPI+"/teachmanager/teach-course/get",
		map[string]string{"id": fmt.Sprint(courseID)}, nil)
	if err != nil {
		return nil, err
	}
	m, _ := data.(map[string]interface{})
	return m, nil
}

// GetTeacherAttendance 考勤主表详情（GET teach-course-attendance/get）。
func (c *Client) GetTeacherAttendance(params map[string]string) (interface{}, error) {
	return c.Call("GET", WebAPI+"/teachmanager/teach-course-attendance/get", params, nil)
}

// UpdateTeacherAttendance 考勤主表更新：可改 status / finishTime，用于临时重开或恢复结束。
func (c *Client) UpdateTeacherAttendance(payload map[string]interface{}) (interface{}, error) {
	return c.Call("PUT", WebAPI+"/teachmanager/teach-course-attendance/update", nil, payload)
}

// ---- 工具函数 ----

func first(m map[string]interface{}, keys ...string) interface{} {
	for _, k := range keys {
		if v, ok := m[k]; ok && v != nil {
			return v
		}
	}
	return nil
}

func codeEquals(code interface{}, want int) bool {
	switch v := code.(type) {
	case float64:
		return int(v) == want
	case int:
		return v == want
	case string:
		n, err := strconv.Atoi(v)
		return err == nil && n == want
	}
	return false
}

func isDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

func orEmpty(s string) string {
	if s == "" {
		return "未知业务错误"
	}
	return s
}

func pathOf(rawurl string) string {
	if u, err := url.Parse(rawurl); err == nil {
		return u.Path
	}
	return rawurl
}

func toMapList(data interface{}) []map[string]interface{} {
	var out []map[string]interface{}
	switch v := data.(type) {
	case []interface{}:
		for _, item := range v {
			if m, ok := item.(map[string]interface{}); ok {
				out = append(out, m)
			}
		}
	case map[string]interface{}:
		for _, k := range []string{"list", "records", "rows", "data"} {
			if inner, ok := v[k].([]interface{}); ok {
				for _, item := range inner {
					if m, ok := item.(map[string]interface{}); ok {
						out = append(out, m)
					}
				}
				break
			}
		}
	}
	return out
}
