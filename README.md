# 学院通知查看器

一个给个人使用的轻量版学院通知查看器：

- Python + SQLite + 本地网页；不依赖 Spring、MySQL、MinIO 或 RagFlow。
- 复用项目里已经验证过的学院通知登录、列表接口和附件下载思路。
- 支持手动更新、Agent 命令行触发、HTTP API 触发、增量去重和未读提醒。
- 可选配置 OpenAI 兼容 API，对新增或内容变化的通知自动生成分类建议和结构化纪要；没有 Key 时仍使用本地规则。
- 默认只监听 `127.0.0.1`，不会对局域网开放。

## 当前账号来源

`config.local.json` 默认只引用项目外部的个人配置文件，不复制密码。例如：

```text
C:\Users\你的用户名\.notice-viewer\credentials.json
```

也可以改成环境变量：

```powershell
$env:TEC_USERNAME = '你的账号'
$env:TEC_PASSWORD = '你的密码'
```

环境变量优先级高于本地配置文件。请注意，原项目配置曾保存过明文凭据，建议尽快修改密码，然后更新本项目的凭据来源。

### 给其他人使用时的账号配置

推荐让每个人各自运行一个实例，并使用自己的学校账号，不要多人共用一个账号或把密码写进项目文件。

方案 A：使用当前 PowerShell 会话中的环境变量：

```powershell
$env:TEC_USERNAME = '本人的学号'
$env:TEC_PASSWORD = '本人的密码'
.\.venv\Scripts\python.exe notice_viewer.py check-config
.\.venv\Scripts\python.exe notice_viewer.py update --json
```

方案 B：使用项目外部的凭据 JSON 文件。文件内容格式为：

```json
{
  "username": "本人的学号",
  "password": "本人的密码"
}
```

然后在每个人自己的 `config.local.json` 中设置：

```json
{
  "credentials_file": "C:\\Users\\你的用户名\\.notice-viewer\\credentials.json"
}
```

也可以用环境变量选择另一份配置文件，而不修改项目文件：

```powershell
$env:NOTICE_VIEWER_CONFIG = 'C:\\Users\\你的用户名\\.notice-viewer\\config.json'
```

运行 `check-config` 只会显示脱敏账号、配置来源和是否已配置密码，不会执行登录，也不会打印密码。它只能检查配置完整性，不能证明学校账号当前有效。

当前项目仍是单机个人看板：不同使用者应各自运行自己的实例。若要让多人共用一个服务器，需要另行增加应用登录、用户隔离、加密凭据存储和权限控制，不能直接把 `bind_host` 改成局域网地址。

## 可选 AI 解读

AI 功能使用 OpenAI 兼容的 `POST /chat/completions` 接口。项目不会内置 API Key；每个人都应使用自己的 Key、模型和服务地址。没有配置或没有启用时，不会发起外部模型请求。

推荐用环境变量配置，不会写入项目文件：

```powershell
$env:NOTICE_AI_API_KEY = '本人的 API Key'
$env:NOTICE_AI_BASE_URL = 'https://api.openai.com/v1'
$env:NOTICE_AI_MODEL = 'gpt-4o-mini'
$env:NOTICE_AI_ENABLED = '1'
$env:NOTICE_AI_AUTO_BRIEF = '1'
```

也可以写入个人的 `config.local.json`（该文件已被 `.gitignore` 忽略）：

```json
{
  "ai": {
    "enabled": true,
    "auto_brief": true,
    "base_url": "https://api.openai.com/v1",
    "model": "gpt-4o-mini",
    "api_key": "本人的 API Key"
  }
}
```

启动后可以用本机 API 配置，API Key 只会写入被忽略的 `data/ui_ai_config.json`，状态接口只返回是否已配置，不返回 Key：

```powershell
.\.venv\Scripts\python.exe notice_viewer.py ai-status
Invoke-RestMethod http://127.0.0.1:8765/api/config/ai/status
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8765/api/config/ai `
  -ContentType 'application/json' `
  -Body '{"enabled":true,"auto_brief":true,"base_url":"https://api.openai.com/v1","model":"gpt-4o-mini","api_key":"本人的 API Key","remember":true}'
```

开启 `auto_brief` 后，`update` 只会对新增或正文变化的通知调用一次 AI，并写回 `notice_analysis`、`notice_briefs` 和 `data/summaries/`。模型只接收通知正文以及已经提取出的附件文本；无法确认的内容必须标记为 `partial` 或 `needs_ocr`。也可以只解读一条通知：

```powershell
.\.venv\Scripts\python.exe notice_viewer.py ai-brief notice:10108
Invoke-RestMethod -Method Post http://127.0.0.1:8765/api/notices/notice%3A10108/ai-brief
```

AI 配置和单条解读 API 仅允许本机访问。其他 Agent 仍可以按原协议直接 POST `/api/notices/{notice_key}/analysis` 和 `/api/notices/{notice_key}/brief`，不依赖外部 AI。

如果要给已有页面批量补齐 AI 解读，可以使用页面管理页或批量接口。默认跳过已有纪要；加 `force` 才会全部重算：

```powershell
.\.venv\Scripts\python.exe notice_viewer.py ai-brief-all --limit 100
.\.venv\Scripts\python.exe notice_viewer.py ai-brief-all --force --type professional
Invoke-RestMethod -Method Post `
  -Uri 'http://127.0.0.1:8765/api/notices/ai-brief-batch' `
  -ContentType 'application/json' -Body '{"type":"professional","limit":100,"force":false}'
```

## 专业模式：通用网页采集与分析

专业模式是从原项目的复杂爬虫逻辑中抽出的轻量实现，用于采集其他学院、实验室或公开信息页面。它保留了原项目中比较有价值的边界：同源/域名白名单、robots.txt、重定向检查、私网与回环地址拦截、登录会话、Bearer/自定义请求头、HTML/JSON/TEXT 解析、CSS 选择器、分页、深度/页数/响应体/总流量上限、附件引用和增量去重；不引入 Spring、MySQL、MinIO 或 RagFlow。

每个人只需要在自己的 `config.local.json` 或被忽略的 `data/professional_config.json` 中配置目标网站。示例模板见 `config.example.json`，密码、Token 和自定义请求头不要提交到 Git：

```powershell
Copy-Item config.example.json config.local.json
# 在 config.local.json 的 professional 中填写 site_url、entry_url 和 parser
.\.venv\Scripts\python.exe notice_viewer.py professional-status
.\.venv\Scripts\python.exe notice_viewer.py professional-login-test
.\.venv\Scripts\python.exe notice_viewer.py professional-update --json --notify
```

也可以让本地 Agent 通过 API 操作。配置、登录测试和更新触发接口只接受本机请求；配置状态只返回脱敏结果：

```powershell
Invoke-RestMethod 'http://127.0.0.1:8765/api/professional/config'
Invoke-RestMethod -Method Post `
  -Uri 'http://127.0.0.1:8765/api/professional/config' `
  -ContentType 'application/json' `
  -Body (Get-Content .\professional-profile.json -Raw)
Invoke-RestMethod -Method Post `
  -Uri 'http://127.0.0.1:8765/api/professional/update' `
  -ContentType 'application/json' -Body '{"notify":true}'
Invoke-RestMethod 'http://127.0.0.1:8765/api/professional/status'
```

专业模式采集出的记录类型为 `professional`，会进入同一个 SQLite、附件、规则分析和可选 AI 纪要流程；因此可直接用 `/api/notices` 查询，也可用 `ai.auto_brief` 自动解读新增或发生变化的页面。没有提供真实目标配置时，专业模式保持禁用，不会主动联网。

## 安装与运行

在 PowerShell 中执行：

```powershell
Set-Location -LiteralPath 'K:\syai_notice_viewer'
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

首次更新：

```powershell
.\.venv\Scripts\python.exe notice_viewer.py update --json --notify
```

启动本地查看页面：

```powershell
.\.venv\Scripts\python.exe notice_viewer.py serve
```

然后打开 <http://127.0.0.1:8765>。

看板顶部的“页面管理 / AI / 导出”可以按来源显示通知（包括专业模式页面）、触发新增页面 AI 解析或全量重解析，并查看专业模式状态。

### 在网页中配置账号

打开看板后点击顶部的“账号配置”。账号和密码只提交到本机服务：

- 不勾选“保存到本机”时，只保存在当前运行实例，服务重启后需要重新配置。
- 勾选后保存到 `data/ui_credentials.json`，该文件已被 `.gitignore` 忽略。
- 页面和 API 都不会回显密码；“清除本机保存”只删除本项目通过网页保存的凭据文件。
- 该面板仅适用于绑定 `127.0.0.1` 的个人实例，不是多人共享账号系统。

## Agent / 手动触发方式

命令行方式适合 Agent：

```powershell
.\.venv\Scripts\python.exe notice_viewer.py update --json --notify
```

本地服务运行后，也可以调用：

```powershell
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8765/api/update' -ContentType 'application/json' -Body '{"notify":true}'
```

查看状态和未读通知：

```powershell
Invoke-RestMethod 'http://127.0.0.1:8765/api/status'
Invoke-RestMethod 'http://127.0.0.1:8765/api/notices?unread=1'
```

看板支持通过地址栏筛选：

```text
http://127.0.0.1:8765/?q=比赛&since=2026-08-31&until=2026-09-27&category=competition&limit=100
```

`q` 会模糊匹配标题、正文、部门和附件名称；`limit` 只限制本次看板显示条数，不会删除数据库记录。

看板默认每页显示 20 条，底部提供上一页/下一页；翻页会保留关键词、日期和分类条件。通知详情中会同时显示规则/智能分析、分类、关键词、置信度、来源和最近更新时间；如果 Agent 已写回纪要，还会显示纪要重点、时间节点、材料、证据、模型和 Agent 最近解析时间。

按时间范围采集并生成本地分类建议：

```powershell
.\.venv\Scripts\python.exe notice_viewer.py update --since 2026-08-31 --until 2026-09-27 --json --notify
.\.venv\Scripts\python.exe notice_viewer.py analyze
```

限制每个来源类型最多处理多少条：

```powershell
.\.venv\Scripts\python.exe notice_viewer.py update --max-items 50 --json
```

重复采集时，通知按“来源类型 + 源站 ID”去重；没有源站 ID 的发文按规范化 URL 生成稳定键。附件按“通知 + 附件 URL”去重。重复运行不会新增重复记录，结果中的 `created`、`changed`、`unchanged` 分别表示新增、内容变化和已存在且未变化的数量。

默认情况下，采集不会自动生成或覆盖 Agent 纪要：`update` 只处理通知、附件和规则分类（`notice_analysis`，`source` 为 `rules-v1`），已写回的 `notice_briefs` 和 `data/summaries/` 保持原样。开启 `ai.auto_brief` 且配置有效 Key 后，`update` 会自动处理新增的 `created` 和内容变化的 `changed` 通知；AI 失败时不会让采集失败，并会在结果中的 `ai.errors` 报告。没有 AI 配置时，仍可由其他 Agent 通过 `/api/notices/{notice_key}/analysis` 与 `/api/notices/{notice_key}/brief` 写回。

生成原文快照和纪要（已有 Agent 纪要时生成 Agent 版，否则生成规则版）：

```powershell
.\.venv\Scripts\python.exe notice_viewer.py export
```

提取已下载附件的文本：

```powershell
.\.venv\Scripts\python.exe notice_viewer.py extract-attachments
```

提取结果保存在 `data/extracted/`，原文快照保存在 `data/originals/`，纪要保存在 `data/summaries/`。支持 TXT、Markdown、CSV、JSON、DOCX、XLSX、PPTX；PDF 需要 `pypdf`，扫描 PDF 和图片需要 OCR，旧版 DOC/XLS/PPT 需要转换器。失败或不支持的格式会记录状态，不会伪造文本。

导出通知数据（不包含密码、Token 或本地附件路径）：

```powershell
.\.venv\Scripts\python.exe notice_viewer.py export-data --format zip
.\.venv\Scripts\python.exe notice_viewer.py export-data --format csv --type professional --output .\professional-notices.csv
```

本地服务也提供 `GET /api/export?format=json|csv|markdown|zip`，可附加 `type`、`category`、`since`、`until`、`q` 和 `limit` 筛选。网页管理页提供 JSON、CSV、Markdown 和 ZIP 下载按钮。

检查账号和配置：

```powershell
.\.venv\Scripts\python.exe notice_viewer.py check-config
```

看板会按比赛竞赛、专利知识产权、就业招聘、奖助学金、考试课程、校园活动等分类显示，并给出可执行的本地规则建议。没有 API Key 时也能运行；其他 Agent 可以通过 `/api/notices/{notice_key}/analysis` 写入更高质量的分析。

跨 Agent 的采集、回填、分类、结果回报和安全边界见 [NOTICE_AGENT_PROTOCOL.md](NOTICE_AGENT_PROTOCOL.md)。

## 目录说明

```text
notice_viewer.py       抓取、SQLite、HTTP API 和本地页面
notice_analysis.py     本地规则分类、截止时间提示和行动建议
notice_ai.py           可选 OpenAI 兼容 API、AI 分析和结构化纪要
document_pipeline.py   原文快照、纪要文件和多格式附件文本提取
NOTICE_AGENT_PROTOCOL.md 其他 Agent 使用的采集与分析规范
agent_specs/           附件解读 Agent 规范和 JSON Schema
config.example.json    脱敏配置模板
config.local.json      本机配置，不提交
data/notices.sqlite3   通知和未读状态
attachments/           登录态下载的附件
data/originals/        通知原文快照
data/summaries/        规则版或 Agent 版纪要
data/extracted/        附件提取文本
data/exports/          CLI 导出文件（忽略，不提交）
tests/                 不登录、不联网的单元测试
```

通知更新默认只处理新增或内容变化的条目。附件默认开启，但受单文件大小上限限制。站点登录、页面结构或接口发生变化时，程序会保留已有数据库，并在命令行/API 中报告失败原因。
