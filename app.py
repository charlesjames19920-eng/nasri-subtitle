import os
import re
import time
import socket
import ipaddress
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse, urljoin, unquote

import gradio as gr
import requests

APP_DIR = Path("/tmp/nasri_subtitle_studio")
APP_DIR.mkdir(parents=True, exist_ok=True)
CHUNK = 1024 * 1024
MAX_REDIRECTS = 5

def safe_name(name):
    name = unquote(name or "").strip()
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name)
    name = re.sub(r"\s+", "_", name).strip("._ ")
    return (name[:90] or "Nasri_Subtitle_Studio")

def validate_public_http_url(url):
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("أدخل رابطًا مباشرًا يبدأ بـ https:// أو http://.")
    host = parsed.hostname.lower()
    if host in ("localhost",) or host.endswith(".local"):
        raise ValueError("العناوين المحلية غير مسموحة.")
    try:
        addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError("تعذّر العثور على خادم الرابط. تحقق من الرابط واتصالك.") from e
    for item in addresses:
        ip = ipaddress.ip_address(item[4][0].split("%")[0])
        if not ip.is_global:
            raise ValueError("الرابط يشير إلى عنوان شبكة خاص أو محلي، وهذا غير مسموح.")
    return parsed.geturl()

def request_direct_download(url, progress):
    current = validate_public_http_url(url)
    session = requests.Session()
    headers = {"User-Agent": "NasriSubtitleStudio/1.0", "Accept": "*/*"}
    response = None
    try:
        for _ in range(MAX_REDIRECTS + 1):
            current = validate_public_http_url(current)
            response = session.get(current, stream=True, timeout=(20, 90), allow_redirects=False, headers=headers)
            if response.is_redirect or response.is_permanent_redirect:
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise RuntimeError("أعاد الخادم تحويلًا دون عنوان وجهة.")
                current = urljoin(current, location)
                continue
            break
        else:
            raise RuntimeError("عدد التحويلات بين الروابط كبير جدًا.")
        response.raise_for_status()
        ctype = response.headers.get("Content-Type", "").lower()
        if "text/html" in ctype:
            raise ValueError("هذا رابط صفحة HTML وليس ملف فيديو مباشرًا. استخدم رابط تنزيل مباشر.")
        total_header = response.headers.get("Content-Length", "")
        total = int(total_header) if total_header.isdigit() else None
        disposition = response.headers.get("Content-Disposition", "")
        match = re.search(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';]+)', disposition, re.I)
        name = match.group(1).strip() if match else Path(urlparse(current).path).name
        suffix = Path(name).suffix.lower()
        if suffix not in (".mp4", ".mkv", ".mov", ".webm", ".m4v", ".avi", ".ts", ".mpg", ".mpeg"):
            suffix = ".video"
        dest = APP_DIR / f"source_{int(time.time())}{suffix}"
        received = 0
        start = time.time()
        with open(dest, "wb") as f:
            for chunk in response.iter_content(CHUNK):
                if not chunk:
                    continue
                f.write(chunk)
                received += len(chunk)
                if total:
                    frac = min(received / total, 0.99)
                    progress(frac, desc=f"تنزيل الفيلم: {received/1048576:.1f}/{total/1048576:.1f} MiB")
                else:
                    progress(desc=f"تم تنزيل {received/1048576:.1f} MiB")
        if received < 1024:
            dest.unlink(missing_ok=True)
            raise ValueError("الملف الذي تم تنزيله صغير جدًا؛ قد لا يكون فيديو.")
        progress(1.0, desc=f"اكتمل تنزيل الفيلم: {received/1048576:.1f} MiB")
        return dest, received
    except requests.exceptions.Timeout as e:
        raise RuntimeError("انتهت مهلة الاتصال أثناء تنزيل الفيلم. حاول مجددًا.") from e
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "غير معروف"
        raise RuntimeError(f"رفض الخادم التنزيل (HTTP {code}). قد يكون الرابط منتهي الصلاحية أو يتطلب تسجيل الدخول.") from e
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"فشل الاتصال أثناء تنزيل الفيلم: {e}") from e
    finally:
        if response is not None:
            response.close()
        session.close()

def probe(path):
    p = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration,size,format_name:stream=codec_type,codec_name,width,height",
        "-of", "json", str(path)
    ], capture_output=True, text=True, timeout=90)
    if p.returncode:
        raise RuntimeError("تعذّر قراءة الفيديو. قد يكون الملف تالفًا أو غير مكتمل.\n" + p.stderr[-1200:])
    import json
    data = json.loads(p.stdout)
    if not any(s.get("codec_type") == "video" for s in data.get("streams", [])):
        raise RuntimeError("الملف لا يحتوي على مسار فيديو.")
    return data

def process(url, srt_file, output_name, progress=gr.Progress()):
    movie = None
    output = None
    try:
        if not srt_file:
            raise ValueError("ارفع ملف SRT أولًا.")
        srt_path = Path(srt_file)
        if srt_path.suffix.lower() != ".srt":
            raise ValueError("الملف يجب أن يكون بامتداد .srt.")
        raw = srt_path.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as e:
            raise ValueError("ملف SRT ليس بترميز UTF-8. احفظه بترميز UTF-8 ثم أعد رفعه.") from e
        if "-->" not in text or not re.search(r"\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->", text):
            raise ValueError("لم نعثر على توقيتات SRT المعتادة في الملف.")

        yield "⏬ جارٍ تنزيل الفيلم…", None
        movie, bytes_count = request_direct_download(url, progress)
        yield f"✅ اكتمل التنزيل ({bytes_count/1048576:.1f} MiB).\n🔎 جارٍ فحص الفيديو…", None
        data = probe(movie)
        fmt = data.get("format", {})
        streams = data.get("streams", [])
        video = next(s for s in streams if s.get("codec_type") == "video")
        duration = float(fmt.get("duration") or 0)
        resolution = f"{video.get('width','?')}×{video.get('height','?')}"
        audio_count = sum(s.get("codec_type") == "audio" for s in streams)
        yield f"🎞️ الدقة: {resolution} | المدة: {duration/60:.1f} دقيقة\n🔊 مسارات الصوت: {audio_count}\n🧩 جارٍ إضافة SRT كمسار مستقل…", None

        name = safe_name(output_name) + ".mkv"
        output = APP_DIR / f"result_{int(time.time())}_{name}"
        cmd = [
            "ffmpeg", "-hide_banner", "-y", "-nostdin", "-i", str(movie), "-i", str(srt_path),
            "-map", "0:v?", "-map", "0:a?", "-map", "1:0",
            "-map_metadata", "0", "-map_chapters", "0",
            "-c", "copy",
            "-metadata:s:s:0", "language=ara",
            "-metadata:s:s:0", "title=Arabic",
            "-disposition:s:0", "default",
            "-progress", "pipe:1", "-nostats", str(output)
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        try:
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("out_time_ms=") and duration > 0:
                    try:
                        frac = min(int(line.split("=",1)[1]) / (duration * 1_000_000), .99)
                        progress(frac, desc=f"دمج المسار: {frac*100:.0f}%")
                        yield f"⚙️ جارٍ دمج الترجمة: {frac*100:.0f}%", None
                    except ValueError:
                        pass
            stderr = proc.stderr.read()
            code = proc.wait()
            if code:
                raise RuntimeError("فشل FFmpeg في إنشاء MKV:\n" + stderr[-2500:])
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()
            proc.stderr.close()

        if not output.exists() or output.stat().st_size < 1024:
            raise RuntimeError("لم يتم إنشاء ملف الناتج بصورة صحيحة.")
        result_size = output.stat().st_size
        yield (
            f"✅ اكتملت العملية!\n📦 الحجم النهائي: {result_size/1048576:.1f} MiB\n"
            "🎬 تم نسخ الفيديو والصوت دون إعادة ترميز.\n"
            "💬 الترجمة مسار منفصل قابل للاختيار، وليست محروقة على الصورة.\n"
            "📁 الصيغة: MKV",
            str(output)
        )
    except Exception as e:
        yield f"❌ {e}", None
    finally:
        if movie and movie.exists():
            movie.unlink(missing_ok=True)

with gr.Blocks(title="Nasri Subtitle Studio", theme=gr.themes.Soft()) as demo:
    gr.Markdown("# 🎬 Nasri Subtitle Studio\nإضافة ترجمة SRT كمسار مستقل داخل MKV دون حرقها على الصورة.")
    gr.Markdown("**مهم:** هذا التطبيق عام إذا نُشر علنًا. لا تدخل روابط خاصة أو بيانات تسجيل دخول، واستخدم ملفات لديك حق معالجتها. الملفات مؤقتة وقد تُحذف عند إعادة تشغيل التطبيق.")
    url = gr.Textbox(label="رابط الفيلم المباشر", placeholder="https://example.com/movie.mp4", lines=2)
    srt = gr.File(label="ملف الترجمة SRT (UTF-8)", file_types=[".srt"], type="filepath")
    name = gr.Textbox(label="اسم الملف الناتج", value="Nasri_Subtitle_Studio", max_lines=1)
    start = gr.Button("🚀 تنزيل الفيلم وإضافة الترجمة", variant="primary")
    status = gr.Textbox(label="حالة العملية", lines=8, interactive=False)
    result = gr.File(label="تنزيل ملف MKV")
    start.click(process, inputs=[url, srt, name], outputs=[status, result], show_progress="full")
    gr.Markdown("إذا فشل الرابط، جرّب رابط ملف مباشرًا لا صفحة مشاهدة. ملفات كبيرة جدًا قد تتجاوز مساحة أو مهلة الاستضافة المجانية.")

demo.queue(max_size=2).launch()
