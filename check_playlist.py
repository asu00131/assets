#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
检查 playlist.m3u 内各 m3u 播放流畅评分排序 (真实 HLS 播放模拟)

思路: 像播放器一样
  1. 拉主播放列表(可能是多码率, 自动选第一个)
  2. 拉切片列表, 得到每个 ts 的 URL 与时长
  3. 按"播放时钟"连续下载 ts, 维护缓冲区余量 buffer = 已到账时长 - 已播放时长
  4. 模拟播放 PLAY_SECONDS 秒, 统计:
       - stall: 缓冲区耗尽需等待新切片 = 卡顿
       - 平均下载速度 / 缓冲余量
  能稳定连续播放(无 stall、缓冲有余量)的排最前。

评分(总分 100):
  - 连通 (20): 能解析出可播放切片
  - 无卡顿 (50): 模拟播放期间 stall 次数, 每次扣 15, 0 次满分
  - 缓冲余量 (30): 最终 buffer 越长分越高(满 ~1 个切片时长满分)
"""

import concurrent.futures
import os
import re
import shutil
import sys
import time
import urllib.request
import urllib.error

PROG_LOG = "progress.log"


def log(msg):
    with open(PROG_LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")

M3U_PATH = "playlist.m3u"
TIMEOUT = 10
WORKERS = 8
PLAY_SECONDS = 15       # 模拟播放时长
CHAN_CAP = 40           # 单频道最大耗时(秒), 超时直接判失败
UA = "Mozilla/5.0 (Linux; Android) AppleWebKit/537.36"


def http_get(url, binary=True, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read() if binary else r.read().decode("utf-8", "ignore")


def join_url(base, rel):
    if rel.startswith("http://") or rel.startswith("https://"):
        return rel
    return base.rsplit("/", 1)[0] + "/" + rel


def parse_m3u8(base_url, depth=0):
    """返回 (segments, base_for_segments)。segments: list of (url, dur)。"""
    text = http_get(base_url, binary=False)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines or lines[0] != "#EXTM3U":
        raise ValueError("not m3u8")
    # 多级: 主列表含 #EXT-X-STREAM-INF -> 选带宽最高的子列表
    if any(l.startswith("#EXT-X-STREAM-INF") for l in lines):
        best = None
        best_bw = -1
        for i, l in enumerate(lines):
            if l.startswith("#EXT-X-STREAM-INF"):
                m = re.search(r"BANDWIDTH=(\d+)", l)
                bw = int(m.group(1)) if m else 0
                sub = lines[i + 1] if i + 1 < len(lines) else ""
                if bw >= best_bw:
                    best_bw = bw
                    best = sub
        if best:
            return parse_m3u8(join_url(base_url, best), depth + 1)
    # 切片列表
    segs = []
    dur = 0.0
    for l in lines:
        if l.startswith("#EXTINF"):
            m = re.search(r":([\d.]+)", l)
            dur = float(m.group(1)) if m else 0.0
        elif l and not l.startswith("#"):
            segs.append((join_url(base_url, l), dur))
    if not segs:
        raise ValueError("no segments")
    return segs, base_url


def play_channel(ch):
    """像真实播放器: 周期性刷新播放列表拿到"当前"切片并下载, 模拟 PLAY_SECONDS 直播。
    卡顿判定: 切片下载失败, 或下载耗时 > 切片时长(追不上播放节奏)。"""
    url = ch["url"]
    res = {"name": ch["name"], "group": ch["group"], "url": url,
           "score": 0, "detail": "", "stalls": 0, "buf": 0.0,
           "secs": 0.0, "rate": 0.0}
    try:
        segs, seg_base = parse_m3u8(url)
        played_dur = 0.0
        downloaded_dur = 0.0
        total_bytes = 0
        stalls = 0
        fetched_seqs = set()
        t_start = time.time()

        while played_dur < PLAY_SECONDS:
            if time.time() - t_start > CHAN_CAP:
                break
            # 重新拉列表, 拿最新切片(模拟播放器定时刷新)
            try:
                segs, _ = parse_m3u8(url)
            except Exception:
                stalls += 1
                time.sleep(0.3)
                continue
            if not segs:
                stalls += 1
                time.sleep(0.3)
                continue
            # 取列表最后一个(当前最新)切片
            seg_url, dur = segs[-1]
            key = seg_url
            if key in fetched_seqs:
                # 还没出新片, 等一会儿(真实播放器行为)
                time.sleep(min(0.5, dur / 2))
                played_dur += 0.5
                continue
            fetched_seqs.add(key)
            t0 = time.time()
            try:
                data = http_get(seg_url, binary=True, timeout=TIMEOUT)
            except Exception:
                stalls += 1
                time.sleep(0.2)
                continue
            dl = time.time() - t0
            total_bytes += len(data)
            downloaded_dur += dur
            played_dur += dur
            # 下载耗时超过切片时长 -> 追不上 -> 卡顿
            if dl > dur:
                stalls += 1
            # 缓冲余量被追平 -> 卡顿
            if downloaded_dur <= played_dur + 0.001:
                stalls += 1

        elapsed = time.time() - t_start
        rate = (total_bytes / elapsed) if elapsed > 0 else 0
        buf = downloaded_dur - played_dur

        conn = 20 if played_dur > 0 else 0
        cont = max(0, 50 - stalls * 15)
        buf_score = max(0, min(30, buf / 10.0 * 30))
        res["score"] = int(conn + cont + buf_score)
        res["stalls"] = stalls
        res["buf"] = round(buf, 1)
        res["secs"] = round(played_dur, 1)
        res["rate"] = rate / 1024
        res["detail"] = (f"play={played_dur:.1f}s segs={len(segs)} "
                         f"stalls={stalls} buf={buf:.1f}s rate={rate/1024:.1f}KB/s")
    except urllib.error.HTTPError as e:
        res["detail"] = f"HTTP {e.code}"
        res["score"] = 5
    except Exception as e:
        res["detail"] = type(e).__name__
        res["score"] = 0
    return res


def parse_m3u(path):
    channels = []
    name = None
    group = None
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("#EXTINF"):
                m = re.search(r'group-title="([^"]*)"', line)
                group = m.group(1) if m else ""
                name = line.split(",")[-1].strip()
            elif line.startswith("http://") or line.startswith("https://"):
                if name is None:
                    name = line
                channels.append({"name": name, "group": group, "url": line})
                name = None
                group = None
    return channels


def main():
    if os.path.exists(PROG_LOG):
        os.remove(PROG_LOG)
    channels = parse_m3u(M3U_PATH)
    print(f"共解析到 {len(channels)} 个频道，开始 15s 级播放模拟...\n")
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(play_channel, c): c for c in channels}
        done = 0
        for fut in concurrent.futures.as_completed(futs):
            r = fut.result()
            results.append(r)
            done += 1
            bar = "#" * int(r["score"] / 5)
            line = f"[{done}/{len(channels)}] {r['score']:>3} {bar:<20} " \
                   f"{r['name'][:16]:<16} buf={r['buf']:>5}s stall={r['stalls']}  {r['detail']}"
            print(line)
            log(line)

    results.sort(key=lambda x: (x["score"], x["buf"]), reverse=True)
    print("\n" + "=" * 72)
    print("排名结果 (按真实播放流畅度降序):")
    print("=" * 72)
    print(f"{'#':>3} {'分':>3} {'缓冲':>6} {'卡顿':>4} {'分组':<9} {'名称':<18} URL")
    for i, r in enumerate(results, 1):
        url_short = r["url"] if len(r["url"]) <= 40 else r["url"][:37] + "..."
        print(f"{i:>3} {r['score']:>3} {r['buf']:>5}s {r['stalls']:>4} "
              f"{r['group'][:8]:<9} {r['name'][:16]:<18} {url_short}")

    avg = sum(r["score"] for r in results) / len(results) if results else 0
    alive = sum(1 for r in results if r["score"] > 0)
    smooth = sum(1 for r in results if r["stalls"] == 0 and r["score"] > 0)
    print(f"\n总计: {len(results)} | 可用: {alive} | 流畅(0卡顿): {smooth} | 平均分: {avg:.1f}")

    # 写回排序后的 playlist
    src = M3U_PATH
    bak = src + ".bak"
    if not os.path.exists(bak):
        shutil.copy(src, bak)
        print(f"已备份原文件到: {bak}")
    score_by_url = {r["url"]: r["score"] for r in results}
    blocks = [(score_by_url[ch["url"]], ch) for ch in channels if ch["url"] in score_by_url]
    blocks.sort(key=lambda x: (x[0], x[1].get("buf", 0)), reverse=True)
    out = ["#EXTM3U"]
    for score, ch in blocks:
        out.append(f'#EXTINF:-1 group-title="{ch["group"]}",{ch["name"]} ({score})')
        out.append(ch["url"])
    with open(src, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    print(f"已按流畅度排序重写 {src}（{len(blocks)} 条）")


if __name__ == "__main__":
    main()
