# 学院通知查看器项目报告

## 一、项目概述

### 1.1 项目名称

学院通知查看器（`syai_notice_viewer`）

### 1.2 项目定位

本项目是一个面向个人使用的校园通知采集、检索、分类、附件管理和 Agent 协作工具。它将学校通知来源采集到本地 SQLite 数据库，通过本地网页看板进行筛选和阅读，并为其他 Agent 提供统一的原文、附件文本和纪要写回接口。

项目当前采用轻量化架构，不依赖 MySQL、Spring、MinIO 或 RagFlow，适合单机运行和持续迭代。

### 1.3 报告范围

本报告基于截至 2026-10-07 的代码、数据库、附件目录和本地运行结果编写。报告中的数据是当前工作目录的实际快照，不等同于学校网站的全部历史数据保证。

## 二、建设目标

项目主要解决以下问题：

1. 学校通知分散在不同来源类型中，难以统一查看。
2. 重复运行爬取容易产生重复记录，需要稳定去重。
3. 重要通知混在大量普通通知中，需要按日期、关键词和类别筛选。
4. 附件格式复杂，单纯保存下载文件不利于 Agent 快速理解。
5. AI 生成的结论需要能够追溯到原文和附件证据，不能用摘要替代原文。
6. 未来可能更换 Codex、OpenCode、本地模型或其他 Agent，因此分析结果不能绑定单一模型。

## 三、当前完成情况

| 模块 | 当前状态 | 说明 |
| --- | --- | --- |
| 登录采集 | 已实现 | 支持学校统一身份认证登录；账号密码从配置文件或环境变量读取 |
| 通知采集 | 已实现 | 支持 `notice`、`fw`、`xngs` 三类来源 |
| 日期采集 | 已实现 | 支持 `--since`、`--until`，并按列表页翻页 |
| 增量去重 | 已实现 | 通知按来源类型和稳定 ID 去重，附件按通知和 URL 去重 |
| 附件下载 | 已实现 | 下载原始附件并记录状态、哈希和本地路径 |
| 网页看板 | 已实现 | 支持关键词、日期、分类和显示条数筛选 |
| 详情阅读 | 已实现 | 详情以弹出卡片显示，减少对总体列表浏览的干扰 |
| 本地分类 | 已实现 | 支持比赛、专利、就业、奖助学金等分类 |
| 规则建议 | 已实现 | 无 API Key 时也能提供基础行动建议 |
| 原文快照 | 已实现 | 每条通知保存一份 Markdown 原文快照 |
| 附件文本提取 | 已实现 | 支持常见文本、DOCX、XLSX、PPTX 和可提取文字的 PDF |
| Agent 纪要 | 已实现并完成批量写回 | Agent 输出 JSON 后通过 API 写回，自动生成 Markdown 纪要；当前 172 条通知均已写入 Agent 版纪要，采集后新增的通知会再补写 |
| OCR | 尚未接入 | 扫描 PDF 和图片会标记为 `NEEDS_OCR`，不会猜测内容 |

## 四、系统架构

```text
学校通知网站 / SSO
        │
        ▼
NoticeSpider
        │  登录、列表、详情、附件下载
        ▼
NoticeStore（SQLite）
        │
        ├── 通知正文、日期、部门、来源、未读状态
        ├── 附件元数据、下载状态、文件哈希
        ├── 本地规则分类和行动建议
        ├── Agent 分析结果
        └── 附件提取状态
        │
        ├── 本地 HTTP API
        ├── 网页看板
        ├── data/originals/ 原文快照
        ├── data/extracted/ 附件文本
        └── data/summaries/ 纪要
```

### 4.1 核心代码模块

| 文件 | 职责 |
| --- | --- |
| `notice_viewer.py` | 采集、登录、附件下载、SQLite、HTTP API、本地网页看板 |
| `notice_analysis.py` | 规则分类、行动建议、Agent 分析和纪要 JSON 规范化 |
| `document_pipeline.py` | 附件文本提取、原文快照生成、纪要 Markdown 生成 |
| `tests/test_notice_viewer.py` | 不登录、不联网的单元测试 |
| `NOTICE_AGENT_PROTOCOL.md` | 其他 Agent 的采集、分析、写回和安全规范 |
| `agent_specs/` | 附件解读流程和 JSON Schema |

### 4.2 数据表

当前数据库主要包含以下数据：

- `notices`：通知正文、标题、来源、日期、部门、未读状态等。
- `attachments`：附件名称、URL、本地路径、下载状态和内容哈希。
- `runs`：每次采集任务的范围、数量、成功状态和错误信息。
- `notice_analysis`：规则分类、关键词、建议和 Agent 分析结果。
- `notice_briefs`：Agent 生成的一句话总结、重点、时间节点、材料和证据。
- `attachment_extractions`：附件文本提取状态、提取器、文本路径和错误信息。

## 五、原文、附件和纪要设计

项目采用“双层内容 + 中间提取层”设计：

```text
原始附件 binary ───────► attachments/
                            │
                            ▼
                     data/extracted/*.txt
                            │
通知正文快照 ────────► data/originals/*.md
                            │
                            ▼
                     Agent 生成纪要 JSON
                            │
                            ▼
                     data/summaries/*.md
```

### 5.1 原文层

`data/originals/<notice_key>.md` 保存：

- 通知标题、类型、发布时间和部门。
- 来源 URL。
- 网页解析后的正文内容。
- 附件名称、下载状态、文本提取状态和附件 URL。

需要说明的是，当前原文快照是网页解析后的正文快照，不是完整 HTML 存档；附件原始二进制文件仍保存在 `attachments/` 中。

### 5.2 提取层

`data/extracted/<attachment_id>.txt` 是给 Agent 使用的统一文本入口。系统不会执行附件中的程序，也不会把附件中的文字当成系统指令。

### 5.3 纪要层

`data/summaries/<notice_key>.md` 是人和其他 Agent 方便阅读的副本；数据库中的 `notice_briefs` JSON 是机器检索和 API 查询的依据。

当前 172 份纪要均为 Agent 版：`notice_briefs.source` 为 `agent:syai-notice-brief-v1`，`model` 为 `opencode/mimo-v2.6-flash-free`，`data/summaries/` 下的 Markdown 已同步为同一份内容。规则版分析仍保留在 `notice_analysis` 表中（`source` 为 `rules-v1`），作为看板上的“智能建议”，不与纪要混用。原文和附件不会被纪要替代。

采集本身不会生成或覆盖纪要：`update` 只新增或更新通知、附件和规则分类。新增和内容变化的通知需要 Agent 重新写回 `/brief`，否则详情页显示“尚未生成 Agent 纪要”，或仍显示基于旧正文的纪要。

## 六、当前数据快照

### 6.1 数量统计

截至本报告生成时：

| 项目 | 数量 |
| --- | ---: |
| 数据库通知 | 172 |
| 已下载附件 | 165 |
| 原文快照 | 172 |
| 纪要文件 | 172 |
| Agent 纪要 `notice_briefs` | 172 |
| 其中 `status=complete` | 135 |
| 其中 `status=partial`（正文截断或附件不可读） | 32 |
| 其中 `status=needs_ocr`（关键内容只在扫描件/图片里） | 5 |
| 含原文可核对时间节点的纪要 | 146 |
| 成功生成的附件文本 | 121 |
| 需要 OCR 的附件 | 8 |
| 旧版 Office 暂不支持 | 33 |
| 其他暂不支持格式 | 3 |

### 6.2 附件格式

当前已下载附件格式分布如下：

| 格式 | 数量 | 当前处理方式 |
| --- | ---: | --- |
| PDF | 48 | 使用 `pypdf` 提取文字；扫描件转为 OCR 待处理 |
| DOCX | 42 | 直接读取 Office Open XML 文本 |
| XLSX | 32 | 读取工作表和共享字符串 |
| XLS | 17 | 标记为旧版二进制，等待转换器 |
| DOC | 16 | 标记为旧版二进制，等待转换器 |
| PPTX | 3 | 读取幻灯片文本 |
| RAR | 3 | 暂不解压，标记为不支持 |
| JPG/PNG | 4 | 标记为 OCR 待处理 |

### 6.3 最近一次采集记录

`data/last_update.json` 记录的最近一次增量任务为：

- 状态：`SUCCESS`
- 运行编号：7
- 采集范围：`--since 2026-09-27`
- 列表返回（过滤前）：45 条；落在日期范围内的：3 条
- 新增：0 条（同日更早的运行编号 6 已新增 3 条）
- 内容变化：0 条
- 失败：0 条
- 完成时间：2026-10-07 09:23:11

本次新增的 3 条通知为 `notice:10106`（商学院考研扶持计划，2026-09-29）、`notice:10107`（十一期间网络安全保障值班，2026-09-30）、`notice:10108`（学生考勤通报批评第1-3周，2026-10-02），已在采集后补写 Agent 纪要。

数据库中的历史完整性仍应以每次采集命令输出的日期范围和分页结果为准，不能仅凭当前记录数量声称已经覆盖网站全部历史。

## 七、主要使用方式

### 7.1 启动看板

```powershell
Set-Location -LiteralPath 'K:\syai_notice_viewer'
.\.venv\Scripts\python.exe notice_viewer.py serve
```

浏览器打开：

```text
http://127.0.0.1:8765/
```

### 7.2 增量采集

```powershell
.\.venv\Scripts\python.exe notice_viewer.py update --json --notify
```

指定时间范围：

```powershell
.\.venv\Scripts\python.exe notice_viewer.py update `
  --since 2026-08-31 `
  --until 2026-09-27 `
  --json --notify
```

限制本次每种来源最多处理数量：

```powershell
.\.venv\Scripts\python.exe notice_viewer.py update --max-items 50 --json
```

### 7.3 生成原文、纪要和附件文本

```powershell
.\.venv\Scripts\python.exe notice_viewer.py export
.\.venv\Scripts\python.exe notice_viewer.py extract-attachments
```

### 7.4 看板筛选

```text
http://127.0.0.1:8765/?q=比赛&since=2026-08-31&until=2026-09-27&category=competition&limit=100
```

其中：

- `q`：标题、正文、部门和附件名称的模糊搜索。
- `since`、`until`：日期范围。
- `category`：分类筛选。
- `limit`：只限制页面显示数量，不删除数据库记录。

## 八、Agent 协作方案

其他 Agent 不需要重新理解数据库结构，可以按照以下流程工作：

1. 读取 `NOTICE_AGENT_PROTOCOL.md`。
2. 通过 `/api/notices` 获取目标通知列表。
3. 通过 `/api/notices/{notice_key}` 获取正文、附件和已有分析。
4. 读取 `data/originals/` 和对应的 `data/extracted/` 文件。
5. 按 `agent_specs/attachment_analysis.schema.json` 生成 JSON。
6. POST 到 `/api/notices/{notice_key}/brief`。
7. 系统自动保存结构化纪要并生成 Markdown 副本。
8. 每次 `update` 之后，对结果中的 `new_notice_keys` 和 `changed` 重复第 3-7 步；采集本身不会生成或覆盖纪要。

建议采用分级模型策略：

- 清晰文本、简单通知：使用本地或低成本模型。
- 表格、多截止时间、资格条件复杂的通知：交给更强模型复核。
- 图片、扫描 PDF、提取失败的附件：先 OCR，再做纪要。
- 所有重要结论必须带原文证据；无法确认时明确写“需查看原文/附件确认”。

## 九、安全和可靠性设计

### 9.1 凭据安全

- 优先使用环境变量 `TEC_USERNAME` 和 `TEC_PASSWORD`。
- 本地配置文件不纳入版本控制。
- 报告、规范和 Agent 输出中不保存账号密码。
- 服务默认只监听 `127.0.0.1`，不直接暴露到局域网。

### 9.2 采集安全

- 检测到登录页、验证码、403 或页面结构异常时应停止扩展采集。
- 默认保持请求间隔，避免并发轰击源站。
- 增量更新不会删除已有通知和附件。
- 单个坏附件不会中断整批附件处理。

### 9.3 内容可信度

- 原文和原始附件不会被纪要替代。
- 规则版建议的 `source` 固定为 `rules-v1`；没有 Agent 纪要时 Markdown 纪要状态显示 `RULE_BASELINE`，不会冒充模型结论。
- Agent 纪要记录 `source`、`model`、`status` 和 `confidence`。
- 不从文件名或不完整 OCR 结果猜测日期、资格和地点。

## 十、已知限制

1. 学校 SSO、验证码和源站页面结构变化可能导致采集失败。
2. 旧版 `.doc/.xls/.ppt` 尚未接入 LibreOffice 或专用转换器。
3. 图片和扫描 PDF 尚未接入 OCR 引擎。
4. RAR 等压缩附件目前不自动解压，避免无边界处理未知压缩内容。
5. 172 条纪要已批量写回，但 32 条因正文截断或旧版 Office/RAR 附件不可读标为 `partial`，5 条因关键内容只在扫描件或图片中标为 `needs_ocr`，需接入 OCR 和转换器后重跑；采集产生的新增和内容变化通知也需要按第八章流程补写纪要。
6. 当前原文快照保存的是解析后的正文，不是网页完整 HTML、CSS 和脚本快照。
7. 项目目录已初始化为 Git 仓库（`main` 分支），但提交仍以整批为主，尚未形成小步提交、分支评审和回滚流程。

## 十一、后续开发建议

### 优先级一：提高附件可读性

- 接入 OCR，处理扫描 PDF、JPG 和 PNG。
- 增加旧版 Office 转换流程，并记录转换器版本。
- 增加表格结构保留，避免 XLSX 提取后只剩纯文本。

### 优先级二：批量生成高质量纪要

- 172 条已写回，后续改为增量：新增或内容变化的通知按日期、分类触发重跑。
- 把 `partial`、`needs_ocr` 和 `confidence < 0.7` 的纪要放入复核队列，OCR 和旧版转换器接入后重跑。
- 低置信度或证据不足的结果交给更强模型复核，并记录复核前后的版本变化。

### 优先级三：增强看板

- 增加“待 OCR”“待 Agent 解析”“已复核”等状态筛选。
- 增加附件类型和提取状态筛选。
- 增加纪要导出和收藏功能。
- 增加通知变更记录，区分首次采集和后续内容变化。

### 优先级四：工程化

- 建立小步提交、分支评审和回滚规范（Git 仓库和 `main` 分支已初始化）。
- 增加 SQLite 备份和恢复命令。
- 增加定时任务，但保留人工确认和失败告警。
- 增加 API 认证后再考虑局域网访问。

## 十二、验证结果

本报告生成前完成了以下验证：

- `py_compile`：通过。
- 单元测试：13/13 通过（`python -m unittest discover -s tests`）。
- 原文和纪要导出：172/172 成功。
- Agent 纪要写回：172/172 通过 `POST /api/notices/{notice_key}/brief` 写入并回读校验（`complete` 135、`partial` 32、`needs_ocr` 5）。
- 纪要 JSON 本地校验：字段、长度、条数、`status` 枚举和 `source` 前缀全部合规，0 错误。
- 附件文本提取：165 个附件均有明确处理状态。
- 本地 HTTP API：可返回通知正文、附件提取状态、规则分析和 Agent 纪要字段。
- 本地看板：运行于 `http://127.0.0.1:8765/`。

## 十三、项目交付文件

- [README.md](README.md)：安装、运行和日常使用说明。
- [NOTICE_AGENT_PROTOCOL.md](NOTICE_AGENT_PROTOCOL.md)：跨 Agent 采集和分析规范。
- [agent_specs/ATTACHMENT_REVIEW.md](agent_specs/ATTACHMENT_REVIEW.md)：附件解读工作流。
- [agent_specs/attachment_analysis.schema.json](agent_specs/attachment_analysis.schema.json)：纪要 JSON Schema。
- [document_pipeline.py](document_pipeline.py)：附件提取和原文/纪要生成。
- [notice_analysis.py](notice_analysis.py)：分类、建议和 Agent 结果规范化。
- [notice_viewer.py](notice_viewer.py)：采集器、数据库、看板和 API。

## 十四、结论

项目已经从“单纯爬取通知”发展为一个具备采集、去重、筛选、分类、原文保留、附件提取和 Agent 协作能力的本地通知知识入口。

当前最重要的基础已经建立：原文不会被摘要覆盖，附件不会因为格式复杂而被静默忽略，172 条通知已经有一份带原文证据、可核对的 Agent 纪要。下一阶段的核心价值不是继续堆叠页面功能，而是完成 OCR 和旧版 Office 转换，让 32 条 `partial`、5 条 `needs_ocr` 的纪要升级为完整版本，并在每次采集后对新增和内容变化的通知做增量纪要复核。
