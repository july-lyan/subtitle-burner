#!/usr/bin/env python3
"""
独立字幕烧录脚本
用法: python3 burn_subtitle.py <视频文件> [输出文件]

流程:
1. Whisper 转写音频 → 字幕列表
2. MoviePy + PIL 烧录字幕到视频
"""

import os
import sys
import subprocess
import tempfile
import shutil


# ─── 字幕样式配置 ───────────────────────────────────────────
FONT_SIZE = 48
MAX_CHARS = 16      # 每行最多字符数
SUB_Y = 1650        # 字幕 Y 坐标（1080x1920 竖屏，偏下方）
FONT_COLOR = "#FFFFFF"
BG_ALPHA = 70       # 背景透明度 0-100
WHISPER_MODEL = "medium"
# ────────────────────────────────────────────────────────────


def transcribe(video_path):
    """Whisper 转写，返回 segments 列表 [{start, end, text}, ...]"""
    print("📝 提取音频...")
    tmpdir = tempfile.mkdtemp(prefix="burn_sub_")
    audio_path = os.path.join(tmpdir, "audio.wav")

    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        audio_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RuntimeError(f"音频提取失败: {result.stderr[-300:]}")

    print(f"🎤 Whisper 转写中（faster-whisper {WHISPER_MODEL}）...")
    from faster_whisper import WhisperModel
    PROMPT = "以下是一段中文口播视频，内容关于AI、科技和创业。常见词汇：MCP、Anthropic、Claude、Vibecoding、API、AI、大模型、小红书、公众号、USB-C、协议、工具调用、智能体、agent、产品经理、一人公司。"
    model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    fw_segs, _ = model.transcribe(audio_path, language="zh",
        initial_prompt=PROMPT, vad_filter=True)

    segments = []
    for seg in fw_segs:   # 先消费生成器，再删音频
        text = seg.text.strip()
        if text:
            segments.append({
                "start": seg.start,
                "end": seg.end,
                "text": text,
            })

    print(f"✅ 转写完成，共 {len(segments)} 句")
    return segments


def _segments_to_ass(segments, vw, vh):
    """将字幕 segments 转换为 ASS 格式字符串"""
    def fmt_ts(sec):
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = int(sec % 60)
        cs = int(round((sec - int(sec)) * 100))
        return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

    def wrap_text(text):
        lines = []
        while len(text) > 20:
            lines.append(text[:20])
            text = text[20:]
        if text:
            lines.append(text)
        return r"\N".join(lines)

    # BG_ALPHA=70 表示70%不透明，ASS alpha字节：0x00=不透明，0xFF=全透明
    # 70%不透明 → alpha = int((1 - 0.70) * 255) = 77 ≈ 0x4D
    bg_alpha_hex = format(int((1 - BG_ALPHA / 100) * 255), "02X")
    # ASS颜色格式 &HAABBGGRR（alpha, blue, green, red）
    # 白色文字: &H00FFFFFF  黑色描边: &H00000000  半透明黑色背景: &H{bg}000000
    margin_v = vh - SUB_Y  # 从底部算

    # BorderStyle=3: 不透明背景框（BackColour填充）
    # Outline=2: 描边宽度  Alignment=2: 底部居中
    ass_content = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {vw}
PlayResY: {vh}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial Unicode MS,{FONT_SIZE},&H00FFFFFF,&H000000FF,&H00000000,&H{bg_alpha_hex}000000,0,0,0,0,100,100,0,0,3,2,0,2,20,20,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    dialogue_lines = []
    for seg in segments:
        text = wrap_text(seg["text"])
        dialogue_lines.append(
            f"Dialogue: 0,{fmt_ts(seg['start'])},{fmt_ts(seg['end'])},Default,,0,0,0,,{text}"
        )

    return ass_content + "\n".join(dialogue_lines) + "\n"


def burn(video_path, output_path, segments):
    """烧录字幕到视频
    短视频（<=100句）：MoviePy + PIL（兼容性最好）
    长视频（>100句）：ASS + imageio ffmpeg libass（避免 pthread 线程上限）
    """
    LONG_VIDEO_THRESHOLD = 100
    if len(segments) > LONG_VIDEO_THRESHOLD:
        print(f"🔥 烧录字幕中（ASS 模式，共 {len(segments)} 句）...")
        burn_with_ass(video_path, output_path, segments)
    else:
        print(f"🔥 烧录字幕中（MoviePy 模式，共 {len(segments)} 句）...")
        burn_with_moviepy(video_path, output_path, segments)


def burn_with_ass(video_path, output_path, segments):
    """长视频方案：ASS 字幕 + imageio ffmpeg libass，一条命令烧录"""
    # imageio 捆绑的 ffmpeg 有 libass
    imageio_ffmpeg = os.path.join(
        os.path.expanduser("~/Library/Python/3.9/lib/python/site-packages"),
        "imageio_ffmpeg", "binaries", "ffmpeg-macos-aarch64-v7.1"
    )
    if not os.path.exists(imageio_ffmpeg):
        raise RuntimeError(f"找不到 imageio ffmpeg: {imageio_ffmpeg}")

    # 探测视频分辨率
    probe_cmd = [
        "ffprobe", "-v", "quiet", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "csv=p=0", video_path
    ]
    probe_result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=10)
    if probe_result.returncode == 0 and probe_result.stdout.strip():
        vw, vh = [int(x) for x in probe_result.stdout.strip().split(",")]
    else:
        vw, vh = 1080, 1920

    # 生成 ASS 字幕文件
    ass_content = _segments_to_ass(segments, vw, vh)
    tmpdir = tempfile.mkdtemp(prefix="burn_ass_")
    try:
        ass_path = os.path.join(tmpdir, "subtitles.ass")
        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(ass_content)

        # ass 路径特殊字符转义
        ass_escaped = ass_path.replace(":", "\\:").replace("'", "\\'")
        cmd = [
            imageio_ffmpeg, "-y",
            "-i", video_path,
            "-vf", f"ass={ass_escaped}",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac",
            "-movflags", "+faststart",
            output_path,
        ]
        print(f"  分辨率: {vw}x{vh}, 字幕数: {len(segments)}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
        if result.returncode != 0:
            raise RuntimeError(f"ASS 烧录失败: {result.stderr[-500:]}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"✅ 完成！输出: {output_path}")


def burn_with_moviepy(video_path, output_path, segments):
    """短视频方案：MoviePy + PIL 合成字幕"""
    from moviepy import VideoFileClip, ImageClip, CompositeVideoClip
    from PIL import Image, ImageDraw, ImageFont
    import numpy as np

    font_paths = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
    ]
    font_path = next((fp for fp in font_paths if os.path.exists(fp)), None)
    if not font_path:
        raise RuntimeError("找不到中文字体")

    clip = VideoFileClip(video_path)
    vw, vh = clip.size
    font = ImageFont.truetype(font_path, FONT_SIZE)

    def make_sub_img(text):
        lines = []
        while len(text) > 20:
            lines.append(text[:20])
            text = text[20:]
        if text:
            lines.append(text)

        dummy = Image.new("RGBA", (1, 1))
        draw = ImageDraw.Draw(dummy)
        line_sizes = []
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            line_sizes.append((bbox[2] - bbox[0], bbox[3] - bbox[1]))

        max_w = max(s[0] for s in line_sizes)
        total_h = sum(s[1] for s in line_sizes) + (len(lines) - 1) * 6
        pad_x, pad_y = 20, 12
        img_w, img_h = max_w + pad_x * 2, total_h + pad_y * 2

        img = Image.new("RGBA", (vw, vh), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        bg_x = (vw - img_w) // 2
        bg_y = SUB_Y - pad_y
        draw.rounded_rectangle(
            [bg_x, bg_y, bg_x + img_w, bg_y + img_h],
            radius=10, fill=(0, 0, 0, int(255 * BG_ALPHA / 100))
        )
        r = int(FONT_COLOR[1:3], 16)
        g = int(FONT_COLOR[3:5], 16)
        b = int(FONT_COLOR[5:7], 16)
        y_cursor = SUB_Y
        for i, line in enumerate(lines):
            bbox = draw.textbbox((0, 0), line, font=font)
            lw = bbox[2] - bbox[0]
            x = (vw - lw) // 2
            for dx in [-2, -1, 0, 1, 2]:
                for dy in [-2, -1, 0, 1, 2]:
                    if dx == 0 and dy == 0:
                        continue
                    draw.text((x + dx, y_cursor + dy), line, font=font, fill=(0, 0, 0, 200))
            draw.text((x, y_cursor), line, font=font, fill=(r, g, b, 255))
            y_cursor += line_sizes[i][1] + 6
        return np.array(img)

    sub_clips = []
    for seg in segments:
        dur = seg["end"] - seg["start"]
        if dur <= 0:
            continue
        arr = make_sub_img(seg["text"])
        ic = ImageClip(arr, duration=dur).with_start(seg["start"])
        sub_clips.append(ic)

    final = CompositeVideoClip([clip] + sub_clips)
    final.write_videofile(
        output_path,
        codec="libx264",
        fps=clip.fps,
        preset="fast",
        audio_codec="aac",
        logger=None,
    )
    clip.close()
    print(f"✅ 完成！输出: {output_path}")


def save_srt(segments, srt_path):
    """保存 SRT 格式字幕文件"""
    def fmt(sec):
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = int(sec % 60)
        ms = int((sec - int(sec)) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    with open(srt_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, 1):
            f.write(f"{i}\n")
            f.write(f"{fmt(seg['start'])} --> {fmt(seg['end'])}\n")
            f.write(f"{seg['text']}\n\n")


def load_srt(srt_path):
    """读取 SRT 文件，返回 segments"""
    import re
    segments = []
    with open(srt_path, encoding="utf-8") as f:
        content = f.read()
    blocks = re.split(r"\n\n+", content.strip())
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        # 第2行是时间轴
        m = re.match(r"(\d+:\d+:\d+,\d+) --> (\d+:\d+:\d+,\d+)", lines[1])
        if not m:
            continue
        def parse_ts(ts):
            h, rest = ts.split(":", 1)
            mi, rest = rest.split(":", 1)
            s, ms = rest.split(",")
            return int(h)*3600 + int(mi)*60 + int(s) + int(ms)/1000
        segments.append({
            "start": parse_ts(m.group(1)),
            "end": parse_ts(m.group(2)),
            "text": " ".join(lines[2:]),
        })
    return segments


def main():
    if len(sys.argv) < 2:
        print("用法: python3 burn_subtitle.py <视频文件> [输出文件]")
        print("      python3 burn_subtitle.py <视频文件> --preview   # 只预览不烧录")
        sys.exit(1)

    video_path = sys.argv[1]
    if not os.path.exists(video_path):
        print(f"❌ 找不到文件: {video_path}")
        sys.exit(1)

    preview_only = "--preview" in sys.argv
    base, ext = os.path.splitext(video_path)

    # 解析 --from-srt 参数
    from_srt = None
    if "--from-srt" in sys.argv:
        idx = sys.argv.index("--from-srt")
        from_srt = sys.argv[idx + 1]

    # 输出路径：排除 --xxx 标志和 --from-srt 的值
    skip_values = {from_srt} if from_srt else set()
    extra_args = [a for a in sys.argv[2:] if not a.startswith("--") and a not in skip_values]
    output_path = extra_args[0] if extra_args else f"{base}_字幕{ext}"

    if from_srt:
        segments = load_srt(from_srt)
        print(f"📄 从 SRT 加载 {len(segments)} 句字幕")
    else:
        # 检查是否有缓存的 SRT，避免重复转写
        cached_srt = base + ".srt"
        if os.path.exists(cached_srt):
            segments = load_srt(cached_srt)
            print(f"💾 发现缓存 SRT，跳过转写（{len(segments)} 句）: {cached_srt}")
        else:
            segments = transcribe(video_path)
            # 转写完自动保存 SRT，下次直接复用
            save_srt(segments, cached_srt)
            print(f"💾 SRT 已缓存: {cached_srt}")

        print(f"\n{'─'*50}")
        print(f"共 {len(segments)} 句字幕：")
        print('─'*50)
        for s in segments:
            print(f"  [{s['start']:6.1f}s - {s['end']:6.1f}s]  {s['text']}")
        print('─'*50)

        if preview_only:
            print(f"\n✅ 预览完成，SRT 已保存: {cached_srt}")
            print(f"修改后运行烧录: python3 burn_subtitle.py '{video_path}' --from-srt '{cached_srt}'")
            return

    burn(video_path, output_path, segments)


if __name__ == "__main__":
    main()
