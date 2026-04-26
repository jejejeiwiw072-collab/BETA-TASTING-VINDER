import os
import re
import time
import requests
import logging
import yt_dlp
from flask import Flask, request, jsonify, send_file, Response, stream_with_context
from flask_cors import CORS

# Setup Mata-mata (Logging)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

# User-Agent khusus mobile untuk mendapatkan bitrate lebih tinggi (HYPE v1.3)
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
    
    # Perbaikan: TikTok butuh Referer ke situs utamanya, bukan ke subdomain video
    if "tiktok.com" in url or "ttwstatic.com" in url:
        headers["Referer"] = "https://www.tiktok.com/"
        headers["Origin"] = "https://www.tiktok.com"
    else:
        domain = re.search(r'https?://([^/]+)', url)
        if domain:
            headers["Origin"] = f"https://{domain.group(1)}"
            headers["Referer"] = f"https://{domain.group(1)}/"
    
    headers.update({
        "Accept-Encoding": "identity",
        "Range": "bytes=0-"
    })
    
    try:
        r = session.get(url, stream=True, timeout=30, headers=headers, allow_redirects=True)
        content_type = r.headers.get('Content-Type', '').lower()
        content_length = int(r.headers.get('Content-Length', 0))
        
        # Guard: Jika ukuran file terlalu kecil (< 500KB), kemungkinan besar itu bukan video asli
        # Video TikTok rata-rata di atas 1MB. Kalau cuma 160 byte (0,16KB), itu pesan error.
        if content_length > 0 and content_length < 500000 and ('text/html' in content_type or 'text/plain' in content_type):
            logger.warning(f"⚠️ Deteksi file korup/kecil ({content_length} bytes)")
            return None, False

        # Validasi: Jika server asal mengirimkan HTML (error/captcha), blokir!
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
        # Gunakan session untuk efisiensi
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
                'title': v.get('title', 'Video TikTok'),
                'duration': format_durasi(v.get('duration')),
                'play': v.get('play', ''),
                'hdplay': v.get('hdplay', '') or v.get('play', ''),
                'cover': cover_url,
                'size': f"{size_mb} MB",
                'video_id': v.get('id', ''),  # untuk reconstruct TikTok URL di MP3
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
    
    # Konfigurasi yt-dlp dikunci untuk kualitas maksimal
    ydl_opts = {
        'format': 'bestvideo+bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'user_agent': TIKTOK_UA,
        'http_headers': DEFAULT_HEADERS
    }
    
    try:
        if 'tiktok.com' in url_input:
            api_url = f"https://www.tikwm.com/api/?url={url_input}"
            resp = requests.get(api_url).json()
            if resp.get('code') == 0:
                v = resp['data']
                return jsonify({
                    "status": "success",
                    "title": v.get('title', 'TikTok Video'),
                    "cover": v.get('cover'),
                    "author": v.get('author', {}).get('nickname', 'User'),
                    "duration": f"{v.get('duration', 0)}s",
                    "size": f"{(v.get('size', 0)/1024/1024):.2f}MB",
                    "play": v.get('play'),
                    "hdplay": v.get('hdplay'),
                    "music": v.get('music')
                })
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url_input, download=False)
            return jsonify({
                "status": "success",
                "title": info.get('title', 'Video'),
                "cover": info.get('thumbnail'),
                "author": info.get('uploader', 'Unknown'),
                "duration": f"{info.get('duration', 0)}s",
                "size": "N/A",
                "play": info.get('url'),
                "hdplay": info.get('url')
            })
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})

@app.route('/api/get_video')
def get_video_api():
    video_url = request.args.get('url')
    fallback_url = request.args.get('fallback')
    title = request.args.get('title', 'video')
    
    if not video_url:
        return "URL Kosong", 400

    try:
        r, used_fallback = fetch_video_stream(video_url, fallback_url)
        
        if r is None or r.status_code >= 400:
             return "Gagal: Video tidak ditemukan atau link kadaluarsa (Access Denied).", 403

        content_type = r.headers.get('Content-Type', '').lower()
        if 'text/html' in content_type:
             return "Gagal: Server mengirimkan file korup (HTML).", 403

        safe_title = re.sub(r'[^a-zA-Z0-9]', '_', title)[:40] or 'video'
        
        headers = {
            'Content-Type': content_type,
            'Content-Disposition': f'attachment; filename="[vinder]_{safe_title}.mp4"',
            'Cache-Control': 'no-cache'
        }
        
        return Response(stream_with_context(r.iter_content(chunk_size=1024*1024)), headers=headers)
    except Exception as e:
        return f"Error: {str(e)}", 500

def resolve_tiktok_url(url):
    """Resolve short TikTok URL (vt.tiktok.com) ke URL lengkap."""
    try:
        r = session.head(url, allow_redirects=True, timeout=10)
        resolved = r.url
        logger.info(f"🔗 Resolved: {url} → {resolved}")
        return resolved
    except Exception as e:
        logger.warning(f"⚠️ Gagal resolve URL: {e}")
        return url


def get_tiktok_audio_via_tikwm(tiktok_url):
    """
    Fallback: ambil direct audio URL dari TikWM API
    kalau yt-dlp gagal extract (status code 0 / region block).
    """
    try:
        resp = session.get(f"https://www.tikwm.com/api/?url={tiktok_url}", timeout=15)
        data = resp.json()
        if data.get('code') == 0:
            v = data['data']
            music_url = v.get('music')  # direct MP3/audio URL
            cover_url = v.get('cover') or v.get('origin_cover')
            title     = v.get('title', 'audio')
            return music_url, cover_url, title
    except Exception as e:
        logger.warning(f"⚠️ TikWM fallback gagal: {e}")
    return None, None, None


@app.route('/api/get_mp3')
def get_mp3_api():
    tiktok_url = request.args.get('tiktok_url') or request.args.get('url')
    title      = request.args.get('title', 'audio')

    if not tiktok_url:
        return "URL Kosong", 400

    # Resolve short URL dulu (vt.tiktok.com → www.tiktok.com/...)
    if 'vt.tiktok.com' in tiktok_url or 'vm.tiktok.com' in tiktok_url:
        tiktok_url = resolve_tiktok_url(tiktok_url)

    safe_title = re.sub(r'[^a-zA-Z0-9]', '_', title)[:40] or 'audio'
    uid        = str(int(time.time() * 1000))
    out_tmpl   = f'/tmp/vinder_{uid}'
    out_mp3    = out_tmpl + '.mp3'

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': out_tmpl + '.%(ext)s',
        'postprocessors': [
            {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'},
            {'key': 'EmbedThumbnail'},
            {'key': 'FFmpegMetadata', 'add_metadata': True},
        ],
        'writethumbnail': True,
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'user_agent': TIKTOK_UA,
        'http_headers': DEFAULT_HEADERS,
    }

    def do_cleanup():
        for ext in ['.mp3', '.jpg', '.jpeg', '.png', '.webp', '.m4a', '.webm', '.opus']:
            f = out_tmpl + ext
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

    try:
        logger.info(f"🎵 MP3 Processing (yt-dlp): {tiktok_url}")

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([tiktok_url])
        except Exception as ydl_err:
            logger.warning(f"⚠️ yt-dlp gagal: {ydl_err} — coba fallback TikWM...")

            # Fallback: ambil audio URL langsung dari TikWM, download manual
            music_url, cover_url, api_title = get_tiktok_audio_via_tikwm(tiktok_url)
            if not music_url:
                raise Exception(f"yt-dlp gagal dan TikWM fallback tidak menemukan audio. Error awal: {ydl_err}")

            logger.info(f"🎵 Fallback TikWM audio: {music_url}")
            title = api_title or title

            # Download audio langsung
            audio_resp = session.get(music_url, headers=DEFAULT_HEADERS, timeout=60, stream=True)
            audio_resp.raise_for_status()
            raw_audio = out_tmpl + '.mp3'
            with open(raw_audio, 'wb') as f:
                for chunk in audio_resp.iter_content(chunk_size=1024 * 512):
                    f.write(chunk)

            # Embed cover art manual pakai ffmpeg kalau ada
            if cover_url:
                try:
                    cover_resp = session.get(cover_url, timeout=15)
                    cover_path = out_tmpl + '.jpg'
                    with open(cover_path, 'wb') as f:
                        f.write(cover_resp.content)
                    # ffmpeg embed cover ke mp3
                    import subprocess
                    tagged = out_tmpl + '_tagged.mp3'
                    subprocess.run([
                        'ffmpeg', '-y',
                        '-i', raw_audio,
                        '-i', cover_path,
                        '-map', '0:a', '-map', '1:v',
                        '-c:a', 'copy', '-c:v', 'mjpeg',
                        '-id3v2_version', '3',
                        '-metadata:s:v', 'title=Album cover',
                        '-metadata:s:v', 'comment=Cover (front)',
                        tagged
                    ], check=True, capture_output=True)
                    os.replace(tagged, raw_audio)
                except Exception as cover_err:
                    logger.warning(f"⚠️ Cover embed gagal (tidak fatal): {cover_err}")

        if not os.path.exists(out_mp3):
            do_cleanup()
            return "Gagal: File MP3 tidak berhasil dibuat.", 500

        logger.info(f"✅ MP3 siap dikirim: {out_mp3}")

        with open(out_mp3, 'rb') as f:
            audio_data = f.read()

        do_cleanup()

        return Response(
            audio_data,
            mimetype='audio/mpeg',
            headers={
                'Content-Disposition': f'attachment; filename="[vinder]_{safe_title}.mp3"',
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