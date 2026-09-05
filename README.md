# 东软智慧教育 App 全量接口客户端（Python）

基于《东软智慧教育 App 全量接口文档》（APK v1.0.74 / `__UNI__F603DEF` 静态分析结果）实现的 Python 客户端。

当前业务域名为 `https://study.neusoft.edu.cn`。已覆盖文档中的 56 个接口：

| 分组 | 编号 | 数量 |
| --- | --- | ---: |
| 认证与租户 | AUTH-01～08 | 8 |
| 用户、设备与消息 | USR-01～06、DEV-01～02、MSG-01～05 | 13 |
| 版本、日志与错误上报 | INFRA-01～03 | 3 |
| 教学、课程和通知 | EDU-01～13 | 13 |
| 考勤 | ATT-01～05 | 5 |
| 课程资源与学习记录 | RES-01～09 | 9 |
| 文件服务 | FILE-01～05 | 5 |

公共请求层实现了 `Authorization`、`Tenant-Id`、`Id-Code` 请求头，校验 `{code,msg,data}` 响应，并在业务 `code=401` 时刷新令牌后重试一次。文件下载返回二进制，批量上传使用 `multipart/form-data`。

> AUTH-01 和 AUTH-05 仍按静态分析文档分别使用固定租户地址和认证刷新地址；其余业务、教学、考勤、资源与文件接口均使用当前业务域名。

## 安装

```bash
pip install -r requirements.txt
```

## 登录与会话

```bash
python neumooc_login.py login -u 学号 -p 密码 -t 租户ID
python neumooc_login.py sms --phone 13800000000 -t 租户ID
python neumooc_login.py sms-login --phone 13800000000 --code 123456 -t 租户ID
python neumooc_login.py profile
python neumooc_login.py refresh
```

不带子命令运行会进入原有的交互式登录菜单：

```bash
python neumooc_login.py
```

登录信息保存在 `neumooc_token.json`。如果缓存来自旧版且其中仍是旧默认域名，客户端会自动迁移到 `https://study.neusoft.edu.cn`；显式保存的其他自定义域名不会被覆盖。

## 保存密码与自动重新登录

登录时加 `--save-credentials` 会把账号密码（明文）写入 `neumooc_credentials.json`：

```bash
python neumooc_login.py login -u 学号 -p 密码 -t 租户ID --save-credentials
```

此后访问令牌失效（且刷新令牌也失败）时，客户端会自动用保存的账号密码重新登录并重试一次；
`auto-checkin` 启动时本地没有令牌也会自动尝试登录，无需人工干预。明文密码有泄漏风险，
Linux 下建议 `chmod 600 neumooc_credentials.json`；不再需要时删除该文件即可。

## 调用其余接口

文档中大量接口采用“对象透传”，CLI 提供通用 `call` 命令，查询参数和请求体均接受 JSON：

```bash
# EDU-01 获取学期选项
python neumooc_login.py call GET /web-api/teachmanager/teach-dropdown/getTeachTermDropDown

# MSG-03 获取消息详情
python neumooc_login.py call GET /web-api/system/notify-target/get --params '{"id":123}'

# ATT-01 查询考勤
python neumooc_login.py call POST /web-api/teachmanager/teach-course-attendance-detail/getAppStuAttendancePage --body '{"studentId":1,"termId":2,"courseId":null,"attendanceStatus":null,"status":null}'

# MSG-04 数组请求体
python neumooc_login.py call PUT /web-api/system/notify-target/update-read --body '[101,102]'
```

全局选项要放在子命令之前，例如：

```bash
python neumooc_login.py --debug --website https://study.neusoft.edu.cn call GET /web-api/system/notify-target/get-unread-count
```

文件下载与上传：

```bash
python neumooc_login.py download --eid 文件EID -o output.pdf
python neumooc_login.py upload first.pdf second.docx
```

## 学生自动签到

`auto-checkin` 子命令（交互菜单第 8 项）会轮询“进行中”的课程考勤，发现未签到的考勤后自动提交签到（ATT-01 查询 → ATT-05 提交）：

```bash
# 先登录保存令牌
python neumooc_login.py login -u 学号 -p 密码 -t 租户ID

# 演练：只看会提交什么，不真正签到
python neumooc_login.py auto-checkin --once --dry-run

# 正式启动：默认每 30 秒扫描一轮，Ctrl+C 停止
python neumooc_login.py auto-checkin

# 定位签到：需要提供坐标（提交为 signLongitude/signLatitude/signAddressName）
python neumooc_login.py auto-checkin --longitude 121.5 --latitude 38.9 --address 教学楼A

# 二维码考勤直接签到：默认按 type=1 提交；服务端仍校验刷新种子时改 type=0
python neumooc_login.py auto-checkin --qr-sign-type 0

# 常用选项
python neumooc_login.py auto-checkin --interval 15       # 轮询间隔
python neumooc_login.py auto-checkin --course-id 课程ID   # 只关注一门课
python neumooc_login.py auto-checkin --term-id 学期ID     # 手动指定学期（默认自动识别）
python neumooc_login.py auto-checkin --quiet --max-rounds 120  # 安静模式 + 限定轮数
```

签到规则与依据（APK v1.0.74 静态分析接口文档第 5 节 + Web 端源码佐证）：

- 提交体为 `PUT /teachmanager/teach-course-attendance-detail/update`，核心字段
  `{attendanceId, id, status: 1, type, signRole: 4, signUserId}`；定位签到额外携带
  `signLongitude/signLatitude/signAddressName`；
- 二维码签到已下线：不再扫描二维码、不再调用 ATT-04 校验、不携带 `qrCodeId`；
  二维码考勤(type=1)直接发包提交 ATT-05，提交体 `type` 默认 1，并附带伪造的
  `refreshSeed="0"`（该直签通道不校验凭证）与默认坐标（可用 `--longitude/--latitude/
  --address` 覆盖）；如需按普通签到提交可用 `--qr-sign-type 0`；
- 考勤场次状态 `status`：0 未开始 / 1 进行中 / 2 已结束，机器人只处理进行中的场次；
- 签到类型 `type`：0 普通签到自动提交；0 + 定位（`attendanceLocationType=1`）需提供
  `--longitude/--latitude`（可选 `--address`）；2 教师考勤默认跳过（`--include-teacher` 开启）；
- 已签判定：行内 `signRole` 为 3/4 或存在签到时间即视为已签；服务端报“已签到”也会标记完成；
  每场考勤失败重试上限默认 3 次（`--max-attempts`）；
- 登录态失效（401 且刷新失败）时自动停止并提示重新登录；查询接口偶发失败只记录日志继续轮询。

ATT-01 返回行为“对象透传”，字段名若与实际不一致，机器人会跳过并打印原始行数据，
按 `neumooc_checkin.py` 顶部常量区调整即可。可配合 Windows 任务计划程序在上课时段定时启动。

## 教师发布签到

当前 Web 教师端还提供了 APK 学生端清单之外的发布接口，客户端已补充支持。建议先用 `--dry-run` 核对参数：

```bash
# 普通签到，仅生成请求体
python neumooc_login.py publish-attendance --course-id 课程ID --class-id 班级ID --title 测试签到 --duration 10 --dry-run

# 二维码签到，去掉 --dry-run 后会真实发布
python neumooc_login.py publish-attendance --course-id 课程ID --class-id 班级ID --title 二维码签到 --type qr --qr-refresh 10
```

发布会改变线上考勤数据，并可能对班级学生可见，因此真实执行前必须确认 `teachCourseId` 和 `teachClassId` 属于目标教学班。定位签到还需提供 `--location-type 1`、经纬度和地址。

## Python SDK 示例

`NeumoocClient` 为全部 56 个接口提供了具名方法，也保留 `request_api()` 作为后续接口的通用入口：

```python
from neumooc_login import NeumoocClient

client = NeumoocClient()  # 自动读取 neumooc_token.json

terms = client.get_term_options()                         # EDU-01
messages = client.get_message_page({"pageNo": 1})         # MSG-01
attendance = client.get_student_attendance_page({         # ATT-01
    "studentId": client.user_id,
    "termId": 1,
    "courseId": None,
    "attendanceStatus": None,
    "status": None,
})
is_valid = client.check_qr_code_valid(1001, "qr-code-id") # ATT-04
```

自动签到也可以在代码中直接驱动（`neumooc_checkin.AutoCheckinBot`）：

```python
from neumooc_login import NeumoocClient
from neumooc_checkin import AutoCheckinBot

client = NeumoocClient()                       # 自动读取 neumooc_token.json
bot = AutoCheckinBot(client, interval=30)      # 选项与 CLI 参数一一对应
bot.run()                                      # Ctrl+C 或 max_rounds 结束
```

具名方法按文档分组排列在 `NeumoocClient` 中：对象透传接口接收 `dict`，批量已读接收 ID 序列，文件接口接收 EID 或本地路径列表。

## 联调注意事项

- 登录体字段名仍按 `username/password`、`phoneNumber/code` 实现，可在文件顶部常量区调整。
- `Id-Code` 的官方算法未公开，当前实现仍为与用户、随机请求 ID、接口路径相关的占位算法。
- 文档未确认的字段约束、枚举和错误码需要根据实际服务端响应调整。
- 静态分析中的令牌刷新固定地址为 `https://studytest3.neumooc.com`；若生产环境刷新端点不同，应在联调确认后修改 `AUTH_REFRESH_BASE`。

## Linux 服务器部署

服务端只需 Python 3.8+ 和 `requests`，无其他系统依赖。把 `neumooc_login.py`、
`neumooc_checkin.py`、`requirements.txt` 上传到服务器同一目录即可（`tests/` 可不传）。

```bash
# 1. 安装依赖（Debian/Ubuntu 示例）
sudo apt-get update && sudo apt-get install -y python3 python3-pip
cd /opt/neumooc-api
python3 -m pip install -r requirements.txt

# 2. 设置时区（学期识别使用服务器本地日期，务必与上课地区一致）
sudo timedatectl set-timezone Asia/Shanghai

# 3. 非交互登录，令牌保存到当前目录 neumooc_token.json
python3 neumooc_login.py login -u 学号 -p 密码 -t 租户ID
python3 neumooc_login.py profile          # 校验令牌可用
```

### 方式一：tmux / screen / nohup 常驻轮询（最省事）

```bash
# 登录后挂到 tmux/screen，退出会话也继续跑
tmux new -s checkin
python3 neumooc_login.py auto-checkin --interval 30
# Ctrl+B 后按 D 退出 tmux；重新进入：tmux attach -t checkin

# 或 nohup 后台运行 + 日志
nohup python3 neumooc_login.py auto-checkin --interval 30 >> checkin.log 2>&1 &
tail -f checkin.log
```

### 方式二：systemd 服务（开机自启、崩溃自动拉起）

`/etc/systemd/system/neumooc-checkin.service`：

```ini
[Unit]
Description=Neumooc auto checkin
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/neumooc-api
ExecStart=/usr/bin/python3 /opt/neumooc-api/neumooc_login.py auto-checkin --interval 30
Restart=on-failure
RestartSec=30
Environment=TZ=Asia/Shanghai

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now neumooc-checkin
sudo systemctl status neumooc-checkin        # 查看状态
journalctl -u neumooc-checkin -f             # 实时日志
```

> 登录态失效（令牌刷新失败）后机器人会退出；systemd 会自动重启，但会因无令牌反复失败。
> 此时重新执行一次 `login` 再 `systemctl restart neumooc-checkin` 即可。

### 方式三：cron 定时 --once（只在需要时段跑，签完即退）

不想常驻时，用 `--once` 每轮只扫描一次、签完即退：

```cron
# 每天 8:00-22:00 每 5 分钟尝试一次（已签/无考勤会自动跳过）
*/5 8-22 * * * cd /opt/neumooc-api && /usr/bin/python3 neumooc_login.py auto-checkin --once --quiet >> /var/log/neumooc-checkin.log 2>&1
```

常用参数（Linux 与 Windows 一致）：`--interval`（轮询秒数）、`--course-id`（只看某门课）、
`--term-id`（手动指定学期）、`--longitude/--latitude/--address`（定位签到）、
`--qr-sign-type 0|1`（二维码考勤直接签到的 type，默认 1）、`--dry-run`（演练）、
`--max-rounds`（限定轮数）。

## 测试

```bash
python -m unittest discover -s tests -v
```
