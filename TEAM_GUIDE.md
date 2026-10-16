# 软件工程小组项目：马来西亚车牌识别与交通流量监控

本项目针对**马来西亚车牌**：静态图片识别、CCTV 视频中的车辆跟踪 + 车牌识别 + 车流计数。
所有准确率数字都是在**人工逐块核对过车牌文字**的马来西亚数据上测出来的（见下文"评测方法"）。

> 关于旧文档里的"99.9%"：那是检测器在 `archive` 数据集验证集上的 mAP50。`archive` 是**印度车牌**，
> 而且 `valid` 和 `test` 是同一批图片，模型也是在这批图片上挑的最佳轮次，所以这个数字不代表马来西亚车牌的效果。

## 1. 环境配置

Python 3.11 / 3.12。在项目根目录运行：

```bash
pip install -r requirements.txt
```

车牌 OCR 用的是 `fast-plate-ocr`（第一次运行会自动下载约 10 MB 的模型）。如果 pip 把 numpy 升级到了 2.x
导致 torch 报错，执行 `pip install "numpy<2"` 即可（onnxruntime 在 numpy 1.26 下工作正常）。

## 2. 静态图片识别

```bash
python run_image.py --source "../Malaysian Car Plate Dataset/val/images"     # 文件夹 / 单张图片 / 通配符
```

输出到 `outputs/images_alpr/`：每张图的标注图（绿框 = 合法马来西亚车牌，橙框 = 非标准文字）和
`results.json`（车牌号、是否合法格式、州属/类别、OCR 和检测置信度）。

在带人工标注的数据集上评测：

```bash
python -m alpr.eval_images --dataset "../Malaysian Car Plate Dataset" --split val --gt alpr/gt/malaysia_val_plates.csv --save outputs/eval/val
```

马来西亚车牌数据集验证集（89 张图，94 块车牌；车牌文字是人工逐块核对的，见 `alpr/gt/malaysia_val_plates.csv`）：

| 方案 | 检测召回 | 端到端完全正确 | 双排车牌 | 特殊号牌 |
|---|---|---|---|---|
| 原代码：印度数据训练的检测器 + EasyOCR | 82.1% | 41.5% | — | — |
| 印度检测器 + 新 OCR（fast-plate-ocr + 马来西亚语法约束解码） | 82.1% | 79.8% | 12.5% | 83.3% |
| **马来西亚检测器 + 新 OCR（当前默认）** | **98.9%** | **94.7%** | **100%** | **100%** |

- 剩下的 5 块错误中：2 块是宽度不到 60 像素的模糊小车牌，1 块是自定义字体，1 块 J/O 混淆，1 块漏检。
- 检测"误报"13 个，其中 11 个是**背景车辆上真实存在但数据集没有标注的车牌**（可在 `outputs/eval/val_final/` 里看到），
  真正的误检只有 2 个（停车贴纸、广告牌电话号码，后者已被标记为"非标准"）。
- 注意：这个验证集在开发 OCR 时用过（例如选 OCR 模型），所以这是开发集成绩；视频的保留窗口才是干净的测试。

## 3. 视频车牌识别 + 车流统计

```bash
python run_alpr.py --profile malaysia_road_anpr                     # 默认 100-130 s，输出演示视频
python run_alpr.py --profile malaysia_road_anpr --start-sec 0 --duration 0   # 整段视频
python -m alpr.evaluate --pred outputs/malaysia_road_anpr_30s.plates.json --gt alpr/gt/malaysia_road_anpr_100-130s.txt
```

演示素材：LILIN CCTV MALAYSIA 的 "INTEL AIDA ANPR TESTING"（youtube.com/watch?v=raQOCJ_wzKE，路边固定 ANPR 摄像头）。
该视频是厂商软件界面的录屏，`frame_crop` 只保留摄像头画面（裁掉厂商自己的车牌列表）。仅用于课程演示，请注明来源。

**演示视频：`demo/malaysia_road_anpr_demo_30s.mp4`**（1920×1080，H.264，30 秒，100–130 s）。界面：左侧摄像头画面
（车辆框 + 车牌框 + 实时读数，绿 = 已确认 / 合格，黄 = 识别中），右侧车牌日志（每辆车一行：车牌、置信度、读数次数、
州属、车型、最清晰的车牌截图），顶部时间 / 过线计数 / 每分钟流量 / 告警数，底部分车型计数。
车牌核验告警（红/橙徽章）和紫色的 `SIMULATED EVENT` 模拟事件见第 4 节。

三个时间窗口的车牌都是人工逐块核对的（`alpr/gt/malaysia_road_anpr_*.txt`）：

| 窗口 | 用途 | 车牌 | 正确 | 误报 |
|---|---|---|---|---|
| 48–82 s | 开发（调参用） | 4 | 4 | 0 |
| 100–130 s | 演示视频（诊断问题时看过） | 9 | 8 | 0 |
| 0–44 s | 保留测试（标注完才第一次运行） | 5 | 5 | 0 * |
| **合计** | | **18** | **17（94.4%）** | **0** |

\* 保留窗口第一次运行：5/5 正确，但多报了 5 条（4 条低置信度 TENTATIVE + 1 条同车碎片轨迹的误读）。
之后加了"只记录 CONFIRMED"和"近似车牌合并"两条规则才变成 0 误报，所以这个 0 已经不是完全干净的测试结果。

改进前（原视频管线 + 新 OCR）在演示窗口是 6/9，而且同一辆车被重复记录。唯一没读出的 `W 6954 F`
在 106–111 s 全程被一辆 CR-V 挡住，只在画面边缘露出不到 1 秒。

视频管线做了哪些改进（细节见 `alpr/pipeline.py` 顶部注释）：
1. **跟踪**：类别无关 NMS（皮卡同时被检测成 car 和 truck，曾经把一辆车拆成 17 条轨迹）；低分框交给 ByteTrack 做二次关联；
   轨迹缓冲 2 秒（树木遮挡）；车型和置信度门限按整条轨迹统计。
2. **双排车牌**：马来西亚检测器 + 长宽比下限 0.8。
3. **候选框用 OCR 验证**：车牌下方的黑色格栅会被检测成"双排车牌"，多个重叠候选框时选读出合法车牌号的那个。
4. **投票**：权重 = OCR 置信度 × 质量 × (车牌宽度/100px)²，远处模糊的读数（错得还很一致）不会压过近处清晰的读数；
   低分辨率读数限 15 次，全分辨率读数不限次数；**每个字符都要有 ≥60% 的多数**才算 CONFIRMED。
5. **车牌日志**：一辆车一行，只记录 CONFIRMED；10 秒内相同或只差 ≤2 个字符的车牌视为同一辆车（重新捕获）。
6. **车流统计**（`alpr/flow.py`）：计数线、分车型计数、每分钟流量、每 10 秒时间序列（写在 `*.plates.json` 的 `flow` 字段），
   可以直接作为后续车流预测模型的训练数据。

速度：纯 CPU 约 3–5 FPS（含界面渲染；车辆检测 ~140 ms/帧，车牌检测批量 ~35 ms/辆，界面 ~60 ms/帧，OCR 很少）。
演示视频是离线渲染的，播放是正常的 30 FPS；要实时运行需要 GPU，或把 YOLO 导出为 OpenVINO / ONNX。

### 场景二：门岗闸口（验证模型能否用于不止一个场景）

```bash
python run_alpr.py --profile malaysia_guardhouse                              # 30 秒演示（66–96 s）
python run_alpr.py --profile malaysia_guardhouse --start-sec 0 --duration 0   # 整段 186 秒
python -m alpr.evaluate --pred outputs/malaysia_guardhouse_30s.plates.json --gt alpr/gt/malaysia_guardhouse_0-186s.txt
```

素材：LEAF SMART COMMUNITY 的 "License plate recognition system for guard house"（youtube.com/watch?v=eNIGPl-W2II），
马来西亚大楼门岗闸口的固定 CCTV。和场景一的区别：斜前方近距离视角、车辆在闸口前停下（停留 20–40 秒）再通过、
车型以货车 / 皮卡 / 面包车为主、旁边有保安和摩托车。下载：
`python -m yt_dlp -f 137 -o data/videos/malaysia_guardhouse_lpr.mp4 https://www.youtube.com/watch?v=eNIGPl-W2II`

做法：**先标注、后运行**。整段 186 秒的车牌在模型第一次运行前就人工核对完（`alpr/gt/malaysia_guardhouse_0-186s.txt`）；
模型和所有阈值都和场景一相同，配置档 `malaysia_guardhouse` 只设置了场景几何：
`frame_crop` 裁掉顶部 14.5%（厂商系统叠加的车牌文字和 logo，否则会被我们的 OCR 读到），`count_line_y` 计数线。

| 车道 | 车牌 | 正确 | 误报 |
|---|---|---|---|
| 闸口车道：`JKM 810`、`JPP 3473`、`JMT 8577`（双排）、`JME 5737`、`JMG 4910` | 5 | **5/5（第一次运行）** | **0** |

- 另一条车道经过的车辆车牌只有 40–60 像素宽、角度很斜，人眼无法核对，在 GT 里标为 `?`；系统对它们只显示黄色的
  "待确认"读数（例如把一辆 Myvi 读成 `SSY 110`），没有一辆进入车牌日志。
- 过线计数 4 辆，和实际一致（最后一辆 `JMG 4910` 到视频结束时还停在闸口）。
- COCO 车辆检测把皮卡和面包车归为 truck，所以日志里它们的车型显示为 Truck。
- 这一段**没有开启登记库核验**：画面里全是真实车辆，开启后读错或未收录的车牌会显示"未登记"。
- 渲染时修了一个界面问题：多辆车靠近画面顶部时标签会叠在一起，现在会自动错开（两段演示视频都已重新渲染）。

### 违规识别演示：合格车牌与违规车牌对比（整段 2 分 35 秒）

```bash
python -m alpr.simulate                                  # 生成 data/videos/malaysia_road_violations_SIMULATED.mp4（约 25 分钟）
python run_alpr.py --profile malaysia_road_violations    # 识别 + 核验整段视频，结尾附 8 秒汇总表
python -m alpr.evaluate --pred outputs/malaysia_road_violations.plates.json --gt alpr/gt/simulated/malaysia_road_violations_0-155s.txt
```

**为什么要替换车牌**：马来西亚的车辆登记数据不公开，网上也没有"已知哪些车违规"的真实视频；就算画面里真有违规车，
我们也无法核实，更不能在公开仓库里把真实车辆标成违规。所以在场景一的整段视频里，选 7 辆车，把它们的车牌用数字方式
换成虚构车牌（X 字头或故意做成非法格式），它们的记录放在模拟登记库里；其余 13 辆保留真实车牌，应全部判为合格。
画面左上角的紫色说明条一直提示"7 辆车的车牌是数字插入的虚构车牌"。

**替换怎么做的**（`alpr/simulate.py`，和识别管线完全分开，识别时不知道哪些车被换过）：
1. 第一遍：用和管线相同的模型跟踪车辆、检测并读出车牌，按真实车牌找到要替换的车（一辆车常被拆成几段轨迹，只差 2 个
   字符以内的都算；同一时间出现的两段轨迹只保留读数多的那段，避免把从前面开过的车当成目标车）。
2. 第二遍：每一帧在车辆框里重新检测车牌，选读数最接近真实车牌的那块，换成渲染好的马来西亚样式车牌，并匹配原车牌的
   亮度、模糊程度和噪声。OCR 很确定读到的是"另一块合法车牌"时不贴（那是旁边别的车）；车牌被挡住时也不贴。

**结果**（标注在 `alpr/gt/simulated/`，每行车牌后面用 `! 告警代码` 写明应该触发的告警，`alpr.evaluate` 会逐车核对）：

| 车辆 | 车牌 | 应报告警 | 结果 |
|---|---|---|---|
| 12 辆真实车辆 | 真实车牌 | 无 | 车牌 12/12 正确（其中 `VCV 155` 只是"待确认"，见下），**0 误报** |
| 白色本田 City | XSA 1001 | EXPIRED + TYPE_MISMATCH | ✓ |
| 银色现代 | XSE 5005 | UNMATCHED | ✓ |
| 深色宝马 | XSD 4004 | DUPLICATE（17 分钟前在新山出现，模拟跨摄像头记录） | ✓ |
| 灰色马自达 CX-5 | WXY 12345 | ILLEGAL_FORMAT | ✓ |
| 银色 Proton Persona | XSB 2002 | BLACKLIST | ✓ |
| 红色 Iswara、Bezza | 两辆同时挂 XSG 7007 | 两辆都 DUPLICATE | ✓ ✓ |

**7/7 违规全部抓到，真实车辆 0 误报。** 另外：被 CR-V 全程挡住的 Myvi `W 6954 F` 没有读出（和场景一相同）；
`VCV 155` 在车牌完全清晰时 OCR 仍在 `VCV155` / `VCV1555` 之间各占一半地摇摆，所以只给"待确认"，不进入核验。

做这个演示时修了 3 个真实存在的问题（都有单元测试）：
1. **非法车牌被"修正"成合法车牌**：OCR 很有把握地读出 5 位数字的 `WXY 12345`，约束解码却把第 5 位当成空位，变成了
   合法的 `WXY 1234`，等于放过了违规。现在解码时不允许删掉（或凭空加上）OCR 有把握的字符；需要改变长度才能合法的车牌
   判为非标准格式，多帧高分辨率读数一致时报 `ILLEGAL_FORMAT`。
2. **套牌被合并成一辆车**：第二辆车刚确认车牌时停在第一辆车后面，两个框重叠，被当成"同一辆车重新出现"；之后两车分开
   行驶 6 秒也无法撤销。现在每一帧都记录哪些车同时出现、互不重叠；同一行日志里的两条轨迹一旦被确认是分开的两辆车，
   后出现的那辆会拆成单独一行，两行都报套牌。
3. **非法车牌的州属**：之前按截短后的车牌显示 "Kuala Lumpur"，现在显示"非标准格式"。

以上 3 处改动后重新跑了所有真实视频：场景一三个窗口（5/5、4/4、8/9）、场景二（5/5）、静态图片（94.7%）都和之前完全一样，
真实车辆没有任何告警。

## 4. 车牌核验与告警（模拟登记库）

> **登记库是模拟的（SIMULATED），不是真实登记数据。** 马来西亚 JPJ 的车辆登记数据不公开，所以
> `data/registry/demo_registry.db` 是脚本生成的演示库（SQLite，不进 git）。因为仓库是公开的：
> - 虚构车辆的车牌**全部以 `X` 开头**（马来西亚没有任何州使用 X 字头），不会对应到真实车辆；
> - 演示视频和数据集里人工标注过的**真实车牌**（`alpr/gt/` 里的 112 块）在库里只是**中性占位记录**：
>   状态 ok、没有路税日期、没有车型和颜色，任何规则都不可能对它们报警（单元测试
>   `test_demo_registry_never_flags_a_real_plate` 逐块检查）。

```bash
python -m alpr.registry --make-demo                        # 生成 / 重建模拟登记库（文件缺失时管线会自动生成）
python -m alpr.registry --check "XSB 2002" --vtype Car     # 查询单个车牌
python run_alpr.py --profile malaysia_road_anpr            # 视频：演示 profile 已开启核验
python run_image.py --source photos/ --check               # 图片：加 --check 开启核验（车型未知，不查 TYPE_MISMATCH）
```

六条规则（`alpr/registry.py`，纯函数，有单元测试）：

| 告警 | 含义 | 颜色 |
|---|---|---|
| `UNMATCHED` | 登记库里查不到这个车牌；如果和某个已登记车牌只差 1 个字符，视为 OCR 误读，不报警（`registry_misread_tolerance`） | 橙 |
| `EXPIRED` | 路税（road tax）在观测时间之前已过期 | 橙 |
| `ILLEGAL_FORMAT` | 不是合法的马来西亚车牌格式（`plates.parse_plate` / `OcrRead.valid`） | 橙 |
| `BLACKLIST` | 登记状态为黑名单（例如报失窃） | 红 |
| `TYPE_MISMATCH` | 观测车型和登记车型不符（例如登记为摩托车的车牌装在轿车上）；可选比较 HSV 粗估的车身颜色（`registry_check_colour`，默认关，受光照影响大） | 橙 |
| `DUPLICATE` | 套牌嫌疑：同一车牌同时出现在画面里两辆分开的车上（连续 ≥5 帧），或在另一个摄像头出现、两地距离在时间差内不可能开到（隐含速度 > `registry_max_speed_kmh`，默认 250 km/h） | 红 |

- **视频**：车辆进入车牌日志（CONFIRMED）时核验，之后只在 CONFIRMED 读数 / 车型变化时重新核验，
  TENTATIVE 的临时误读不会触发或清除告警。告警写进 `*.plates.json`（每行车牌的 `alerts`，以及 `registry` 汇总），
  并显示在 CCTV 仪表盘上：车辆标签上方的红/橙徽章、车牌日志里的告警行（代码 + 说明）、顶部栏的 `ALERTS` 计数。
- 画面中**同时出现、互不重叠**的两辆车不再被合并成一行日志（以前 10 秒内相近的车牌会被当成同一辆车合并）；
  车牌完全相同就是 `DUPLICATE`。
- **误读容忍**：演示视频里 VCH 1587 曾被短暂确认成 VCR 1587、VN 1683 被确认成 VW 1683，如果直接查库就会对真实车辆报
  `UNMATCHED`。所以和已登记车牌只差 1 个字符的读数当作误读处理（不报警，也不套用那条记录的其他规则）；模拟库里的
  虚构车牌和所有真实车牌之间至少差 2 个字符，误读不会落到虚构记录上。
- 车型匹配是宽松的：SUV / MPV / 皮卡 / 面包车常被检测成 truck，Car 和 Truck 都算符合；
  有意义的是"摩托车牌装在汽车上"这类不符。

**演示中的模拟事件**：真实车辆不会被报警，所以演示 profile 用 `registry_sim_events` 在指定秒数往车牌日志里加入
4 条**虚构车牌**的模拟事件。它们在仪表盘上用紫色 `SIMULATED EVENT` 标注（画面里没有对应的车辆框），在 JSON 里
单独放在 `simulated_events`，不混进摄像头的识别结果，也不影响 `alpr.evaluate` 的评测。

| 时间 | 车牌（虚构） | 告警 |
|---|---|---|
| 103 s | XSB 2002 | BLACKLIST（模拟：报失窃） |
| 110 s | XSA 1001 | EXPIRED（路税 2026-03-31 到期）+ TYPE_MISMATCH（登记为摩托车） |
| 117 s | XSE 5005 | UNMATCHED（库里没有） |
| 124 s | XSD 4004 | DUPLICATE（17 分钟前在新山 Johor Bahru 出现过，相距约 290 km） |

注意：
- 在其他时间窗口（例如整段视频）运行时，`alpr/gt` 之外的真实车牌在模拟库里查不到，会显示 `UNMATCHED`——
  这只表示"不在模拟库里"，**不代表车辆未登记**；错 2 个字符以上的 OCR 误读也会表现为 `UNMATCHED`。
  这类输出不要公开发布。
- `video_start_time`（2026-10-14 08:00）和 `camera_latlon`（吉隆坡）是演示用的名义时间和坐标，
  用于路税到期和跨摄像头行驶时间的计算。
- 配置项都在 `alpr/config.yaml` 的 `registry_*` 下：`registry_enabled`、`registry_path`、`registry_rules`
  （可以关掉某条规则）、`registry_check_colour`、`registry_max_speed_kmh`、`registry_misread_tolerance`、
  `registry_sim_events`。

## 5. 代码结构（`alpr/`）

| 文件 | 作用 |
|---|---|
| `config.yaml` / `config.py` | 所有阈值和每段视频的配置（profile）；`image` 是静态图片配置 |
| `image.py` | 静态图片：整图车牌检测 → 裁剪 → OCR → 马来西亚语法 |
| `pipeline.py` | 视频：车辆检测 + ByteTrack 跟踪 → 车牌检测 → OCR → 多帧投票 → 车牌日志 → 车流计数 |
| `ocr.py` | OCR 后端：`fast_plate_ocr`（默认）/ `easyocr`（备用） |
| `plates.py` | 马来西亚车牌语法：标准格式、特殊号牌（GOLD、VIP、1M4U、PUTRAJAYA…）、州属、约束解码 |
| `voting.py` | 同一辆车多帧读数的加权投票（近处清晰的读数权重大） |
| `flow.py` | 计数线车流统计（分车型、每分钟流量、每 10 秒时间序列，供后续车流预测使用） |
| `registry.py` | 模拟登记库（SQLite）+ 六条核验规则 + 模拟库生成脚本（`python -m alpr.registry --make-demo`） |
| `simulate.py` / `simulation.yaml` | 生成违规演示视频：把指定车辆的车牌替换成虚构车牌（`python -m alpr.simulate`） |
| `hud.py` | 界面渲染：CCTV 仪表盘（`dashboard`）、行车记录仪 AR 卡片（`lead`）、结尾汇总表、车牌徽章；苹果风格（见第 8 节） |
| `dataset.py` | 读取 YOLO 标注（多边形/矩形），生成单类车牌训练集 |
| `eval_images.py` / `evaluate.py` | 图片 / 视频评测脚本 |
| `gt/` | 人工核对的车牌文字标注 |
| `tests/test_alpr.py` | 单元测试（`python alpr/tests/test_alpr.py`，不需要模型和 GPU） |

## 6. 评测方法与已知局限

- **GT 是人工核对的**：每块车牌都是放大图片后逐字读出来的；看不清的车辆在视频 GT 里用 `?` 标记（不算命中也不算误报）。
- **静态图片**：只有验证集有文字标注；训练集（414 张）已用于训练马来西亚检测器（`models/plate_malaysia.pt`，
  从 `finetuned.pt` 继续训练 40 轮，固定使用最后一轮 `last.pt`，没有在验证集上挑轮次）。复现训练：
  `python -m alpr.dataset --src "../Malaysian Car Plate Dataset" --dst data/malaysia_plates_yolo`，然后用 ultralytics 训练。
- **已知局限**：宽度 < 60 px 的小车牌仍容易读错；个别车牌（如 `VCV 155`）即使清晰，OCR 也会在两种读法间摇摆，只能给出"待确认"；
  H/R、M/W 这类形近字在阴影下可能短暂误读（会在约 1 秒内被纠正）；
  不支持外交 / 车商临时牌；纯 CPU 达不到实时。
- **超速检测**还没做：需要先做相机标定（像素 → 米），否则测出来的速度不可信。

## 7. 下一步建议

1. 车流预测：用 `flow.per_10s` 时间序列（跑整段或更长的视频）训练时间序列模型。
2. 超速：标定两条参考线的实际距离 + 轨迹时间戳。
3. 车牌核验：规则和模拟库已完成（第 4 节）；如果拿到真实 / 授权的登记数据，只需换 `registry_path`。
   多摄像头部署时，可以把每次识别写进 `sightings` 表，`DUPLICATE` 的跨摄像头检查就能用真实数据。
4. AR / VR：`hud.py` 的 `lead` 模式已经是 AR 卡片风格，可以接到眼镜视角上。
5. 生成式 AI：例如用 VLM 复核低置信度车牌，或自动生成交通报告。

## 8. 界面设计与代码优化（2026-10-15）

**界面（`alpr/hud.py`，参考苹果深色模式的设计）**：
- 摄像头画面做成圆角卡片，浮在"氛围背景"上（当前画面经过重度模糊和压暗），卡片下有柔和阴影；侧栏和标签是毛玻璃。
- 配色使用苹果系统色：绿 = 合格 / 已确认，黄 = 识别中，灰 = 搜索车牌，橙 = 告警，红 = 严重告警，紫 = 模拟事件；颜色只用在
  状态点、边框和告警胶囊上，大面积保持中性色。
- 文字用 Segoe UI Variable（Windows 11 上最接近 SF Pro 的字体），正常大小写；告警用自然语言（"Road tax expired"、
  "Cloned plate"），JSON 里仍保留告警代码。
- 车牌显示成真实车牌的样子（黑底白字徽章，Arial Narrow Bold），旁边是摄像头拍到的车牌截图，方便人工对照。
- 还没读到车牌的远处车辆只画淡淡的轮廓，不挂标签，减少画面杂乱。
- 所有圆角和细线都是抗锯齿的（4 倍超采样的蒙版，按尺寸缓存）；静态图片标注、行车记录仪界面、结尾汇总表是同一套风格。

**代码优化**（改动后所有视频窗口和静态图片的识别结果与改动前完全一致，56 个单元测试全部通过）：

| 问题 | 改进 |
|---|---|
| 每辆车单独调用一次车牌检测模型 | 一帧里所有车辆一起批量检测，车牌检测快约 35% |
| 先用 OpenCV 写 MPEG-4，再用 ffmpeg 转一次 H.264 | 直接一次编码成 H.264（更快、少一次画质损失） |
| 车辆对的"同时出现"记录保存所有帧号，每次更新还要遍历全部车辆对 | 改成计数 + 按车辆的索引，长视频内存不再增长、查询 O(1) |
| 画面上的 FPS 不含界面渲染时间 | 改成真实帧率（检测 + 渲染 + 读写视频） |
| 车牌检测没有排除水印区域（行车记录仪视频里，车牌框落在了 "MALAYSIA" 水印上） | `ignore_zones` 同时作用于车牌 |
| 模拟登记库要手动改版本号才会重建，新标注的真实车牌可能被误报"未登记" | 按标注内容自动生成指纹，内容一变就自动重建 |
| 视频刚开始时"每分钟流量"被放大（3 秒过 1 辆 = 20 辆/分钟） | 统计时长至少 15 秒 |
| 新界面最初每帧 500 ms | 边框只混合边缘带、背景和阴影在 1/4 分辨率计算、改用 `cv2.blendLinear`，降到约 60 ms |

整体速度：整段 155 秒违规视频的处理时间从 1395 秒降到 1047 秒（3.3 → 4.4 FPS），新界面虽然更复杂，总体仍快了约 25%。

**还可以继续改进（没有在这次做）**：
- 车辆检测（YOLOv8s，约 140 ms/帧）是 CPU 上最大的瓶颈；导出为 OpenVINO（Intel CPU）或用 GPU 可以接近实时，但要新增依赖。
- `voting.py` 里的 `resolve_ligatures`（W↔VJ 连笔修正）是给 EasyOCR 用的旧逻辑，默认已关闭，可以删除。
- 如果要跑几个小时的直播流，还应该定期清理已经离开画面的车辆的跟踪状态（目前只对视频片段做过测试）。
