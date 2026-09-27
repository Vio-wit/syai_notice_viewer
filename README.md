# 学院通知查看器

一个给个人使用的轻量版学院通知查看器：

- Python + SQLite + 本地网页；不依赖 Spring、MySQL、MinIO 或 RagFlow。
- 复用项目里已经验证过的学院通知登录、列表接口和附件下载思路。
- 支持手动更新、Agent 命令行触发、HTTP API 触发、增量去重和未读提醒。
- 默认只监听 `127.0.0.1`，不会对局域网开放。

## 当前账号来源

`config.local.json` 默认只引用原项目中的个人配置文件，不复制密码：

```text
K:\syai_main\plan_b_爬虫兜底\config.json
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

生成原文快照和当前规则版纪要：

```powershell
.\.venv\Scripts\python.exe notice_viewer.py export
```

提取已下载附件的文本：

```powershell
.\.venv\Scripts\python.exe notice_viewer.py extract-attachments
```

提取结果保存在 `data/extracted/`，原文快照保存在 `data/originals/`，纪要保存在 `data/summaries/`。支持 TXT、Markdown、CSV、JSON、DOCX、XLSX、PPTX；PDF 需要 `pypdf`，扫描 PDF 和图片需要 OCR，旧版 DOC/XLS/PPT 需要转换器。失败或不支持的格式会记录状态，不会伪造文本。

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
tests/                 不登录、不联网的单元测试
```

通知更新默认只处理新增或内容变化的条目。附件默认开启，但受单文件大小上限限制。站点登录、页面结构或接口发生变化时，程序会保留已有数据库，并在命令行/API 中报告失败原因。
