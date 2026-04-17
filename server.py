#!/usr/bin/env python3
"""字幕烧录服务 - 配合 index.html 使用
提供 Whisper 转写 + 字幕烧录的 HTTP 接口
"""

import json
import os
import subprocess
import tempfile
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = 7891

# 全局状态
burn_state = {"state": "idle", "message": "空闲"}


def _save_srt(segments, srt_path):
    """将 segments 保存为 SRT 文件"""
    def fmt(sec):
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = int(sec % 60)
        ms = int(round((sec - int(sec)) * 1000))
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, 1):
            f.write(f"{i}\n{fmt(seg['start'])} --> {fmt(seg['end'])}\n{seg['text']}\n\n")


def _burn_transcribe(video_path):
    """后台线程：Whisper 转写视频，返回 segments"""
    global burn_state
    try:
        burn_state = {"state": "processing", "message": "提取音频..."}
        tmpdir = tempfile.mkdtemp(prefix="burn_")
        audio_path = os.path.join(tmpdir, "audio.wav")
        cmd = ["ffmpeg", "-y", "-i", video_path,
               "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", audio_path]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            burn_state = {"state": "error", "message": f"音频提取失败: {result.stderr[-200:]}"}
            return

        burn_state = {"state": "processing", "message": "Whisper 转写中（faster-whisper）..."}
        from faster_whisper import WhisperModel
        import shutil
        PROMPT = "以下是一段中文口播视频，内容关于AI、科技和创业。常见词汇：MCP、Anthropic、Claude、Vibecoding、API、AI、大模型、小红书、公众号、USB-C、协议、工具调用、智能体、agent、产品经理、一人公司。"
        model = WhisperModel("medium", device="cpu", compute_type="int8")
        fw_segs, _ = model.transcribe(audio_path, language="zh",
            initial_prompt=PROMPT, vad_filter=True, word_timestamps=True)

        # 收集所有词（带时间戳），再按自然断句重新切分
        all_words = []
        for seg in fw_segs:
            if seg.words:
                for w in seg.words:
                    word = w.word.strip()
                    if word:
                        all_words.append({"word": word, "start": w.start, "end": w.end})

        # 按标点 + 字数切分短句（每句 <= 15 字）
        MAX_CHARS = 15
        MIN_CHARS = 5   # 短于此字数的句子合并到上一句
        BREAK_PUNCTS = set("，。！？,!?…、")
        segments = []
        buf_words = []
        buf_text = ""

        def flush_buf():
            if buf_words:
                text = buf_text.strip()
                # 太短的句子合并到上一句
                if len(text.replace(" ", "")) < MIN_CHARS and segments:
                    prev = segments[-1]
                    prev["end"] = round(buf_words[-1]["end"], 2)
                    prev["text"] += text
                else:
                    segments.append({
                        "start": round(buf_words[0]["start"], 2),
                        "end": round(buf_words[-1]["end"], 2),
                        "text": text,
                    })

        for w in all_words:
            buf_words.append(w)
            buf_text += w["word"]
            # 遇到标点则切断
            if w["word"] and w["word"][-1] in BREAK_PUNCTS:
                flush_buf()
                buf_words, buf_text = [], ""
            elif len(buf_text.replace(" ", "")) >= MAX_CHARS:
                # 避免劈开英文单词
                if w["word"].isascii() and w["word"].isalpha():
                    continue
                flush_buf()
                buf_words, buf_text = [], ""
        flush_buf()

        shutil.rmtree(tmpdir, ignore_errors=True)

        # 自动保存 SRT
        srt_path = os.path.splitext(video_path)[0] + ".srt"
        _save_srt(segments, srt_path)

        burn_state = {"state": "done", "message": f"转写完成，共 {len(segments)} 句", "segments": segments, "srt_path": srt_path}
    except Exception as e:
        burn_state = {"state": "error", "message": str(e)[:300]}


def _segments_to_ass(segments, vw, vh, font_size, bg_alpha, sub_y_pct):
    """将字幕 segments 转换为 ASS 格式"""
    sub_y = int(vh * sub_y_pct)
    margin_v = vh - sub_y

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

    bg_alpha_hex = format(int((1 - bg_alpha / 100) * 255), "02X")

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {vw}
PlayResY: {vh}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial Unicode MS,{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H{bg_alpha_hex}000000,0,0,0,0,100,100,0,0,3,2,0,2,20,20,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header.strip()]
    for seg in segments:
        lines.append(f"Dialogue: 0,{fmt_ts(seg['start'])},{fmt_ts(seg['end'])},Default,,0,0,0,,{wrap_text(seg['text'])}")
    return "\n".join(lines) + "\n"


def _burn_render(video_path, segments, style=None):
    """后台线程：烧录字幕到视频
    短视频（<=100句）：PIL 生成 PNG + ffmpeg overlay
    长视频（>100句）：ASS 字幕 + imageio ffmpeg libass
    """
    global burn_state
    if style is None:
        style = {}
    try:
        base, ext = os.path.splitext(video_path)
        output_path = base + "_字幕" + ext
        burn_state = {"state": "processing", "message": "烧录字幕中..."}

        import json as _json, shutil

        FONT_SIZE = int(style.get("font_size", 48))
        BG_ALPHA = int(style.get("bg_alpha", 70))
        SUB_Y_PCT = style.get("sub_y_pct", 0.86)

        # 探测视频分辨率
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", video_path],
            capture_output=True, text=True
        )
        info = _json.loads(probe.stdout)
        vstream = next(s for s in info["streams"] if s["codec_type"] == "video")
        vw, vh = int(vstream["width"]), int(vstream["height"])

        # 分流：长视频用 ASS + libass，短视频用 PNG overlay
        LONG_VIDEO_THRESHOLD = 100
        if len(segments) > LONG_VIDEO_THRESHOLD:
            burn_state = {"state": "processing", "message": f"烧录字幕中（ASS模式，{len(segments)}句）..."}
            _burn_render_ass(video_path, output_path, segments, vw, vh, FONT_SIZE, BG_ALPHA, SUB_Y_PCT)
        else:
            burn_state = {"state": "processing", "message": f"烧录字幕中（PNG模式，{len(segments)}句）..."}
            _burn_render_png(video_path, output_path, segments, vw, vh, FONT_SIZE, BG_ALPHA, SUB_Y_PCT)

        burn_state = {"state": "done", "message": "完成！", "output": output_path}
    except Exception as e:
        burn_state = {"state": "error", "message": str(e)[:300]}


def _burn_render_ass(video_path, output_path, segments, vw, vh, font_size, bg_alpha, sub_y_pct):
    """长视频方案：ASS 字幕 + imageio ffmpeg libass"""
    import shutil

    # imageio 捆绑的 ffmpeg 有 libass
    IMAGEIO_FFMPEG = os.path.join(
        os.path.expanduser("~/Library/Python/3.9/lib/python/site-packages"),
        "imageio_ffmpeg", "binaries", "ffmpeg-macos-aarch64-v7.1"
    )
    if not os.path.exists(IMAGEIO_FFMPEG):
        raise RuntimeError(f"找不到 imageio ffmpeg: {IMAGEIO_FFMPEG}")

    tmpdir = tempfile.mkdtemp(prefix="burn_ass_")
    try:
        ass_content = _segments_to_ass(segments, vw, vh, font_size, bg_alpha, sub_y_pct)
        ass_path = os.path.join(tmpdir, "subtitles.ass")
        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(ass_content)

        ass_escaped = ass_path.replace(":", "\\:").replace("'", "\\'")
        cmd = [
            IMAGEIO_FFMPEG, "-y",
            "-i", video_path,
            "-vf", f"ass={ass_escaped}",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "copy",
            "-movflags", "+faststart",
            output_path,
        ]
        print(f"[ASS烧录] 字幕数: {len(segments)}, 分辨率: {vw}x{vh}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
        if result.returncode != 0:
            raise RuntimeError(f"ASS 烧录失败: {result.stderr[-500:]}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _burn_render_png(video_path, output_path, segments, vw, vh, font_size, bg_alpha, sub_y_pct):
    """短视频方案：PIL 生成字幕 PNG + ffmpeg overlay"""
    from PIL import Image, ImageDraw, ImageFont
    import shutil

    sub_y = int(vh * sub_y_pct)

    font_paths = ["/System/Library/Fonts/PingFang.ttc",
                  "/System/Library/Fonts/STHeiti Light.ttc",
                  "/Library/Fonts/Arial Unicode.ttf"]
    font_path = next((fp for fp in font_paths if os.path.exists(fp)), None)
    if not font_path:
        raise RuntimeError("找不到中文字体")

    font = ImageFont.truetype(font_path, font_size)

    def make_sub_img(text):
        lines, t = [], text
        while len(t) > 20:
            lines.append(t[:20])
            t = t[20:]
        if t:
            lines.append(t)
        dummy = Image.new("RGBA", (1, 1))
        draw = ImageDraw.Draw(dummy)
        line_sizes = []
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            line_sizes.append((bbox[2]-bbox[0], bbox[3]-bbox[1]))
        max_w = max(s[0] for s in line_sizes)
        total_h = sum(s[1] for s in line_sizes) + (len(lines)-1)*6
        pad_x, pad_y = 20, 12
        img_w, img_h = max_w+pad_x*2, total_h+pad_y*2
        img = Image.new("RGBA", (vw, vh), (0,0,0,0))
        draw = ImageDraw.Draw(img)
        bg_x = (vw-img_w)//2
        bg_y = sub_y - pad_y
        draw.rounded_rectangle([bg_x, bg_y, bg_x+img_w, bg_y+img_h],
                               radius=10, fill=(0,0,0,int(255*bg_alpha/100)))
        y_cursor = sub_y
        for i, line in enumerate(lines):
            bbox = draw.textbbox((0,0), line, font=font)
            lw = bbox[2]-bbox[0]
            x = (vw-lw)//2
            for dx in [-2,-1,0,1,2]:
                for dy in [-2,-1,0,1,2]:
                    if dx==0 and dy==0: continue
                    draw.text((x+dx, y_cursor+dy), line, font=font, fill=(0,0,0,200))
            draw.text((x, y_cursor), line, font=font, fill=(255,255,255,255))
            y_cursor += line_sizes[i][1]+6
        return img

    tmpdir = tempfile.mkdtemp(prefix="burn_")
    try:
        sub_files = []
        for i, sub in enumerate(segments):
            img = make_sub_img(sub["text"])
            png_path = os.path.join(tmpdir, f"sub_{i:04d}.png")
            img.save(png_path)
            sub_files.append((png_path, sub["start"], sub["end"]))

        cmd = ["ffmpeg", "-y", "-i", video_path]
        for png_path, _, _ in sub_files:
            cmd += ["-i", png_path]
        filter_parts = []
        current = "[0:v]"
        for i, (_, start, end) in enumerate(sub_files):
            is_last = (i == len(sub_files) - 1)
            next_v = "[vout]" if is_last else f"[ov{i}]"
            filter_parts.append(
                f"{current}[{i+1}:v]overlay=0:0:format=auto:"
                f"enable='between(t,{start:.3f},{end:.3f})'{next_v}"
            )
            current = next_v
        cmd += [
            "-filter_complex", ";".join(filter_parts),
            "-map", "[vout]", "-map", "0:a",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "copy", output_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg 失败: {result.stderr[-400:]}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


class BurnHandler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/burn-status":
            self._json_response(burn_state)
        elif self.path == "/pick-file":
            # macOS 原生文件选择框
            try:
                result = subprocess.run(
                    ["osascript", "-e",
                     'POSIX path of (choose file with prompt "选择视频文件" of type {"mov", "mp4", "m4v", "avi", "mkv"})'],
                    capture_output=True, text=True, timeout=60
                )
                if result.returncode == 0:
                    path = result.stdout.strip()
                    self._json_response({"path": path})
                else:
                    self._json_response({"path": "", "cancelled": True})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
        elif self.path == "/":
            self._json_response({"status": "ok", "message": "字幕烧录服务运行中"})
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        if self.path == "/burn-transcribe":
            try:
                req = json.loads(body)
            except json.JSONDecodeError:
                self._json_response({"error": "无效的 JSON"}, 400)
                return
            video_path = req.get("video_path", "")
            if not video_path or not os.path.exists(video_path):
                self._json_response({"error": f"文件不存在: {video_path}"}, 400)
                return
            global burn_state
            burn_state = {"state": "processing", "message": "转写中..."}
            t = threading.Thread(target=_burn_transcribe, args=(video_path,), daemon=True)
            t.start()
            self._json_response({"status": "started"})

        elif self.path == "/burn-render":
            try:
                req = json.loads(body)
            except json.JSONDecodeError:
                self._json_response({"error": "无效的 JSON"}, 400)
                return
            video_path = req.get("video_path", "")
            segments = req.get("segments", [])
            style = req.get("style", {})
            if not video_path or not os.path.exists(video_path):
                self._json_response({"error": f"文件不存在: {video_path}"}, 400)
                return
            burn_state = {"state": "processing", "message": "烧录中..."}
            t = threading.Thread(target=_burn_render, args=(video_path, segments, style), daemon=True)
            t.start()
            self._json_response({"status": "started"})

        elif self.path == "/burn-status":
            self._json_response(burn_state)

        else:
            self.send_error(404)

    def _json_response(self, data, code=200):
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, format, *args):
        print(f"[{time.strftime('%H:%M:%S')}] {format % args}")


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", PORT), BurnHandler)
    print(f"字幕烧录服务已启动: http://localhost:{PORT}")
    print(f"打开 index.html 即可使用")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
