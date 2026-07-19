import re
import sys
import time
import concurrent.futures
from urllib.parse import urljoin, urlparse

import requests

PLAYLIST = "playlist.m3u"
TEST_DURATION = 15
TIMEOUT = (5, TEST_DURATION + 5)
CONCURRENCY = 8
CHUNK = 64 * 1024

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Encoding": "identity",
    "Accept": "*/*",
}


def parse_playlist(path):
    entries = []
    name = None
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if line.startswith("#EXTINF"):
                m = re.search(r",(.+)$", line)
                name = m.group(1).strip() if m else None
            elif line and not line.startswith("#"):
                entries.append((name or "?", line))
                name = None
    return entries


def fetch_text(url):
    r = requests.get(url, headers=HEADERS, timeout=(5, 10))
    r.raise_for_status()
    return r.text


def resolve_segments(playlist_url, text):
    """Return list of segment absolute urls from a media playlist,
    following one level of master->variant indirection."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    # master playlist?
    if any(l.startswith("#EXT-X-STREAM-INF") for l in lines):
        # pick the variant with the highest BANDWIDTH if present
        best = None
        best_bw = -1
        for i, l in enumerate(lines):
            if l.startswith("#EXT-X-STREAM-INF"):
                m = re.search(r"BANDWIDTH=(\d+)", l)
                bw = int(m.group(1)) if m else 0
                if i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                    if bw >= best_bw:
                        best_bw = bw
                        best = lines[i + 1]
        if not best:
            return []
        variant_url = urljoin(playlist_url, best)
        try:
            vtext = fetch_text(variant_url)
        except Exception:
            return []
        return resolve_segments(variant_url, vtext)
    # media playlist: collect segment lines
    segs = []
    for l in lines:
        if l.startswith("#"):
            continue
        if l.split("?")[0].lower().endswith((".ts", ".m4s", ".aac", ".mp4")) or \
           "://" in l or l.endswith(".m3u8"):
            segs.append(urljoin(playlist_url, l))
    return segs


def test_url(url, duration=TEST_DURATION):
    start = time.time()
    try:
        text = fetch_text(url)
    except Exception as e:
        return False, 0, f"playlist {type(e).__name__}: {e}"

    segs = resolve_segments(url, text)
    if not segs:
        return False, 0, "no segments found"

    total = 0
    seg_idx = 0
    # round-robin over segments like a real player
    while time.time() - start < duration:
        seg_url = segs[seg_idx % len(segs)]
        seg_idx += 1
        try:
            with requests.get(seg_url, headers=HEADERS, stream=True,
                              timeout=(5, duration + 5)) as r:
                if r.status_code != 200:
                    continue
                for chunk in r.iter_content(chunk_size=CHUNK):
                    if chunk:
                        total += len(chunk)
                    if time.time() - start >= duration:
                        break
        except Exception:
            continue
        if time.time() - start >= duration:
            break

    elapsed = time.time() - start
    speed = (total * 8) / elapsed / 1_000_000 if elapsed > 0 else 0
    return True, speed, f"{total/1024/1024:.2f}MB/{seg_idx}seg"


def main():
    entries = parse_playlist(PLAYLIST)
    print(f"共 {len(entries)} 个源，测试时长 {TEST_DURATION}s，并发 {CONCURRENCY}\n")

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futures = {ex.submit(test_url, url): (name, url) for name, url in entries}
        done = 0
        for fut in concurrent.futures.as_completed(futures):
            name, url = futures[fut]
            done += 1
            ok, speed, info = fut.result()
            results.append((name, url, ok, speed, info))
            mark = "OK " if ok else "ERR"
            print(f"[{done}/{len(entries)}] {mark} {speed:8.3f} Mbps  {name}  ({info})",
                  flush=True)

    results.sort(key=lambda x: x[3], reverse=True)
    ok_results = [r for r in results if r[2]]
    ok_count = len(ok_results)
    avg = sum(r[3] for r in ok_results) / ok_count if ok_count else 0

    print("\n" + "=" * 72)
    print(f"成功 {ok_count}/{len(results)}，所有成功源平均真实速度 {avg:.3f} Mbps")
    print("=" * 72)
    print("排名(成功源):")
    for i, (name, url, ok, speed, info) in enumerate(ok_results, 1):
        print(f"{i:3}. {speed:8.3f} Mbps  {name}  {url}  ({info})")


if __name__ == "__main__":
    main()
