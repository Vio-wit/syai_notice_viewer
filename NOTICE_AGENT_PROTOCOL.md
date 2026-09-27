# 学院通知 Agent 采集与分析规范

本文件是给其他 Agent、定时任务和人工开发者使用的操作约定。目标是让不同 Agent 都能安全地采集、去重、分类和回报结果。

## 1. 项目边界

- 工作目录：`K:\syai_notice_viewer`
- 本地服务：`http://127.0.0.1:8765`
- 数据库：`data/notices.sqlite3`
- 附件目录：`attachments/`
- 原文快照：`data/originals/`
- 纪要：`data/summaries/`
- 附件提取文本：`data/extracted/`
- 当前来源类型：`notice`（通知公告）、`fw`（学校发文）、`xngs`（校内公示）
- 默认只绑定本机地址，不要改成 `0.0.0.0`，除非另行增加身份认证。

凭据优先使用当前会话的环境变量，不要把账号、密码、Token 写入本文件或提交到 Git：

```powershell
$env:TEC_USERNAME = '账号'
$env:TEC_PASSWORD = '密码'
```

给其他人交付时，推荐每个人使用自己的 `config.local.json` 和学校账号，或把 `credentials_file` 指向项目外部的个人凭据文件。可通过 `NOTICE_VIEWER_CONFIG` 指定另一份配置文件。运行下面的命令只能检查配置是否齐全，不会执行登录，也不会打印密码：

```powershell
.\.venv\Scripts\python.exe notice_viewer.py check-config
```

本项目当前是单机个人实例，不提供多用户应用登录。不要为了让别人访问而直接把 `bind_host` 改成 `0.0.0.0`；多人共用需要额外实现应用认证、用户隔离、加密凭据存储和权限控制。

本地看板也提供“账号配置”面板。默认只写入当前运行实例；勾选保存后写入 `data/ui_credentials.json`，该文件已被 `.gitignore` 忽略。网页配置接口只接受本机请求，密码不会通过状态接口返回。保存前仍应确认这是个人电脑，并在不需要时点击“清除本机保存”。

## 2. 标准采集命令

首次安装：

```powershell
Set-Location -LiteralPath 'K:\syai_notice_viewer'
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

按日期采集，日期范围两端都包含：

```powershell
.\.venv\Scripts\python.exe notice_viewer.py update `
  --since 2026-08-31 `
  --until 2026-09-27 `
  --json --notify
```

`update` 是增量操作：不会删除已有通知；已存在且标题、日期没有变化的通知不会重复抓详情。需要重新核对详情时才使用：

```powershell
.\.venv\Scripts\python.exe notice_viewer.py update --since 2026-08-31 --force --json
```

限制每个来源类型本次最多处理多少条（不会删除数据库中的其他记录）：

```powershell
.\.venv\Scripts\python.exe notice_viewer.py update --max-items 50 --json
```

指定 `--since` 时，采集器会使用来源列表页的 `current=1,2,...` 自动翻页，直到发现早于起始日期的页面或达到安全页数上限；不指定日期范围时只抓当前列表页，适合日常增量更新。

采集后重新生成本地分类和建议：

```powershell
.\.venv\Scripts\python.exe notice_viewer.py analyze
```

为现有数据生成原文快照和规则版纪要：

```powershell
.\.venv\Scripts\python.exe notice_viewer.py export
```

提取已下载附件：

```powershell
.\.venv\Scripts\python.exe notice_viewer.py extract-attachments
```

附件提取状态必须如实报告。文字格式和 DOCX/XLSX/PPTX 可直接处理；PDF 没有 `pypdf` 时记录 `NEEDS_PDF_DEPENDENCY`；扫描 PDF/图片记录 `NEEDS_OCR`；旧版 DOC/XLS/PPT 记录 `LEGACY_BINARY_UNSUPPORTED`。

## 3. 结果检查

Agent 必须保存并回报命令输出中的这些字段：

- `status`：`SUCCESS`、`PARTIAL` 或 `FAILED`
- `scope.since`、`scope.until`
- `scope.listed_before_filter`
- `listed`、`created`、`changed`、`unchanged`、`failed`
- `new_notice_keys`

去重规则：通知优先使用 `来源类型 + 源站 ID` 作为 `notice_key`；发文没有详情 ID 时使用规范化 URL 的稳定哈希。附件使用 `notice_key + url` 唯一约束。`created=0` 不代表没有读取到数据，通常表示本次条目已经存在；应结合 `unchanged` 和 `changed` 判断。

`listed_before_filter` 只是来源接口本次返回的条数，不等于网站全部历史数据。若来源接口只返回最近一页，必须报告“历史完整性无法确认”，不能声称已覆盖全部历史。

## 4. 分类约定

分类值使用稳定英文枚举，页面显示中文名称：

| 值 | 中文 |
| --- | --- |
| `competition` | 比赛竞赛 |
| `patent` | 专利知识产权 |
| `employment` | 就业招聘 |
| `scholarship` | 奖助学金 |
| `exam` | 考试课程 |
| `activity` | 校园活动 |
| `safety` | 安全与管理 |
| `personnel` | 人事任免 |
| `publicity` | 公示 |
| `other` | 其他 |

一条通知可以有多个分类，`primary_category` 表示最主要分类。分类必须基于标题、正文或附件名称中的证据；无法判断时使用 `other`，不要猜测。

## 5. Agent 分析写回接口

先获取通知：

```powershell
Invoke-RestMethod 'http://127.0.0.1:8765/api/notices?since=2026-08-31&until=2026-09-27&limit=500'
```

关键词和分类筛选：

```powershell
Invoke-RestMethod 'http://127.0.0.1:8765/api/notices?q=比赛&category=competition&since=2026-08-31&until=2026-09-27&limit=100'
```

API 支持 `page` 分页参数，例如 `...?q=比赛&limit=20&page=2`。返回值包含 `page`、`limit` 和 `has_next`。网页看板默认每页 20 条，翻页会保留当前筛选条件。

对某条通知写入更高质量的 Agent 分析：

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri 'http://127.0.0.1:8765/api/notices/notice%3A10103/analysis' `
  -ContentType 'application/json' `
  -Body '{"categories":["competition"],"primary_category":"competition","confidence":0.92,"keywords":["大赛","赛项"],"advice":"确认参赛对象、报名截止时间和作品提交格式。","source":"agent:your-agent-name"}'
```

附件/通知纪要写回接口：

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri 'http://127.0.0.1:8765/api/notices/notice%3A10103/brief' `
  -ContentType 'application/json' `
  -Body (Get-Content .\brief.json -Raw)
```

纪要字段规范见 `agent_specs/attachment_analysis.schema.json`，解读流程见 `agent_specs/ATTACHMENT_REVIEW.md`。结构化 JSON 是机器查询的依据，Markdown 是方便人和其他 Agent 快速阅读的副本。

Agent 纪要写回后，网页详情会显示 `updated_at` 对应的最近解析时间、模型、置信度、证据和待确认事项；没有 Agent 纪要时，网页会明确显示“尚未生成 Agent 纪要”，不会把规则建议冒充深度解析。

写回要求：

- `source` 使用 `agent:<名称>`，规则分析使用 `rules-v1`。
- 建议必须是可执行动作，不要只写“值得关注”。
- `confidence` 范围为 `0` 到 `1`。
- 不确定的信息写“需查看原文/附件确认”，不要编造截止时间、地点或资格。

## 6. 采集安全规则

- 不删除 `data/`、`attachments/` 或数据库记录。
- 不覆盖凭据文件。
- 遇到登录页、验证码、403、页面结构变化或连续失败时，停止扩展采集并报告原因。
- 尊重来源站点，保持请求间隔；不要并发轰炸接口。
- 不使用 `git add .`。如果以后建立 Git 仓库，只显式暂存源代码、测试、模板配置和规范文档。
- 每次任务结束报告：范围、来源类型、返回数量、入库数量、失败数量、附件状态、历史完整性和剩余问题。
