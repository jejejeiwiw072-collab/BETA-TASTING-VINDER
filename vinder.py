import os
import re
import subprocess
import requests
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

DOWNLOAD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Referer": "https://www.tikwm.com/",
    "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.8",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8",
}

def scan_media(filepath):
    # Pass (Android only command)
    pass

def format_durasi(detik):
    if detik is None: return "?"
    m, s = divmod(int(detik), 60)
    return f"{m}m{s:02d}s"

def parse_filter(filter_str):
    if not filter_str: return None
    filter_str = filter_str.strip()
    m1 = re.match(r'^>(\d+)m$', filter_str)
    if m1: return ('gt', int(m1.group(1)) * 60)
    m2 = re.match(r'^<(\d+)m$', filter_str)
    if m2: return ('lt', int(m2.group(1)) * 60)
    m3 = re.match(r'^(\d+)m-(\d+)m$', filter_str)
    if m3: return ('range', int(m3.group(1)) * 60, int(m3.group(2)) * 60)
    return None

def lolos_filter(durasi_detik, filter_parsed):
    if filter_parsed is None: return True
    if durasi_detik is None: return False
    t = filter_parsed[0]
    if t == 'gt': return durasi_detik > filter_parsed[1]
    if t == 'lt': return durasi_detik < filter_parsed[1]
    if t == 'range': return filter_parsed[1] <= durasi_detik <= filter_parsed[2]
    return True

def pilih_url_video(data_video):
    for key in ['play', 'hdplay', 'wmplay']:
        url = data_video.get(key, '').strip()
        if url and url.startswith('http'):
            return url, key
    return None, None

@app.route('/api/search', methods=['POST'])
def search_videos_api():
    data = request.json
    query = data.get('keyword', '')
    limit = int(data.get('limit', 5))
    filter_parsed = parse_filter(data.get('filter', ''))
    
    results = []
    cursor, page, max_page = 0, 0, 5

    while len(results) < limit and page < max_page:
        try:
            resp = requests.post("https://www.tikwm.com/api/feed/search", data={"keywords": query, "count": 20, "cursor": cursor, "HD": 1}, timeout=15)
            api_data = resp.json()
        except Exception as e:
            return jsonify({"status": "error", "msg": str(e)})

        if api_data.get('code') != 0: break
        videos = api_data.get('data', {}).get('videos', [])
        if not videos: break

        for v in videos:
            durasi = v.get('duration')
            durasi = int(durasi) if durasi else None
            if not lolos_filter(durasi, filter_parsed): continue

            uid = v.get('author', {}).get('unique_id', '')
            vid_id = v.get('id', '')
            results.append({
                'title': v.get('title', 'Tanpa Judul'),
                'id': vid_id,
                'link': f"https://www.tiktok.com/@{uid}/video/{vid_id}",
                'duration': format_durasi(durasi),
                'channel': v.get('author', {}).get('nickname', 'Unknown'),
                'play': v.get('play', ''),
                'hdplay': v.get('hdplay', ''),
                'wmplay': v.get('wmplay', '')
            })
            if len(results) >= limit: break
        cursor = api_data.get('data', {}).get('cursor', 0)
        page += 1

    return jsonify({"status": "success", "data": results})

@app.route('/api/download', methods=['POST'])
def download_video_api():
    video = request.json
    folder = 'downloads/'
    os.makedirs(folder, exist_ok=True)

    video_url, _ = pilih_url_video(video)

    if not video_url:
        try:
            resp = requests.post("https://www.tikwm.com/api/", data={"url": video['link'], "hd": 1}, timeout=15)
            api_data = resp.json()
            if api_data.get('code') == 0:
                video_url, _ = pilih_url_video(api_data['data'])
        except:
            pass

    if not video_url: return jsonify({"status": "error", "msg": "URL tidak ditemukan"})

    safe_title = re.sub(r'[\\/*?:"<>|]', '', video.get('title', 'video'))[:50].strip()
    filename = os.path.join(folder, f"{safe_title}_{video.get('id', 'id')}.mp4")

    try:
        r = requests.get(video_url, stream=True, timeout=60, headers=DOWNLOAD_HEADERS)
        r.raise_for_status()
        with open(filename, 'wb') as f:
            for chunk in r.iter_content(chunk_size=1024 * 64):
                if chunk: f.write(chunk)

        if os.path.getsize(filename) < 10240:
            os.remove(filename)
            return jsonify({"status": "error", "msg": "File terlalu kecil/corrupt"})

        scan_media(filename)
        return jsonify({"status": "success", "msg": f"Tersimpan: {filename}"})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})

@app.route('/api/download_url', methods=['POST'])
def download_url_api():
    url = request.json.get('url')
    try:
        resp = requests.post("https://www.tikwm.com/api/", data={"url": url, "hd": 1}, timeout=15)
        data = resp.json()
        if data.get('code') != 0:
            return jsonify({"status": "error", "msg": "API error"})
        
        d = data['data']
        video_obj = {
            'title': d.get('title', 'Tanpa Judul'),
            'id': d.get('id', ''),
            'link': url,
            'play': d.get('play', ''),
            'hdplay': d.get('hdplay', ''),
            'wmplay': d.get('wmplay', '')
        }
        
        with app.test_request_context('/api/download', method='POST', json=video_obj):
            return download_video_api()
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})

@app.route('/')
def index():
    # Pastikan path ini mengarah ke lokasi file HTML lu disimpan
    return send_file('vinder.html')

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)


