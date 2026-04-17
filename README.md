# 字幕烧录工具

Whisper 自动转写 + 一键烧录字幕到视频。支持 Web 界面和命令行两种方式。

## 功能

- **自动转写**：faster-whisper 语音识别，自动断句
- **手动校对**：Web 界面逐句编辑，自动去标点
- **字幕样式**：可调字号、背景透明度、垂直位置
- **双引擎烧录**：短视频用 PNG overlay，长视频用 ASS + libass

## 依赖

- Python 3.9+
- ffmpeg（系统安装）

```bash
pip install -r requirements.txt
```

## 使用方式

### Web 界面（推荐）

```bash
python3 server.py
# 浏览器打开 index.html
```

选择视频 → 自动转写 → 校对修改 → 点击烧录。输出文件在原视频同目录，文件名加 `_字幕` 后缀。

### 命令行

```bash
# 转写 + 烧录
python3 burn_subtitle.py video.mp4

# 只预览字幕（不烧录）
python3 burn_subtitle.py video.mp4 --preview

# 从 SRT 文件烧录（跳过转写）
python3 burn_subtitle.py video.mp4 --from-srt video.srt

# 指定输出路径
python3 burn_subtitle.py video.mp4 output.mp4
```
