# SECS Vision 网页系统：测试计划与报告

[English version / 英文版](TEST_PLAN_REPORT_EN.md) · 系统文档目录：[索引](README.md)

报告日期：2026 年 10 月 24 日。下面所有测试都是在随本文档提交的代码上运行的。

## 1. 计划：测什么、怎么测

| 层次 | 测什么 | 怎么测 | 在哪里 |
|---|---|---|---|
| 单元测试（识别） | `alpr/` 包：车牌识别与投票、跟踪、车牌记录、登记库规则、车道、危险驾驶规则、预测，以及新增的可选证据输出 | 86 个测试，使用合成输入；不需要模型、GPU 或网络 | `alpr/tests/test_alpr.py` |
| API / 集成测试 | 导入程序、登录、会话、角色、打码与显示完整车牌、审计日志、模糊检索、点击跳转视频、告警、时间线、证据包、门岗、预测、推演、交通分析、保留期限、上传、报表、API 文档、`requirements.txt` | 22 个测试，用 FastAPI 的 TestClient，临时数据库由已提交的演示结果填充；每个测试使用自己的副本 | `webapp/tests/test_web.py` |
| 识别流水线回归 | 可选的 `--evidence` 输出不改变任何识别结果 | 四个演示配置分别开 / 关该选项重跑（OpenVINO、无界面）；把已提交 JSON 的每个字段与新结果逐一比较 | 第 5 节 |
| 端到端 | 用真实后台任务和模型上传并分析 | 运行中的服务器；通过 API 上传一段 8 秒视频和一张照片 | 第 6 节 |
| 浏览器 | 每个角色的每个页面都能加载，没有脚本错误；版面 | 通过 DevTools 协议控制无头 Chrome，宽 1440 px，记录 JavaScript 异常和控制台错误；人工查看截图 | 第 7 节 |
| 性能 | 响应时间；大数据量下的模糊检索 | 每个接口预热后请求 20 次；检索时另加 10 万个虚构车牌 | 第 8 节 |
| CI 预演 | GitHub 工作流的安装和测试 | 用 `.github/workflows/ci.yml` 里的命令新建一个全新的虚拟环境 | 第 9 节 |

**通过标准：** 所有自动化测试通过；回归比较中已提交结果的任何字段都没有差异；任何角色的任何页面都没有脚本错误；
[需求规格说明](REQUIREMENTS_ZH.md) 中的每条功能需求（FR-1 … FR-23）都有自动化测试或有记录的人工检查。

## 2. 测试环境

| 项目 | 值 |
|---|---|
| 电脑 | Windows 11 家庭版（版本 26200），只用 CPU |
| Python | 项目虚拟环境 `.venv`，Python 3.12.0b3（测试版：pip 26 不支持它，所以 CI 预演跳过了 pip 升级；GitHub 用正式版 3.12） |
| 主要包 | torch 2.3.1（CPU）、ultralytics 8.4.171、OpenCV 4.11、numpy 1.26.4、LightGBM 4.7.0、FastAPI 0.161.1、Starlette 1.9.0、uvicorn 0.54.0、Jinja2 3.1.6、fpdf2 2.8.9、httpx 0.28.1（版本固定在 `requirements-ci.txt`） |
| 浏览器 | Google Chrome 155（无头模式） |
| CI | GitHub Actions，`ubuntu-latest`，Python 3.12 |

## 3. 结果一览

| 检查 | 结果 |
|---|---|
| `alpr` 单元测试，项目虚拟环境 | **86 / 86 通过** |
| `alpr` 单元测试，全新的 CI 式虚拟环境 | **86 / 86 通过** |
| 网页 API 测试，项目虚拟环境 | **22 / 22 通过** |
| 网页 API 测试，全新的 CI 式虚拟环境 | **22 / 22 通过** |
| 识别流水线回归（7 次比较） | 识别结果 **0 处差异**（第 5 节） |
| 端到端上传（视频 + 照片） | **完成**：视频读出 5 个车牌（与演示结果相同），照片 1 个 |
| 浏览器检查（4 个角色，32 次页面加载） | **没有脚本错误** |
| 性能 | 交互页面的 API 调用中位数 20–65 ms（第 8 节） |

## 4. 22 个网页测试

| 测试 | 需求 | 证明了什么 |
|---|---|---|
| `test_importer_loads_every_demo_result` | FR-21、FR-13 | 4 个批次、35 条识别记录（7 个插入车牌、6 个合成）、13 条登记库告警 + 5 条车道告警、2 条关注名单和 1 条禁入命中、**没有任何告警指向真实车辆**、355 次过线、12 个车道事件、10 个被隐去、571 分钟预测；记录了每个源文件的 SHA-256；`seen_at` = 时钟起点 + `first_sec` |
| `test_importer_is_idempotent_and_keeps_alert_workflow_state` | FR-21 | 导入两次，各表数量不变，已确认的告警仍是已确认 |
| `test_importer_reproduces_the_committed_forecast_and_adds_the_zones` | FR-17、FR-21 | 导入时重新计算的 LightGBM 与已提交的预测 JSON 一致（最大差异 < 0.001）；另外算出 9 个路段 |
| `test_login_lockout_logout_and_csrf` | FR-1 | 5 次错误密码后账号锁定 5 分钟（正确密码也不行）；不带 CSRF 请求头的退出被拒绝；每次尝试都有审计记录；只保存 scrypt 哈希 |
| `test_roles_see_only_their_scope` | FR-2 | 保安只看到本门岗；警察看不到公寓门岗；交通管理没有告警中心；只有管理员能管理用户；页面遵循同样规则；未登录访问被转到登录页 |
| `test_masked_roles_see_masked_plates_and_blurred_crops_until_they_reveal_with_a_reason` | FR-8 | 打码车牌和模糊截图（字节不同）；看不到场景帧和车牌摄像头视频（403），车道视频正常（206）；显示完整车牌需要有效理由，之后显示车牌和清晰截图并记入日志；按车牌号检索需要理由且只做精确匹配；打码规则本身 |
| `test_every_search_view_and_export_is_audited_with_its_purpose` | FR-5、FR-9 | 不选用途不能检索；用途和案件编号写入审计日志；查看和导出都有记录；CSV 有 provenance 列且三种取值都有 |
| `test_audit_log_is_append_only_and_hash_chained` | FR-9、NFR-9 | 数据库拒绝 `UPDATE` 和 `DELETE`；绕过系统插入的一行会破坏哈希链并被发现 |
| `test_fuzzy_search_tolerates_ocr_look_alikes` | FR-4 | `XSD 4OO4` 在容错 1 时能找到 `XSD 4004`（容错 0 不行）；通配符；形近字符的距离；向量化索引在 400 个随机车牌上与逐对计算的距离完全一致；未确认的读数单独标出且不告警 |
| `test_click_to_timestamp_seeks_one_second_before_the_record` | FR-6、FR-11 | 每条结果都跳到 `first_sec − 偏移 − 1`；门岗卡片在回放的 0.03 / 9.2 / 16.0 / 24.0 秒出现；视频支持 Range 请求（206） |
| `test_alerts_sorted_by_severity_and_workflow_with_correction_pool` | FR-7 | 按级别排序；`UNMATCHED` 的说明；关闭必须写备注；确认 -> 误报并更正车牌 -> 纠错样本池；已关闭的告警不能再标为已解决；管理员不能修改告警 |
| `test_cross_camera_timeline_flags_impossible_travel` | FR-14 | 新山 -> 吉隆坡：290–296 公里、1100–1180 km/h、不可能；同一站点的一段不算；模拟标记；交通管理角色被拒绝 |
| `test_evidence_pack_marks_every_page_hashes_images_and_cites_only_verified_sections` | FR-15 | 演示标记多次出现；每张图片的 SHA-256 与嵌入的字节一致；哈希说明；只引用 `legal.yaml` 中的条文（克隆车牌引用第 108(3)(b)、(f) 条）；没有 PDRM 字样或标志；没有外部图片；PDF 有效；两次导出都有记录；交通管理角色被拒绝 |
| `test_gate_identity_colours_passes_and_overstays` | FR-11、FR-12 | 绿 / 青 / 红 / 黄；过期的通行证无效；签发通行证后访客变青色；拦停必须写备注；放行 -> 在场；5 小时后变为超时；超时的排在最前；标记离开 -> 不再在场；进出记录顺序正确 |
| `test_forecast_state_accuracy_and_warning_match_the_committed_json` | FR-17 | 首次预警 06:56，拥堵开始 07:13 和 08:17；“Congestion in ~15 min”；预测值等于已提交的 JSON；独立重算 MAE；提前 17 分钟和 3 分钟预警（TEAM_GUIDE 10.5） |
| `test_whatif_more_capacity_never_lengthens_the_queue` | FR-18 | +0 % 等于实测；能力每提高一档都不会高于上一档；标注和至少 5 条假设；默认估计 μ，给定时用假设值 |
| `test_traffic_analytics_flow_lanes_events_and_csv` | FR-16 | 355 辆 = 各车道 + 4 辆不在任何车道内；隐去 7 次超速、3 次跟车过近；车速误差 6.5 %；按车型的流量合计 355；8 条车道序列，片段结束后没有多余的时间段；12 个事件，车牌打码，违规只有合成车辆；CSV 355 行 + 表头 |
| `test_retention_deletes_ordinary_sightings_but_keeps_open_alerts` | FR-10 | 一开始没有要删的；31 天后预览的数量正好是没有告警的记录数；清理保留所有有未关闭告警的记录；清理有记录；哈希链仍然有效 |
| `test_upload_job_is_analysed_by_the_worker_and_imported` | FR-19 | 文本文件被拒绝；图片进入队列，被分析（用替身代替 `run_image.py`，所以不需要模型），按给定的录制时间导入；上传有记录 |
| `test_reports_are_built_per_role_as_csv_and_pdf` | FR-20 | 告警日报统计到警察的 20 条告警；交通周报 CSV 带演示标记；交通管理角色不能看告警报表；保安的门岗 PDF |
| `test_api_documentation_lists_the_service_endpoints` | FR-22 | OpenAPI 文档列出了主要的服务接口 |
| `test_requirements_cover_every_third_party_import_of_the_web_system` | 约束 | `webapp/` 的每个第三方导入都在 `requirements.txt` 中，且该文件按 ASCII 读取 |

总览页的细节（摄像头卡片、日期选择、按车型的流量）没有自动化测试：已在浏览器中检查（第 7 节），并在演示走查中展示。

## 5. 识别流水线回归（`--evidence`）

对识别代码的唯一改动是可选的证据输出。2026 年 10 月 23 日把四个演示配置重跑了一遍（OpenVINO、无界面），并把每个新结果
文件与已提交的文件逐字段比较（已提交的每个字段在新文件中都必须存在且值相同；新增字段单独列出）。

| 配置 | 运行方式 | 已提交字段中的差异 | 新增字段 | 带证据的记录 |
|---|---|---|---|---|
| `malaysia_road_anpr` | 不加 `--evidence` | 0 | `processing` | 0 / 8 |
| `malaysia_road_anpr` | 加 `--evidence` | 0 | `processing`、`evidence` | 8 / 8 |
| `malaysia_guardhouse` | 不加 | 0 | `processing` | 0 / 2 |
| `malaysia_guardhouse` | 加 | 0 | `processing`、`evidence` | 2 / 2 |
| `malaysia_road_violations` | 不加 | 0 | `processing` | 0 / 19 |
| `malaysia_road_violations` | 加 | 0 | `processing`、`evidence` | 19 / 19 |
| `malaysia_selayang_lanes` | 加 | 0 | `processing`、`observed_sec`（104 个车道时间段） | 没有车牌 |

`observed_sec` 不是这次工作加的：它是在 Selayang 演示 JSON 提交（d519842）之后，由之前的提交 f7b13f9 和 d576a9a 加进
`alpr/flow.py` 的。除了这 104 个新值，其他字段全部相同。已提交的演示 JSON 没有重新生成，也没有改动。新增的单元测试
`test_evidence_output_saves_crop_and_frame_and_changes_no_other_field` 在合成输入上检查同样的性质。

## 6. 用真实后台任务做端到端上传

启动带后台任务的服务器；以 `police1` 通过 API 登录；把两个文件上传到 KL 路边摄像头，录制时间 2026-10-23 09:00
（2026 年 10 月 24 日测试）：

| 文件 | 场景 | 结果 |
|---|---|---|
| 8 秒视频，240 帧，1920×1080，截自路边视频第 117–125 秒 | LILIN 演示摄像头（裁剪厂商的屏幕录像） | 排队 -> 运行，实时显示进度（“50/240 frames · 2.3 FPS · about 1:23 left” … “200/240 frames · 3.6 FPS · about 0:11 left”）-> 约 70 秒完成，其中约 20 秒用于加载模型：**5 个已确认车牌** `WRS 7607`、`CAX 3029`、`WA 9299 M`、`WB 7605 J`、`VCH 1587`——正好是已提交演示结果中这段画面里的车牌，每个首次出现时间都等于演示结果的时间减 117.0 秒；每条记录都跳到它出现前 1 秒；没有告警（上传只用 `ILLEGAL_FORMAT` 和 `DUPLICATE`） |
| 车牌数据集验证集中的一张照片 | 路边 | 开始后约 10 秒完成：**1 个车牌**，`JYD 9104` |

这段短视频平均 3.6 FPS，其中包括模型预热；TEAM_GUIDE 第 11 节测得路边场景在 20 帧预热之后为 10.5 FPS。

## 7. 浏览器检查

- **所有角色的所有页面：** 无头 Chrome 依次以每个演示账号登录，打开其导航中的每个页面（警察 9 个含时间线、保安 5 个、
  交通管理 6 个、管理员 11 个，共 31 个页面，另外为文档截图再打开一次交通分析页）。记录 JavaScript 异常和控制台错误：
  **没有**。
- **人工查看截图：** 逐张查看[演示走查](DEMO_WALKTHROUGH_ZH.md)的 16 张截图；31 张检查截图以缩略图拼图查看（每个页面
  都显示了内容，没有错误页；版面、数字、模拟标记、打码）。
- **窄窗口：** 版面在 1180 px 和 1100 px 处减少列数。在 1024 px 下检查了总览、告警中心、预测和门岗：摄像头和路段卡片
  重排为两到三列，告警详情移到列表下方，门岗卡片移到视频下方；没有内容溢出。
- **媒体：** 视频可以跳转（HTTP 206），门岗回放在对应时刻显示每张卡片，证据包直接在浏览器中打开。

## 8. 性能（2026 年 10 月 24 日）

在演示数据库的副本上，经过完整的 HTTP 处理流程（TestClient），预热后每个接口请求 20 次的中位数和第 95 百分位：

| 调用 | 中位数 | p95 |
|---|---|---|
| 总览 | 33.7 ms | 54.3 ms |
| 车牌检索，模糊容错 2 | 23.9 ms | 40.2 ms |
| 车牌检索，不填车牌（每页 50 条） | 22.5 ms | 36.0 ms |
| 告警列表 | 28.2 ms | 41.7 ms |
| 跨摄像头时间线 | 62.5 ms | 74.1 ms |
| 预测状态（5 个路段） | 34.6 ms | 46.9 ms |
| 预测与实际对比 | 42.9 ms | 68.5 ms |
| 推演 | 22.2 ms | 30.2 ms |
| 交通汇总 | 21.2 ms | 53.7 ms |
| 证据包（PDF，335 KB） | 1.5 秒（单次请求） | — |
| 另加 **10 万** 个不同车牌后的模糊检索：容错 0 / 1 / 2 | 34 / 136 / 131 ms | 37 / 145 / 137 ms |

这些测量带来了两处提速：模糊检索用 numpy 向量化并按数据库版本缓存（之前 10 万个车牌约 5 秒）；拥堵开始时刻和首次预警
现在也缓存（之前中位数：总览 302 ms、预测状态 183 ms、推演 93 ms）。

## 9. CI 预演

这台笔记本上无法复现 GitHub 的 Linux 运行环境（没有 WSL 或 Docker），所以在 Windows 上按工作流本身的命令新建了一个全新的
虚拟环境来预演：先从 PyTorch 的 CPU 源安装 CPU 版 torch 2.3.1 / torchvision 0.18.1，再装 `requirements-ci.txt`。第一次
因为临时的下载错误失败，第二次完成（安装后 2.6 GB）。2026 年 10 月 24 日的结果：**86 / 86**（`alpr`，26 秒）和
**22 / 22**（网页，32 秒）。因此 GitHub 上的第一次运行同时也是第一次在 Linux 上测试；工作流为 OpenCV 和 PDF 安装了
`libgl1`、`libglib2.0` 和 DejaVu 字体。

## 10. 测试中发现并已修复的问题

| 发现方式 | 问题 | 修复 |
|---|---|---|
| 浏览器检查 | 时间线页面返回 500：登记库的 SQLite 连接被另一个线程使用 | 每个线程一个登记库连接 |
| 截图的隐私审查 | 需打码的角色能播放车牌摄像头的标注视频，而视频里每个车牌都是完整的 | 需打码的角色不能看车牌摄像头视频和场景帧（403），并加了测试 |
| 浏览器检查 | 门岗卡片和车道表格里出现了“null”字样 | 渲染前过滤掉空值 |
| 浏览器检查 | 从其他页面打开告警链接（`/alerts#12`）时没有选中该告警 | 页面也监听 hash 变化 |
| 浏览器检查 | 隐藏时绘制的图表被压扁；时间标签重叠 | 尺寸变化时重绘；刻度数量随宽度调整 |
| API 测试 | 车道序列在片段结束后多出一个空时间段 | 去掉片段结束后才开始的时间段 |
| API 测试 | 各车道计数之和（351）与总数（355）对不上 | 统计并显示不在任何车道内的过线车辆（4 辆） |
| 性能测量 | 总览约 300 ms，因为每次请求都重算拥堵开始时刻 | 按预测数据版本缓存 |
| 性能测量 | 在 10 万个车牌中模糊检索约 5 秒 | numpy 向量化距离计算，索引缓存 |
| 浏览器检查 | 证据包被下载而不是直接打开 | 改为在浏览器中直接打开 |
| 浏览器检查 | 警察的总览没有拥堵卡片 | 警察的摄像头范围包含拥堵摄像头（只读） |
| PDF 审查 | 两端对齐的文字中间空隙过大 | 改为左对齐 |
| 证据包审查 | 车牌框画成了浅蓝色，而说明写的是黄色（RGB / BGR 搞反） | 修正颜色，重拍截图 |
| 法律条文核对 | 第 23 条的马来文标题拼错（应为“berkaitan”而非“berkenaan”） | 对照 JPJ 文本后更正 |
| 代码审查 | API 也接受 URL 中的 CSRF 令牌 | 只从请求头读取 |

## 11. 未覆盖的部分（已知缺口）

- **Linux：** 没有在本机运行；GitHub Actions 的第一次运行就是 Linux 上的检验。
- **CI 中的浏览器测试：** 页面检查是一个本地脚本流程，不在 CI 里；JavaScript 没有单元测试。
- **负载：** 没有测试大量用户同时访问（SQLite 只有一个写入者，只有一个分析任务线程）。
- **真实摄像头：** 没有实时 RTSP 视频流（未实现）；上传只测试了一段短视频和一张照片。
- **登记库：** 是模拟的，所以无法测试针对真实车牌的告警；所有告警测试都用虚构车牌。
- **CI 用替身代替分析子进程**（运行环境里没有模型）；真实的子进程由端到端测试覆盖（第 6 节）。
- **PDF 的外观**（字体、分页）靠人工检查，没有自动化。
- **无障碍：** 每个图表都有表格视图，颜色从不是唯一的信号，但没有做屏幕阅读器测试。
- **本机 Python 是 3.12 测试版**；CI 用正式版 3.12。
