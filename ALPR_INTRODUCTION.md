# SECS — Malaysian ALPR & Traffic Flow Monitoring / 马来西亚车牌识别与交通流量监控

[中文](#中文) | [English](#english)

![CCTV dashboard](docs/images/alpr_dashboard.jpg)

---

## 中文

### 项目简介

本项目是软件工程小组项目，目标是帮助交警和保安**识别和记录马来西亚车牌、对照登记库核验车牌、监控车流**，并为后续的
车流预测、AR/VR 和生成式 AI 功能打基础。目前代码已经实现：

| 功能 | 说明 |
|---|---|
| **静态图片车牌识别** | 整图车牌检测 → 裁剪 → 车牌专用 OCR → 马来西亚车牌语法校验，输出车牌号、是否合法格式、所属州属/类别 |
| **视频车牌识别** | 车辆检测 + ByteTrack 多目标跟踪 → 车辆内车牌检测 → OCR → 同一辆车多帧加权投票，结果分为 ACQUIRING / TENTATIVE / CONFIRMED |
| **车牌日志（去重）** | 每辆车只记一行；同一辆车被遮挡后重新出现（换了跟踪 ID）也不会重复计数 |
| **车流统计** | 计数线过线计数、分车型统计（轿车/摩托/巴士/卡车）、每分钟流量、每 10 秒时间序列（可作为车流预测的训练数据） |
| **车牌核验与告警** | 每块确认的车牌对照**模拟**车辆登记库检查：未登记、路税过期、非法格式、黑名单、车型不符、套牌（两辆车同时挂同一车牌 / 跨摄像头不可能的行驶），告警显示在仪表盘和 JSON 里 |
| **马来西亚车牌规则** | 标准格式（如 `WA 1234 B`）、双排车牌、特殊号牌（GOLD、VIP、1M4U、PUTRAJAYA 等）、州属识别（Johor、Sabah、Sarawak、出租车、军车…），非法/非标准文字会被标记出来 |
| **界面** | 苹果风格的深色 CCTV 仪表盘（圆角画面卡片 + 毛玻璃车牌日志 + 车流指标 + 结尾汇总表）和行车记录仪 AR 卡片界面 |
| **评测工具** | 图片和视频都有基于人工核对标注的自动评测脚本 |

### 效果（全部基于人工逐块核对的马来西亚车牌）

**静态图片**：Malaysian Car Plate Dataset 验证集，89 张图 / 94 块车牌

| 方案 | 检测召回 | 车牌号完全正确 |
|---|---|---|
| 旧版（印度数据训练的检测器 + EasyOCR） | 82.1% | 41.5% |
| **当前版本** | **98.9%** | **94.7%**（双排车牌 100%，特殊号牌 100%） |

![Static results](docs/images/alpr_static.jpg)

**视频（场景一）**：马来西亚路边 ANPR 摄像头，三个人工标注的时间窗口，共 18 辆车

| 窗口 | 用途 | 正确 | 误报 |
|---|---|---|---|
| 48–82 s | 开发调参 | 4/4 | 0 |
| 100–130 s | 演示视频 | 8/9 | 0 |
| 0–44 s | 保留测试 | 5/5 | 0 |
| **合计** | | **17/18（94.4%）** | **0** |

唯一漏读的车牌几乎全程被另一辆车遮挡。旧版在演示窗口只有 6/9，并且会重复记录同一辆车。
评测细节和注意事项（例如保留窗口第一次运行时有 5 条误报，后来修复）见 [TEAM_GUIDE.md](TEAM_GUIDE.md)。

**视频（场景二，验证泛化能力）**：马来西亚大楼门岗闸口 CCTV，斜前方近距离视角，车辆在闸口前停下再通过，
有货车、皮卡、面包车、轿车和摩托车。整段 186 秒在模型第一次运行前就人工标注完；**模型和所有阈值都没有为这段视频调整**，
只设置了画面裁剪和计数线：

| 车道 | 车牌 | 正确 | 误报 |
|---|---|---|---|
| 闸口车道（含 1 块双排车牌） | 5 | **5/5** | **0** |

另一条车道经过的车辆车牌只有 40–60 像素宽、角度很斜，人眼也无法核对，不计入成绩（系统也没有把它们记进车牌日志）。
过线计数 4 辆，和实际一致（最后一辆到视频结束时还停在闸口）。

![Guardhouse gate](docs/images/alpr_guardhouse.jpg)

**违规识别演示（合格车牌与违规车牌对比，2 分 35 秒，20 辆车）**：真实的车辆登记数据不公开，网上也没有"已知哪些车违规"的
真实视频，所以我们把场景一的**整段**路边视频中 7 辆车的车牌用数字方式替换成**虚构车牌**（X 字头，马来西亚没有任何州使用；
或故意做成非法格式），其余 13 辆保留真实车牌。替换是一个独立的预处理步骤（`python -m alpr.simulate`，配置在
`alpr/simulation.yaml`），识别系统不知道哪些车被替换过，必须自己从画面里读出车牌再核验。

| 车辆 | 车牌 | 系统判定 | 正确？ |
|---|---|---|---|
| 12 辆真实车辆（含 2 块双排车牌） | 真实车牌 | **合格**（VALID） | 12/12 车牌正确，**0 误报** |
| 白色本田 City | XSA 1001（虚构） | 路税过期 + 车型不符（登记为摩托车） | ✓ |
| 银色现代 | XSE 5005（虚构） | 未登记 | ✓ |
| 深色宝马 | XSD 4004（虚构） | 套牌：17 分钟前在新山出现过，相距 293 km | ✓ |
| 灰色马自达 CX-5 | WXY 12345（5 位数字） | 非法格式 | ✓ |
| 银色 Proton Persona | XSB 2002（虚构） | 黑名单（模拟：报失窃） | ✓ |
| 红色 Iswara + Bezza | 两辆车同时挂 XSG 7007 | 两辆都报套牌 | ✓ ✓ |
| **合计** | | **7/7 违规全部抓到，真实车辆 0 误报** | |

另外 1 辆真实车辆（被 CR-V 全程遮挡的 Myvi）没有读出；1 块真实车牌 `VCV 155` 因 OCR 在 `VCV155` / `VCV1555` 之间
摇摆，只给出"待确认"（不会报警）。视频结尾有一张汇总表，列出每辆车的车牌、车型、出现时间和判定结果。

![Violation check](docs/images/alpr_violations.jpg)

**演示视频**：
- **违规识别（主演示，2 分 43 秒，含结尾汇总表）**：`demo/malaysia_road_violations_demo.mp4`（1920×1080）
- 场景一（路边摄像头 + 车牌核验告警）：`demo/malaysia_road_anpr_demo_30s.mp4`（1920×1080，30 秒）
- 场景二（门岗闸口）：`demo/malaysia_guardhouse_demo_30s.mp4`（1920×1080，30 秒）

### 快速开始

```bash
pip install -r requirements.txt

# 静态图片（单张 / 文件夹 / 通配符），结果在 outputs/images_alpr/
python run_image.py --source path/to/images

# 视频：生成 30 秒 CCTV 演示视频 + 结果 JSON（需要先下载演示视频，见下文"数据"）
python run_alpr.py --profile malaysia_road_anpr
# 违规识别演示：先生成替换了虚构车牌的视频，再识别 + 核验整段视频，并对照标注评测
python -m alpr.simulate
python run_alpr.py --profile malaysia_road_violations
python -m alpr.evaluate --pred outputs/malaysia_road_violations.plates.json --gt alpr/gt/simulated/malaysia_road_violations_0-155s.txt

# 车牌核验：生成模拟登记库（缺失时会自动生成）、查询单个车牌、图片识别时开启核验
python -m alpr.registry --make-demo
python -m alpr.registry --check "XSB 2002" --vtype Car
python run_image.py --source path/to/images --check

# 评测
python -m alpr.eval_images --dataset "../Malaysian Car Plate Dataset" --split val --gt alpr/gt/malaysia_val_plates.csv
python -m alpr.evaluate --pred outputs/malaysia_road_anpr_30s.plates.json --gt alpr/gt/malaysia_road_anpr_100-130s.txt

# 单元测试（不需要模型和 GPU）
python alpr/tests/test_alpr.py
```

### 工作流程（视频）

```mermaid
flowchart LR
    A[视频帧] --> B[YOLOv8 车辆检测<br/>+ ByteTrack 跟踪]
    B --> C[车辆框内<br/>车牌检测]
    C --> D[车牌 OCR<br/>fast-plate-ocr]
    D --> E[马来西亚语法<br/>约束解码]
    E --> F[多帧加权投票]
    F --> G[车牌日志<br/>去重]
    G --> R[登记库核验<br/>模拟数据]
    B --> H[计数线<br/>车流统计]
    R --> I[CCTV 仪表盘 / JSON]
    H --> I
```

### 车牌核验与告警（模拟登记库）

> **登记库是模拟的，不是真实登记数据。** 马来西亚 JPJ 登记数据不公开，`python -m alpr.registry --make-demo`
> 生成的演示库里：虚构车辆的车牌全部以 `X` 开头（没有任何州使用 X 字头）；`alpr/gt/` 里人工标注过的真实车牌只是
> 中性占位记录（状态 ok、无路税日期、无车型颜色），任何规则都不会对真实车辆报警。

| 告警 | 含义 |
|---|---|
| `UNMATCHED` | 登记库里查不到这个车牌（只差 1 个字符的视为 OCR 误读，不报警） |
| `EXPIRED` | 路税在观测时间之前已过期 |
| `ILLEGAL_FORMAT` | 不是合法的马来西亚车牌格式 |
| `BLACKLIST` | 登记状态为黑名单（例如报失窃） |
| `TYPE_MISMATCH` | 观测车型（可选：车身颜色）与登记不符，例如摩托车的车牌装在轿车上 |
| `DUPLICATE` | 套牌嫌疑：画面里两辆分开的车同时挂同一车牌，或在另一个摄像头出现、时间内不可能开到 |

视频中，车辆进入车牌日志（CONFIRMED）时核验；仪表盘在车辆标签和日志行上显示红色（高）/ 橙色（中）告警徽章，
顶部栏显示 `ALERTS` 计数。因为真实车辆不会被报警，演示视频在 103 / 110 / 117 / 124 秒加入了 4 条**虚构车牌的
模拟事件**（紫色 `SIMULATED EVENT`，画面里没有对应车辆），分别演示黑名单、路税过期 + 车型不符、未登记、
跨摄像头套牌；它们在 JSON 里单独放在 `simulated_events`。详见 [TEAM_GUIDE.md](TEAM_GUIDE.md) 第 4 节。

### 代码结构

| 路径 | 作用 |
|---|---|
| `run_image.py` / `run_alpr.py` | 静态图片 / 视频的命令行入口 |
| `alpr/config.yaml` | 所有参数和每段视频的配置（profile） |
| `alpr/image.py` | 静态图片识别 |
| `alpr/pipeline.py` | 视频识别：跟踪、读牌、投票、车牌日志、车流 |
| `alpr/ocr.py` | OCR 后端（fast-plate-ocr 默认，EasyOCR 备用） |
| `alpr/plates.py` | 马来西亚车牌语法、特殊号牌、州属、约束解码 |
| `alpr/voting.py` | 多帧投票 |
| `alpr/flow.py` | 车流计数 |
| `alpr/registry.py` | 模拟登记库（SQLite）、核验规则、模拟库生成 |
| `alpr/simulate.py` / `alpr/simulation.yaml` | 生成违规演示视频：把指定车辆的车牌替换成虚构车牌 |
| `alpr/hud.py` | 仪表盘和 AR 界面渲染 |
| `alpr/gt/` | 人工核对的车牌标注（图片 + 视频） |
| `models/plate_malaysia.pt` | 在马来西亚车牌数据集上微调的车牌检测器（默认） |
| `models/finetuned.pt` | 旧版车牌检测器（印度车牌数据） |

### 模型与数据

- 车辆检测用 COCO 预训练的 `yolov8s.pt`，OCR 模型 `cct-s-v2-global-model`，二者第一次运行时会自动下载。
- 车牌检测器 `plate_malaysia.pt`：在 `finetuned.pt` 的基础上，用 Malaysian Car Plate Dataset 训练集（414 张）继续训练 40 轮。
- 数据集不在仓库里（放在仓库上一级目录 `../Malaysian Car Plate Dataset`）。
- 演示视频素材来自 YouTube：LILIN CCTV MALAYSIA 的 "INTEL AIDA ANPR TESTING"（https://www.youtube.com/watch?v=raQOCJ_wzKE），
  版权归原作者，仓库里不包含原视频，仅用于课程演示，请注明来源。下载方法：
  `python -m yt_dlp -f 137 -o data/videos/malaysia_lilin_anpr_road.mp4 https://www.youtube.com/watch?v=raQOCJ_wzKE`
- 场景二素材：LEAF SMART COMMUNITY 的 "License plate recognition system for guard house"
  （https://www.youtube.com/watch?v=eNIGPl-W2II），同样版权归原作者、仓库不含原视频。下载方法：
  `python -m yt_dlp -f 137 -o data/videos/malaysia_guardhouse_lpr.mp4 https://www.youtube.com/watch?v=eNIGPl-W2II`

### 已知局限与下一步

- 纯 CPU 约 3–5 FPS（含界面渲染），实时需要 GPU 或导出 OpenVINO / ONNX；宽度小于 60 像素的车牌仍容易读错。
- 车牌核验用的是模拟登记库；在其他时间窗口运行时，未标注的真实车牌会显示 `UNMATCHED`，只表示"不在模拟库里"。
- **下一步**：车流预测（用 `flow.per_10s` 时间序列）、超速检测（需要先做相机标定）、接入真实/授权的登记数据和多摄像头识别记录、
  AR/VR 显示、生成式 AI（例如复核低置信度车牌、自动生成交通报告）。

---

## English

### Overview

A software-engineering group project that helps traffic police and security guards **read and log Malaysian licence
plates, check them against a vehicle registry and monitor traffic flow**, as the base for later features: flow
prediction, AR/VR and generative AI. What the code does today:

| Feature | Description |
|---|---|
| **Image ALPR** | Whole-image plate detection → crop → plate-specific OCR → Malaysian plate syntax check; outputs plate number, legal-format flag and registration state/category |
| **Video ALPR** | Vehicle detection + ByteTrack multi-object tracking → plate detection inside each vehicle → OCR → weighted multi-frame voting per vehicle, with ACQUIRING / TENTATIVE / CONFIRMED status |
| **Plate log (de-duplicated)** | One row per vehicle; a vehicle re-acquired after an occlusion (new track ID) is not counted twice |
| **Traffic flow** | Counting-line crossings, per-type counts (car / motorcycle / bus / truck), vehicles per minute, 10-second time series (training data for flow prediction) |
| **Plate verification & alerts** | Every confirmed plate is checked against a **simulated** vehicle registry: unmatched, expired road tax, illegal format, blacklisted, wrong vehicle type, duplicate / cloned plate (two vehicles in view with one plate, or an impossible trip between cameras); alerts show on the dashboard and in the JSON |
| **Malaysian plate rules** | Standard format (e.g. `WA 1234 B`), two-row plates, special series (GOLD, VIP, 1M4U, PUTRAJAYA …), registration state (Johor, Sabah, Sarawak, taxi, military …); non-standard text is flagged |
| **UI** | Apple-style dark CCTV dashboard (rounded camera card, frosted-glass plate log, flow KPIs, end-of-session summary) and a dashcam AR-card overlay |
| **Evaluation tools** | Automatic image and video evaluation against hand-verified labels |

### Results (all measured on hand-verified Malaysian plates)

**Images** — Malaysian Car Plate Dataset validation split, 89 images / 94 plates

| Version | Detection recall | Exact plate match |
|---|---|---|
| Previous (Indian-plate detector + EasyOCR) | 82.1% | 41.5% |
| **Current** | **98.9%** | **94.7%** (two-row plates 100%, special series 100%) |

**Video, scene 1** — Malaysian roadside ANPR camera, three hand-labelled windows, 18 vehicles

| Window | Purpose | Correct | False positives |
|---|---|---|---|
| 48–82 s | development / tuning | 4/4 | 0 |
| 100–130 s | demo video | 8/9 | 0 |
| 0–44 s | held-out test | 5/5 | 0 |
| **Total** | | **17/18 (94.4%)** | **0** |

The one missed plate is hidden behind another vehicle for almost the whole clip. The previous version read 6/9 in
the demo window and logged the same vehicle twice. Evaluation details and caveats (e.g. the held-out window's first
run had 5 false positives that were fixed afterwards) are in [TEAM_GUIDE.md](TEAM_GUIDE.md) (Chinese).

**Video, scene 2 (generalisation check)** — Malaysian building guardhouse barrier-gate CCTV: oblique close-up
view, vehicles stop at the barrier, lorries / pickup / van / cars / motorcycles. The whole 186 s clip was hand-labelled
before the pipeline first ran on it, and **neither the model nor any threshold was changed for it** — only the frame
crop and the counting line were set:

| Lane | Plates | Correct | False positives |
|---|---|---|---|
| gate lane (incl. one two-row plate) | 5 | **5/5** | **0** |

Plates of vehicles passing in the far lane are only 40–60 px wide and very oblique; a human cannot verify them, so they
are not scored (the system did not log them either). The counting line counted 4 vehicles, which is correct (the last
one is still at the barrier when the clip ends).

![Guardhouse gate](docs/images/alpr_guardhouse.jpg)

**Violation check demo (valid vs flagged plates, 2 min 35 s, 20 vehicles)** — real registration data is not public
and no public footage comes with "these vehicles are in violation", so in the **whole** scene-1 roadside clip the plates
of 7 vehicles were digitally replaced with **fictitious** plates (X-series, which no Malaysian state uses, or a
deliberately malformed one); the other 13 keep their real plates. The replacement is a separate pre-processing step
(`python -m alpr.simulate`, configured in `alpr/simulation.yaml`): the recognition pipeline does not know which
vehicles were altered and has to read and check every plate itself.

| Vehicle | Plate | Verdict | Correct? |
|---|---|---|---|
| 12 real vehicles (incl. 2 two-row plates) | real plates | **VALID** | 12/12 plates right, **0 false alerts** |
| white Honda City | XSA 1001 (fictitious) | expired road tax + type mismatch (registered motorcycle) | ✓ |
| silver Hyundai | XSE 5005 (fictitious) | unmatched (no record) | ✓ |
| dark BMW | XSD 4004 (fictitious) | cloned: seen in Johor Bahru 17 min earlier, 293 km away | ✓ |
| grey Mazda CX-5 | WXY 12345 (five digits) | illegal format | ✓ |
| silver Proton Persona | XSB 2002 (fictitious) | blacklisted (simulated: reported stolen) | ✓ |
| red Iswara + Bezza | both carry XSG 7007 at once | both flagged as cloned | ✓ ✓ |
| **Total** | | **7/7 violations caught, 0 false alerts on real vehicles** | |

One more real vehicle (a Myvi hidden behind a CR-V the whole time) is not read, and the real plate `VCV 155` stays
TENTATIVE (never alerted) because the OCR flips between `VCV155` and `VCV1555`. The video ends with a summary card
listing every vehicle with its plate, type, time and verdict.

![Violation check](docs/images/alpr_violations.jpg)

**Demo videos**:
- **violation check (main demo, 2 min 43 s incl. summary card)**: `demo/malaysia_road_violations_demo.mp4` (1920×1080)
- scene 1 (roadside camera + plate verification alerts): `demo/malaysia_road_anpr_demo_30s.mp4` (1920×1080, 30 s)
- scene 2 (guardhouse gate): `demo/malaysia_guardhouse_demo_30s.mp4` (1920×1080, 30 s)

### Quick start

```bash
pip install -r requirements.txt

# images (file / folder / glob) -> outputs/images_alpr/
python run_image.py --source path/to/images

# video: 30 s CCTV demo + results JSON (download the source clip first, see "Data")
python run_alpr.py --profile malaysia_road_anpr
# violation demo: build the video with fictitious plates, then read + check the whole clip and score it
python -m alpr.simulate
python run_alpr.py --profile malaysia_road_violations
python -m alpr.evaluate --pred outputs/malaysia_road_violations.plates.json --gt alpr/gt/simulated/malaysia_road_violations_0-155s.txt

# plate verification: build the simulated registry (also built automatically when missing),
# look up one plate, check plates found in photos
python -m alpr.registry --make-demo
python -m alpr.registry --check "XSB 2002" --vtype Car
python run_image.py --source path/to/images --check

# evaluation
python -m alpr.eval_images --dataset "../Malaysian Car Plate Dataset" --split val --gt alpr/gt/malaysia_val_plates.csv
python -m alpr.evaluate --pred outputs/malaysia_road_anpr_30s.plates.json --gt alpr/gt/malaysia_road_anpr_100-130s.txt

# unit tests (no models or GPU needed)
python alpr/tests/test_alpr.py
```

### Video pipeline

```mermaid
flowchart LR
    A[Video frame] --> B[YOLOv8 vehicle detection<br/>+ ByteTrack]
    B --> C[Plate detection<br/>inside each vehicle]
    C --> D[Plate OCR<br/>fast-plate-ocr]
    D --> E[Malaysian syntax<br/>constrained decoding]
    E --> F[Weighted multi-frame<br/>voting]
    F --> G[De-duplicated<br/>plate log]
    G --> R[Registry checks<br/>simulated data]
    B --> H[Counting line<br/>traffic flow]
    R --> I[CCTV dashboard / JSON]
    H --> I
```

### Plate verification and alerts (simulated registry)

> **The registry is SIMULATED, not real registration data.** Malaysian (JPJ) registration data is not public. In
> the demo registry built by `python -m alpr.registry --make-demo`, every fictitious vehicle has a plate starting
> with `X` (no state series uses X); the real plates hand-labelled in `alpr/gt/` are neutral placeholders (status
> ok, no road-tax date, no type or colour), so no rule can ever raise an alert against a real vehicle.

| Alert | Meaning |
|---|---|
| `UNMATCHED` | the plate is not in the registry (one character off a registered plate counts as an OCR misread, no alert) |
| `EXPIRED` | road tax expired before the observation time |
| `ILLEGAL_FORMAT` | not a legal Malaysian plate format |
| `BLACKLIST` | registry status is blacklisted (e.g. reported stolen) |
| `TYPE_MISMATCH` | observed vehicle type (optionally body colour) does not fit the record, e.g. a motorcycle's plate on a car |
| `DUPLICATE` | cloned plate suspected: two separate vehicles in view with the same plate, or seen at another camera too far away to have driven here in time |

In the video, a vehicle is checked when it enters the plate log (CONFIRMED); the dashboard shows a red (high) /
orange (medium) badge on the vehicle label and the plate-log row, and an `ALERTS` counter in the top bar. Because
real vehicles are never flagged, the demo adds 4 **simulated events with fictitious plates** at 103 / 110 / 117 /
124 s (purple `SIMULATED EVENT`, no vehicle box in the view): blacklist, expired road tax + type mismatch,
unmatched, and a cloned plate seen at another camera. They are kept apart in the JSON (`simulated_events`).
Details (Chinese): [TEAM_GUIDE.md](TEAM_GUIDE.md), section 4.

### Code layout

| Path | Purpose |
|---|---|
| `run_image.py` / `run_alpr.py` | command-line entry points for images / video |
| `alpr/config.yaml` | every threshold, plus one profile per video |
| `alpr/image.py` | image ALPR |
| `alpr/pipeline.py` | video ALPR: tracking, plate reading, voting, plate log, flow |
| `alpr/ocr.py` | OCR backends (fast-plate-ocr default, EasyOCR fallback) |
| `alpr/plates.py` | Malaysian plate syntax, special series, states, constrained decoding |
| `alpr/voting.py` | multi-frame voting |
| `alpr/flow.py` | traffic-flow counting |
| `alpr/registry.py` | simulated vehicle registry (SQLite), verification rules, demo-registry generator |
| `alpr/simulate.py` / `alpr/simulation.yaml` | builds the violation demo video: replaces chosen vehicles' plates with fictitious ones |
| `alpr/hud.py` | dashboard and AR overlay rendering |
| `alpr/gt/` | hand-verified plate labels (images + video) |
| `models/plate_malaysia.pt` | plate detector fine-tuned on Malaysian plates (default) |
| `models/finetuned.pt` | previous plate detector (Indian-plate data) |

### Models and data

- Vehicles: COCO-pretrained `yolov8s.pt`; OCR: `cct-s-v2-global-model`. Both download automatically on first run.
- Plate detector `plate_malaysia.pt`: `finetuned.pt` trained 40 more epochs on the Malaysian Car Plate Dataset
  training split (414 images).
- The dataset is not in the repository (expected at `../Malaysian Car Plate Dataset`).
- Demo footage: "INTEL AIDA ANPR TESTING" by LILIN CCTV MALAYSIA on YouTube
  (https://www.youtube.com/watch?v=raQOCJ_wzKE). Copyright belongs to its owner; the source clip is not in the
  repository and is used for a course demo only, with attribution. To download:
  `python -m yt_dlp -f 137 -o data/videos/malaysia_lilin_anpr_road.mp4 https://www.youtube.com/watch?v=raQOCJ_wzKE`
- Scene 2 footage: "License plate recognition system for guard house" by LEAF SMART COMMUNITY
  (https://www.youtube.com/watch?v=eNIGPl-W2II); same terms, not in the repository. To download:
  `python -m yt_dlp -f 137 -o data/videos/malaysia_guardhouse_lpr.mp4 https://www.youtube.com/watch?v=eNIGPl-W2II`

### Limitations and next steps

- CPU only: ~3–5 FPS including rendering; real time needs a GPU or an OpenVINO / ONNX export. Plates narrower than ~60 px are still often misread.
- Plate verification uses a simulated registry: run on other windows of the footage, real plates that are not in
  `alpr/gt` show as `UNMATCHED`, which only means "not in the simulated registry".
- **Next**: traffic-flow prediction (from `flow.per_10s`), speeding detection (needs camera calibration first),
  real / authorised registry data and multi-camera sightings, AR/VR display, generative AI (e.g. re-checking
  low-confidence plates, automatic traffic reports).
