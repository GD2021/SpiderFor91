CRAWLER_NAME = "pimpbunny"

import subprocess
import re
import json
import sys
import os
import time
import tempfile
from collections import defaultdict

LIST_URL = 'https://pimpbunny.com/videos/?sort_by=video_viewed'
BASE_URL = 'https://pimpbunny.com'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

QUALITY_RANK = {'1440p': 6, '1080p': 5, '720p': 4, '360p': 3, 'original': 2, 'standard': 1}


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def load_seen(path):
    seen = set()
    if path and os.path.exists(path):
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if line:
                    seen.add(line)
    return seen


def clean_source_id(raw):
    cleaned = re.sub(r'[^a-zA-Z0-9_.\-]', '-', raw)
    return cleaned[:160]


def get_quality(url):
    if '_1440p' in url: return '1440p'
    if '_1080p' in url: return '1080p'
    if '_720p' in url: return '720p'
    if '_360p' in url: return '360p'
    m = re.search(r'/get_file/(\d+)/', url)
    if m and m.group(1) == '1': return 'original'
    return 'standard'


def curl_get(url, cookie_jar, referer='', max_time=10):
    try:
        cmd = ['curl', '-s', '-L', '--max-time', str(max_time),
               '-H', 'User-Agent: ' + UA,
               '-H', 'Accept-Language: en-US,en;q=0.9']
        if referer:
            cmd += ['-H', 'Referer: ' + referer]
        if os.path.exists(cookie_jar):
            cmd += ['-b', cookie_jar, '-c', cookie_jar]
        else:
            cmd += ['-c', cookie_jar]
        cmd.append(url)
        result = subprocess.run(cmd, capture_output=True, timeout=max_time + 5,
                                encoding='utf-8', errors='replace')
        return result.stdout or ''
    except Exception:
        return ''


def test_url(url, cookie_jar, referer):
    try:
        result = subprocess.run([
            'curl', '-s', '-L', '--max-time', '10',
            '-b', cookie_jar, '-c', cookie_jar,
            '-H', 'Referer: ' + referer,
            '-H', 'User-Agent: ' + UA,
            '-H', 'Range: bytes=0-65535',
            '-o', 'NUL',
            '-w', '%{http_code}|%{size_download}|%{content_type}',
            url
        ], capture_output=True, text=True, timeout=12,
           encoding='utf-8', errors='replace')
        parts = result.stdout.strip().split('|')
        code = int(parts[0]) if parts[0].isdigit() else 0
        size = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        ctype = parts[2] if len(parts) > 2 else ''
        return code in (200, 206) and 'video' in ctype and size > 1000, size
    except Exception:
        return False, 0


def scrape_listing_page():
    log('[+] Scraping listing page: ' + LIST_URL)
    result = subprocess.run(
        ['curl', '-s', '-L', '--max-time', '15',
         '-H', 'User-Agent: ' + UA, LIST_URL],
        capture_output=True, text=True, timeout=20,
        encoding='utf-8', errors='replace'
    )
    html = result.stdout
    if not html:
        log('[!] Failed to load listing page')
        return []

    videos = []
    seen = set()
    pattern = re.compile(
        r'<a[^>]*href="(?:https://pimpbunny\.com)?(/videos/([a-z0-9][a-z0-9-]+?)/)"[^>]*>'
        r'(?:.*?<img[^>]*src="([^"]+)"[^>]*alt="([^"]*)")?',
        re.DOTALL | re.IGNORECASE
    )
    for m in pattern.finditer(html):
        slug = m.group(2)
        if slug in seen:
            continue
        seen.add(slug)
        title = m.group(4) or slug.replace('-', ' ').title()
        thumbnail = m.group(3) or ''
        if thumbnail.startswith('data:') or len(thumbnail) < 10:
            thumbnail = ''
        videos.append({
            'slug': slug,
            'title': title,
            'thumbnail': thumbnail,
            'page_url': BASE_URL + '/videos/' + slug + '/'
        })

    log('[+] Found ' + str(len(videos)) + ' videos on listing page')
    return videos


def process_video(video, cookie_jar):
    slug = video['slug']
    page_url = video['page_url']

    html = curl_get(page_url, cookie_jar, referer=LIST_URL, max_time=10)
    if not html:
        return None

    all_urls = re.findall(
        r'https://pimpbunny\.com/get_file/\d+/[a-f0-9]+/\d+/\d+[^"\'<>\s]+',
        html
    )

    groups = defaultdict(list)
    for url in all_urls:
        m = re.search(r'/get_file/\d+/[a-f0-9]+/(\d+)/(\d+)', url)
        if m and '_preview' not in url:
            groups[m.group(2)].append(url)

    main_urls = []
    real_video_id = ''
    for vid, urls in groups.items():
        if len(urls) > len(main_urls):
            main_urls = sorted(set(u.rsplit('?', 1)[0] for u in urls))
            real_video_id = vid

    working_urls = []
    for url in main_urls:
        ok, size = test_url(url, cookie_jar, page_url)
        if ok:
            q = get_quality(url)
            working_urls.append({'url': url, 'quality': q, 'size_mb': round(size / 1024 / 1024, 1)})

    working_urls.sort(key=lambda u: QUALITY_RANK.get(u['quality'], 0), reverse=True)

    if not working_urls:
        return None

    best = working_urls[0]
    return {
        'media_url': best['url'],
        'media_quality': best['quality'],
        'real_video_id': real_video_id,
        'all_qualities': working_urls,
    }


def emit_item(video, item):
    out = {
        'type': 'item',
        'source_id': clean_source_id(video['slug']),
        'title': video['title'],
        'media_url': item['media_url'],
        'thumbnail_url': video.get('thumbnail', ''),
        'detail_url': video['page_url'],
        'headers': {'Referer': BASE_URL + '/'},
        'thumbnail_headers': {'Referer': BASE_URL + '/'},
    }
    if item.get('media_quality'):
        out['quality'] = item['media_quality']
    if item.get('real_video_id'):
        out['media_id'] = item['real_video_id']
    print(json.dumps(out, ensure_ascii=False), flush=True)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--job', required=True, help='Path to job.json')
    args = parser.parse_args()

    with open(args.job, 'r', encoding='utf-8') as f:
        job = json.load(f)

    candidate_budget = job.get('candidate_budget') or job.get('target_new') or 10
    try:
        candidate_budget = int(candidate_budget)
    except (ValueError, TypeError):
        candidate_budget = 10
    if candidate_budget <= 0:
        candidate_budget = 10

    seen_path = job.get('seen_source_ids_file', '')
    proxy_url = job.get('network', {}).get('proxy_url', '')

    log('[+] Job loaded: candidate_budget=' + str(candidate_budget) +
        ' seen=' + str(seen_path) + ' proxy=' + str(proxy_url or 'none'))

    seen = load_seen(seen_path)
    log('[+] Loaded ' + str(len(seen)) + ' seen IDs')

    if proxy_url:
        os.environ['http_proxy'] = proxy_url
        os.environ['https_proxy'] = proxy_url
        os.environ['HTTP_PROXY'] = proxy_url
        os.environ['HTTPS_PROXY'] = proxy_url
        log('[+] Proxy configured: ' + proxy_url)

    videos = scrape_listing_page()
    if not videos:
        log('[!] No videos found')
        sys.exit(0)

    checked = 0
    emitted = 0
    cookie_jar = tempfile.mktemp(suffix='.txt')

    for video in videos:
        source_id = clean_source_id(video['slug'])

        if source_id in seen:
            log('[-] Skip seen: ' + source_id)
            checked += 1
            continue

        if emitted >= candidate_budget:
            log('[+] Reached candidate_budget=' + str(candidate_budget) + ', stopping')
            break

        log('[*] Processing: ' + video['title'][:55])

        item = process_video(video, cookie_jar)
        checked += 1

        if item is None:
            log('[-] No working URL for: ' + video['slug'])
            continue

        emit_item(video, item)
        emitted += 1
        log('[+] Emitted #' + str(emitted) + ': ' + source_id +
            ' [' + item.get('media_quality', '?') + ']')

        time.sleep(0.3)

    try:
        os.unlink(cookie_jar)
    except Exception:
        pass

    done = {
        'type': 'done',
        'stats': {
            'checked': checked,
            'emitted': emitted,
        }
    }
    print(json.dumps(done, ensure_ascii=False), flush=True)

    log('[+] Finished: checked=' + str(checked) + ' emitted=' + str(emitted))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        log('[!] Interrupted')
        sys.exit(0)
    except BrokenPipeError:
        sys.exit(0)
