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

app = Flask(__name__)
CORS(app)

# Global Session dengan Headers yang lebih mirip Browser sungguhan
session = requests.Session()
DOWNLOAD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "video/mp4,video/*;q=0.9,audio/*;q=0.8,*/*;q=0.5",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.tiktok.com/",
    "Origin": "https://www.tiktok.com/",
    "Range": "bytes=0-"
}

def format_durasi(detik):
    if detik is None: return "?"
    try:
        m, s = divmod(int(detik), 60)
        return f"{m}m{s:02d}s"
    except: return "?"

def is_audio_only(response):
    ct = response.headers.get('Content-Type', '').lower()
    cl = int(response.headers.get('Content-Length', 0))
    if ct.startswith('audio/'): return True
    cd = response.headers.get('Content-Disposition', '').lower()
    if any(ext in cd for ext in ['.m4a', '.aac', '.mp3']): return True
    if cl > 0 and cl < 500000: # Dibawah 500KB pasti bukan video
        return True
    return False

def fetch_video_stream(url, fallback_url=None):
    try:
        r = session.get(url, stream=True, timeout=30, headers=DOWNLOAD_HEADERS, allow_redirects=True)
        if r.status_code != 200 or is_audio_only(r):
            if fallback_url and fallback_url != url:
                r_fallback = session.get(fallback_url, stream=True, timeout=30, headers=DOWNLOAD_HEADERS)
                if r_fallback.status_code == 200:
                    return r_fallback, True
        r.raise_for_status()
        return r, False
    except Exception as e:
        if fallback_url:
            return session.get(fallback_url, stream=True, timeout=30, headers=DOWNLOAD_HEADERS), True
        raise e

@app.route('/')
def index():
    return send_file('vinder.html')

@app.route('/api/search', methods=['POST'])
def search_videos_api():
    data = request.json
    keyword = data.get('keyword')
    limit = data.get('limit', 10)
    try:
        resp = session.post("https://www.tikwm.com/api/feed/search", 
                           data={"keywords": keyword, "count": limit, "HD": 1},
                           timeout=30)
        json_data = resp.json()
        if json_data.get('code') != 0: return jsonify({"status": "error", "msg": "Gagal mencari video"})
        
        videos = json_data.get('data', {}).get('videos', [])
        results = []
        for v in videos:
            results.append({
                'title': v.get('title', 'Video TikTok'),
                'duration': format_durasi(v.get('duration')),
                'play': v.get('play', ''),
                'hdplay': v.get('hdplay', '') or v.get('play', ''),
                'cover': v.get('origin_cover') or v.get('cover') or '',
                'size': f"{round(v.get('size', 0)/(1024*1024), 2)} MB" if v.get('size') else "?"
            })
        return jsonify({"status": "success", "data": results})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})

@app.route('/api/get_video')
def get_video_api():
    video_url = request.args.get('url')
    fallback_url = request.args.get('fallback')
    title = request.args.get('title', 'video')
    
    if not video_url:
        logger.error("❌ Request /api/get_video tanpa URL!")
        return "URL Kosong", 400

    try:
        r, used_fallback = fetch_video_stream(video_url, fallback_url)
        safe_title = re.sub(r'[^a-zA-Z0-9]', '_', title)[:40] or 'video'
        filename = f"VidFinder_{safe_title}_{int(time.time())}.mp4"

        def generate():
            try:
                for chunk in r.iter_content(chunk_size=1024*1024):
                    if chunk: yield chunk
            except Exception as e:
                logger.error(f"Stream error: {e}")

        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "video/mp4",
            "Access-Control-Expose-Headers": "Content-Length"
        }
        cl = r.headers.get('Content-Length')
        if cl: headers["Content-Length"] = cl
        return Response(stream_with_context(generate()), headers=headers)
    except Exception as e:
        logger.error(f"Proxy Error: {str(e)}")
        return f"Gagal mengambil video: {str(e)}", 500

@app.route('/api/download_url', methods=['POST'])
def download_url_api():
    data = request.json
    url_input = data.get('url')
    logger.info(f"🔗 Processing URL: {url_input}")
    
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'user_agent': DOWNLOAD_HEADERS['User-Agent']
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url_input, download=False)
            video_url = info.get('url')
            
            # Cari format MP4 asli jika link utama bukan MP4
            if not video_url or 'googlevideo' in video_url:
                formats = info.get('formats', [])
                for f in reversed(formats):
                    if f.get('vcodec') != 'none' and f.get('ext') == 'mp4' and f.get('url'):
                        video_url = f.get('url')
                        break

            if not video_url:
                return jsonify({"status": "error", "msg": "Video tidak ditemukan"})

            raw_size = info.get('filesize') or info.get('filesize_approx') or 0
            display_size = f"{round(raw_size/(1024*1024), 2)} MB" if raw_size > 0 else "?"

            # SINKRONISASI FIELD: Kirim 'url', 'hdplay', dan 'play' sekaligus agar Frontend tidak bingung
            return jsonify({
                "status": "success",
                "url": video_url,
                "hdplay": video_url, 
                "play": video_url,
                "title": info.get('title', 'Video TikTok'),
                "author": info.get('uploader', 'User'),
                "duration": format_durasi(info.get('duration')),
                "size": display_size,
                "cover": info.get('thumbnail', '')
            })
    except Exception as e:
        logger.error(f"yt-dlp Error: {str(e)}")
        return jsonify({"status": "error", "msg": f"Gagal: {str(e)}"})

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
