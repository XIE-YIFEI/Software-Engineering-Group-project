# SECS Vision 网页系统：API 概览

[English version / 英文版](API_EN.md) · 系统文档目录：[索引](README.md)

完整、始终与代码一致的接口参考由代码自动生成：启动服务器后打开 **`http://127.0.0.1:8000/docs`**（可交互的
OpenAPI / Swagger 页面）或 `/openapi.json`。本页说明约定，并列出每个接口和允许调用它的角色。

## 1. 约定

| 主题 | 规则 |
|---|---|
| 路径 | JSON 接口在 `/api/v1` 下；图片和视频在 `/media` 下 |
| 登录 | `POST /api/v1/auth/login` `{username, password}` 设置 HttpOnly 会话 Cookie `secs_session`，返回 `{user, csrf_token}`。密码连续错 5 次锁定 5 分钟。会话有效 8 小时（设置项 `session_hours`）。 |
| CSRF | 所有 `POST` / `PUT` / `PATCH` / `DELETE` 必须在 `X-CSRF-Token` 请求头里带上本会话的令牌（`GET /api/v1/auth/me` 可再次取得） |
| 错误 | JSON `{"detail": "..."}`：401 未登录 · 403 该角色无权操作（或 CSRF 令牌错误）· 404 不存在 · 422 输入无效（如缺少用途或理由） |
| 分页 | `page`、`page_size` -> `{items, total, page, page_size}` |
| 隐私 | 角色范围、车牌打码、查看授权和审计日志都在**服务层**执行，任何接口都绕不过去。需打码的角色（traffic、admin）拿到的是 `VJF ****` 和模糊截图。 |
| 用途 | `GET /sightings`（及其 CSV）必须带 `purpose`（Alert follow-up、Investigation (case reference)、Patrol check、Gate operations、Incident at site、Traffic study、Data-quality check、Audit），连同筛选条件写入审计日志 |
| 模拟数据 | 每条观测都带 `provenance`（`camera`、`inserted_plate`、`synthetic`）和可读的 `provenance_label`；CSV 有 provenance 列 |
| 时间 | 马来西亚时间，ISO 8601 文本（`2026-10-14T08:00:23.000`） |

## 2. 接口

角色：**P** 警察 · **G** 保安 · **T** 交通管理 · **A** 管理员。“打码”表示该角色看到的车牌被打码。

### 认证与总览

| 方法和路径 | 角色 | 作用 |
|---|---|---|
| `POST /auth/login` | 所有人 | 登录（请求体 `{username, password}`） |
| `POST /auth/logout` | 所有人 | 结束会话 |
| `GET /auth/me` | 所有人 | 当前用户和 CSRF 令牌 |
| `GET /overview?date=` | P T A | 摄像头卡片、当天车流、按严重程度的未处理告警、首次预警时刻的拥堵；`date` 默认为最近一个有摄像头数据的日期，`dates` 列出其他日期 |
| `GET /cameras` | 所有人（按范围） | 该角色能看到的摄像头及计数 |
| `GET /runs?camera_id=` | 所有人（按范围） | 分析批次（只有该角色可播放时才给视频地址） |

### 车牌

| 方法和路径 | 角色 | 作用 |
|---|---|---|
| `GET /sightings?plate=&tolerance=&start=&end=&camera_id=&run_id=&vtype=&status=&provenance=&has_alerts=&purpose=&reference=&reveal_reason=&page=&page_size=` | P，G（本门岗），T A 打码 | 车牌模糊检索：形近字符只算 0.5 个差异，`tolerance` 0–2，`*` / `?` 通配符（需打码的角色不能用）；必须带 `purpose`；需打码的角色按车牌号检索时必须给 `reveal_reason`（算一次查看授权，只做精确匹配） |
| `GET /sightings/export.csv?…` | 同上 | 整个结果导出 CSV（对需打码的角色打码），记入审计日志 |
| `GET /sightings/{id}?purpose=` | 同上 | 单条记录：告警、视频位置、整帧画面、模拟登记库记录（仅警察） |
| `POST /sightings/{id}/reveal` | T A | “显示完整车牌”：请求体 `{reason_code, note}`；15 分钟内给出完整车牌和清晰截图；记入审计日志 |
| `GET /plates/{plate}/timeline` | P | 跨摄像头时间线：每段的距离、时间差、推算车速、不可能行程标记；形近车牌 |

### 告警

| 方法和路径 | 角色 | 作用 |
|---|---|---|
| `GET /alerts?status=&severity=&code=&camera_id=&include_simulated=&plate=&page=&page_size=` | P，G（本门岗），A（只读、打码） | 按 高 / 违规 -> 隐患 -> 中 排序，同级新的在前 |
| `GET /alerts/counts` | 同上 | 按状态和严重程度计数 |
| `GET /alerts/{id}` | 同上 | 详情：历史、登记库与观测对比、车速 ± 误差、可以进行的下一步 |
| `POST /alerts/{id}/transition` | P，G（本门岗） | 请求体 `{to, note, corrected_plate}`：new -> acknowledged -> resolved / false_alarm（关闭必须写备注）；`corrected_plate` 会把截图加入纠错样本池 |
| `GET /alerts/{id}/evidence?format=html\|pdf` | P | 证据包；记入 `evidence_exports`、告警历史和审计日志 |

### 交通分析

| 方法和路径 | 角色 | 作用 |
|---|---|---|
| `GET /traffic/runs` | P T A | 有计数线或车道的分析批次 |
| `GET /traffic/summary?run_id=` | P T A | 关键指标、分车道表、不在任何车道内的车辆数、被隐去的违规数、车速说明 |
| `GET /traffic/flow?run_id=&by=type\|lane\|time&bin=` | P T A | 每个时间段的车辆数（`bin` 以秒为单位，1–600，默认 10） |
| `GET /traffic/lanes/series?run_id=&metric=occupancy\|density\|speed\|flow\|count` | P T A | 每条车道一条序列 |
| `GET /traffic/events?run_id=` | P T A（打码） | 带视频位置的车道事件 |
| `GET /traffic/export.csv?run_id=&what=crossings\|lane_bins\|flow_bins\|lanes\|events` | P T A | CSV |

### 拥堵预测

| 方法和路径 | 角色 | 作用 |
|---|---|---|
| `GET /forecast/days` | P T A | 有拥堵序列的摄像头日，含路段、首次预警、拥堵开始时刻、延时视频 |
| `GET /forecast/state?day=&at=HH:MM` | P T A | 该分钟每个路段：当前值、5 分钟均值、画面内车辆数、状态、+5 / +15 / +30 预测及误差带、状态文字和预警 |
| `GET /forecast/accuracy?day=&section=&horizon=5\|15\|30` | P T A | 实测值与 h 分钟前做出的预测对比；MAE、RMSE、拥堵开始时刻、提前预警分钟数、精确率、召回率 |
| `GET /forecast/whatif?day=&section=&capacity_pct=&mu=&start=` | P T A | 增加放行能力后的排队推算，附带假设（“在所述假设下的估计”） |

### 门岗（某个站点的保安）

| 方法和路径 | 角色 | 作用 |
|---|---|---|
| `GET /gate/identify?plate=` | G | 当前身份颜色：红 禁入，绿 住户，青 有效通行证，黄 未登记 |
| `GET /gate/replay?run_id=` | G | 演示回放视频，以及每个车牌一张卡片和它在视频中的时间 |
| `POST /gate/decisions` | G | 请求体 `{plate, decision: allowed\|stopped\|exited, note, sighting_id, unit_no}`（拦停必须写备注） |
| `GET /gate/inside` | G | 当前在场的访客，超时的排在最前 |
| `GET /gate/access-log?plate=&start=&end=&decision=&identity=` · `GET /gate/access-log.csv` | G | 进出记录（车牌模糊检索）、CSV |
| `GET /passes` · `POST /passes` · `POST /passes/{id}/cancel` | G | 访客通行证：请求体 `{plate, unit_no, visitor_label, valid_from, valid_to, hours}` |

### 名单

| 方法和路径 | 角色 | 作用 |
|---|---|---|
| `GET /lists/{resident\|banned}` · `POST /lists/{resident\|banned}` | G（本站点） | 住户（必须有单元号）和本站点禁入名单 |
| `GET /lists/{watch\|blacklist}` · `POST /lists/{watch\|blacklist}` | P | 关注名单和黑名单（必须写原因）；请求体 `{plate, reason, label, unit_no, valid_to}` |
| `POST /lists/entries/{id}/deactivate` | 名单所属角色 | 请求体 `{reason}` |

### 上传与分析、报表

| 方法和路径 | 角色 | 作用 |
|---|---|---|
| `GET /jobs/scenes` | P T A | 上传可选的场景配置（路边车牌识别、门岗闸口 ……） |
| `POST /jobs`（multipart：`file`、`camera_id`、`scene`、`render_video`、`recorded_at`） | P T A | 把图片或视频（最大 2 GB）交给后台任务 |
| `GET /jobs` · `GET /jobs/{id}` · `POST /jobs/{id}/cancel` | P T A | 任务及进度（`612/900 frames · 10.4 FPS · about 0:28 left`） |
| `GET /reports/{daily\|weekly}?date=&kind=traffic\|alerts\|gate&format=json\|csv\|pdf` | 交通：P T A · 告警：P A · 门岗：G | 报表 |

### 管理

| 方法和路径 | 角色 | 作用 |
|---|---|---|
| `GET /users` · `POST /users` · `PATCH /users/{id}` | A | 账号（`{username, display_name, role, password, site_code}`；修改 `{active, password, role}` 会让该用户下线） |
| `GET /settings` · `PUT /settings` | A | 保留天数、超时小时数、查看授权分钟数 …… |
| `GET /audit?username=&action=&start=&end=&limit=` · `GET /audit/verify` | A | 审计日志；哈希链校验 |
| `POST /retention/purge?dry_run=true\|false` | A | 预览或执行保留期限清理 |
| `GET /corrections` | A | 纠错样本池 |
| `GET /data` | A | 各分析批次的源文件、SHA-256 和计数；拥堵数据日；预测批次；登记库路径；最近一次导入 |

### 媒体

| 路径 | 谁可以 | 说明 |
|---|---|---|
| `GET /media/runs/{id}/video` | 能看到该摄像头的角色；需打码的角色**不能**看车牌摄像头的视频（视频里车牌是完整的） | 支持 HTTP Range 请求（206），播放器可以跳转 |
| `GET /media/runs/{id}/image` | 同上 | 图片分析的标注图 |
| `GET /media/series/{day_id}/video` | P T A | 拥堵延时视频 |
| `GET /media/sightings/{id}/crop` | 按范围 | 警察、保安和已授权查看的记录给清晰图，其余在服务器端模糊 |
| `GET /media/sightings/{id}/scene` | P、G、已授权查看的记录 | 最佳截图所在的整帧画面；记入审计日志 |

## 3. 示例（Python，连接正在运行的服务器）

```python
import httpx

c = httpx.Client(base_url="http://127.0.0.1:8000")
r = c.post("/api/v1/auth/login", json={"username": "police1", "password": "<见 webapp/seed/demo_users.yaml>"})
c.headers["X-CSRF-Token"] = r.json()["csrf_token"]          # POST / PUT / PATCH / DELETE 需要

hits = c.get("/api/v1/sightings", params={"plate": "XSD4OO4", "tolerance": 2, "purpose": "Alert follow-up"}).json()
for s in hits["items"]:
    print(s["plate"], s["camera_name"], s["seen_at"], s["provenance"], s["distance"])

alert = c.get("/api/v1/alerts", params={"code": "BLACKLIST"}).json()["items"][0]
c.post(f"/api/v1/alerts/{alert['id']}/transition", json={"to": "acknowledged", "note": "Patrol informed"})
```

## 4. 服务函数（供 AI 助手调用）

API 路由只是薄薄一层：认证 -> 调用服务函数 -> 返回它的 pydantic 模型。服务函数带类型、有文档、接收当前登录的 `User`，
所以下一个任务的 AI 助手可以把它们当作工具调用，并继承同样的权限、打码和审计（助手发起的调用记为 `via: assistant`）。

| 模块（`webapp/services/`） | 函数（都接收 `conn, user, …`） | 返回 |
|---|---|---|
| `overview` | `summary(date)`、`cameras()` | `Overview`、`List[CameraOut]` |
| `plates` | `search(SightingQuery)`、`get(sighting_id, purpose)`、`reveal_sighting(sighting_id, reason, note)`、`timeline_of(plate)`、`export_csv(SightingQuery)` | `Page[SightingOut]`、`SightingDetail`、`RevealOut`、`Timeline`、CSV 文本 |
| `alerts` | `list_alerts(status, severity, code, camera_id, include_simulated, plate, page, page_size)`、`counts()`、`get_alert(alert_id)`、`transition(alert_id, to, note, corrected_plate)` | `Page[AlertOut]`、`AlertCounts`、`AlertDetail` |
| `traffic` | `runs()`、`summary(run_id)`、`flow_series(run_id, by, bin_sec)`、`lane_series(run_id, metric)`、`events(run_id)`、`export_csv(run_id, what)` | `List[RunOut]`、`TrafficSummary`、`Series`、`List[LaneEventOut]` |
| `forecast` | `days()`、`state(day, at)`、`accuracy(day, section, horizon)`、`whatif(day, section, capacity_pct, mu, start)` | `List[ForecastDay]`、`ForecastState`、`Accuracy`、`WhatIfOut` |
| `gate` | `identify(plate)`、`replay(run_id)`、`decide(plate, decision, note, sighting_id, unit_no)`、`inside()`、`access_log(…)`、`passes()`、`issue_pass(…)`、`cancel_pass(pass_id)` | `Identity`、dict、`AccessEventOut`、`List[InsideOut]`、`List[PassOut]` |
| `lists` | `entries(lst)`、`add(lst, plate, reason, label, unit_no, valid_to)`、`deactivate(entry_id, reason)` | `ListEntryOut` |
| `jobs` | `create(stream, filename, camera_id, scene, render_video, recorded_at)`、`list_jobs()`、`get(job_id)`、`cancel(job_id)` | `JobOut` |
| `evidence` | `build(alert_id)`、`export(alert_id, fmt)` | dict、文件路径 |
| `reports` | `build(kind, day, weekly)`、`to_csv(rep)`、`to_pdf(rep)` | dict、CSV 文本、PDF 字节 |
| `admin` | `users()`、`add_user(…)`、`update_user(…)`、`get_settings()`、`put_settings(values)`、`audit_log(…)`、`verify()`、`run_purge(dry_run)`、`corrections()`、`data_status()` | 模型 / dict |

模型定义在 `webapp/models.py`；它们的 JSON Schema 在 `/openapi.json`（`components.schemas`）里，可以直接转换成工具定义。
会改动数据的操作（告警处理、名单修改、门岗决定）在助手里应保留一步人工确认。
