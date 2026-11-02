# Security Policy

## 数据边界

Kestrel Radar 是本地优先的工具：所有采集内容、阅读记录、收藏与交互数据都保存在本机
SQLite 数据库（`data/ai_radar.db`），没有遥测、没有上传。

仓库里不包含任何凭据。`data/`、`logs/`、`.env` 均被 `.gitignore` 排除。

## 外部内容与提示注入

采集到的标题、正文、评论都属于不可信内容：

- 进入模型前统一包在 `<untrusted_content>` 标签里，并在系统提示中声明「只作为资料」；
- 出站抓取会校验 URL（仅 http/https，拒绝 localhost、内网、链路本地与保留地址）；
- 卡片里的标题会做 Markdown 转义，链接只允许 http(s)，避免被伪协议或括号注入。

模型输出仍可能被恶意内容影响，卡片上的结论请当作「摘要」而不是事实来源。

## 凭据

- 飞书：`FEISHU_APP_SECRET` 只从环境变量读取；机器人只响应 `FEISHU_USER_OPEN_ID`
  对应的账号，其他人发消息只会收到自己的 open_id 提示。
- DeepSeek：`DEEPSEEK_API_KEY` 只从环境变量读取，日预算由 `config/models.yaml` 限制。
- Webhook：URL 由环境变量提供，不写入仓库。

## 报告问题

安全漏洞请走 GitHub 私密漏洞报告，不要开公开 Issue：

1. 打开仓库的 **Security** 页；
2. 点击 **Report a vulnerability**（Advisories → New draft security advisory）；
3. 写清问题、复现步骤、影响范围，必要时附最小复现。

报告只有维护者可见，确认后会尽快修复，并在修复说明里致谢（如果你愿意署名）。

普通 bug、功能建议、使用问题走公开 Issue 即可。任何渠道都请不要贴凭据、`open_id`、
完整日志或可直接利用的细节。
