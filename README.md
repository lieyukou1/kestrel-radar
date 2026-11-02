# Kestrel Radar

> A radar that remembers what it already told you.
> 会记住自己说过什么的 AI 情报雷达。

Kestrel Radar 是一个本地优先的 AI 情报系统：从 arXiv、Hugging Face Daily Papers、Hacker News、
GitHub、Lobsters、官方博客和中文技术媒体采集内容，打分、聚主线、去重，然后按你的节奏推到飞书
（交互卡片）或任意 Webhook。

它和大多数「AI 新闻聚合器」的区别不在采集，而在**记忆**：同一条新闻不会因为换了个标题、
晚了两天就被重复推送；同一个话题出现了真正的新进展，它也能认出来再推一次。

---

## 它做什么

- **多源采集**：arXiv / HF Daily / Hacker News / GitHub / Lobsters / 官方博客（OpenAI、DeepMind、
  NVIDIA、Apple ML、HF）/ 中文技术媒体 RSS；公众号等个人订阅源可通过 `config/sources.local.yaml` 追加。
- **打分与闸门**：来源权威度、热度（点赞、评论、星数、引用 + 快照增速）、时效、新颖度、
  个人偏好五项加权；GitHub 仓库要求多次观测下的星数增速达标，避免「老仓库重新冒头」。
- **主线聚类**：不是按篇推送，而是先聚成「今天大家在讨论的几件事」，跨来源或成员足够多才算强主线。
- **回声判定**：记录每个话题的事件时间（不是材料发布时间），三天以内的跟进报道降级成快讯，
  真正的新版本/新功能才允许再次开主线。
- **周报 + 日报**：周一一早发周报（论文 + 仓库，按发酵后的热度排序），工作日每天一份日报；
  周末不打扰。
- **交互闭环**（飞书）：详情、收藏、不感兴趣、精读、相关推荐、全库检索；反馈会回流成偏好权重。
- **本地优先**：SQLite 单文件，无遥测，数据不出本机。

## 推送渠道

| 渠道 | 命令 | 能力 |
|---|---|---|
| 飞书 | `serve --channel feishu` | 交互卡片、按钮回调、自然语言指令（一等公民） |
| Webhook | `serve --channel webhook` | 单向推送；支持 `generic / slack / discord / dingtalk / feishu` |
| 本地文件 | `serve --channel fake` | 写到 `data/outbox/*.md`，适合无凭据试跑与调试 |

交互式卡片目前只有飞书；Webhook 与本地渠道是单向推送（精读结果会降级成文本发送）。
调度器、精读 worker 与渠道解耦，三种渠道都能跑定时任务。

## 快速开始

```bash
git clone https://github.com/<you>/kestrel-radar.git
cd kestrel-radar
uv sync
cp .env.example .env        # 填 DEEPSEEK_API_KEY；飞书/ Webhook 二选一
uv run ai-radar init
uv run ai-radar collect
uv run ai-radar digest --channel fake        # 先看本地生成的 Markdown
```

常驻运行：

```bash
uv run ai-radar serve --channel fake         # 无凭据：定时采集 + 生成 Markdown
uv run ai-radar serve --channel webhook      # 配置 AI_RADAR_WEBHOOK_URL 后使用
uv run ai-radar serve --channel feishu       # 飞书长连接 + 交互卡片
```

飞书需要自建应用（长连接模式），在 `.env` 填入 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`，
再给机器人发一条消息，它会把自己的 `open_id` 回给你，填进 `FEISHU_USER_OPEN_ID` 后重启即可。
机器人只响应这一个账号。

## 配置

| 文件 | 作用 |
|---|---|
| `config/sources.yaml` | 信源、代理、单源上限、关键词过滤 |
| `config/sources.local.yaml` | 可选，本地私有信源（个人订阅的公众号等），不入库，加载时与上者合并 |
| `config/weights.yaml` | 打分权重、热度动量、成熟窗口、推送节奏 |
| `config/models.yaml` | 模型名、价格、每日预算 |
| `.env` | 凭据与运行期覆盖（代理、Webhook） |

代理默认留空（直连），需要时用 `AI_RADAR_PROXY` 覆盖，避免把个人网络配置写进仓库。

## 架构

```
sources ──► normalize/dedupe ──► signals + rank ──► topics ──► composer ──► digest
  │              │                    │               │            │           │
  │         标题相似度+实体互斥    快照动量       主线聚类      周报/日报    飞书卡片
  │                                                                        Webhook
  └── SQLite：documents / signals / digests / topics / interactions        本地 Markdown
```

- `jobs/`：调度、采集、精读 worker、飞书路由
- `pipeline/`：归一化、打分、话题、编排、事件时间、回声判定
- `adapters/`：信源、通知渠道、检索
- `store/`：SQLite schema 与 repository

## 已知边界

- 单人使用：机器人只服务一个 `open_id`，没有多用户权限体系。
- Reddit 官方 API 需要 OAuth，这里用 Lobsters 作社区信号补充（量很小）。
- 事件时间靠「材料正文 → 原文页面结构化日期 → 新闻检索」三档推断，可能差几天。
- 摘要与社区观点由模型生成，可能出错；卡片上的结论不是事实来源。
- 不做跨设备同步，数据库就是唯一事实。

## 开发

```bash
uv run pytest -q
```

测试覆盖采集解析、归一化与去重、打分与聚类、回声判定、卡片渲染、渠道重试、预算降级等。
GitHub Actions 会在 push / PR 时跑全量测试。

## License

MIT。安全问题见 [SECURITY.md](SECURITY.md)。
