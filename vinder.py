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
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "video/mp4,video/*;q=0.9,audio/*;q=0.8,*/*;q=0.5",
    "Accept-Language": "en-US,en;q=0.9"
}

def format_durasi(detik):
    if detik is None: return "?"
    try:
        m, s = divmod(int(detik), 60)
        return f"{m}m{s:02d}s"
    except: return "?"

def is_invalid_video(response):
    ct = response.headers.get('Content-Type', '').lower()
    cl = int(response.headers.get('Content-Length', 0))
    # Jika HTML, sudah pasti bukan video (biasanya halaman error/captcha)
    if 'text/html' in ct or 'application/json' in ct:
        return True
    # Jika file terlalu kecil (di bawah 10KB), kemungkinan besar bukan video valid
    if cl > 0 and cl < 10000:
        return True
    return False

def fetch_video_stream(url, fallback_url=None):
    headers = DEFAULT_HEADERS.copy()
    if 'tiktok.com' in url:
        headers.update({
            "Referer": "https://www.tiktok.com/",
            "Origin": "https://www.tiktok.com/"
        })
    
    try:
        r = session.get(url, stream=True, timeout=30, headers=headers, allow_redirects=True)
        if r.status_code >= 400 or is_invalid_video(r):
            if fallback_url and fallback_url != url:
                r_fallback = session.get(fallback_url, stream=True, timeout=30, headers=headers)
                if r_fallback.status_code < 400 and not is_invalid_video(r_fallback):
                    return r_fallback, True
        r.raise_for_status()
        return r, False
    except Exception as e:
        if fallback_url and fallback_url != url:
            try:
                r_fb = session.get(fallback_url, stream=True, timeout=30, headers=headers)
                if r_fb.status_code < 400: return r_fb, True
            except: pass
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
        
        # Ambil content-type asli dari upstream
        content_type = r.headers.get('Content-Type', 'video/mp4')
        ext = 'mp4'
        if 'video/webm' in content_type: ext = 'webm'
        elif 'video/quicktime' in content_type: ext = 'mov'
        elif 'video/x-matroska' in content_type: ext = 'mkv'
        
        filename = f"VidFinder_{safe_title}_{int(time.time())}.{ext}"

        def generate():
            try:
                for chunk in r.iter_content(chunk_size=1024*1024):
                    if chunk: yield chunk
            except Exception as e:
                logger.error(f"Stream error: {e}")

        # Teruskan status code (terutama 206) dan headers penting
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": content_type,
            "Access-Control-Expose-Headers": "Content-Length, Content-Range"
        }
        
        for h in ['Content-Length', 'Content-Range', 'Accept-Ranges']:
            if h in r.headers:
                headers[h] = r.headers[h]
        
        return Response(stream_with_context(generate()), status=r.status_code, headers=headers)
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
        'user_agent': DEFAULT_HEADERS['User-Agent']
    }
    
    try:
        # Jika TikTok, coba pakai tikwm dulu karena lebih stabil daripada yt-dlp di server
        if 'tiktok.com' in url_input:
            try:
                # Resolve redirect safely
                resp = session.get(url_input, allow_redirects=True, timeout=10, headers=DEFAULT_HEADERS, stream=True)
                real_url = resp.url
                resp.close() # Close stream immediately
                
                video_id_match = re.search(r'video/(\d+)', real_url)
                if video_id_match:
                    # Menggunakan URL lengkap (setelah resolve redirect) lebih stabil untuk TikWM
                    api_resp = session.get(f"https://www.tikwm.com/api/?url={real_url}", timeout=10).json()
                    if api_resp.get('code') == 0:
                        v = api_resp.get('data', {})
                        video_url = v.get('hdplay') or v.get('play')
                        return jsonify({
                            "status": "success",
                            "url": video_url,
                            "hdplay": video_url,
                            "play": video_url,
                            "title": v.get('title', 'Video TikTok'),
                            "author": v.get('author', {}).get('nickname', 'User'),
                            "duration": format_durasi(v.get('duration')),
                            "size": f"{round(v.get('size', 0)/(1024*1024), 2)} MB" if v.get('size') else "?",
                            "cover": v.get('cover', '')
                        })
            except Exception as e:
                logger.warning(f"TikWM Fallback failed: {e}")

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

            return jsonify({
                "status": "success",
                "url": video_url,
                "hdplay": video_url, 
                "play": video_url,
                "title": info.get('title', 'Video'),
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
