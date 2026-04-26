import os
import re
import time
import requests
import logging
import subprocess
import yt_dlp
from flask import Flask, request, jsonify, send_file, Response, stream_with_context
from flask_cors import CORS

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

TIKTOK_UA = "com.zhiliaoapp.musically/2022505030 (Linux; U; Android 12; en_US; Pixel 6; Build/SQ3A.220705.004; Cronet/58.0.2991.0)"

DEFAULT_HEADERS = {
    "User-Agent": TIKTOK_UA,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

session = requests.Session()

def fetch_video_stream(url, fallback_url=None):
    headers = DEFAULT_HEADERS.copy()
    if "tiktok.com" in url or "ttwstatic.com" in url:
        headers["Referer"] = "https://www.tiktok.com/"
        headers["Origin"] = "https://www.tiktok.com"
    else:
        domain = re.search(r'https?://([^/]+)', url)
        if domain:
            headers["Origin"] = f"https://{domain.group(1)}"
            headers["Referer"] = f"https://{domain.group(1)}/"
    headers.update({"Accept-Encoding": "identity", "Range": "bytes=0-"})
    try:
        r = session.get(url, stream=True, timeout=30, headers=headers, allow_redirects=True)
        content_type = r.headers.get('Content-Type', '').lower()
        content_length = int(r.headers.get('Content-Length', 0))
        if content_length > 0 and content_length < 500000 and ('text/html' in content_type or 'text/plain' in content_type):
            logger.warning(f"⚠️ Deteksi file korup/kecil ({content_length} bytes)")
            return None, False
        if 'text/html' in content_type or 'application/json' in content_type:
            logger.warning(f"⚠️ Blokir non-video content: {content_type}")
            if fallback_url:
                return session.get(fallback_url, stream=True, timeout=30, headers=headers, allow_redirects=True), True
            return None, False
        return r, False
    except Exception as e:
        logger.error(f"Stream Error: {e}")
        if fallback_url:
            return session.get(fallback_url, stream=True, timeout=30, headers=headers, allow_redirects=True), True
        raise e

def format_durasi(detik):
    if detik is None: return "?"
    try:
        m, s = divmod(int(detik), 60)
        return f"{m}m{s:02d}s"
    except: return "?"

def resolve_tiktok_url(url):
    try:
        r = session.head(url, allow_redirects=True, timeout=10)
        logger.info(f"🔗 Resolved: {url} → {r.url}")
        return r.url
    except Exception as e:
        logger.warning(f"⚠️ Gagal resolve URL: {e}")
        return url

def get_meta_via_tikwm(tiktok_url):
    """Ambil metadata video dari TikWM: play URL, cover, title. BUKAN music field."""
    try:
        resp = session.get(f"https://www.tikwm.com/api/?url={tiktok_url}", timeout=15)
        data = resp.json()
        if data.get('code') == 0:
            v = data['data']
            # Prioritas: hdplay > play (video stream asli, bukan music)
            video_url = v.get('hdplay') or v.get('play')
            cover_url = v.get('origin_cover') or v.get('cover')
            title     = v.get('title', 'audio')
            return video_url, cover_url, title
    except Exception as e:
        logger.warning(f"⚠️ TikWM API gagal: {e}")
    return None, None, None

def extract_audio_from_video(video_url, out_mp3, out_tmpl):
    """
    Download video stream lalu extract audio-nya via ffmpeg pipe.
    Tidak simpan file video — langsung pipe ke ffmpeg.
    """
    headers = DEFAULT_HEADERS.copy()
    headers.update({
        "Referer": "https://www.tiktok.com/",
        "Origin": "https://www.tiktok.com",
        "Accept-Encoding": "identity",
    })

    logger.info(f"⬇️ Streaming video untuk extract audio...")
    video_resp = session.get(video_url, headers=headers, timeout=60, stream=True)
    video_resp.raise_for_status()

    # Pipe stream langsung ke ffmpeg → extract audio → simpan MP3
    # ffmpeg baca dari stdin (-i pipe:0), output MP3 ke file
    ffmpeg_cmd = [
        'ffmpeg', '-y',
        '-i', 'pipe:0',          # baca dari stdin
        '-vn',                    # skip video track
        '-acodec', 'libmp3lame',
        '-ab', '192k',
        '-ar', '44100',
        out_mp3
    ]

    proc = subprocess.Popen(
        ffmpeg_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )

    try:
        for chunk in video_resp.iter_content(chunk_size=1024 * 512):
            proc.stdin.write(chunk)
        proc.stdin.close()
        proc.wait(timeout=120)
    except Exception as e:
        proc.kill()
        raise RuntimeError(f"ffmpeg pipe error: {e}")

    if proc.returncode != 0:
        err = proc.stderr.read().decode(errors='ignore')
        raise RuntimeError(f"ffmpeg gagal (code {proc.returncode}): {err[-300:]}")

    logger.info(f"✅ Audio berhasil di-extract: {out_mp3}")

def extract_frame_at_2s(video_url, out_tmpl):
    """
    Ambil frame video di detik ke-2 sebagai cover art via ffmpeg.
    Fallback ke None kalau gagal.
    """
    cover_path = out_tmpl + '_cover.jpg'
    headers_str = '\r\n'.join([f"{k}: {v}" for k, v in {
        **DEFAULT_HEADERS,
        "Referer": "https://www.tiktok.com/",
        "Origin": "https://www.tiktok.com",
    }.items()])

    try:
        subprocess.run([
            'ffmpeg', '-y',
            '-headers', headers_str,
            '-ss', '2',               # seek ke detik ke-2
            '-i', video_url,
            '-vframes', '1',          # ambil 1 frame
            '-q:v', '2',              # kualitas JPEG tinggi
            cover_path
        ], check=True, capture_output=True, timeout=30)

        if os.path.exists(cover_path) and os.path.getsize(cover_path) > 1000:
            logger.info("🖼️ Frame detik ke-2 berhasil diambil")
            return cover_path
    except Exception as e:
        logger.warning(f"⚠️ Gagal ambil frame detik ke-2: {e}")

    return None

def embed_cover(mp3_path, cover_path, out_tmpl):
    """Embed cover art ke MP3 via ffmpeg."""
    try:
        tagged = out_tmpl + '_tagged.mp3'
        subprocess.run([
            'ffmpeg', '-y',
            '-i', mp3_path,
            '-i', cover_path,
            '-map', '0:a', '-map', '1:v',
            '-c:a', 'copy', '-c:v', 'mjpeg',
            '-id3v2_version', '3',
            '-metadata:s:v', 'title=Album cover',
            '-metadata:s:v', 'comment=Cover (front)',
            tagged
        ], check=True, capture_output=True, timeout=30)
        os.replace(tagged, mp3_path)
        logger.info("🖼️ Cover art berhasil di-embed ke MP3")
    except Exception as e:
        logger.warning(f"⚠️ Cover embed gagal (tidak fatal): {e}")

@app.route('/')
def index():
    return send_file('vinder.html')

@app.route('/api/search', methods=['POST'])
def search_videos_api():
    data = request.json
    keyword = data.get('keyword')
    limit = data.get('limit', 10)
    logger.info(f"🔍 Searching for: {keyword}")
    try:
        resp = session.post("https://www.tikwm.com/api/feed/search",
                            data={"keywords": keyword, "count": limit, "HD": 1},
                            timeout=30)
        resp.raise_for_status()
        json_data = resp.json()
        if json_data.get('code') != 0:
            msg = json_data.get('msg', 'API TikWM return non-zero code')
            logger.error(f"❌ TikWM API Error: {msg}")
            return jsonify({"status": "error", "msg": f"TikWM API: {msg}"})
        videos = json_data.get('data', {}).get('videos', [])
        results = []
        for v in videos:
            cover_url = v.get('origin_cover') or v.get('cover') or ''
            size_bytes = v.get('size', 0)
            size_mb = round(size_bytes / (1024 * 1024), 2) if size_bytes else "?"
            results.append({
                'title':     v.get('title', 'Video TikTok'),
                'duration':  format_durasi(v.get('duration')),
                'play':      v.get('play', ''),
                'hdplay':    v.get('hdplay', '') or v.get('play', ''),
                'cover':     cover_url,
                'size':      f"{size_mb} MB",
                'video_id':  v.get('id', ''),
                'author_id': v.get('author', {}).get('id', '') if isinstance(v.get('author'), dict) else ''
            })
        logger.info(f"✅ Found {len(results)} videos")
        return jsonify({"status": "success", "data": results})
    except Exception as e:
        logger.error(f"Search Error: {str(e)}")
        return jsonify({"status": "error", "msg": str(e)})

@app.route('/api/download_url', methods=['POST'])
def download_url_api():
    data = request.json
    url_input = data.get('url')
    logger.info(f"🔗 HYPE Processing: {url_input}")
    ydl_opts = {
        'format': 'bestvideo+bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'user_agent': TIKTOK_UA,
        'http_headers': DEFAULT_HEADERS
    }
    try:
        if 'tiktok.com' in url_input or 'vt.tiktok.com' in url_input or 'vm.tiktok.com' in url_input:
            api_url = f"https://www.tikwm.com/api/?url={url_input}"
            resp = requests.get(api_url).json()
            if resp.get('code') == 0:
                v = resp['data']
                return jsonify({
                    "status": "success",
                    "title":    v.get('title', 'TikTok Video'),
                    "cover":    v.get('cover'),
                    "author":   v.get('author', {}).get('nickname', 'User'),
                    "duration": f"{v.get('duration', 0)}s",
                    "size":     f"{(v.get('size', 0)/1024/1024):.2f}MB",
                    "play":     v.get('play'),
                    "hdplay":   v.get('hdplay'),
                    "music":    v.get('music')
                })
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url_input, download=False)
            return jsonify({
                "status": "success",
                "title":    info.get('title', 'Video'),
                "cover":    info.get('thumbnail'),
                "author":   info.get('uploader', 'Unknown'),
                "duration": f"{info.get('duration', 0)}s",
                "size":     "N/A",
                "play":     info.get('url'),
                "hdplay":   info.get('url')
            })
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})

@app.route('/api/get_video')
def get_video_api():
    video_url    = request.args.get('url')
    fallback_url = request.args.get('fallback')
    title        = request.args.get('title', 'video')
    if not video_url:
        return "URL Kosong", 400
    try:
        r, used_fallback = fetch_video_stream(video_url, fallback_url)
        if r is None or r.status_code >= 400:
            return "Gagal: Video tidak ditemukan atau link kadaluarsa (Access Denied).", 403
        content_type = r.headers.get('Content-Type', '').lower()
        if 'text/html' in content_type:
            return "Gagal: Server mengirimkan file korup (HTML).", 403
        safe_title = re.sub(r'[^a-zA-Z0-9]', '_', title)[:60] or 'video'
        headers = {
            'Content-Type': content_type,
            'Content-Disposition': f'attachment; filename="[Vinder].{safe_title}.mp4"',
            'Cache-Control': 'no-cache'
        }
        return Response(stream_with_context(r.iter_content(chunk_size=1024*1024)), headers=headers)
    except Exception as e:
        return f"Error: {str(e)}", 500

@app.route('/api/get_mp3')
def get_mp3_api():
    tiktok_url = request.args.get('tiktok_url') or request.args.get('url')
    title      = request.args.get('title', 'audio')

    if not tiktok_url:
        return "URL Kosong", 400

    if 'vt.tiktok.com' in tiktok_url or 'vm.tiktok.com' in tiktok_url:
        tiktok_url = resolve_tiktok_url(tiktok_url)

    uid      = str(int(time.time() * 1000))
    out_tmpl = f'/tmp/vinder_{uid}'
    out_mp3  = out_tmpl + '.mp3'

    def do_cleanup():
        for suffix in ['.mp3', '.jpg', '_cover.jpg', '_tagged.mp3', '.m4a', '.webm', '.opus']:
            f = out_tmpl + suffix
            if os.path.exists(f):
                try: os.remove(f)
                except: pass

    try:
        # ── STEP 1: Ambil metadata dari TikWM (video URL, cover, title) ──
        logger.info(f"🎵 Ambil metadata via TikWM: {tiktok_url}")
        video_url, cover_url, api_title = get_meta_via_tikwm(tiktok_url)
        title = api_title or title

        if video_url:
            # ── STEP 2: Extract audio langsung dari video stream via ffmpeg pipe ──
            # Ini ambil audio ASLI dari video, bukan background music
            extract_audio_from_video(video_url, out_mp3, out_tmpl)

            # ── STEP 3: Ambil cover dari frame detik ke-2 (hindari fade-in hitam) ──
            cover_path = extract_frame_at_2s(video_url, out_tmpl)

            # Fallback ke thumbnail TikWM kalau screenshot gagal
            if not cover_path and cover_url:
                logger.info("🖼️ Fallback ke thumbnail TikWM...")
                try:
                    cover_path = out_tmpl + '_cover.jpg'
                    cr = session.get(cover_url, timeout=15)
                    with open(cover_path, 'wb') as f:
                        f.write(cr.content)
                except Exception:
                    cover_path = None

            if cover_path:
                embed_cover(out_mp3, cover_path, out_tmpl)

        else:
            # ── FALLBACK: yt-dlp kalau TikWM gagal total ──
            logger.warning("⚠️ TikWM gagal, fallback ke yt-dlp...")
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': out_mp3,
                'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
                'writethumbnail': False,
                'quiet': True,
                'no_warnings': True,
                'noplaylist': True,
                'socket_timeout': 20,
                'user_agent': TIKTOK_UA,
                'http_headers': DEFAULT_HEADERS,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(tiktok_url, download=True)
                title = info.get('title', title)
            if not os.path.exists(out_mp3) and os.path.exists(out_mp3 + '.mp3'):
                os.rename(out_mp3 + '.mp3', out_mp3)

        if not os.path.exists(out_mp3):
            do_cleanup()
            return "Gagal: File MP3 tidak berhasil dibuat.", 500

        safe_title = re.sub(r'[^a-zA-Z0-9]', '_', title)[:60] or 'audio'
        filename   = f"[Vinder].{safe_title}.mp3"

        logger.info(f"✅ Siap dikirim: {filename}")
        with open(out_mp3, 'rb') as f:
            audio_data = f.read()

        do_cleanup()

        return Response(
            audio_data,
            mimetype='audio/mpeg',
            headers={
                'Content-Disposition': f'attachment; filename="{filename}"',
                'Content-Length': str(len(audio_data)),
            }
        )

    except Exception as e:
        logger.error(f"MP3 Error: {str(e)}")
        do_cleanup()
        return f"Error: {str(e)}", 500

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
