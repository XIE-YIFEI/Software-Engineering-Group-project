# 软件工程小组项目：车牌检测与交通流量监控 (阶段一)

欢迎加入本项目！这是我们初步跑通的核心基础框架，主要实现了**高精度车牌检测**。
目前的专属微调模型 (`finetuned.pt`) 已经在测试集上达到了 **99.9%** 的准确率！

## 🚀 快速上手指南

### 1. 下载代码
你可以直接点击网页上的 `Code -> Download ZIP`，或者使用 Git 克隆到本地：
```bash
git clone https://github.com/XIE-YIFEI/Software-Engineering-Group-project.git
```

### 2. 环境配置
请确保你的电脑安装了 **Python 3.11 或 3.12**。
打开终端，进入项目文件夹，运行以下命令安装必要的依赖库：
```bash
pip install ultralytics supervision opencv-python numpy PyYAML requests
```

### 3. 如何运行测试
我们已经把训练好的超高精度模型（`models/finetuned.pt`）包含在仓库里了。
配置文件 `config.yaml` 也已经默认设置好了使用这个模型。

你只需要在终端中运行：
```bash
python run.py
```

### 4. 验收成果
运行结束后，代码会自动生成检测结果！
请打开 `outputs` 文件夹，双击打开 **`results.html`**。
你可以在浏览器里直观地看到模型是如何精准框出每一张测试图片里的车牌的。

---
**💡 组内下一步开发计划预告：**
1. **接入 OCR 识别**：不仅要框出车牌，还要把车牌号码转换成文字文本。
2. **视频追踪 (ByteTrack)**：从单张图片检测升级到处理视频，统计车流。
3. **AR 功能整合**：将现有的检测结果映射到 AR 视图中。
