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

