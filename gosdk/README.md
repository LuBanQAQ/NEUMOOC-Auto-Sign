# 东软智慧教育 Go SDK / CLI

Python 版 `neumooc_login.py` / `neumooc_checkin.py` 的 Go 移植：纯标准库、无第三方依赖，
编译后单文件可执行，方便挂到 Linux 服务器。

- `client.go`：`neumooc.Client`（登录、刷新令牌、通用请求、401 自动刷新/凭据重登、会话与凭据持久化）
- `checkin.go`：`neumooc.AutoCheckinBot`（自动签到，直接发包，二维码考勤直签 `refreshSeed="0"`）
- `cmd/neumooc/main.go`：命令行入口

## 构建

```bash
cd gosdk
go build ./cmd/neumooc
# 或直接运行
go run ./cmd/neumooc --help
```

交叉编译（Linux 服务器上跑）：

```bash
GOOS=linux GOARCH=amd64 go build -o neumooc-linux ./cmd/neumooc
```

## 用法

```bash
# 登录（--save-credentials 保存密码，令牌失效自动重登）
./neumooc login -u 学号 -p 密码 -t 租户ID --save-credentials

# 演练：只看会提交什么
./neumooc auto-checkin --once --dry-run

# 正式直接发包签到（每 30 秒扫一轮，Ctrl+C 停）
./neumooc auto-checkin

# 只跑一轮
./neumooc auto-checkin --once

# 教师接口强制补签（仍用学生账号）：教师端考勤列表 -> 直签
./neumooc force-checkin --once --dry-run          # 演练（该学期全部课程）
./neumooc force-checkin                           # 正式，只处理进行中的场次
./neumooc force-checkin --course-id c1,c2         # 只补签指定课程
./neumooc force-checkin --include-ended --ended-within 120   # 也尝试含最近结束的场次

# 不知道课程 ID？列出本学期课程及 teachCourseId
./neumooc courses

# 校验令牌 / 手动刷新
./neumooc profile
./neumooc refresh

# 调用任意接口
./neumooc call GET /web-api/system/notify-target/get-unread-count
./neumooc call POST /web-api/teachmanager/teach-course-attendance-detail/getAppStuAttendancePage --body '{"studentId":1,"termId":1}'
```

常用 `auto-checkin` 参数与 Python 版一致：

| 参数 | 作用 |
| --- | --- |
| `--interval 15` | 轮询间隔秒（默认 30，最小 5） |
| `--once` | 只扫一轮 |
| `--dry-run` | 只打印提交体，不真签 |
| `--qr-sign-type 0` | 二维码考勤改按普通签到(type=0)提交 |
| `--longitude 121.5 --latitude 38.9 --address 教学楼A` | 覆盖默认学校坐标 |
| `--include-teacher` | 教师考勤也签 |
| `--max-rounds 120 --quiet` | 安静模式 + 限定轮数 |
| `--website 域名 --debug` | 覆盖域名 / 打印请求 |

`force-checkin`（教师接口强制补签，学生账号）参数：

| 参数 | 作用 |
| --- | --- |
| `--course-id c1,c2` | 只补签指定课程（逗号分隔；留空 = 该学期全部课程） |
| `--interval 3` | 扫描间隔秒（默认 5，最小 1） |
| `--once` / `--max-rounds N` | 只扫一轮 / 限定轮数 |
| `--include-ended --ended-within 120` | 也尝试补签最近 N 分钟内结束的场次（学生形态会被拒 `1020065005 考勤已结束`，随后自动改用教师形态） |
| `--sign-type 0` | 提交体 type 改 0（默认 1，走直签通道） |
| `--refresh-seed S` | 覆盖伪造的 refreshSeed（默认 `"0"`） |
| `--sign-role 3` | 提交体 signRole：4=学生签到形态（默认），3=教师手动修改形态 |
| `--teacher-user-id ID` | 教师形态用的 `signUserId`（必须传教师 user id；留空则从考勤行 `teacherUserId`/`createBy` 等字段推断） |
| `--no-teacher-fallback` | 学生形态被拒时不自动改用教师形态重试 |
| `--reopen-ended` | **已结束场次补签**：临时重开考勤窗口 → 补签 → 立即还原（实测通过） |
| `--reopen-seconds 60` | 临时重开的窗口秒数（默认 180） |
| `--sign-status 1` | 补签写入的状态：1=出勤 2=缺勤 3=事假 4=病假 5=迟到 6=早退 |
| `--sign-time 时间` | 补签写回的 `signTime`；留空沿用该场原有签到时间 |
| `--payload-style teacher` | 提交体用 Web 教师端补签的最小字段集 `{attendanceId,id,status,signRole:3,signUserId}` |
| `--teacher-user-id ID` | 覆盖 `signUserId`；默认自动取课程 `teacherId` |
| `--no-skip-signed` | 不先拉本人考勤列表过滤已签场次（默认会跳过已签） |
| `--extra-fields JSON` | 额外并入提交体的字段（联调用） |
| `--submit-path 路径` | 覆盖提交路由（默认 ATT-05 `detail/update`） |
| `--no-verify` | 提交成功后不回读 ATT-02 校验 |
| `--longitude/--latitude/--address` | 覆盖默认学校坐标 |
| `--dry-run` / `--quiet` | 只打印提交体 / 安静模式 |

## SDK 调用示例

```go
package main

import (
    "fmt"
    "neumooc"
)

func main() {
    c := neumooc.NewClient("", false, false) // 自动读取 neumooc_token.json
    if _, err := c.LoginWithPassword("学号", "密码", "租户ID"); err != nil {
        panic(err)
    }
    terms, _ := c.GetTermOptions()
    fmt.Println(terms)

    bot := neumooc.NewAutoCheckinBot(c, neumooc.DefaultCheckinConfig())
    _ = bot.Run()
}
```

## 说明

- 令牌保存到 `neumooc_token.json`，凭据保存到 `neumooc_credentials.json`（明文，Linux 建议 `chmod 600`）。
- 二维码考勤直签提交体与 Python 版一致：`type=1`、`refreshSeed="0"`、默认学校坐标
  （`121.614682 / 38.914003 / 大连东软信息学院`），不扫描二维码、不调用 ATT-04 校验。
- 补签形态：默认 App 直签形态（`type=1` + `refreshSeed="0"` + 坐标）。被拒
  （如 `1020065005 考勤已结束`）时自动改用 **Web 教师端补签形态**重试一次 ——
  提交体只剩 `{attendanceId, id, status, signRole:3, signUserId}`，
  **去掉 type/坐标/refreshSeed**（这几个字段会让服务端走"学生签到"校验路径）。
  也可用 `--payload-style teacher` 一开始就走它。`signUserId` 取
  `--teacher-user-id` → 考勤行 `teacherUserId`/`teacherId`/`createBy` → 当前用户 ID。
- 默认先拉本人考勤列表，**跳过已签场次**（`--no-skip-signed` 可关闭）；顺带记下各场原有
  `signTime`，补签时原样写回。
- **已结束场次补签**（`--reopen-ended`，实测通过）：
  1. `GET teach-course-attendance/get` 读原 `status`/`finishTime`；
  2. `PUT teach-course-attendance/update {id,status:1,finishTime:now+窗口}` 重开；
  3. `PUT detail/update` 补签（`signRole=3` + `signUserId=课程 teacherId` + 原 `signTime`）；
  4. **无论成败都还原**原 `status`/`finishTime`。
  注意：重开期间该场对全班显示「进行中」；记录一旦 `signRole=3` 就锁死（`1020065006`）。
- 登录仍是明文 `username/password`；若服务端要求 `isPasswordEncrypt` + AES，需自行补充加密逻辑。
