import os
import re
import time
import requests
import logging
import subprocess
import yt_dlp
from flask import Flask, request, jsonify, send_file, Response, stream_with_context


# =============================================================================
# SETUP
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='.', static_url_path='')

from flask_cors import CORS
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


def parse_filter_durasi(filter_str):
    """
    Parse string filter durasi ke (operator, detik).
    Format: '< 30 s', '> 5 m', '< 2 h'  (spasi bebas, case-insensitive)
    Satuan: s = detik, m = menit, h = jam
    Return: (operator, total_detik) atau (None, None) kalau gagal parse.
    """
    if not filter_str:
        return None, None
    try:
        f = filter_str.strip().lower()
        match = re.match(r'^([<>])\s*(\d+(?:\.\d+)?)\s*([smh])$', f)
        if not match:
            return None, None
        op, angka, satuan = match.group(1), float(match.group(2)), match.group(3)
        multiplier = {'s': 1, 'm': 60, 'h': 3600}[satuan]
        return op, angka * multiplier
    except Exception:
        return None, None


def lolos_filter(durasi_detik, op, batas_detik):
    """Cek apakah durasi video lolos filter. Return True kalau lolos."""
    if op is None or durasi_detik is None:
        return True
    try:
        d = float(durasi_detik)
        if op == '<':
            return d < batas_detik
        if op == '>':
            return d > batas_detik
    except Exception:
        pass
    return True


def resolve_tiktok_url(url):
    """Resolve short URL (vt.tiktok.com / vm.tiktok.com) ke URL panjang."""
    try:
        r = session.head(url, allow_redirects=True, timeout=10)
        logger.info(f"[URL] Resolved: {url} -> {r.url}")
        return r.url
    except Exception as e:
        logger.warning(f"[WARN] Gagal resolve URL: {e}")
        return url


def safe_filename(title, max_len=60):
    """
    Bersihkan judul jadi nama file yang aman.
    - Hapus karakter berbahaya OS: \\ / : * ? " < > |
    - Hapus token yang diawali # (hashtag) atau @ (mention)
    - Pertahankan emoji, unicode, font aneh, simbol umum, spasi
    """
    # Hapus karakter berbahaya untuk nama file (OS-level)
    cleaned = re.sub(r'[\\/:*?"<>|]', '', title)
    # Hapus karakter kontrol
    cleaned = re.sub(r'[\x00-\x1f\x7f]', '', cleaned)
    # Hapus token hashtag (#kata) dan mention (@kata)
    cleaned = re.sub(r'[#@]\S*', '', cleaned)
    # Bersihkan spasi berlebih
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned[:max_len] or 'vinder'


def make_content_disposition(filename):
    """
    Buat header Content-Disposition yang aman untuk filename berisi
    emoji / unicode / karakter non-ASCII (RFC 5987).
    Browser modern baca filename* (UTF-8 encoded), browser lama baca
    filename fallback (ASCII-only).
    """
    from urllib.parse import quote
    ascii_fallback = filename.encode('ascii', errors='replace').decode('ascii').replace('?', '_')
    utf8_encoded = quote(filename, safe=" !()\'~")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{utf8_encoded}"


def do_cleanup(out_tmpl):
    """Hapus semua file temp yang terkait satu sesi download."""
    suffixes = ['.mp3', '.mp3.raw', '_cover.jpg', '.ready']
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

        # FIX: blokir HTML/JSON tanpa andal Content-Length
        # CDN publik sering tidak kirim Content-Length, cek content-type saja
        if 'text/html' in content_type or 'application/json' in content_type:
            logger.warning(f"[WARN] Blokir non-video content: {content_type}")
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


def get_meta_via_tikwm(tiktok_url, retries=3, for_audio=False):
    """
    Ambil metadata video dari TikWM API dengan retry otomatis.
    for_audio=True  -> pakai play (SD/360p) - audio track sama, video lebih ringan
    for_audio=False -> pakai hdplay (HD) - untuk download video
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
                if for_audio:
                    # Untuk MP3: pakai wmplay (watermark/SD) atau play (SD)
                    # Audio track-nya identik dengan hdplay, tapi video jauh lebih kecil
                    # ffmpeg akan buang video track langsung, jadi resolusi tidak relevan
                    video_url = v.get('wmplay') or v.get('play')
                    logger.info(f"[OK] TikWM OK - pakai SD URL untuk audio (attempt {attempt})")
                else:
                    video_url = v.get('hdplay') or v.get('play')
                    logger.info(f"[OK] TikWM OK - pakai HD URL untuk video (attempt {attempt})")
                cover_url = v.get('origin_cover') or v.get('cover')
                title     = v.get('title', 'audio')
                return video_url, cover_url, title
            else:
                logger.warning(f"[WARN] TikWM code != 0 (attempt {attempt}): {data.get('msg')}")

        except Exception as e:
            logger.warning(f"[WARN] TikWM gagal attempt {attempt}: {e}")

        if attempt < retries:
            time.sleep(1.5 * attempt)

    return None, None, None


def detect_audio_bitrate(url, headers):
    """
    Detect bitrate audio asli dari URL via ffprobe.
    Return bitrate dalam format string e.g. '128k', '96k'.
    Fallback ke '128k' kalau gagal detect.
    """
    try:
        probe = subprocess.run(
            [
                'ffprobe', '-v', 'quiet',
                '-print_format', 'json',
                '-show_streams',
                '-select_streams', 'a:0',
                url,
            ],
            capture_output=True, timeout=15,
            env={**__import__('os').environ, 'FFPROBE_USER_AGENT': headers.get('User-Agent', '')},
        )
        import json
        data = json.loads(probe.stdout.decode())
        streams = data.get('streams', [])
        if streams:
            br = streams[0].get('bit_rate')
            if br:
                kbps = int(br) // 1000
                # Bulatkan ke nilai standar MP3: 64, 96, 128, 160, 192
                for std in [64, 96, 128, 160, 192]:
                    if kbps <= std:
                        logger.info(f"[PROBE] Bitrate asli: {kbps}k -> pakai {std}k")
                        return f"{std}k"
                return "192k"
    except Exception as e:
        logger.warning(f"[WARN] ffprobe gagal: {e} -> fallback 128k")
    return "128k"


def download_audio_direct(audio_url, out_mp3):
    """
    Pipe audio/video URL langsung ke ffmpeg tanpa buffer ke disk.
    Bitrate MP3 output mengikuti bitrate audio asli dari source.
    """
    headers = TIKTOK_HEADERS.copy()
    headers["Range"] = "bytes=0-"

    logger.info(f"[DL] Pipe audio ke ffmpeg: {audio_url[:80]}...")

    # Detect bitrate asli dulu sebelum download
    bitrate = detect_audio_bitrate(audio_url, headers)

    r = session.get(audio_url, stream=True, timeout=60, headers=headers, allow_redirects=True)
    r.raise_for_status()

    content_type = r.headers.get('Content-Type', '').lower()
    logger.info(f"[PKG] Content-Type: {content_type} | Target bitrate: {bitrate}")

    # Pipe stream langsung ke ffmpeg via stdin - tanpa temp file
    cmd = [
        'ffmpeg', '-y',
        '-i', 'pipe:0',          # baca dari stdin
        '-vn',                   # buang video track
        '-acodec', 'libmp3lame',
        '-ab', bitrate,          # ikuti bitrate asli source
        '-ar', '44100',
        out_mp3,
    ]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    try:
        for chunk in r.iter_content(chunk_size=512 * 1024):
            if chunk:
                proc.stdin.write(chunk)
        proc.stdin.close()
    except BrokenPipeError:
        pass

    proc.wait(timeout=120)

    if proc.returncode != 0:
        err = proc.stderr.read().decode(errors='ignore')[-300:]
        raise RuntimeError(f"ffmpeg pipe->mp3 gagal: {err}")

    size_mb = os.path.getsize(out_mp3) / 1024 / 1024
    logger.info(f"[MP3] Encode selesai: {size_mb:.2f} MB ({bitrate})")


def download_audio_ytdlp(url, out_mp3):
    """
    Download audio asli video via yt-dlp dengan format bestaudio.
    Dipakai untuk YouTube, Instagram, Twitter/X, Facebook.
    Tidak download video sama sekali - langsung ambil audio stream.
    """
    ydl_opts = {
        'format':        'bestaudio/best',
        'outtmpl':       out_mp3 + '.%(ext)s',
        'quiet':         True,
        'no_warnings':   True,
        'noplaylist':    True,
        'user_agent':    TIKTOK_UA,
        'http_headers':  DEFAULT_HEADERS,
        'postprocessors': [{
            'key':            'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '0',    # 0 = ikuti bitrate asli source
        }],
        # Pastikan output final adalah file .mp3
        'keepvideo': False,
    }

    logger.info(f"[DL] yt-dlp bestaudio: {url[:80]}...")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    # yt-dlp output: out_mp3.mp3 (karena postprocessor rename)
    expected = out_mp3 + '.mp3'
    if os.path.exists(expected):
        os.replace(expected, out_mp3)
        logger.info(f"[OK] yt-dlp audio selesai: {out_mp3}")
    elif os.path.exists(out_mp3):
        logger.info(f"[OK] yt-dlp audio selesai (langsung): {out_mp3}")
    else:
        # fallback scan file hasil yt-dlp
        import glob
        candidates = glob.glob(out_mp3 + '.*')
        if candidates:
            os.replace(candidates[0], out_mp3)
            logger.info(f"[OK] yt-dlp audio (fallback rename): {out_mp3}")
        else:
            raise RuntimeError("yt-dlp tidak menghasilkan file audio")


def download_cover(cover_url, cover_path):
    """Download thumbnail dari TikWM sebagai cover art."""
    try:
        cr = session.get(cover_url, timeout=15)
        cr.raise_for_status()
        if len(cr.content) > 1000:
            with open(cover_path, 'wb') as f:
                f.write(cr.content)
            logger.info("[IMG] Cover berhasil didownload dari TikWM")
            return True
    except Exception as e:
        logger.warning(f"[WARN] Gagal download cover: {e}")
    return False


def embed_cover(mp3_path, cover_path):
    """
    Embed cover art ke file MP3 via ffmpeg.
    - Resize cover ke 500x500 (standar ID3) biar tidak bengkak
    - Embed sebagai JPEG attachment bukan video stream (fix preview Google)
    """
    tmp_path  = mp3_path + '.tmp'
    thumb_path = cover_path + '.thumb.jpg'
    try:
        # Step 1: resize cover ke 500x500 JPEG quality 85
        subprocess.run(
            [
                'ffmpeg', '-y',
                '-i', cover_path,
                '-vf', 'scale=500:500:force_original_aspect_ratio=decrease,pad=500:500:(ow-iw)/2:(oh-ih)/2',
                '-q:v', '6',   # JPEG quality ~85 (skala 2-31, makin kecil makin bagus)
                thumb_path,
            ],
            check=True,
            capture_output=True,
            timeout=15,
        )

        # Step 2: embed thumbnail ke MP3 sebagai ID3 APIC frame (pure JPEG, bukan video stream)
        subprocess.run(
            [
                'ffmpeg', '-y',
                '-i', mp3_path,
                '-i', thumb_path,
                '-map', '0:a',
                '-map', '1:v',
                '-c:a', 'copy',
                '-c:v', 'copy',          # copy JPEG as-is, bukan encode ulang ke mjpeg
                '-id3v2_version', '3',
                '-metadata:s:v', 'title=Album cover',
                '-metadata:s:v', 'comment=Cover (front)',
                '-disposition:v', 'attached_pic',  # tandai sebagai attached picture, BUKAN video stream
                tmp_path,
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        os.replace(tmp_path, mp3_path)
        logger.info("[IMG] Cover art berhasil di-embed ke MP3 (500x500)")
    except Exception as e:
        logger.warning(f"[WARN] Cover embed gagal (tidak fatal): {e}")
        for p in [tmp_path, thumb_path]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


def get_tiktok_audio_url(tiktok_url):
    """
    Ambil URL audio stream asli video TikTok via yt-dlp (bestaudio).
    Ini adalah audio yang benar-benar tertanam di video - bukan field 'music'
    yang merupakan lagu background TikWM terpisah.

    Return: (audio_direct_url, cover_url, title) atau (None, cover, title)
    """
    ydl_opts = {
        'format':      'bestaudio/best',
        'quiet':       True,
        'no_warnings': True,
        'noplaylist':  True,
        'user_agent':  TIKTOK_UA,
        'http_headers': DEFAULT_HEADERS,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(tiktok_url, download=False)
            audio_url = None

            # Cari format audio saja (acodec ada, vcodec none/null)
            for fmt in (info.get('formats') or []):
                if fmt.get('acodec') not in (None, 'none') and fmt.get('vcodec') in (None, 'none'):
                    audio_url = fmt.get('url')
                    logger.info(f"[MP3] Audio stream ditemukan: {fmt.get('format_id')} | {fmt.get('ext')}")
                    break

            # Fallback: pakai URL terbaik (meski campur video, tetap bisa extract audio)
            if not audio_url:
                audio_url = info.get('url')
                logger.info("[WARN] Tidak ada pure audio stream, fallback ke URL terbaik")

            cover_url = info.get('thumbnail')
            title     = info.get('title', 'audio')
            return audio_url, cover_url, title
    except Exception as e:
        logger.warning(f"[WARN] yt-dlp gagal ambil audio URL TikTok: {e}")
        return None, None, None


def process_mp3_pipeline(url, title, out_tmpl, progress_cb=None):
    """
    Pipeline MP3 LANGSUNG AUDIO - tidak download video, langsung ambil audio stream.

    - TikTok  : yt-dlp extract audio stream URL -> download raw audio -> encode MP3
    - Lainnya : yt-dlp bestaudio + FFmpegExtractAudio postprocessor

    Return: (path_mp3, final_title)
    """
    def emit(pct, msg):
        if progress_cb:
            progress_cb(pct, msg)
        logger.info(f"[{pct}%] {msg}")

    out_mp3 = out_tmpl + '.mp3'
    is_tiktok = any(x in url for x in ['tiktok.com', 'vt.tiktok.com', 'vm.tiktok.com'])

    if is_tiktok:
        # --- TIKTOK: extract audio stream URL via yt-dlp, lalu download langsung ---
        emit(15, "[API] Ambil metadata & audio stream URL...")

        # Coba yt-dlp dulu untuk audio stream asli
        audio_url, cover_url, api_title = get_tiktok_audio_url(url)
        final_title = api_title or title

        # Fallback ke TikWM untuk cover art kalau yt-dlp berhasil
        if not cover_url:
            _, cover_url_tikwm, tikwm_title = get_meta_via_tikwm(url)
            cover_url  = cover_url_tikwm
            if not final_title or final_title == 'audio':
                final_title = tikwm_title or title

        if audio_url:
            emit(30, "[MP3] Download audio stream langsung...")
            download_audio_direct(audio_url, out_mp3)
        else:
            # Terakhir: fallback ke TikWM video URL + extract audio
            # for_audio=True -> ambil play/SD bukan hdplay, audio track identik tapi stream lebih ringan
            emit(20, "[API] Fallback: ambil URL dari TikWM...")
            video_url, cover_url2, tikwm_title = get_meta_via_tikwm(url, for_audio=True)
            if not cover_url:
                cover_url = cover_url2
            if not final_title or final_title == 'audio':
                final_title = tikwm_title or title
            if not video_url:
                raise RuntimeError("Gagal ambil audio maupun video dari TikTok")
            emit(35, "[MP3] Download & extract audio dari video...")
            download_audio_direct(video_url, out_mp3)

    else:
        # --- PLATFORM LAIN: yt-dlp bestaudio + FFmpegExtractAudio ---
        emit(15, "[API] Ambil audio stream via yt-dlp...")
        final_title = title

        try:
            # Ambil info dulu untuk title & cover
            with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True, 'noplaylist': True}) as ydl:
                info = ydl.extract_info(url, download=False)
                final_title = info.get('title', title)
                cover_url   = info.get('thumbnail')
        except Exception:
            cover_url = None

        emit(30, "[MP3] Download audio langsung (bestaudio)...")
        download_audio_ytdlp(url, out_mp3)

    # Embed cover art kalau ada
    if cover_url:
        cover_path = out_tmpl + '_cover.jpg'
        emit(88, "[IMG] Embed cover art...")
        if download_cover(cover_url, cover_path):
            embed_cover(out_mp3, cover_path)

    return out_mp3, final_title


# =============================================================================
# ROUTES
# =============================================================================

@app.route('/')
def index():
    return send_file('vinder.html')


@app.route('/api/search', methods=['POST'])
def search_videos_api():
    data       = request.json
    keyword    = data.get('keyword')
    limit      = data.get('limit', 10)
    filter_str = data.get('filter', '').strip()
    logger.info(f"[SEARCH] Searching for: {keyword} | filter: '{filter_str}'")

    filter_op, filter_detik = parse_filter_durasi(filter_str)
    if filter_str and filter_op is None:
        logger.warning(f"[WARN] Format filter tidak dikenali: '{filter_str}'")

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
            logger.error(f"[ERR] TikWM API Error: {msg}")
            return jsonify({"status": "error", "msg": f"TikWM API: {msg}"})

        videos  = json_data.get('data', {}).get('videos', [])
        results = []

        for v in videos:
            durasi_detik = v.get('duration')

            if not lolos_filter(durasi_detik, filter_op, filter_detik):
                continue

            cover_url  = v.get('origin_cover') or v.get('cover') or ''
            size_bytes = v.get('size', 0)
            size_mb    = round(size_bytes / (1024 * 1024), 2) if size_bytes else "?"
            author     = v.get('author', {})

            results.append({
                'title':     v.get('title', 'Video TikTok'),
                'duration':  format_durasi(durasi_detik),
                'play':      v.get('play', ''),
                'hdplay':    v.get('hdplay', '') or v.get('play', ''),
                'cover':     cover_url,
                'size':      f"{size_mb} MB",
                'video_id':  v.get('id', ''),
                'author_id': author.get('id', '') if isinstance(author, dict) else '',
            })

        logger.info(f"[OK] Found {len(results)} videos (after filter)")
        return jsonify({"status": "success", "data": results})

    except Exception as e:
        logger.error(f"Search Error: {str(e)}")
        return jsonify({"status": "error", "msg": str(e)})


# Platform yang didukung - FIX agar URL asing tidak nyasar ke static files
SUPPORTED_PLATFORMS = [
    'tiktok.com', 'vt.tiktok.com', 'vm.tiktok.com',
    'youtube.com', 'youtu.be',
    'instagram.com', 'twitter.com', 'x.com',
    'facebook.com', 'fb.watch',
]

def is_supported_url(url):
    if not url:
        return False
    return any(p in url for p in SUPPORTED_PLATFORMS)


@app.route('/api/download_url', methods=['POST'])
def download_url_api():
    data      = request.json
    url_input = data.get('url', '').strip()
    logger.info(f"[URL] Processing: {url_input}")

    # FIX: tolak URL platform yang tidak didukung (Pinterest, dll)
    # Sebelumnya Pinterest URL lolos ke yt_dlp dan sering menyebabkan
    # Flask fallback serve vinder.html sebagai file download
    if not is_supported_url(url_input):
        logger.warning(f"[WARN] Platform tidak didukung: {url_input}")
        return jsonify({
            "status": "error",
            "msg":    "Platform tidak didukung. Vinder mendukung: TikTok, YouTube, Instagram, Twitter/X, Facebook."
        })

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
            resp = session.get(f"https://www.tikwm.com/api/?url={url_input}", timeout=15).json()
            if resp.get('code') == 0:
                v = resp['data']
                return jsonify({
                    "status":   "success",
                    "title":    v.get('title', 'TikTok Video'),
                    "cover":    v.get('origin_cover') or v.get('cover'),
                    "author":   v.get('author', {}).get('nickname', 'User'),
                    "duration": f"{v.get('duration', 0)}s",
                    "size":     f"{v.get('size', 0) / 1024 / 1024:.2f}MB",
                    "play":     v.get('play'),
                    "hdplay":   v.get('hdplay'),
                    # 'music' field sengaja dihapus - pakai hdplay/play untuk audio
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
                'Content-Disposition': make_content_disposition(fname),
                'Cache-Control':       'no-cache',
            }
        )

    except Exception as e:
        return f"Error: {str(e)}", 500


@app.route('/api/mp3_progress')
def mp3_progress_api():
    """
    SSE endpoint - push progress real-time ke frontend tiap tahap selesai.
    Format pesan : "data: {pct}|{msg}\\n\\n"
    Pesan selesai: "data: 100|[OK] DONE|{uid}|{filename}\\n\\n"
    Pesan error  : "data: -1|[ERR] {msg}\\n\\n"
    """
    tiktok_url = request.args.get('url')
    title      = request.args.get('title', 'audio')

    if not tiktok_url:
        return "URL Kosong", 400

    def generate():
        def send(pct, msg):
            return f"data: {pct}|{msg}\n\n"

        uid      = str(int(time.time() * 1000))
        out_tmpl = f'/tmp/vinder_{uid}'

        # FIX: gunakan queue + thread agar SSE bisa yield progress real-time
        # Sebelumnya events dikumpul di list, baru di-yield setelah pipeline selesai
        # - menyebabkan bubble lompat langsung 0% -> 100% tanpa animasi bertahap
        import queue, threading

        q = queue.Queue()

        def emit_sse(pct, msg):
            q.put(send(pct, msg))

        def run_pipeline():
            try:
                emit_sse(5, "[URL] Resolve URL...")
                url = tiktok_url
                if 'vt.tiktok.com' in url or 'vm.tiktok.com' in url:
                    url = resolve_tiktok_url(url)

                out_mp3, final_title = process_mp3_pipeline(url, title, out_tmpl, progress_cb=emit_sse)


                if not os.path.exists(out_mp3):
                    q.put(send(-1, "[ERR] File MP3 tidak berhasil dibuat"))
                    do_cleanup(out_tmpl)
                    q.put(None)
                    return

                fname = f"[Vinder].{safe_filename(final_title)}.mp3"
                emit_sse(95, "[PKG] Siapkan file...")
                with open(out_tmpl + '.ready', 'w') as f:
                    f.write(fname)

                q.put(send(100, f"[OK] DONE|{uid}|{fname}"))
            except Exception as e:
                logger.error(f"SSE MP3 Error: {e}")
                do_cleanup(out_tmpl)
                q.put(send(-1, f"[ERR] Error: {str(e)[:100]}"))
            finally:
                q.put(None)  # sentinel = selesai

        t = threading.Thread(target=run_pipeline, daemon=True)
        t.start()

        while True:
            try:
                item = q.get(timeout=120)
            except queue.Empty:
                yield send(-1, "[ERR] Timeout: proses terlalu lama")
                break
            if item is None:
                break
            yield item

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

    # Kirim file dengan Content-Disposition RFC 5987 (aman untuk emoji/unicode)
    def generate_mp3_file():
        with open(out_mp3, 'rb') as audio_f:
            while True:
                chunk = audio_f.read(512 * 1024)
                if not chunk:
                    break
                yield chunk
        do_cleanup(out_tmpl)

    return Response(
        stream_with_context(generate_mp3_file()),
        headers={
            'Content-Type':        'audio/mpeg',
            'Content-Disposition': make_content_disposition(filename),
            'Cache-Control':       'no-cache',
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
        logger.info(f"[MP3] MP3 request: {tiktok_url}")
        out_mp3, final_title = process_mp3_pipeline(tiktok_url, title, out_tmpl)

        if not os.path.exists(out_mp3):
            do_cleanup(out_tmpl)
            return "Gagal: File MP3 tidak berhasil dibuat.", 500

        filename = f"[Vinder].{safe_filename(final_title)}.mp3"
        logger.info(f"[OK] Siap dikirim: {filename}")

        # Kirim file dengan Content-Disposition RFC 5987 (aman untuk emoji/unicode)
        def generate_mp3():
            with open(out_mp3, 'rb') as audio_f:
                while True:
                    chunk = audio_f.read(512 * 1024)
                    if not chunk:
                        break
                    yield chunk
            do_cleanup(out_tmpl)

        return Response(
            stream_with_context(generate_mp3()),
            headers={
                'Content-Type':        'audio/mpeg',
                'Content-Disposition': make_content_disposition(filename),
                'Cache-Control':       'no-cache',
            }
        )

    except Exception as e:
        logger.error(f"MP3 Error: {str(e)}")
        do_cleanup(out_tmpl)
        return f"Error: {str(e)}", 500




@app.route('/api/fast_mp3')
def fast_mp3_api():
    """
     FAST MP3 - zero encode, langsung pipe audio CDN ke browser.
    File output: .mp3 (rename saja, isi m4a/aac - semua player bisa baca).
    Kecepatan setara MP4 download karena skip ffmpeg total.
    """
    tiktok_url = request.args.get('url', '').strip()
    title      = request.args.get('title', 'audio')

    if not tiktok_url:
        return "URL Kosong", 400

    # Resolve short URL
    if 'vt.tiktok.com' in tiktok_url or 'vm.tiktok.com' in tiktok_url:
        tiktok_url = resolve_tiktok_url(tiktok_url)

    is_tiktok = any(x in tiktok_url for x in ['tiktok.com', 'vt.tiktok.com', 'vm.tiktok.com'])

    try:
        audio_url  = None
        final_title = title

        if is_tiktok:
            # Coba yt-dlp dulu untuk audio stream murni
            audio_url, _, api_title = get_tiktok_audio_url(tiktok_url)
            if api_title:
                final_title = api_title

            # Fallback ke TikWM
            if not audio_url:
                video_url, _, tikwm_title = get_meta_via_tikwm(tiktok_url)
                audio_url = video_url
                if tikwm_title:
                    final_title = tikwm_title
        else:
            # YouTube / Instagram / dll - ambil bestaudio URL via yt-dlp
            ydl_opts = {
                'format':      'bestaudio/best',
                'quiet':       True,
                'no_warnings': True,
                'noplaylist':  True,
                'user_agent':  TIKTOK_UA,
                'http_headers': DEFAULT_HEADERS,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(tiktok_url, download=False)
                audio_url   = info.get('url')
                final_title = info.get('title', title)

        if not audio_url:
            return "Gagal: tidak bisa ambil URL audio", 500

        # Pipe langsung dari CDN ke browser - zero temp file, zero encode
        headers = TIKTOK_HEADERS.copy()
        headers['Range'] = 'bytes=0-'

        r = session.get(audio_url, stream=True, timeout=30, headers=headers, allow_redirects=True)
        if r.status_code >= 400:
            return f"Gagal: CDN return {r.status_code}", 502

        filename = f"[Vinder].{safe_filename(final_title)}.mp3"

        def generate():
            for chunk in r.iter_content(chunk_size=512 * 1024):
                if chunk:
                    yield chunk

        return Response(
            stream_with_context(generate()),
            headers={
                'Content-Type':        'audio/mpeg',
                'Content-Disposition': make_content_disposition(filename),
                'Cache-Control':       'no-cache',
            }
        )

    except Exception as e:
        logger.error(f"fast_mp3 error: {e}")
        return f"Error: {str(e)}", 500

# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)