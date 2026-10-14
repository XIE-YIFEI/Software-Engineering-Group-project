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
（车辆框 + 车牌框 + 实时读数，绿 = CONFIRMED，黄 = TENTATIVE），右侧车牌日志（每辆车一行：车牌、置信度、读数次数、
州属、车型、最清晰的车牌截图），顶部时间 / 过线计数 / 每分钟流量，底部分车型计数。

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

速度：纯 CPU 约 3–7 FPS（车辆检测 ~140 ms/帧 + 车牌检测 ~90 ms/次，OCR 只占 ~3%）。演示视频是离线渲染的，
播放是正常的 30 FPS；要实时运行需要 GPU，或把 YOLO 导出为 ONNX/OpenVINO。

## 4. 代码结构（`alpr/`）

| 文件 | 作用 |
|---|---|
| `config.yaml` / `config.py` | 所有阈值和每段视频的配置（profile）；`image` 是静态图片配置 |
| `image.py` | 静态图片：整图车牌检测 → 裁剪 → OCR → 马来西亚语法 |
| `pipeline.py` | 视频：车辆检测 + ByteTrack 跟踪 → 车牌检测 → OCR → 多帧投票 → 车牌日志 → 车流计数 |
| `ocr.py` | OCR 后端：`fast_plate_ocr`（默认）/ `easyocr`（备用） |
| `plates.py` | 马来西亚车牌语法：标准格式、特殊号牌（GOLD、VIP、1M4U、PUTRAJAYA…）、州属、约束解码 |
| `voting.py` | 同一辆车多帧读数的加权投票（近处清晰的读数权重大） |
| `flow.py` | 计数线车流统计（分车型、每分钟流量、每 10 秒时间序列，供后续车流预测使用） |
| `hud.py` | 两种界面：行车记录仪 AR 卡片（`lead`）和 CCTV 仪表盘（`dashboard`） |
| `dataset.py` | 读取 YOLO 标注（多边形/矩形），生成单类车牌训练集 |
| `eval_images.py` / `evaluate.py` | 图片 / 视频评测脚本 |
| `gt/` | 人工核对的车牌文字标注 |
| `tests/test_alpr.py` | 单元测试（`python alpr/tests/test_alpr.py`，不需要模型和 GPU） |

## 5. 评测方法与已知局限

- **GT 是人工核对的**：每块车牌都是放大图片后逐字读出来的；看不清的车辆在视频 GT 里用 `?` 标记（不算命中也不算误报）。
- **静态图片**：只有验证集有文字标注；训练集（414 张）已用于训练马来西亚检测器（`models/plate_malaysia.pt`，
  从 `finetuned.pt` 继续训练 40 轮，固定使用最后一轮 `last.pt`，没有在验证集上挑轮次）。复现训练：
  `python -m alpr.dataset --src "../Malaysian Car Plate Dataset" --dst data/malaysia_plates_yolo`，然后用 ultralytics 训练。
- **已知局限**：宽度 < 60 px 的小车牌仍容易读错；H/R、M/W 这类形近字在阴影下可能短暂误读（会在约 1 秒内被纠正）；
  不支持外交 / 车商临时牌；纯 CPU 达不到实时。
- **超速检测**还没做：需要先做相机标定（像素 → 米），否则测出来的速度不可信。

## 6. 下一步建议

1. 车流预测：用 `flow.per_10s` 时间序列（跑整段或更长的视频）训练时间序列模型。
2. 超速：标定两条参考线的实际距离 + 轨迹时间戳。
3. 车牌核验：接入车牌登记数据（过期 / 未登记 / 重复车牌）；`plates.py` 已经能判断格式是否合法和所属州属。
4. AR / VR：`hud.py` 的 `lead` 模式已经是 AR 卡片风格，可以接到眼镜视角上。
5. 生成式 AI：例如用 VLM 复核低置信度车牌，或自动生成交通报告。
