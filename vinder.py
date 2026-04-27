import os
import re
import time
import requests
import logging
import subprocess
import yt_dlp
from flask import Flask, request, jsonify, send_file, Response, stream_with_context
from flask_cors import CORS


# =============================================================================
# SETUP
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

TIKTOK_UA = (
    "com.zhiliaoapp.musically/2022505030 "
    "(Linux; U; Android 12; en_US; Pixel 6; Build/SQ3A.220705.004; Cronet/58.0.2991.0)"
)

DEFAULT_HEADERS = {
    "User-Agent":      TIKTOK_UA,
    "Accept":          "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection":      "keep-alive",
}

TIKTOK_HEADERS = {
    **DEFAULT_HEADERS,
    "Referer":         "https://www.tiktok.com/",
    "Origin":          "https://www.tiktok.com",
    "Accept-Encoding": "identity",
}

session = requests.Session()


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def format_durasi(detik):
    """Format detik ke string 'Xm00s'."""
    if detik is None:
        return "?"
    try:
        m, s = divmod(int(detik), 60)
        return f"{m}m{s:02d}s"
    except Exception:
        return "?"


def resolve_tiktok_url(url):
    """Resolve short URL (vt.tiktok.com / vm.tiktok.com) ke URL panjang."""
    try:
        r = session.head(url, allow_redirects=True, timeout=10)
        logger.info(f"🔗 Resolved: {url} → {r.url}")
        return r.url
    except Exception as e:
        logger.warning(f"⚠️ Gagal resolve URL: {e}")
        return url


def safe_filename(title, max_len=60):
    """Bersihkan judul jadi nama file yang aman."""
    return re.sub(r'[^a-zA-Z0-9]', '_', title)[:max_len] or 'vinder'


def do_cleanup(out_tmpl):
    """Hapus semua file temp yang terkait satu sesi download."""
    suffixes = [
        '.mp3', '.mp4', '_cover.jpg', '_tagged.mp3',
        '.m4a', '.webm', '.opus', '.ready',
    ]
    for suffix in suffixes:
        path = out_tmpl + suffix
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass


# =============================================================================
# VIDEO / AUDIO FUNCTIONS
# =============================================================================

def fetch_video_stream(url, fallback_url=None):
    """Stream video langsung dari URL, dengan validasi content-type."""
    headers = DEFAULT_HEADERS.copy()

    if "tiktok.com" in url or "ttwstatic.com" in url:
        headers["Referer"] = "https://www.tiktok.com/"
        headers["Origin"]  = "https://www.tiktok.com"
    else:
        domain = re.search(r'https?://([^/]+)', url)
        if domain:
            headers["Origin"]  = f"https://{domain.group(1)}"
            headers["Referer"] = f"https://{domain.group(1)}/"

    headers.update({"Accept-Encoding": "identity", "Range": "bytes=0-"})

    try:
        r = session.get(url, stream=True, timeout=30, headers=headers, allow_redirects=True)
        content_type   = r.headers.get('Content-Type', '').lower()
        content_length = int(r.headers.get('Content-Length', 0))

        if (content_length > 0
                and content_length < 500_000
                and ('text/html' in content_type or 'text/plain' in content_type)):
            logger.warning(f"⚠️ File korup/kecil ({content_length} bytes)")
            return None, False

        if 'text/html' in content_type or 'application/json' in content_type:
            logger.warning(f"⚠️ Blokir non-video content: {content_type}")
            if fallback_url:
                return session.get(
                    fallback_url, stream=True, timeout=30,
                    headers=headers, allow_redirects=True
                ), True
            return None, False

        return r, False

    except Exception as e:
        logger.error(f"Stream Error: {e}")
        if fallback_url:
            return session.get(
                fallback_url, stream=True, timeout=30,
                headers=headers, allow_redirects=True
            ), True
        raise


def get_meta_via_tikwm(tiktok_url, retries=3):
    """
    Ambil metadata video dari TikWM API dengan retry otomatis.
    Return: (video_url, cover_url, title) — pakai hdplay/play, BUKAN music field.
    """
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(
                f"https://www.tikwm.com/api/?url={tiktok_url}",
                timeout=15
            )
            data = resp.json()

            if data.get('code') == 0:
                v         = data['data']
                video_url = v.get('hdplay') or v.get('play')
                cover_url = v.get('origin_cover') or v.get('cover')
                title     = v.get('title', 'audio')
                logger.info(f"✅ TikWM OK (attempt {attempt})")
                return video_url, cover_url, title
            else:
                logger.warning(f"⚠️ TikWM code != 0 (attempt {attempt}): {data.get('msg')}")

        except Exception as e:
            logger.warning(f"⚠️ TikWM gagal attempt {attempt}: {e}")

        if attempt < retries:
            time.sleep(1.5 * attempt)  # backoff: 1.5s → 3s

    return None, None, None


def download_video_with_retry(video_url, out_vid, retries=3):
    """
    Download video ke file temp dengan retry otomatis.
    Validasi ukuran file setelah download.
    """
    last_err = None

    for attempt in range(1, retries + 1):
        try:
            logger.info(f"⬇️ Download video attempt {attempt}...")
            r = session.get(video_url, headers=TIKTOK_HEADERS, timeout=90, stream=True)
            r.raise_for_status()

            content_type = r.headers.get('Content-Type', '').lower()
            if 'text/html' in content_type or 'application/json' in content_type:
                raise ValueError(f"Server kirim non-video: {content_type}")

            with open(out_vid, 'wb') as f:
                for chunk in r.iter_content(chunk_size=512 * 1024):
                    f.write(chunk)

            size = os.path.getsize(out_vid)
            if size < 50_000:
                raise ValueError(f"File terlalu kecil ({size} bytes)")

            logger.info(f"✅ Video downloaded: {size / 1024 / 1024:.2f} MB")
            return True

        except Exception as e:
            last_err = e
            logger.warning(f"⚠️ Download gagal attempt {attempt}: {e}")
            if os.path.exists(out_vid):
                os.remove(out_vid)
            if attempt < retries:
                time.sleep(2 * attempt)  # backoff: 2s → 4s

    raise RuntimeError(f"Download gagal setelah {retries}x: {last_err}")


def extract_audio_ffmpeg(in_vid, out_mp3):
    """Extract audio dari file video ke MP3 via ffmpeg."""
    result = subprocess.run(
        [
            'ffmpeg', '-y',
            '-i',      in_vid,
            '-vn',
            '-acodec', 'libmp3lame',
            '-ab',     '192k',
            '-ar',     '44100',
            out_mp3,
        ],
        capture_output=True,
        timeout=120,
    )
    if result.returncode != 0:
        err = result.stderr.decode(errors='ignore')[-200:]
        raise RuntimeError(f"ffmpeg extract audio gagal: {err}")
    logger.info("🎵 Audio berhasil di-extract")


def extract_frame_at_2s(video_path, out_tmpl):
    """
    Ambil 1 frame di detik ke-2 dari video sebagai cover art.
    Return: path cover kalau berhasil, None kalau gagal.
    """
    cover_path = out_tmpl + '_cover.jpg'
    try:
        subprocess.run(
            [
                'ffmpeg', '-y',
                '-ss',     '2',
                '-i',      video_path,
                '-vframes', '1',
                '-q:v',    '2',
                cover_path,
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        if os.path.exists(cover_path) and os.path.getsize(cover_path) > 1000:
            logger.info("🖼️ Frame detik ke-2 berhasil diambil")
            return cover_path
    except Exception as e:
        logger.warning(f"⚠️ Gagal ambil frame: {e}")
    return None


def embed_cover(mp3_path, cover_path, out_tmpl):
    """Embed cover art ke file MP3 via ffmpeg. Tidak fatal kalau gagal."""
    tagged = out_tmpl + '_tagged.mp3'
    try:
        subprocess.run(
            [
                'ffmpeg', '-y',
                '-i', mp3_path,
                '-i', cover_path,
                '-map', '0:a', '-map', '1:v',
                '-c:a', 'copy',
                '-c:v', 'mjpeg',
                '-id3v2_version', '3',
                '-metadata:s:v', 'title=Album cover',
                '-metadata:s:v', 'comment=Cover (front)',
                tagged,
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        os.replace(tagged, mp3_path)
        logger.info("🖼️ Cover art berhasil di-embed ke MP3")
    except Exception as e:
        logger.warning(f"⚠️ Cover embed gagal (tidak fatal): {e}")


def process_mp3(tiktok_url, title, out_tmpl):
    """
    Pipeline utama konversi MP3:
      1. Ambil metadata via TikWM (retry 3x)
      2. Download video ke temp file (retry 3x)
      3. Extract audio via ffmpeg
      4. Ambil cover dari frame detik ke-2
      5. Embed cover ke MP3

    Return: (path_mp3, final_title)
    """
    out_mp3 = out_tmpl + '.mp3'
    out_vid = out_tmpl + '.mp4'

    video_url, cover_url, api_title = get_meta_via_tikwm(tiktok_url)
    final_title = api_title or title

    if video_url:
        # Path utama: TikWM berhasil
        download_video_with_retry(video_url, out_vid)
        extract_audio_ffmpeg(out_vid, out_mp3)

        cover_path = extract_frame_at_2s(out_vid, out_tmpl)

        try:
            os.remove(out_vid)
        except Exception:
            pass

        # Fallback cover ke thumbnail TikWM
        if not cover_path and cover_url:
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
        # Fallback: yt-dlp
        logger.warning("⚠️ TikWM gagal total, fallback ke yt-dlp...")
        ydl_opts = {
            'format':         'bestaudio/best',
            'outtmpl':        out_mp3,
            'postprocessors': [{
                'key':              'FFmpegExtractAudio',
                'preferredcodec':   'mp3',
                'preferredquality': '192',
            }],
            'writethumbnail': False,
            'quiet':          True,
            'no_warnings':    True,
            'noplaylist':     True,
            'socket_timeout': 20,
            'user_agent':     TIKTOK_UA,
            'http_headers':   DEFAULT_HEADERS,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(tiktok_url, download=True)
            final_title = info.get('title', title)

        # yt-dlp kadang bikin dobel ekstensi .mp3.mp3
        if not os.path.exists(out_mp3) and os.path.exists(out_mp3 + '.mp3'):
            os.rename(out_mp3 + '.mp3', out_mp3)

    return out_mp3, final_title


# =============================================================================
# ROUTES
# =============================================================================

@app.route('/')
def index():
    return send_file('vinder.html')


@app.route('/api/search', methods=['POST'])
def search_videos_api():
    data    = request.json
    keyword = data.get('keyword')
    limit   = data.get('limit', 10)
    logger.info(f"🔍 Searching for: {keyword}")

    try:
        resp = session.post(
            "https://www.tikwm.com/api/feed/search",
            data={"keywords": keyword, "count": limit, "HD": 1},
            timeout=30,
        )
        resp.raise_for_status()
        json_data = resp.json()

        if json_data.get('code') != 0:
            msg = json_data.get('msg', 'API TikWM return non-zero code')
            logger.error(f"❌ TikWM API Error: {msg}")
            return jsonify({"status": "error", "msg": f"TikWM API: {msg}"})

        videos  = json_data.get('data', {}).get('videos', [])
        results = []

        for v in videos:
            cover_url  = v.get('origin_cover') or v.get('cover') or ''
            size_bytes = v.get('size', 0)
            size_mb    = round(size_bytes / (1024 * 1024), 2) if size_bytes else "?"
            author     = v.get('author', {})

            results.append({
                'title':     v.get('title', 'Video TikTok'),
                'duration':  format_durasi(v.get('duration')),
                'play':      v.get('play', ''),
                'hdplay':    v.get('hdplay', '') or v.get('play', ''),
                'cover':     cover_url,
                'size':      f"{size_mb} MB",
                'video_id':  v.get('id', ''),
                'author_id': author.get('id', '') if isinstance(author, dict) else '',
            })

        logger.info(f"✅ Found {len(results)} videos")
        return jsonify({"status": "success", "data": results})

    except Exception as e:
        logger.error(f"Search Error: {str(e)}")
        return jsonify({"status": "error", "msg": str(e)})


@app.route('/api/download_url', methods=['POST'])
def download_url_api():
    data      = request.json
    url_input = data.get('url')
    logger.info(f"🔗 Processing: {url_input}")

    ydl_opts = {
        'format':       'bestvideo+bestaudio/best',
        'quiet':        True,
        'no_warnings':  True,
        'noplaylist':   True,
        'user_agent':   TIKTOK_UA,
        'http_headers': DEFAULT_HEADERS,
    }

    try:
        if any(x in url_input for x in ['tiktok.com', 'vt.tiktok.com', 'vm.tiktok.com']):
            resp = requests.get(f"https://www.tikwm.com/api/?url={url_input}").json()
            if resp.get('code') == 0:
                v = resp['data']
                return jsonify({
                    "status":   "success",
                    "title":    v.get('title', 'TikTok Video'),
                    "cover":    v.get('cover'),
                    "author":   v.get('author', {}).get('nickname', 'User'),
                    "duration": f"{v.get('duration', 0)}s",
                    "size":     f"{v.get('size', 0) / 1024 / 1024:.2f}MB",
                    "play":     v.get('play'),
                    "hdplay":   v.get('hdplay'),
                    "music":    v.get('music'),
                })

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url_input, download=False)
            return jsonify({
                "status":   "success",
                "title":    info.get('title', 'Video'),
                "cover":    info.get('thumbnail'),
                "author":   info.get('uploader', 'Unknown'),
                "duration": f"{info.get('duration', 0)}s",
                "size":     "N/A",
                "play":     info.get('url'),
                "hdplay":   info.get('url'),
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
        r, _ = fetch_video_stream(video_url, fallback_url)

        if r is None or r.status_code >= 400:
            return "Gagal: Video tidak ditemukan atau link kadaluarsa.", 403

        content_type = r.headers.get('Content-Type', '').lower()
        if 'text/html' in content_type:
            return "Gagal: Server mengirimkan file korup (HTML).", 403

        fname = f'[Vinder].{safe_filename(title)}.mp4'
        return Response(
            stream_with_context(r.iter_content(chunk_size=1024 * 1024)),
            headers={
                'Content-Type':        content_type,
                'Content-Disposition': f'attachment; filename="{fname}"',
                'Cache-Control':       'no-cache',
            }
        )

    except Exception as e:
        return f"Error: {str(e)}", 500


@app.route('/api/mp3_progress')
def mp3_progress_api():
    """
    SSE endpoint — push progress real-time ke frontend tiap tahap selesai.
    Format pesan : "data: {pct}|{msg}\\n\\n"
    Pesan selesai: "data: 100|✅ DONE|{uid}|{filename}\\n\\n"
    Pesan error  : "data: -1|❌ {msg}\\n\\n"
    """
    tiktok_url = request.args.get('url')
    title      = request.args.get('title', 'audio')

    if not tiktok_url:
        return "URL Kosong", 400

    def generate():
        def send(pct, msg):
            return f"data: {pct}|{msg}\n\n"

        uid         = str(int(time.time() * 1000))
        out_tmpl    = f'/tmp/vinder_{uid}'
        final_title = title

        try:
            yield send(5, "🔗 Resolve URL...")
            url = tiktok_url
            if 'vt.tiktok.com' in url or 'vm.tiktok.com' in url:
                url = resolve_tiktok_url(url)

            yield send(15, "📡 Ambil metadata video...")
            video_url, cover_url, api_title = get_meta_via_tikwm(url)
            final_title = api_title or title

            if video_url:
                yield send(30, "⬇️ Download video...")
                download_video_with_retry(video_url, out_tmpl + '.mp4')

                yield send(60, "🎵 Extract audio...")
                extract_audio_ffmpeg(out_tmpl + '.mp4', out_tmpl + '.mp3')

                yield send(75, "🖼️ Ambil cover frame...")
                cover_path = extract_frame_at_2s(out_tmpl + '.mp4', out_tmpl)

                try:
                    os.remove(out_tmpl + '.mp4')
                except Exception:
                    pass

                if not cover_path and cover_url:
                    try:
                        cover_path = out_tmpl + '_cover.jpg'
                        cr = session.get(cover_url, timeout=15)
                        with open(cover_path, 'wb') as f:
                            f.write(cr.content)
                    except Exception:
                        cover_path = None

                if cover_path:
                    yield send(85, "🖼️ Embed cover art...")
                    embed_cover(out_tmpl + '.mp3', cover_path, out_tmpl)

            else:
                yield send(30, "⚠️ Fallback ke yt-dlp...")
                ydl_opts = {
                    'format':         'bestaudio/best',
                    'outtmpl':        out_tmpl + '.mp3',
                    'postprocessors': [{
                        'key':              'FFmpegExtractAudio',
                        'preferredcodec':   'mp3',
                        'preferredquality': '192',
                    }],
                    'writethumbnail': False,
                    'quiet':          True,
                    'no_warnings':    True,
                    'noplaylist':     True,
                    'socket_timeout': 20,
                    'user_agent':     TIKTOK_UA,
                    'http_headers':   DEFAULT_HEADERS,
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    final_title = info.get('title', title)

                                if not os.path.exists(out_tmpl + '.mp3') and os.path.exists(out_tmpl + '.mp3.mp3'):
                    os.rename(out_tmpl + '.mp3.mp3', out_tmpl + '.mp3')

            if not os.path.exists(out_tmpl + '.mp3'):
                yield send(-1, "❌ File MP3 tidak berhasil dibuat")
                do_cleanup(out_tmpl)
                return

            filename = f"[Vinder].{safe_filename(final_title)}.mp3"

            yield send(95, "📦 Siapkan file...")
            with open(out_tmpl + '.ready', 'w') as f:
                f.write(filename)

            yield send(100, f"✅ DONE|{uid}|{filename}")

        except Exception as e:
            logger.error(f"SSE MP3 Error: {e}")
            do_cleanup(out_tmpl)
            yield send(-1, f"❌ Error: {str(e)[:100]}")

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control':     'no-cache',
            'X-Accel-Buffering': 'no',
        }
    )


@app.route('/api/get_mp3_file')
def get_mp3_file_api():
    """Ambil file MP3 yang sudah selesai diproses via SSE."""
    uid = request.args.get('uid')
    if not uid:
        return "UID kosong", 400

    out_tmpl  = f'/tmp/vinder_{uid}'
    out_mp3   = out_tmpl + '.mp3'
    done_flag = out_tmpl + '.ready'

    if not os.path.exists(out_mp3) or not os.path.exists(done_flag):
        return "File tidak ditemukan atau belum selesai", 404

    with open(done_flag) as f:
        filename = f.read().strip()

    with open(out_mp3, 'rb') as f:
        audio_data = f.read()

    do_cleanup(out_tmpl)

    return Response(
        audio_data,
        mimetype='audio/mpeg',
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Content-Length':      str(len(audio_data)),
        }
    )


@app.route('/api/get_mp3')
def get_mp3_api():
    """Endpoint fallback MP3 tanpa SSE (satu request langsung)."""
    tiktok_url = request.args.get('tiktok_url') or request.args.get('url')
    title      = request.args.get('title', 'audio')

    if not tiktok_url:
        return "URL Kosong", 400

    if 'vt.tiktok.com' in tiktok_url or 'vm.tiktok.com' in tiktok_url:
        tiktok_url = resolve_tiktok_url(tiktok_url)

    uid      = str(int(time.time() * 1000))
    out_tmpl = f'/tmp/vinder_{uid}'

    try:
        logger.info(f"🎵 MP3 request: {tiktok_url}")
        out_mp3, final_title = process_mp3(tiktok_url, title, out_tmpl)

        if not os.path.exists(out_mp3):
            do_cleanup(out_tmpl)
            return "Gagal: File MP3 tidak berhasil dibuat.", 500

        filename = f"[Vinder].{safe_filename(final_title)}.mp3"
        logger.info(f"✅ Siap dikirim: {filename}")

        with open(out_mp3, 'rb') as f:
            audio_data = f.read()

        do_cleanup(out_tmpl)

        return Response(
            audio_data,
            mimetype='audio/mpeg',
            headers={
                'Content-Disposition': f'attachment; filename="{filename}"',
                'Content-Length':      str(len(audio_data)),
            }
        )

    except Exception as e:
        logger.error(f"MP3 Error: {str(e)}")
        do_cleanup(out_tmpl)
        return f"Error: {str(e)}", 500


# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)