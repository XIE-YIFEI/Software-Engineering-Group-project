# SECS Vision Web 系统：架构与页面布局

[English version / 英文版](ARCHITECTURE_EN.md) · 系统文档目录：[索引](README.md)

> **状态：已实现（第 2 步）。** 下面的设计在动工前已获批准，现已全部实现。所有标为 **决定** 的地方都已确认；实现过程中
> 的改动列在第 13 节。第 8 节的线框图是批准时的设计：已实现页面的截图见 [DEMO_WALKTHROUGH_ZH.md](DEMO_WALKTHROUGH_ZH.md)，
> 页面上的数字与线框图不同之处以第 13 节为准。相关文档：[需求规格说明](REQUIREMENTS_ZH.md)、[数据库](DATABASE_ZH.md)、
> [API](API_ZH.md)、[测试计划与报告](TEST_PLAN_REPORT_ZH.md)。

## 1. 要做什么

项目现在已经能把摄像头画面变成结果文件：命令行工具（`run_alpr.py`、`run_image.py`、`python -m alpr.forecast*`）
输出 `*.plates.json`、`*.forecast.json`、每分钟一行的 CSV 序列和带标注的视频。这一步之前还没有 Web 系统、数据库和
CI，这一步补上了：

- **数据库**（SQLite），由 **导入器** 从已提交的结果文件填充，不需要重新跑任何识别，系统里就有真实数据；
- **服务层**：一组普通的、带类型注解和文档的 Python 函数（检索、告警、交通、预测、门岗……），隐私规则
  （角色、打码、审计）集中在这一层执行；
- 建在服务层之上的 **JSON API**（FastAPI，`/docs` 自动生成 OpenAPI 文档）和 **服务器端渲染页面**（Jinja2 + 少量 JavaScript）；
- **后台任务**：用现有流水线分析上传的图片和视频；
- **GitHub Actions CI**：运行 `alpr/` 的 86 个单元测试（原有 85 个 + 新增证据输出的 1 个）和 22 个 API 测试。

定位：*把摄像头画面变成可检索、会告警、能预测拥堵的交通数据*，服务马来西亚的三类用户：

| 用户 | 系统角色 | 得到什么 |
|---|---|---|
| 交通警察 / JPJ 执法人员 | `police` | 被标记车牌立即告警；检索某车牌在何时何地出现并附证据；记录车道违规；证据包 |
| 公寓 / 写字楼门岗保安 | `guard` | 门岗自动识别车牌；住户 / 访客 / 禁入车辆显示绿 / 黄 / 红；一键记录进出；访客通行证和超时滞留 |
| 交通管理部门（DBKL、CIQ 关口运营方、高速公路运营方） | `traffic` | 分车道流量、占有率和车速；最多约 30 分钟的拥堵预测；情景推演（what-if）；报表 |
| 系统管理员 | `admin` | 用户、数据导入、上传、数据保留期限、审计日志 |

本步不实现、但在设计上预留（第 11 节）：实时 RTSP 摄像头、在浏览器里画车道、不含车牌的公众拥堵展望页、
推送通知，以及生成式 / 智能体 AI 助手（下一个任务）。

## 2. 组件与数据流

```
  recorded demo footage, uploads                          committed results (no reprocessing needed)
            |                                             demo/*.plates.json, demo/*.mp4
            v                                             demo/jb_ciq_forecast_timelapse.forecast.json
  +---------------------+  results JSON, annotated mp4,   docs/data/causeway/*.csv, *.calibration.json
  | alpr/ (unchanged    |  opt-in crops + scene frames             |
  | recognition code)   |-------------------------+                |
  | run_alpr.py         |                         v                v
  | run_image.py        |               +--------------------------------+     +------------------------+
  +---------------------+               | webapp/importer.py             |---->| alpr/registry.py       |
            ^                           | (idempotent, records SHA-256   |     | SIMULATED registry     |
            | subprocess                |  of every source file)         |     | data/registry/*.db     |
  +---------------------+  results      +---------------+----------------+     +-----------+------------+
  | webapp/worker.py    |--------------------------------+                                 | RegistryProvider
  | 1 background thread |                                v                                 | (replaceable by an
  +---------------------+               +--------------------------------+                 |  authorised source)
            ^ jobs                      | SQLite  data/web/secs.db       |<----------------+
            |                           | (gitignored)                   |
            |                           +---------------+----------------+
            |                                           | data-access layer (webapp/db.py)
            |                           +---------------v----------------+
            |                           | service layer webapp/services/ |  roles, masking, audit,
            |                           | typed functions (= AI tools)   |  retention, "Simulated" labels
            |                           +-------+----------------+-------+
            |                                   |                |
            +------------------------  JSON API /api/v1     HTML pages (Jinja2)
                                                |                |
                                    future AI assistant     browser: vanilla JS, SVG charts, <video>
```

（图中：导入器读取已提交的结果文件并记录每个源文件的 SHA-256；车辆登记库仍是 `alpr/registry.py` 的
**模拟** 数据库，通过 `RegistryProvider` 接口访问，将来可换成经授权的数据源；服务层负责角色、打码、审计、
保留期限和“模拟”标签；API 和页面都只调用服务层。）

**运行方式。** 一个进程：`python -m webapp` 在 `127.0.0.1:8000` 启动 uvicorn + FastAPI，外加一个处理上传的后台
线程和一个每日运行的清理任务。耗时的识别在项目 venv 的 **子进程** 里运行
（`.venv\Scripts\python run_alpr.py … --headless --backend openvino`），所以 Web 进程从不加载 torch，
识别崩溃也不会拖垮服务器。

**技术选型（决定）。**

| 部分 | 选择 | 理由 |
|---|---|---|
| Web 框架 | FastAPI + uvicorn | 请求 / 响应有类型模型，自动生成 OpenAPI 文档，测试可用 TestClient |
| 数据库 | SQLite，用标准库 `sqlite3` 加一个小的数据访问模块 | 不用装数据库服务，一个文件，与 `alpr/registry.py` 做法一致；SQL 都在一个模块里，以后换 PostgreSQL 只需改这一个模块 |
| 页面 | Jinja2 模板 + 原生 JavaScript | 同学们不需要前端构建工具链也能维护 |
| 图表 | 自己写的小型 SVG 图表函数（`static/js/charts.js`） | 不依赖 CDN，答辩现场断网也能用 |
| PDF | `fpdf2`（纯 Python） | 生成证据包和报表，不需要系统库 |
| 密码 | 标准库 `hashlib.scrypt`，每个用户独立的盐 | 无额外依赖 |
| 会话 | HttpOnly、SameSite=Lax 的 Cookie 里放随机令牌，数据库只存它的 SHA-256；CSRF 令牌放在请求头 | 可随时吊销，Cookie 里没有机密 |
| 新增依赖 | fastapi、pydantic、uvicorn、jinja2、python-multipart、httpx（测试用）、fpdf2 | 加入 `requirements.txt`（保持纯 ASCII）；CI 在 `requirements-ci.txt` 里固定确切版本 |

**代码结构（决定：在 `alpr/` 旁边新建顶层包 `webapp/`）。**

```
webapp/
  __main__.py            python -m webapp  (uvicorn, 127.0.0.1:8000)
  app.py, deps.py        the FastAPI app, error handlers; request dependencies (session, CSRF)
  settings.py, clock.py  paths and defaults (retention days, overstay hours ...), env overrides; Malaysia time
  schema.sql, db.py      schema + connection + small helpers (the data-access layer)
  models.py              pydantic models of every response (also the future AI tool schemas)
  importer.py            python -m webapp.importer --demo [--demo-users]
  worker.py              background jobs: run_alpr.py / run_image.py in a subprocess, progress, import
  security.py            password hashing, sessions, CSRF, role checks
  privacy.py             plate masking, reveal, audit log (hash chain), retention purge
  alerting.py            create_alert: the one place where alerts are created (future push hook)
  registry_provider.py   RegistryProvider interface + the SIMULATED alpr.registry implementation
  legal.yaml             Road Transport Act 1987 sections verified for the evidence pack, with their sources
  services/              overview, plates, alerts, traffic, forecast, gate, lists, jobs, evidence, reports, admin
  analysis/              fuzzy.py (OCR-aware plate distance, vectorised), timeline.py, whatif.py
  api/                   FastAPI routers: thin wrappers (auth -> service -> JSON)
  web/pages.py           page routes;  templates/;  static/{css,js}
  seed/                  cameras.yaml, demo_lists.yaml, demo_users.yaml (test accounts, local demo only)
  tests/test_web.py      22 API tests (FastAPI TestClient), same runner style as alpr/tests/test_alpr.py
data/web/ (gitignored)   secs.db, media/ (crops, blurred crops, scene frames), uploads/, jobs/, exports/, corrections/
```

**主要流程。**

1. *导入*：`python -m webapp.importer --demo` 读取已提交的文件，建立摄像头、分析批次（run）、车牌记录、告警、
   车道统计、拥堵序列和预测。重复运行时按自然键（批次代码 + 起始帧 + 车牌）更新，已处理的告警状态不会丢。
2. *上传与分析*：浏览器上传文件 -> 生成一条 `jobs` 记录 -> 后台线程在子进程里运行流水线，读取它的进度输出
   （`[612/900] ...`）-> 成功后用同一套导入代码把结果 JSON 作为新批次入库。
3. *检索 / 查看 / 显示完整车牌*：每个请求都是 API -> 服务层；服务层先检查角色、打码、写审计日志
   （谁、何时、做了什么、为什么），然后才返回数据。
4. *告警处理*：新建 -> 已确认 -> 已解决 / 误报，每一步都记录处理人、时间和备注。
5. *门岗决定*：车牌卡片 -> 放行 / 拦停并备注 -> 进出记录。

## 3. 数据库结构

所有时间均为马来西亚时间（MYT，UTC+8），以 ISO 8601 文本存储。`compact` 是规范车牌（`VJF1234`），
`plate_text` 是显示形式（`VJF 1234`）。

**数据来源类别（provenance）。** 开发计划要求把真实观测、经数字修改的车牌和合成轨迹区分开，所以每条观测类记录都有
`provenance` 字段：

| 取值 | 含义 | 演示数据中的例子 | 界面显示 |
|---|---|---|---|
| `camera` | 真实车辆、真实车牌，从画面中识别 | 路边、门岗、车道摄像头中未被修改的所有车牌 | 无标签 |
| `inserted_plate` | 真实车辆，但车牌被数字替换为虚构车牌 | `malaysia_road_violations` 中的 7 辆车（X 系列和 `WXY 12345`） | 紫色标签“Simulated · fictitious plate inserted” |
| `synthetic` | 画面中根本没有这辆车 | 路边演示的 `simulated_events`、车道画面中的紫色车辆、新山（Johor Bahru）的那次目击、门岗的模拟到达 | 紫色标签“Simulated · synthetic” |

**数据表。** 共 30 张表，分七组；每一列、自然键、触发器和保留规则都在 [DATABASE_ZH.md](DATABASE_ZH.md) 中说明：

| 分组 | 表 |
|---|---|
| 系统与用户 | `meta`、`settings`、`sites`、`users`、`sessions` |
| 摄像头与批次 | `cameras`、`runs` |
| 车牌与告警 | `plates`、`sightings`、`alerts`、`alert_actions` |
| 车道与流量 | `lane_events`、`lanes`、`lane_bins`、`crossings`、`flow_bins`、`withheld` |
| 拥堵与预测 | `series_days`、`zone_minutes`、`calibrations`、`forecast_runs`、`forecast_points` |
| 名单与门岗 | `list_entries`、`visitor_passes`、`access_events` |
| 隐私、任务队列、导出 | `reveals`、`audit_log`（只能追加，哈希成链）、`jobs`、`corrections`、`evidence_exports` |

车辆登记库本身 **不** 复制进这个数据库：它仍在 `data/registry/demo_registry.db`（**模拟** 数据，由
`alpr/registry.py` 生成），通过 `RegistryProvider` 接口（`lookup`、`near`、`sightings`、`is_simulated`）访问。
经授权的真实数据源只需再实现一次这个接口，并在 `settings.py` 里切换，其他代码都不用改。

## 4. API

JSON 接口都在 `/api/v1` 下，OpenAPI 文档在 `/docs`。用会话 Cookie 认证；所有 POST / PUT / PATCH / DELETE
请求都要带 `X-CSRF-Token` 请求头。列表接口分页（`page`、`page_size` -> `{items, total, page, page_size}`）。
打码和审计在服务层完成，API 无法绕过。

共 59 个接口（54 个路径），分 12 组（认证、总览、车牌、告警、交通、预测、门岗、名单、任务、报表、管理、媒体）；
每个接口及允许调用的角色列在 [API_ZH.md](API_ZH.md)，可交互的参考文档在 `/docs` 自动生成。

**服务函数**（API 包装的就是它们，将来 AI 助手也调用它们），例如
`plates.search(conn, user, SightingQuery) -> Page[SightingOut]`、`plates.timeline_of(conn, user, plate) -> Timeline`、
`alerts.transition(conn, user, alert_id, to, note, corrected_plate) -> AlertDetail`、
`forecast.state(conn, user, day, at) -> ForecastState`、
`forecast.whatif(conn, user, day, section, capacity_pct) -> WhatIfOut`、`gate.identify(conn, user, plate) -> Identity`。
每个函数都有类型注解、docstring 和 pydantic 输出模型，可以直接生成 LLM 工具的 schema；由于参数里带着当前用户，AI 助手
自动继承同样的权限、打码和审计（审计中记为 `via: assistant`）。完整列表见 [API_ZH.md](API_ZH.md) 第 4 节。

## 5. 角色、隐私与审计

**谁能看到什么（决定）。**

| 页面 / 操作 | police | guard | traffic | admin |
|---|---|---|---|---|
| 登录后首页 | 总览 | 门岗 | 总览 | 总览 |
| 车牌检索 | 完整车牌，道路和车道摄像头 | 仅本站点门岗（在进出记录页） | 打码；*按车牌号* 检索视为“显示完整车牌”，需填写理由 | 打码 |
| 显示完整车牌 | — | — | 必须填理由，写审计 | 必须填理由，写审计 |
| 车牌摄像头的视频、场景帧 | 可以 | 本门岗 | 不可以（视频里车牌是完整的；场景帧只在显示完整车牌后可看） | 不可以（同左） |
| 告警中心 | 全部道路 / 车道告警，可处理 | 本门岗的告警，可处理 | —（车道事件在“交通分析”页显示） | 只读，打码 |
| 跨摄像头时间线、证据包 | 可以 | — | — | — |
| 门岗、进出记录、住户与访客、本站点禁入名单 | — | 可以 | — | — |
| 关注名单和黑名单 | 可以 | — | — | — |
| 交通分析、拥堵预测 | 只读 | — | 可以 | 只读 |
| 上传与分析 | 可以 | — | 可以 | 可以 |
| 报表 | 告警、交通 | 门岗 | 交通 | 交通、告警 |
| 用户、数据导入、设置、保留期限、审计日志 | — | — | — | 可以 |

**打码（新增功能 1）。** 警察和保安看到完整车牌和截图。交通管理人员和管理员看到 `VJF ****`（保留字母前缀，
其余一律显示四个星号，不泄露位数）以及 **在服务器端模糊处理** 的截图（只在 CSS 里模糊的话，清晰图仍会发到浏览器）。
“显示完整车牌”会弹出对话框，必须选择理由（*事故调查*、*事件复核*、*数据质量检查*、*审计*、*其他* + 备注）；
这次查看写入 `reveals` 表和审计日志，对该条记录有效 15 分钟。需打码的角色看不到车牌摄像头的视频（带标注的视频里每个
车牌都是完整的），在显示完整车牌之前也看不到场景帧；车道和拥堵摄像头的视频照常可看。

**审计（谁、何时、做了什么、为什么）。** 记录：登录和退出（包括失败的登录）、每一次检索（筛选条件和检索表单上
选择的 *用途*）、打开一条记录、查看完整截图或场景帧、每一次显示完整车牌、每一次 CSV / PDF 导出和证据包导出、
告警状态变化、名单和通行证的修改、门岗决定、上传、设置修改、清理操作和用户管理。只有管理员能查看审计日志，
并可在管理页面校验哈希链。

**保留期限（决定：以下为默认值，管理员可修改）。** 普通车牌记录（无告警、未命中任何名单）连同截图在入库
**30 天** 后删除；与告警相关的记录保留到告警关闭，之后再保留 **180 天**；进出记录 **90 天**；纠错样本池 **30 天**；
审计日志 **365 天**。由每日任务和管理员页面的“预览 / 立即清理”按钮执行，清理本身也写入审计。（演示数据从导入时间起算。）

**页面和文档中的措辞：** *“隐私控制遵循马来西亚《2010 年个人数据保护法》（PDPA 2010）的数据最小化原则：
每个角色只看到其工作所需的信息，普通车牌记录只保留一定期限，每一次访问都有记录。”* PDPA 约束的是私营机构，
例如管理门岗的物业公司；它不适用于联邦政府和州政府，因此系统 **不** 声称警察或 JPJ 的使用受其约束——对这些角色
采用同样的控制只是良好做法。已在官方文本（Laws of Malaysia, Act 709，截至 2016 年 6 月 15 日的在线更新文本）中核对：
第 3(1) 条（不适用于联邦政府和州政府）、第 6(3)(c) 条（充分但不过量）、第 10(1) 条（保存时间不超过所需）；2024 年
修订案未针对这些条文重新核对。

**处处标明“模拟”。** `provenance` 不是 `camera` 的每一条记录，在所有页面、CSV 导出（`provenance` 列）、PDF
和证据包里都带紫色标签。`UNMATCHED` 告警总是附带说明：*“Not in the SIMULATED registry. This does not mean a real
vehicle is unregistered.”*（不在模拟登记库中，并不表示真实车辆未登记。）

## 6. 演示数据：哪些是真实的，哪些是模拟的

| 摄像头（seed） | 批次（文件） | 使用的时钟 | 提供的数据 | 真实 / 模拟 |
|---|---|---|---|---|
| KL roadside ANPR (demo)，3.1390, 101.6869（虚拟位置） | `malaysia_road_anpr_demo_30s`（窗口 100–130 秒） | 虚拟时钟 2026-10-14 08:00:00 + 秒数（来自 `video_start_time`） | 8 个车牌；4 个模拟事件共 5 条登记库告警 + 1 条关注名单命中（`XSE 5005`） | 车牌真实；事件为合成 |
| 同一摄像头 | `malaysia_road_violations_demo`（0–155 秒） | 同一虚拟时钟 | 19 个车牌（18 个确认、1 个识别中 `VCV 1555`）、8 条登记库告警 + 1 条关注名单命中、流量 19 | 7 个插入的虚构车牌，12 个真实 |
| Guardhouse gate（演示公寓） | `malaysia_guardhouse_demo_30s`（66–96 秒） | 虚拟时钟 2026-10-14 07:30:00（seed） | `JPP 3473`、`JMT 8577`；流量 1 | 真实；门岗演示另加模拟到达（7.3 节） |
| Taman Sri Selayang 车道摄像头 | `malaysia_selayang_lanes_demo`（0–120 秒） | 虚拟时钟 2026-10-14 08:30:00（seed） | 355 次过线、8 条车道、12 个事件（5 个模拟违规 / 危险、7 个真实变道）、10 个隐去 | 流量真实；违规为合成 |
| 新山 CIQ 引道，Camera 02（A） | `jb_ciq_cam02_2023-03-22.csv` + 标定 + `jb_ciq_forecast_timelapse.forecast.json` | 真实屏幕时钟，2023 年 3 月 22 日（周三）00:24–09:54 | 每分钟分区占满率；LightGBM +5 / +15 / +30（用 C 训练） | 真实测量 |
| 新山 CIQ 引道，IPC 摄像头（C） | `jb_ciq_ipc_2023-03-21.csv` + 标定 | 真实，2023 年 3 月 21 日（周二）02:52–10:06 | 分区占满率；预测在导入时计算（用 A 训练） | 真实 |
| 长堤引道（B） | `jb_causeway_cam1_2024-01-12.csv` | 真实，2024 年 1 月 12 日（周五）00:25–11:12 | 只有占有率（未标定，不用于预测） | 真实 |
| Johor Bahru (SIM) | `alpr/registry.py` 中的 `DEMO_SIGHTINGS` | 2026-10-14 07:45 | `XSD 4004` 在约 293 公里外被看到 | 合成 |

**数据缺口（在页面上明确说明）：** 车辆登记库是 **模拟** 的（无法访问 JPJ）；没有任何真实车辆在我们的两个摄像头下
都出现过（地点和日期都不同），所以跨摄像头时间线用模拟目击来演示；CIQ 摄像头测的是道路被占满的程度，不是每分钟
车辆数或车速；门岗摄像头只能读入口车道的车牌；真实车辆的车道违规被隐去（只计数，不显示）；预测只在 2 天数据上
验证过；纯 CPU 分析约 10 FPS。另外：两个路边批次来自同一段画面、同一虚拟时间（第二个替换了部分车牌），所以同一个
真实车牌可能在两个批次里各出现一次；每条结果都会注明所属批次。

## 7. 已商定的六项新增功能（在基础部分之后按此顺序实现）

### 7.1 按角色打码车牌
见第 5 节。只在 `privacy.py` 实现一次，由服务层应用到每个响应、CSV、PDF 和图片上。

### 7.2 点击记录跳转到视频对应时刻
在车牌检索或告警中心点击任意记录，播放器跳到 `first_sec − 1` 秒并开始播放。演示视频是带标注的仪表盘视频，
从该批次的 `window_sec[0]` 开始，所以实际跳到 `first_sec − window_sec[0] − 1`（存为 `runs.video_offset_sec`）。
上传的视频直接在原视频的 `first_sec − 1` 处播放（若要求渲染了标注视频，则播放标注视频）。媒体接口支持 HTTP Range
请求，浏览器才能拖动定位。车道事件用 `time_sec` 以同样方式跳转。

### 7.3 门岗模式（保安的首页）
左边是视频；右边是一张大卡片，显示最新车牌及其身份颜色，下面两个大按钮 **Allow (log entry)**（放行并记录）和
**Stop & note**（拦停并备注），都会写入进出记录。身份判断顺序：红 = 在本站点禁入名单上；绿 = 住户；青色（蓝绿）=
持有当前有效通行证的访客；黄 = 未登记访客（或通行证不在有效期内）。演示：页面回放
`demo/malaysia_guardhouse_demo_30s.mp4`，视频播到某车牌的 `first_sec`（减去 66 秒偏移）时显示它的卡片：
`JPP 3473` 在 0:00（模拟演示名单中的住户，绿色），`JMT 8577` 在 0:09（不在任何名单上，黄色：保安可当场签发通行证）。
另有两次使用虚构车牌的模拟到达（紫色说明“no vehicle in the video”）来展示其余颜色：0:16 预先登记的访客 `XVP 3003`
（青色）和 0:24 禁入车辆 `XBN 4321`（红色）。回放模式下，视频播到某车牌即视为该车此刻（真实时钟）到达，所以通行证和超时都按真实时间计算。

### 7.4 有时限的访客通行证和超时名单
通行证包括车牌、单位号、可选标签（如 *送货*；不记录姓名或身份证号）和有效期（默认现在 -> 现在 + 4 小时）。
在场超过 **N 小时**（设置项，默认 4）或通行证已过期仍未离开的访客，置顶显示在“当前在场”列表。**局限：**
门岗摄像头只能读入口车道的车牌（出口车道的车牌太小、太斜），所以离开记录来自保安点击 **Exited**（已离开）按钮，
演示数据中则来自预置的模拟离开记录。

### 7.5 跨摄像头时间线
对在多个摄像头出现过的车牌：按时间排列成链，显示相邻两次目击之间的距离、时间间隔和推算车速，用
`alpr.registry.check_travel` 标出不可能的行程（超过 250 km/h；2 公里以内的忽略）。输入：`sightings` 表、摄像头的
`lat/lon`、登记库中的外部目击记录。演示（全部标为模拟）：`XSD 4004` 在 Johor Bahru (SIM) 07:45:00 ->
KL 路边 08:00:23（插入的车牌）：“293 km in 15 min 24 s: 1141 km/h needed - impossible (more than 250 km/h): cloned
plate?”（15 分 24 秒 293 公里，需 1141 km/h，不可能）-> KL 路边 08:02:04（合成事件），“0 km (same site)”。

### 7.6 告警的证据包（HTML 和 PDF）
内容：案件摘要（告警、车牌、时间、摄像头、批次、数据来源类别）；场景帧；最佳车牌截图；登记库比对（**模拟** 记录
与摄像头所见：登记车型与检测车型、路税到期日与观测日期、状态；克隆车牌则列出相互矛盾的目击）；有车速时给出车速 ±
误差（`value`、`error_1sigma`）；法律依据；处理信息（git 提交号、配置档、结果 JSON 的 SHA-256）；每张嵌入图片的
SHA-256，并注明 *“These hashes only prove that the image files were not changed after this pack was exported.”*
（这些哈希只能证明图片在证据包导出后未被修改）；导出人 / 导出时间 / 审计编号。
规则：每一页都有醒目的 **“Course project demo — not an official document”**（课程项目演示——非官方文件）标记；
不使用 PDRM / JPJ 标志，不模仿任何官方表格；《1987 年道路交通法》（Road Transport Act 1987）的条款只有在对照
官方文本（总检察署 Attorney General's Chambers 的 Laws of Malaysia）核实后才引用，并注明来源；无法核实的条款不写。
**流水线改动（可选开启）：** 现在的结果 JSON 里没有截图和场景帧。新增选项 `--evidence DIR`（配置项
`evidence_dir`，默认关闭），为每条车牌记录保存最佳车牌截图和它所在的整帧画面，并在每行车牌记录里加上
`evidence: {crop, scene, frame, time_sec, vehicle_box, plate_box}`。默认关闭，所以平常的运行和 JSON 完全不变。已完成：
四个演示配置开 / 关该选项各跑一遍并逐字段比较（原有字段完全相同，只新增了 `evidence` 和 `processing`），原有 85 个测试
加新增的 1 个全部通过（86/86）。
图片保存在 `outputs/evidence/`（不进 git）；缺少图片时（例如新克隆的仓库）证据包改用已提交的标注视频中的一帧，
并注明这一点。
用违规演示中的登记库告警（虚构车牌）来演示：车道摄像头读不到车牌，真实车辆的违规又被隐去，所以没有带车牌的
真实超速案例。

### 7.7 情景推演：“多开车道”（最后实现）
在预测页面：滑块选择从预警时刻起增加 **+10 / +20 / +30 %** 的放行能力，在实测排队旁画出预计排队。模型（确定性的
点排队模型）：`Q'(t) = max(0, Q'(t−1) + ΔQ(t) − p·μ)`，其中 `ΔQ` 是实测的排队变化（各分区占满率 × 堵塞容量，
即画面内的车辆数），`μ` 是放行能力。CIQ 摄像头只测道路被占满的程度，测不到每分钟车辆数（TEAM_GUIDE 第 10.6 节），
所以 `μ` 要么作为假设手动输入，要么根据排队最快的持续消散速度粗略估计（A 日为每分钟 0.83 辆（画面内），取截至 07:33
的 10 分钟；这是下限，因为消散时仍有车辆到达）。A 日从首次预警（06:56）起增加 20 % 的结果：拥堵分钟数 146 -> 7，
峰值 38 -> 28 辆（画面内）；收益这么大，是因为看不到画面之外的排队（见下面的假设）。页面标注 **“Estimate under stated assumptions”**（基于明示假设的估计），
并列出全部假设：排队集中在路段头部（点排队）；措施不改变到达量；增加的能力从预警时刻起全部可用；`μ` 如页面所示为
估计或假设值；只考虑画面内的那段排队（排队超出画面时实测占满率封顶，推演会高估收益）；单帧测量误差 22–33 %；
只在 2 天数据上验证过。

## 8. 各角色的页面

下面的线框图是批准时的设计（其中数字来自草稿）。已实现的页面与之一致，截图见 [DEMO_WALKTHROUGH_ZH.md](DEMO_WALKTHROUGH_ZH.md)。

所有页面使用同一外框：左侧导航（只列出该角色的页面）、顶栏（用户和角色标签）、数据说明标签（显示演示批次时为
“Demo data · notional clock”）以及数据来源类别图例。风格沿用 `alpr/hud.py` 的仪表盘：深色背景 `#0c0c0e`、
磨砂卡片、白 / 灰文字，颜色只用来表示状态（绿 `#30d158` 有效、黄 `#ffd60a` 识别中 / 未登记、橙 `#ff9f0a` 中等 /
危险、红 `#ff453a` 高 / 违规、紫 `#bf5af2` 模拟、蓝 `#0a84ff` 信息、浅蓝 `#64d2ff` 预测），字体 Segoe UI Variable，
车牌画成车牌样式（黑底浅色字）。界面文字用英文，与现有仪表盘一致。

### 8.1 登录
用户名、密码、登录。提示：“Course project demo. Test accounts: `webapp/seed/demo_users.yaml` (local demo only).”
数据：`users`、`sessions`。

### 8.2 总览（police、traffic、admin；保安登录后直接进入门岗页）

```
+- SECS Vision - Overview ------------------------- Demo data - notional clock -- A. Rahman (Police) [Sign out] +
| Overview      | Cameras                                                                                      |
| Plate search  | +-KL roadside ANPR-----+ +-Guardhouse gate----+ +-Selayang lanes-----+ +-JB CIQ cam 02-----+ |
| Alert centre  | | o Replay  2 runs     | | o Replay  1 run    | | o Replay  1 run    | | o Replay 22/3/23  | |
| Watch lists   | | 27 plates (+4 sim.)  | | 2 plates           | | 355 veh  8 lanes   | | fill 20 %         | |
| Upload        | | 13 alerts            | |                    | |                    | |                   | |
| Reports       | +----------------------+ +--------------------+ +--------------------+ +-------------------+ |
|               | Flow on Wed 14 Oct 2026 (demo day)        | Open alerts (18, all simulated)                 |
|               | 375 vehicles   ._.-'-._.-'  per 10 s       | o 10 high  o 1 hazard  o 7 medium               |
|               | Car 246  Motorcycle 111  Truck 17  Bus 1   | High  Tailgating  XTG 5006  Selayang 08:31:28    |
|               |                                            | High  Solid line  XSL 2002  Selayang 08:30:19    |
|               | Congestion - JB CIQ approach (replay of Wed 22 Mar 2023, 06:56)          Open forecast > |
|               | o Free flow now 20 %   +5 min 25 %   +15 min 53 %   +30 min 54 %   "Congestion in ~15 min" |
+---------------+--------------------------------------------------------------------------------------------+
```

| 页面元素 | 数据库 | 源文件与字段 |
|---|---|---|
| 摄像头卡片 | `cameras`、`runs`，`sightings` / `alerts` 计数 | `seed/cameras.yaml`；`*.plates.json` 的 `source`、`profile`、`window_sec`、`plates[]` |
| 当天流量 | `flow_bins`（车道摄像头另用 `lanes`） | 各批次的 `flow.total`、`flow.by_type`、`flow.per_10s[]`（路边摄像头取整段视频的批次） |
| 未处理告警 | 状态为 new / acknowledged 的 `alerts` | `plates[].alerts[]`、`simulated_events[].alerts[]`、`lanes.events[]`（violation、hazard） |
| 拥堵 | `forecast_points`、`calibrations` | `forecast.json` 的 `measured_fill[]`、`forecast_fill{}`；`congestion_fill` |

不同角色的差异：警察优先看到道路和车道摄像头及告警列表；交通管理人员优先看到流量和拥堵（告警只显示车道事件，
车牌打码）；管理员另有系统卡片（数据库大小、最近一次导入、排队中的任务、下次清理时间、今日审计条数）。

### 8.3 车牌检索（警察看完整车牌；交通管理人员和管理员打码）

```
+- Plate search -----------------------------------------------------------------------------------------+
| Plate [XSD 4OO4    ]  OCR tolerance ( )0 ( )1 (*)2   From [14 Oct 07:00]  To [14 Oct 09:00]             |
| Camera [All v]  Type [All v]  Status [Confirmed + provisional v]  Simulated [include v]                 |
| Purpose [Alert follow-up v]  Ref [        ]                                  [Search]  [Export CSV]     |
+------------------------------------------------------+-------------------------------------------------+
| [crop] XSD 4004  =  Car   KL roadside  08:00:23-37   | +-------------- video of the run -------------+ |
|        Cloned plate  Simulated: inserted     > 0:22  | |   seeks to first_sec - offset - 1 s, plays  | |
| [plate] XSD 4004 =  Car   KL roadside  08:02:04      | +---------------------------------------------+ |
|        Cloned plate  Simulated: synthetic    > 0:23  | XSD 4004  Car  confidence 74 %  40 readings      |
|                                                      | Registry (SIMULATED): Hatchback, white, Perodua  |
|                                                      | Myvi, road tax to 2027-01-31, status ok          |
| 2 results                                            | [Timeline across cameras >]                      |
+------------------------------------------------------+-------------------------------------------------+
```

模糊检索：在去重后的车牌上计算考虑 OCR 特点的编辑距离（O/0、I/1、B/8、S/5、Z/2、H/R、M/W 等易混字符代价更低），
容差 0–2 个字符，每条结果显示其距离。识别中（`TENTATIVE`）的读数用黄色显示为“Reading”，永远不触发告警；
`valid_format: false` 显示“Non-standard format”。对需打码的角色，车牌列显示 `XSD ****`，截图模糊，详情栏有
“Show full plate”按钮。

| 页面元素 | 数据库 | 源文件与字段 |
|---|---|---|
| 结果行 | `sightings` | `plates[]`：plate、compact、confidence、votes、status、valid_format、vtype、region、track_ids、first/last_frame、first/last_sec；`simulated_events[]`：plate、compact、vtype、time_sec |
| 时间 | `sightings.seen_at` | `clock_origin`（seed / `video_start_time`）+ `first_sec` |
| 截图 | `sightings.crop_path` | 新增的可选字段 `evidence.crop`；合成记录用渲染出的车牌图（`alpr.hud.plate_image`） |
| 告警 | `alerts` | `plates[].alerts[]` {code, severity, message} |
| 登记库 | `RegistryProvider.lookup` | `data/registry/demo_registry.db`（模拟） |
| 视频 | `runs.video_path`、`video_offset_sec` | `demo/<run>.mp4`、`window_sec[0]` |

### 8.4 告警中心（警察；保安看本门岗的告警；管理员只读）

```
+- Alert centre ----------------------------- [New 18] [Acknowledged 0] [Resolved 0] [False alarm 0] ----+
| Severity [All v]  Code [All v]  Camera [All v]  [x] include simulated                                    |
+--------------------------------------------------+------------------------------------------------------+
| o High   Tailgating     XTG 5006  Selayang In1 08:31:28 | Blacklisted - "SIMULATED: reported stolen"       |
| o High   Solid line     XSL 2002  Selayang In4 08:30:19 | +-video----------------+ [crop] [scene frame]    |
| ...                                                     | +----------------------+                         |
| o High   Blacklisted    XSB 2002  KL rdside 08:01:46    | Registry (SIMULATED)   vs   observed            |
|          Simulated: inserted plate          New  <-sel. |  Sedan, silver               Car, 08:01:46       |
| o High   Cloned plate   XSD 4004  KL rdside 08:00:23    |  status blacklisted                              |
| o Hazard Stopped in lane XST 3003 Selayang In3 08:31:28 | [Acknowledge] [Resolve] [False alarm...]         |
| o Medium Road tax expired XSA 1001 KL rdside 08:00:00   | [Evidence pack: HTML | PDF]  [Timeline >]        |
| ...                                                     | History: created by import 23 Oct 10:02 ...       |
+---------------------------------------------------------+--------------------------------------------------+
```

排序（决定）：**High**（红：黑名单、克隆车牌、逆行、超速、压实线、跟车过近）-> **Hazard**（橙：在行车道停车）->
**Medium**（橙：路税过期、未登记、车型不符、格式非法、命中关注名单），同级按时间从新到旧。点“False alarm”（误报）时
要求填写备注，可选填正确车牌；该车牌和截图进入纠错样本池（同样受保留期限约束）。

| 页面元素 | 数据库 | 源文件与字段 |
|---|---|---|
| 登记库告警 | `alerts`（source = registry） | `plates[].alerts[]`、`simulated_events[].alerts[]` |
| 车道违规 / 危险 | `alerts`（source = lane）+ `lane_events` | `lanes.events[]`：code、severity、time_sec、lane、from_lane、message、vtype、track_id、plate、simulated、value、error_1sigma |
| 名单命中 | `alerts`（source = list） | 导入或上传时与警察的关注名单 / 黑名单比对 |
| 处理流程 | `alert_actions` | — |

### 8.5 跨摄像头时间线（警察）

```
+- Timeline - XSD 4004 ------------------------------------------------- Simulated --------------------+
| 07:45:00  Johor Bahru (SIM)        external sighting (simulated registry)                               |
|    |      293 km in 15 min 23 s  ->  1141 km/h   IMPOSSIBLE (> 250 km/h): cloned plate?                  |
| 08:00:23  KL roadside ANPR (demo)  run "Violation check" - fictitious plate inserted          [> play]  |
|    |      0 km in 1 min 40 s (same camera)                                                              |
| 08:02:04  KL roadside ANPR (demo)  run "Demo window" - synthetic event                        [> play]  |
| Rule: alpr.registry.check_travel (limit 250 km/h, legs under 2 km ignored)                              |
+---------------------------------------------------------------------------------------------------------+
```

数据：该车牌的 `sightings`、`cameras.lat/lon`、`RegistryProvider.sightings(compact)`。

### 8.6 门岗（保安首页）

```
+- Guardhouse - Demo condominium gate ------------------------------------------- 07:31 -- guard1 (Guard) +
| +----------------------------- replay video ---------------------+ +------------------------------------+ |
| |                                                                 | |      [  J M T   8 5 7 7  ]          | |
| |                                                                 | |  o UNREGISTERED VISITOR (yellow)    | |
| |                                                                 | |  Truck - 07:31:15 - entry lane      | |
| |                                                                 | |  not a resident, no valid pass      | |
| +-----------------------------------------------------------------+ |  +--------------+ +---------------+ | |
|  > || <<  0:09 / 0:30   replay of recorded footage                  |  |    ALLOW     | |  STOP & NOTE  | | |
|                                                                     |  | (log entry)  | |               | | |
| Inside now (visitors)                            overstays first    |  +--------------+ +---------------+ | |
| ! XVA 5120  unit B-07-3  in 6 h 12 min  pass ended 2 h ago [Exited] |  [+ Issue visitor pass]             | |
|   XVP 3003  unit A-12-1  in 0 h 03 min  pass to 18:00     [Exited]  +------------------------------------+ |
| Recent: 07:31 JPP 3473 allowed (resident) ...                                                            |
+----------------------------------------------------------------------------------------------------------+
```

| 页面元素 | 数据库 | 源文件与字段 |
|---|---|---|
| 回放卡片 | 门岗批次的 `sightings`（+ 模拟到达） | `malaysia_guardhouse_demo_30s.plates.json`：plate、vtype、first_sec；`window_sec[0]` = 66 |
| 身份颜色 | `list_entries`、`visitor_passes` | 门岗页面；演示条目在 `seed/demo_lists.yaml` |
| 按钮、当前在场、最近记录 | `access_events` | — |

### 8.7 进出记录（保安）
按车牌（模糊）、日期、决定、身份检索；列：时间、车牌、身份颜色、决定、方向、单位号 / 通行证、保安、备注、数据来源类别；
可导出 CSV。数据：`access_events`（及摄像头读数所在的 `sightings`）。

### 8.8 住户与访客（保安）
标签页：**Residents**（车牌、单位号、标签、是否有效；添加 / 停用）· **Visitor passes**（车牌、单位号、标签、有效期、
状态；签发 / 取消）· **Inside and overstays**（当前在场与超时）· **Banned at this site**（车牌、原因）。数据：
`list_entries`、`visitor_passes`、`access_events`。只保存单位号和车牌，不保存姓名或身份证号。

### 8.9 关注名单和黑名单（警察）
表格：车牌、名单、原因（必填）、添加人 / 时间、到期日、是否有效、过往目击次数（链接到检索）；旁边显示模拟登记库中的
状态。新导入和上传的数据会与有效名单比对，产生 `WATCHLIST`（中等）或 `BLACKLIST`（高，来源为名单）告警。数据：
`list_entries`、`RegistryProvider`。

### 8.10 交通分析（交通管理人员；警察和管理员只读）

```
+- Traffic analytics - Taman Sri Selayang lane camera - 120 s replay ---------------------- [Export CSV v] +
| [355 vehicles] [177.5 veh/min] [occupancy: inbound 15 % - outbound 72 %] [speed: In1 87 km/h ... ]      |
| Flow per 10 s by vehicle type (stacked)             | Per lane                                           |
| ||| ||| ||| ||| ||| ||| ||| ||| ||| ||| |||          | Lane  Dir  Count veh/min Occ   Density  Speed      |
|                                                     | In1   down   42   21.0   11 %   15 /km   87 +-6    |
| Lane metric [Occupancy v]  (one line per lane)      | ...                                                |
|  ~~~ In1  ~~~ In2  ~~~ Out1 ...                      | Out4  up     30   15.0   91 %  254 /km  8.5 +-0.6  |
+-----------------------------------------------------+----------------------------------------------------+
| Lane events:  o 08:30:08 Wrong way In1 XWW **** Simulated [>]   o 08:30:13 Lane change Out4->Out3 [>] ... |
| Withheld (real vehicles, public footage): speeding 7, tailgating 3 - counted, not shown                  |
+-----------------------------------------------------------------------------------------------------------+
```

| 页面元素 | 数据库 | 源文件与字段 |
|---|---|---|
| 指标卡、分车道表 | `lanes` | `lanes.per_lane{}`：count、by_type、veh_per_min、occupancy、density、density_unit、zone_length_m、mean_speed_kmh |
| 流量图 | `flow_bins`、`crossings` | `flow.per_10s[]`；`lanes.crossings[]`（time_sec、lane、vtype、direction） |
| 车道指标曲线 | `lane_bins` | `lanes.per_10s{lane}[]`：count、veh_per_min、occupancy、mean_vehicles、density_veh_km、mean_speed_kmh |
| 事件、隐去计数 | `lane_events`、`withheld` | `lanes.events[]`；`lanes.withheld_violations`、`withheld_note` |
| 车速误差 | 批次 | `lanes.calibration.note`（1σ 6.5 %，TEAM_GUIDE 5.3 节） |

同一页面也显示路边和门岗摄像头的计数线流量（`flow.*`）。

### 8.11 拥堵预测（交通管理人员；警察和管理员只读）

```
+- Congestion forecast - JB CIQ approach -------- Day [Wed 22 Mar 2023 - Camera 02 v] -- LightGBM, trained on C +
| Replay clock  [<] 06:56 [>]  ======*=========================  [> play]                                        |
| +- To CIQ ---------------------+ +- TC1 queue head --+ +- TC2 ------+ +- TC3 ------+ +- TC4 ------+            |
| | o Congestion in ~15 min      | | fill ...          | | ...        | | ...        | | ...        |            |
| | now 20 %   +5 min 25 %       | +-------------------+ +------------+ +------------+ +------------+            |
| | +15 min 53 %   +30 min 54 %  |  +- time-lapse video, synced to the clock (05:30-09:50) -+                    |
| | error band +-9 / 15 / 15 pts |  +--------------------------------------------------------+                    |
| +------------------------------+                                                                                 |
| Forecast vs actual   [+5] [+15] [*+30]    MAE this day 0.077 (expected band 0.150)                              |
|  1.0 |            measured ---   forecast made 30 min earlier - - -   congestion 0.5 .....                     |
|  0.5 |....................../~~~\_/~~~~~~~~~~~~~~                                                               |
|  0.0 +---------------------------------------------------- 00:30 ... 09:50 (onsets 07:13, 08:17)              |
| What-if: more capacity  [+0] [+10] [*+20] [+30] %  from 06:56 (first warning)   mu [0.8] veh/min (estimated)    |
|  queue (vehicles in view): measured --- projected ---     peak, congested minutes, time to clear (illustrative)  |
|  Estimate under stated assumptions:  - point queue ...  - arrivals unchanged ...  - only the part in view ...    |
+------------------------------------------------------------------------------------------------------------------+
```

A 日 06:56 模型第一次发出预警（排队在 17 分钟后的 07:13 开始），这就是交通管理人员的答辩演示场景。路段状态：
堵塞容量的 25 % 以下为畅通，25–50 % 为密集，50 % 及以上为排队（TEAM_GUIDE 10.2 节）。

| 页面元素 | 数据库 | 源文件与字段 |
|---|---|---|
| 路段卡片（当前） | `zone_minutes` | CSV 中各 `time` 的 `<entity>.fill`、`.occupancy`、`.queue` |
| +5 / +15 / +30、误差带 | `forecast_points`、`forecast_runs` | `forecast_fill{5,15,30}[]`、`error_band_mae{}`；各分区和 C 日的路段在导入时用 `alpr.forecast.LightGBMForecaster` 计算（与 `forecast_demo.py` 相同的做法） |
| 预测与实测对比 | `forecast_points` | `measured_fill[]` 对比 `forecast_fill{h}[i − h]`；拥堵开始时刻用 `alpr.forecast.onsets` 计算 |
| 车辆数、容量 | `calibrations` | `capacity{}`、`groups{}.loo_mae` |
| 延时视频 | 批次 | `demo/jb_ciq_forecast_timelapse.mp4`（05:30–09:50，每帧代表摄像头时间 10 秒） |
| 情景推演 | 计算得出 | 第 7.7 节 |

### 8.12 上传与分析（警察、交通管理人员、管理员）

```
+- Upload & analyse ----------------------------------------------------------------------------------+
| File [choose...]  Camera [KL roadside ANPR v | + new]  Scene [Roadside ANPR v]                        |
| Recorded at [2026-10-23 09:00]   [ ] also render the annotated video (about 3x slower)  [Start]       |
| Jobs                                                                                                 |
| #12 video  clip.mp4   ########....  68 %   612/900 frames - 10.4 FPS - about 0:28 left  [Cancel]     |
| #11 image  car.jpg    done - 1 plate                                                  [Open results] |
+------------------------------------------------------------------------------------------------------+
```

图片用 `run_image.py` 处理；视频用 `run_alpr.py --headless --backend openvino`，采用所选场景的配置，录制时间作为
`video_start_time`，并且 **不加入模拟事件**。上传数据的登记库检查只用 `ILLEGAL_FORMAT` 和 `DUPLICATE`：登记库是
模拟的，真实车牌“不在登记库中”并不能说明车辆有任何问题，所以关闭 `UNMATCHED`；关注名单 / 禁入名单照常比对。
回放：若渲染了标注视频则播放标注视频，否则播放原视频（必要时用自带的 ffmpeg 转出一份浏览器可播放的 H.264 副本）。
数据：`jobs`，之后与导入写入相同的各表。

### 8.13 报表
日报或周报；种类：交通（按摄像头 / 车型 / 车道的流量、拥堵时段）、告警（按代码、状态、处理用时）或门岗（进入、拦停、
超时）；CSV 或 PDF（PDF 带演示标记）。数据：以上各表的汇总。

### 8.14 管理
**Users**（添加、停用、重置密码、所属站点）· **Data**（摄像头、批次及其源文件和 SHA-256、重新导入、预测批次）·
**Jobs**（任务）· **Privacy and audit**（带筛选和哈希链校验的审计日志、保留期限设置、清理预览、纠错样本池大小）·
**Settings**（超时小时数、用途选项）。

## 9. CI

`.github/workflows/ci.yml`，每次 push 和 pull request 时运行：Ubuntu、Python 3.12、pip 缓存；先从 PyTorch 的 CPU
源安装 CPU 版 torch 2.3.1（不下载 CUDA），再装一个精简的 `requirements-ci.txt`（只含测试会导入的包：numpy < 2、
opencv、pyyaml、pillow、ultralytics、statsmodels、lightgbm 和 Web 相关的包）；运行 `python alpr/tests/test_alpr.py`
（86 个测试，不需要模型或 GPU）和 `python webapp/tests/test_web.py`（22 个用 FastAPI TestClient 的 API 测试，临时数据库
由已提交的演示 JSON 填充）。README 已有徽章。已在本机用同样方式新建的 venv 中预演：86/86、22/22（Windows；这台电脑没有
WSL / Docker，无法直接运行 Linux，所以 GitHub 上的第一次运行就是 Linux 上的检验）。

## 10. 测试与文档（已交付）

- API 测试：登录与锁定、角色范围、打码与显示完整车牌（写入审计行）、模糊检索、CSV 导出、告警流程与纠错样本池、
  门岗身份颜色与超时、不可能行程的时间线、证据包（标记和哈希都在）、情景推演（增加能力不会让排队变长）、导入器
  可重复运行且计数正确、CSRF、审计日志只能追加、保留期限清理。
- 中英文文档（`docs/system/`，[索引](README.md)）：需求规格说明、本架构文档、数据库结构、API 概览、测试计划与报告、
  含三个答辩场景截图的演示流程；`TEAM_GUIDE.md` / `TEAM_GUIDE_EN.md` 第 13 节；README 链接和 CI 徽章。

## 11. 为以后预留的设计

- **实时 RTSP 摄像头：** `cameras.stream_url` 和 `status`；实时接入任务会通过同一套导入函数写入记录，并用
  Server-Sent Events 推送到页面（门岗卡片本来就是消费一串车牌卡片）。
- **在浏览器里画车道：** 每个摄像头的几何配置（车道、计数线、标定点）存入数据库，并可导出为 `alpr/config.yaml`
  的配置档格式。
- **不含车牌的公众拥堵展望页：** 预测服务只返回路段级的汇总数据，公众页面不需要新的数据访问。
- **推送通知：** 所有告警都由同一个函数创建，那里就是唯一的挂接点。
- **AI 助手：** 服务函数即工具（第 4 节），以当前登录用户的身份执行。

## 12. 实现顺序

1. 基础部分：数据库结构、导入器、登录与角色、核心页面（总览、车牌检索、告警中心、交通分析、预测、上传、名单、
   隐私与审计、报表），点击跳转视频直接做进检索和告警页面。
2. 按商定顺序实现新增功能：打码 -> 点击跳转视频 -> 门岗模式 -> 访客通行证与超时 -> 跨摄像头时间线 -> 证据包
   （连同可选的流水线输出及其回归检查）-> 情景推演。
3. CI、文档、截图、供你审阅的总结。未经你批准，不会提交或推送任何内容。

三步都已完成。

## 13. 已确认的决定与实现中的改动

下面八项决定在动工前已确认。

1. 新建顶层包 `webapp/`（不放进 `alpr/`）。
2. 直接用 `sqlite3` 加一个数据访问模块，不用 ORM。
3. 第 5 节的角色范围：保安只能看本站点门岗；警察看道路和车道摄像头，不看公寓门岗；管理员看到打码车牌；交通管理
   人员按车牌号检索视为“显示完整车牌”。
4. 保留期限默认值：普通记录 30 天，告警关闭后 180 天，进出记录 90 天，纠错样本池 30 天，审计日志 365 天。
5. 门岗演示：真实门岗车牌 `JPP 3473` 在模拟演示名单中设为 *住户*（中性身份）；`JMT 8577` 不在名单上（黄色）；
   青色和红色两种情况用虚构 X 车牌的模拟到达来演示。
6. 上传数据的登记库规则只用 `ILLEGAL_FORMAT` 和 `DUPLICATE`（不用模拟登记库做 `UNMATCHED` 判断）。
7. 证据截图和场景帧不进 git（放在 `outputs/evidence/`）；新克隆的仓库改用已提交标注视频中的帧。
8. CI 安装 CPU 版 torch（每次约 200 MB，有缓存），以便 85 个测试（现为 86 个）都能在 GitHub 上运行。

实现中的改动：

1. **警察也能查看拥堵摄像头**（总览卡片和预测页面），与第 5 节角色表的设计一致；第一版实现漏掉了警察的拥堵摄像头范围。
2. **需打码的角色不能看车牌摄像头的视频。** 测试时发现：路边和门岗摄像头的标注视频里每个车牌都是完整的，所以交通管理
   人员和管理员看不到车牌摄像头的视频（API 返回 403），在显示完整车牌之前也看不到场景帧。车道和拥堵视频照常可看。
3. **演示数据库里有 21 条告警**，而不是线框图里的 18 条：警察关注名单上的 `XSE 5005` 在两个路边批次里各命中一次，门岗
   的禁入车辆模拟到达产生 `BANNED_AT_SITE`。警察看到 20 条未处理告警（10 高、1 隐患、9 中）；警察总览的车流为 374 辆
   （KL 路边 19 + Selayang 355；门岗不在警察范围内）。
4. **总览增加日期选择**（默认是最近一个有摄像头数据的日期），因为上传会带来新的日期。
5. **显示不在任何车道内的过线车辆**（Selayang 355 次过线中有 4 次在路边停靠带），这样分车道计数（351）与总数的关系
   一目了然。
6. **提速：** 车牌模糊检索用 numpy 向量化，并按数据库版本缓存（10 万个不同车牌：精确 34 ms，容错 1–2 为 130–140 ms；
   之前约 5 秒）；拥堵开始时刻和首次预警也同样缓存（总览从 300 ms 降到 34 ms）。
7. **CSRF 令牌只从 `X-CSRF-Token` 请求头读取**（页面的退出表单提交到它自己的页面路由）。
8. **SQL 的位置：** 查询是写在各服务模块里的普通 SQL，通过 `db.py` 的工具函数执行，而不是全部集中在一个模块；
   [DATABASE_ZH.md](DATABASE_ZH.md) 第 9 节列出了换 PostgreSQL 时要改的几处 SQLite 特有写法。
9. **证据包的法律依据** 放在 `webapp/legal.yaml`，每一条都在两份官方文本中核对过（交通部转载的总检察署文本，截至
   2013 年 2 月 1 日；JPJ 的文本，截至 2023 年 10 月 15 日）；不引用罚则。
10. **证据包的备用画面**（新克隆的仓库没有可选的截图时）：取已提交标注视频中、车辆轨迹 70 % 处的一帧，此时车辆完整
    在画面内。
