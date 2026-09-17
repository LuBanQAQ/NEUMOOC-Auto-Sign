# 东软智慧教育 App 自动签到脚本

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

自动签到会兼容服务端把进行中考勤返回为 `status=null` 的情况，并在本地跳过明确未开始或已结束的场次。

教师账号可为指定学生补签；服务端会校验当前账号的教师权限：

```bash
python neumooc_login.py teacher-makeup --attendance-id 考勤ID --detail-id 明细ID --student-id 学生用户ID --dry-run
python neumooc_login.py teacher-makeup --attendance-id 考勤ID --detail-id 明细ID --student-id 学生用户ID
```

