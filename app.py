from flask import Flask, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import time
import os
import sys
import re
import threading
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

VERSION = "1.0.0"

def check_for_updates():
    """patched-by-zyjk: 自更新已彻底禁用。

    原逻辑：远端 /message 报 4.2.1 > 内置 1.0.0，就去 /latest 把返回内容
    直接写进 __file__。而 /latest 现在返回的是 JSON 数据（不是 Python 代码），
    app.py 会被覆盖成 JSON，容器下次重启直接语法错误起不来。
    """
    print("[patch] 自更新已禁用（避免 app.py 被远端 JSON 覆盖）")
    return


# Set Timezone to Asia/Shanghai for Docker environment
if os.name != 'nt':  # Only on Unix-like systems (Docker usually runs Linux)
    os.environ['TZ'] = 'Asia/Shanghai'
    try:
        time.tzset()
    except Exception:
        pass

app = Flask(__name__)

def _env(name, default):
    """patched-by-zyjk: env 为空字符串时回退默认值。

    GitHub Actions 定时触发时，未填的 workflow_dispatch input 会渲染成空串，
    直接 int("") / float("") 会在 import 阶段就崩掉整轮任务。
    """
    _v = os.environ.get(name)
    if _v is None:
        return default
    _v = _v.strip()
    return _v if _v else default


# Constants
API_URL = "https://iptvs.pes.im"
CACHE_FILE = "iptv_sources.m3u8"
TXT_CACHE_FILE = "iptv_sources.txt"
CHANNEL_LIST_FILE = "channel_list.txt"
ADDRESS_LIST_FILE = "address_list.txt"
HSMD_ADDRESS_LIST_FILE = "hsmd_address_list.txt"
ZHGXTV_INTERFACE = "/ZHGXTV/Public/json/live_interface.txt"
TXIPTV_TEST_URI = "/tsfile/live/0001_1.m3u8"
HSMDTV_TEST_URI = "/newlive/live/hls/1/live.m3u8"
MAX_WORKERS = int(_env("MAX_WORKERS", "40"))  # patched-by-zyjk
MIN_SPEED = float(_env("MIN_SPEED", "0.05"))  # patched-by-zyjk
KEEP_UNKNOWN = _env("KEEP_UNKNOWN", "1") == "1"  # patched-by-zyjk
# 全局 host 上限。实测 2318 个候选里只有 1%~2% 还能连上，
# 真正的瓶颈是存活数而不是这个上限，所以开到 60，把活的全用上。
TOP_N = int(_env("TOP_N", "60"))  # patched-by-zyjk
# 取频道列表的超时。原版写死 2 秒（txiptv/jsmpeg），跨省链路稍抖就判死
LIST_TIMEOUT = float(_env("LIST_TIMEOUT", "6"))  # patched-by-zyjk
LIST_RETRIES = int(_env("LIST_RETRIES", "1"))  # patched-by-zyjk
# 额外候选名单：逗号分隔的 raw 地址（CSV 需含 host 列，txt 每行一个 host[:port]）
HOST_LIST_URLS = (os.environ.get("HOST_LIST_URLS") or "").strip()  # patched-by-zyjk
# 每个节目（同名频道）最多保留几条源，尽量来自不同 host；0 = 不限
PER_CHANNEL = int(_env("PER_CHANNEL", "3"))  # patched-by-zyjk
# m3u 标题是否追加速度。播放器里能直接看到"源1 2.35MB/s"
SHOW_SPEED = _env("SHOW_SPEED", "1") != "0"  # patched-by-zyjk
# 容器内自动更新时间，24 小时制、逗号分隔（默认早 7 点、晚 18 点）
UPDATE_TIMES = _env("UPDATE_TIMES", "07:00,18:00")  # patched-by-zyjk

# ---------------------------------------------------------------------------
# patched-by-zyjk: 清晰度过滤
#   HD_ONLY=0  全部保留（原上游行为）
#   HD_ONLY=1  默认：丢掉标注为标清的频道，未标注的保留
#   HD_ONLY=2  只要标注了高清/超清/4K 的频道，未标注的一律不要
# ---------------------------------------------------------------------------
HD_ONLY = _env("HD_ONLY", "1")

# 标清特征：中文标注 / SD 词元（HDSD、CCTV5SD 这种不带分隔的也认）
_SD_PAT = re.compile(
    r"标清|標清|标清画质|标准清晰|标准画质|普清|低清|流畅|平滑|省流|"
    r"(?<![A-Za-z])SD(?![A-Za-z])|SDTV|480[PI]|360[PI]",
    re.I,
)
# 高清特征
_HD_PAT = re.compile(
    r"高清|超清|超高清|蓝光|原画|4K|8K|UHD|FHD|HD|1080|2160|"
    r"H\.?26[45]|HEVC|50帧|60帧",
    re.I,
)

HD_STATS = {"kept": 0, "sd_dropped": 0, "unlabeled_dropped": 0, "sd_sample": []}


def is_sd_channel(name):
    name = (name or "").strip()
    return bool(name) and bool(_SD_PAT.search(name))


def is_hd_channel(name):
    """明确标注了高清的才算 True（"HDSD" 这类先判为标清）。"""
    name = (name or "").strip()
    return bool(name) and bool(_HD_PAT.search(name)) and not is_sd_channel(name)


def keep_channel(name):
    """按 HD_ONLY 判断某条频道是否保留，顺带累计统计。"""
    if HD_ONLY == "0":
        HD_STATS["kept"] += 1
        return True
    if is_sd_channel(name):
        HD_STATS["sd_dropped"] += 1
        if len(HD_STATS["sd_sample"]) < 8:
            HD_STATS["sd_sample"].append(name.strip())
        return False
    if HD_ONLY == "2" and not is_hd_channel(name):
        HD_STATS["unlabeled_dropped"] += 1
        return False
    HD_STATS["kept"] += 1
    return True


global_m3u8_content = ""
global_txt_content = ""
last_run_time = "Never"
is_running = False

def get_standard_channel_map():
    """Returns a dict mapping 'normalized' names to standard names from channel_list.txt."""
    mapping = {}
    try:
        if os.path.exists(CHANNEL_LIST_FILE):
            with open(CHANNEL_LIST_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    std_name = line.strip()
                    if not std_name: continue
                    # Normalize key: remove hyphens, spaces, uppercase
                    key = std_name.replace('-', '').replace(' ', '').upper()
                    # Also handle CCTV1 -> CCTV-1 specifically if standard is CCTV-1
                    # Actually standard name IS the value. Key is normalization.
                    mapping[key] = std_name
    except Exception as e:
        print(f"Error loading channel map: {e}")
    return mapping

def map_to_standard_name(name, mapping):
    """Maps a potentially variant name to a standard one if matches."""
    key = name.replace('-', '').replace(' ', '').upper()
    return mapping.get(key, name)

def fetch_api_data():
    """patched-by-zyjk: 远端 API 的 results 已是空数组，改为读镜像内的本地 CSV。

    远端 https://iptvs.pes.im/ 当前返回：
        {"results": [], "storageSummary": {"totalStoredCount": 0}, ...}
    从 2026-10-28 起每次定时任务的 resultCount 都是 0，因此拿不到任何 host。
    本地 CSV 来自 lihao-1-2/ZYJK，已随镜像打包在 /app 下。
    """
    import csv as _csv
    _base = os.path.dirname(os.path.abspath(__file__))
    _map = [("jsmpeg", "jsmpeg.csv"), ("txiptv", "txiptv.csv"), ("zhgxtv", "zhgxtv.csv")]
    out = []
    seen = set()
    for _mode, _fn in _map:
        _p = os.path.join(_base, _fn)
        if not os.path.exists(_p):
            print("[patch] 缺少 %s，跳过该模式" % _p)
            continue
        try:
            with open(_p, "r", encoding="utf-8-sig") as _f:
                for _row in _csv.DictReader(_f):
                    _host = (_row.get("host") or "").strip()
                    if not _host:
                        continue
                    _key = (_host, _mode)
                    if _key in seen:
                        continue
                    seen.add(_key)
                    out.append({"host": _host, "matchType": _mode, "source": "local:" + _fn})
        except Exception as _e:
            print("[patch] 读取 %s 失败: %s" % (_fn, _e))
    print("[patch] 本地候选 host 共 %d 个" % len(out))

    # patched-by-zyjk: 合并外部候选名单（HOST_LIST_URLS，逗号分隔）
    # 三份 CSV 是静态快照，酒店盒子的 IP 换得很勤，实测存活率只剩 1%~2%。
    # 想让结果真的变多，唯一有效的办法就是喂新名单，而不是调超时参数。
    _ext_added = 0
    for _url in [u.strip() for u in HOST_LIST_URLS.split(",") if u.strip()]:
        _mt = "jsmpeg"
        for _m in ("txiptv", "zhgxtv", "jsmpeg"):
            if _m in _url.lower():
                _mt = _m
                break
        try:
            _resp = requests.get(_url, timeout=20)
            _resp.raise_for_status()
            _n = 0
            for _line in _resp.text.splitlines():
                _line = _line.strip()
                if not _line or _line.startswith("#"):
                    continue
                if _line.lower().startswith("host"):
                    continue  # 表头
                _host = re.split(r"[,\t;]", _line)[0].strip().strip('"')
                if not re.match(r"^[A-Za-z0-9.\-]+(:\d+)?$", _host):
                    continue
                _key = (_host, _mt)
                if _key in seen:
                    continue
                seen.add(_key)
                out.append({"host": _host, "matchType": _mt,
                            "source": "url:" + _url.rsplit("/", 1)[-1]})
                _n += 1
            _ext_added += _n
            print("[patch] 外部名单 %s → 新增 %d 个（按 %s 测）" % (_url, _n, _mt))
        except Exception as _e:
            print("[patch] 下载外部名单失败 %s: %s" % (_url, _e))
    if HOST_LIST_URLS:
        print("[patch] 外部名单合计新增 %d 个 host" % _ext_added)

    print("[patch] 候选 host 合计 %d 个" % len(out))
    return {"results": out}

def _get_list(url):
    """patched-by-zyjk: 取频道列表专用的带重试 GET。

    原版写死 timeout=2，且只有一次机会：跨境/跨省链路上丢一个包，
    这个 host 就被判成"死源"再也不会被考虑。这里把超时放到 LIST_TIMEOUT（默认 6s），
    并允许 LIST_RETRIES 次重试——只对"连不上"重试，4xx/5xx 不重试。
    """
    last = None
    for _ in range(max(1, LIST_RETRIES + 1)):
        try:
            return requests.get(url, timeout=LIST_TIMEOUT)
        except Exception as _e:  # 连接超时 / 拒绝 / DNS
            last = _e
    raise last


def get_download_speed(url):
    """
    Measures download speed of a given URL (usually a TS file).
    Returns speed in MB/s. Returns -1 if failed.
    """
    try:
        start_time = time.time()
        # Download first 512KB is usually enough for speed test, but here let's follow ZHGXTV full download logic or chunk
        # ZHGXTV uses content length / time. Let's limit read size to avoid huge files.
        # But TS files are small chunks usually.
        with requests.get(url, stream=True, timeout=10) as r:
            r.raise_for_status()
            size = 0
            # Read at most 10MB for test to ensure accuracy over longer time
            chunk_size = 8192
            limit_size = 10 * 1024 * 1024
            
            for chunk in r.iter_content(chunk_size=chunk_size):
                if chunk:
                    size += len(chunk)
                # Ensure we download enough data or for enough time
                if size > limit_size:
                    break
                # Or if time exceeds 8 seconds
                if time.time() - start_time > 8:
                    break
        
        duration = time.time() - start_time
        if duration == 0: duration = 0.001
        
        speed = (size / 1024 / 1024) / duration # MB/s
        return speed
    except Exception:
        return -1

def get_ts_url(m3u8_url):
    """
    Parses m3u8 to find the first TS segment URL.
    Returns the full TS URL.
    """
    try:
        response = requests.get(m3u8_url, timeout=5)
        if response.status_code != 200:
            return None
        
        lines = response.text.strip().split('\n')
        for line in lines:
            line = line.strip()
            if line and not line.startswith('#'):
                # Handle relative or absolute URLs
                if line.startswith('http'):
                    return line
                elif line.startswith('/'):
                    # Absolute path
                    base = m3u8_url.split('/')[0] + "//" + m3u8_url.split('/')[2]
                    return base + line
                else:
                    # Relative path
                    base = m3u8_url.rsplit('/', 1)[0]
                    return base + "/" + line
        return None
    except:
        return None

def test_host_speed(item):
    """
    Tests speed for a single host item.
    Returns { ...item, 'speed': speed_mb_s, 'channels': [optional_list_for_zhgxtv] }
    """
    host = item.get('host')
    match_type = item.get('matchType')
    
    if not host:
        return -1, None
        
    speed = -1
    channels = []

    try:
        if match_type == 'txiptv':
            # Updated logic to use JSON API like iptv.py
            json_url = f"http://{host}/iptv/live/1000.json?key=txiptv"
            try:
                # patched-by-zyjk: 原版 timeout=2 太紧，链接抖动就把活源判死
                response = _get_list(json_url)
                if response.status_code == 200:
                    json_data = response.json()
                    valid_channel_url = None
                    
                    if 'data' in json_data:
                        for item in json_data['data']:
                            if isinstance(item, dict):
                                name = item.get('name')
                                urlx = item.get('url')
                                
                                if not name or not urlx:
                                    continue
                                
                                if ',' in urlx:
                                    continue

                                full_url = ""
                                if 'http' in urlx:
                                    full_url = urlx
                                else:
                                    if urlx.startswith('/'):
                                        full_url = f"http://{host}{urlx}" 
                                    else:
                                        full_url = f"http://{host}/{urlx}"

                                channels.append({'name': name, 'url': full_url})
                                
                                if not valid_channel_url:
                                    valid_channel_url = full_url

                    if valid_channel_url:
                        ts_url = get_ts_url(valid_channel_url)
                        if ts_url:
                            speed = get_download_speed(ts_url)
                    else:
                        speed = -1
                else:
                    speed = -1
            except Exception as e:
                # print(f"TXIPTV JSON fetch failed for {host}: {e}")
                speed = -1
        
        elif match_type == 'hsmdtv':
            test_url = f"http://{host}{HSMDTV_TEST_URI}"
            ts_url = get_ts_url(test_url)
            if ts_url:
                speed = get_download_speed(ts_url)
        
        elif match_type == 'jsmpeg':
            # jsmpeg logic from all-z-j-new.py
            json_url = f"http://{host}/streamer/list"
            try:
                response = _get_list(json_url)
                if response.status_code == 200:
                    json_data = response.json()
                    valid_channel_url = None
                    for item in json_data:
                        name = item.get('name', '').strip()
                        key = item.get('key', '').strip()
                        if not name or not key:
                            continue
                        
                        full_url = f"http://{host}/hls/{key}/index.m3u8"
                        channels.append({'name': name, 'url': full_url})
                        
                        # Use the first valid channel for speed test
                        if not valid_channel_url:
                            valid_channel_url = full_url

                    if valid_channel_url:
                        ts_url = get_ts_url(valid_channel_url)
                        if ts_url:
                            speed = get_download_speed(ts_url)
                    else:
                        speed = -1
                else:
                    speed = -1
            except Exception as e:
                # print(f"JSMPEG fetch failed for {host}: {e}")
                speed = -1

        elif match_type == 'zhgxtv':
            # Referencing ZHGXTV.py: Fetch live_interface.txt first
            interface_url = f"http://{host}{ZHGXTV_INTERFACE}"
            target_response = _get_list(interface_url)
            if target_response.status_code == 200:
                content = target_response.content.decode('utf-8', errors='ignore')
                lines = content.split('\n')
                
                valid_channel_url = None
                
                # Parse channels here to save for later use (avoid re-fetching)
                for line in lines:
                    line = line.strip()
                    if ',' in line:
                        parts = line.split(',')
                        if len(parts) >= 2:
                            name = parts[0].strip()
                            url_part = parts[1].strip()
                            
                            # Reconstruct URL as per logic
                            
                            try:
                                full_url = ""
                                if url_part.startswith("http"):
                                    # Parse and replace host
                                    p = urlparse(url_part)
                                    # Reconstruct: scheme + netloc(host) + path + params + query + fragment
                                    # Since we want to use the current 'host' (which is ip:port)
                                    full_url = f"{p.scheme}://{host}{p.path}"
                                    if p.query:
                                        full_url += f"?{p.query}"
                                elif url_part.startswith("/"):
                                    full_url = f"http://{host}{url_part}"
                                else:
                                    full_url = f"http://{host}/{url_part}"
                                
                                channels.append({'name': name, 'url': full_url})
                                
                                if not valid_channel_url:
                                    valid_channel_url = full_url
                            
                            except Exception as e:
                                print(f"Error parsing line {line}: {e}")
                                continue
                if valid_channel_url:
                    ts_url = get_ts_url(valid_channel_url)
                    if ts_url:
                        speed = get_download_speed(ts_url)
                else:
                    speed = -1 # No valid channels found
            else:
                speed = -1

    except Exception as e:
        # print(f"Speed test failed for {host}: {e}")
        speed = -1

    # patched-by-zyjk: 把失败分个类。光看 -1 分不清是两种完全不同的情况：
    #   no_list  连频道列表都取不到（2s 超时 / 404 / JSON 解析失败）→ 这个 host 没用
    #   ts_fail  列表能取到，但首个 TS 分片 8s 内没拉下来       → 海外跑必然一片这个
    if speed > 0:
        reason = "ok"
    elif channels or match_type == "hsmdtv":
        reason = "ts_fail"
    else:
        reason = "no_list"

    # patched-by-zyjk: 只要高清——标清频道在这一步就剔除，顺带少吃内存
    if HD_ONLY != "0":
        channels = [c for c in channels if keep_channel(c.get("name"))]

    return speed, channels, reason

def clean_channel_name(name):
    """Clean and normalize channel name."""
    name = name.replace("cctv", "CCTV")
    name = name.replace("中央", "CCTV")
    name = name.replace("央视", "CCTV")
    for rep in ["高清", "超高", "HD", "标清", "频道", "-", " ", "PLUS", "＋", "(", ")"]:
        name = name.replace(rep, "" if rep not in ["PLUS", "＋"] else "+")
    name = re.sub(r"CCTV(\d+)台", r"CCTV\1", name)
    name_map = {
        "CCTV1综合": "CCTV1", "CCTV2财经": "CCTV2", "CCTV3综艺": "CCTV3", "CCTV4国际": "CCTV4",
        "CCTV4中文国际": "CCTV4", "CCTV4欧洲": "CCTV4", "CCTV5体育": "CCTV5", "CCTV6电影": "CCTV6",
        "CCTV7军事": "CCTV7", "CCTV7军农": "CCTV7", "CCTV7农业": "CCTV7", "CCTV7国防军事": "CCTV7",
        "CCTV8电视剧": "CCTV8", "CCTV9记录": "CCTV9", "CCTV9纪录": "CCTV9", "CCTV10科教": "CCTV10",
        "CCTV11戏曲": "CCTV11", "CCTV12社会与法": "CCTV12", "CCTV13新闻": "CCTV13", "CCTV新闻": "CCTV13",
        "CCTV14少儿": "CCTV14", "CCTV15音乐": "CCTV15", "CCTV16奥林匹克": "CCTV16",
        "CCTV17农业农村": "CCTV17", "CCTV17农业": "CCTV17", "CCTV5+体育赛视": "CCTV5+",
        "CCTV5+体育赛事": "CCTV5+", "CCTV5+体育": "CCTV5+", "CCTV01": "CCTV1", "CCTV02": "CCTV2", "CCTV03": "CCTV3", "CCTV04": "CCTV4",
        "CCTV05": "CCTV5", "CCTV06": "CCTV6", "CCTV07": "CCTV7", "CCTV08": "CCTV8", "CCTV09": "CCTV9"
    }
    name = name_map.get(name, name)
    return name

def process_txiptv_channels(channels, source_label, source_index):
    """Generates m3u8 entries for txiptv source using JSON parsing logic."""
    entries = []
    std_map = get_standard_channel_map()
    
    try:
        if not channels: return []
        for ch in channels:
            name = ch['name']
            url = ch['url']
            
            # Name cleaning 
            name = clean_channel_name(name)
            
            # Standardization
            name = map_to_standard_name(name, std_map)
            
            entries.append({'name': name, 'url': url, 'content': f'#EXTINF:-1 group-title="IPTV",{name}\n{url}', 'index': source_index})
            
    except Exception as e:
        print(f"Error processing txiptv channels: {e}")
    return entries

def process_hsmdtv_channels(host, source_label, source_index):
    """Generates m3u8 entries for hsmdtv source using hsmd_address_list.txt."""
    entries = []
    std_map = get_standard_channel_map()
    try:
        if not os.path.exists(HSMD_ADDRESS_LIST_FILE):
             print(f"{HSMD_ADDRESS_LIST_FILE} not found.")
             return []

        with open(HSMD_ADDRESS_LIST_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        for line in lines:
            line = line.strip()
            if not line: continue
            
            # Find HTTP URL
            match = re.search(r'(http://[^\s]+)', line)
            if match:
                url_in_file = match.group(1)
                
                # Extract Name: everything before URL
                part_before_url = line.split(url_in_file)[0]
                # Remove ID (digits) at start
                name = re.sub(r'^\s*\d+\s+', '', part_before_url).strip()
                # Clean name
                name = name.replace("（默认频道）", "").strip()
                # patched-by-zyjk: 只要高清，标清频道直接跳过（hsmdtv 的频道表来自本地 txt）
                if HD_ONLY != "0" and not keep_channel(name):
                    continue
                name = clean_channel_name(name)
                
                # Standardization
                name = map_to_standard_name(name, std_map)
                
                parsed = urlparse(url_in_file)
                new_url = f"http://{host}{parsed.path}"
                
                entries.append({
                    'name': name,
                    'url': new_url,
                    'content': f'#EXTINF:-1 group-title="IPTV",{name}\n{new_url}',
                    'index': source_index
                })
    except Exception as e:
        print(f"Error processing hsmdtv channels: {e}")
    return entries

def process_zhgxtv_channels(channels, source_label, source_index):
    """Generates m3u8 entries for zhgxtv source."""
    entries = []
    std_map = get_standard_channel_map()
    if not channels: return []
    for ch in channels:
        name = ch['name']
        url = ch['url']
        
        # Cleanup name
        name = clean_channel_name(name)
        
        # Standardization
        name = map_to_standard_name(name, std_map)
        
        entries.append({'name': name, 'url': url, 'content': f'#EXTINF:-1 group-title="IPTV",{name}\n{url}', 'index': source_index})
    return entries

def process_jsmpeg_channels(channels, source_label, source_index):
    """Generates m3u8 entries for jsmpeg source."""
    entries = []
    std_map = get_standard_channel_map()
    
    try:
        for ch in channels:
            name = ch['name']
            url = ch['url']
            
            # Clean name
            name = clean_channel_name(name)
            
            # Standardization
            name = map_to_standard_name(name, std_map)
            
            entries.append({'name': name, 'url': url, 'content': f'#EXTINF:-1 group-title="IPTV",{name}\n{url}', 'index': source_index})
            
    except Exception as e:
        print(f"Error processing jsmpeg channels: {e}")
    return entries

def channel_sort_key(name):
    """
    Sort key for channel names.
    Order: CCTV-X, CCTV-others, Satellite (卫视), Others.
    """
    name_upper = name.upper()
    
    # CCTV channels
    if "CCTV" in name_upper:
        # Extract number if present
        match = re.search(r"CCTV(\d+)", name_upper)
        if match:
            num = int(match.group(1))
            return (0, num)
        elif "5+" in name_upper:
             return (0, 5.5) # Place between 5 and 6
        else:
            # CCTV News, etc. Place after numbered CCTVs
            return (0, 999)
            
    # Satellite TV (卫视)
    if "卫视" in name:
        return (1, name)
        
    return (2, name)

def scheduled_task():
    check_for_updates()
    global global_m3u8_content, global_txt_content, last_run_time, is_running
    is_running = True
    HD_STATS.update(kept=0, sd_dropped=0, unlabeled_dropped=0)  # patched-by-zyjk
    HD_STATS["sd_sample"] = []
    print("Executing scheduled task...")
    last_run_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    
    data = fetch_api_data()
    
    if not data or not isinstance(data, dict) or "results" not in data:
        print("No valid data received or 'results' key missing.")
        is_running = False
        return

    result = data["results"]
    
    if not result:
        print("No data in result.")
        is_running = False
        return

    # Speed test in parallel
    results_with_speed = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_item = {executor.submit(test_host_speed, item): item for item in result}
        for future in as_completed(future_to_item):
            item = future_to_item[future]
            try:
                speed, channels, reason = future.result()
                if speed > 0 or KEEP_UNKNOWN:  # patched-by-zyjk
                    print(f"Host {item['host']} Speed: {speed:.2f} MB/s matchType: {item['matchType']} source: {item.get('source', 'N/A')}")
                    results_with_speed.append({
                        'host': item['host'],
                        'matchType': item['matchType'],
                        'speed': speed,
                        'channels': channels,
                        'reason': reason
                    })
            except Exception as e:
                print(f"Error testing {item['host']}: {e}")

    # Sort and pick top N
    
    # Filter by speed limit 2MB/s
    # patched-by-zyjk: 顺便把"一条高清频道都没有"的 host 排除在候选之外——
    # 它们就算被选进 TOP_N 也生成不出任何条目，纯占名额
    valid_results = [
        r for r in results_with_speed
        if (r["speed"] > MIN_SPEED or (KEEP_UNKNOWN and r["speed"] <= 0))
        and not (HD_ONLY != "0" and r.get("matchType") != "hsmdtv" and not r.get("channels"))
    ]
    
    # Sort by speed descending
    valid_results.sort(key=lambda x: x['speed'], reverse=True)

    # patched-by-zyjk: 落盘测速明细，供 CI 复制为 output/speed_report.txt
    # 列：host / matchType / speed_mb_s / status / reason / channels
    #   reason    no_list=连频道列表都取不到 → 这个 host 是死的，别再调参数
    #             ts_fail=列表能读、首个 TS 分片拉不动 → 源大概率还能用，只是测不出速度
    #             ok=测速成功
    #   channels  清晰度过滤后还剩几条频道（0 说明这个 host 对产出没贡献）
    _scanned = len(results_with_speed)
    _dist = {}
    try:
        _rows = []
        for _r in sorted(results_with_speed, key=lambda x: x["speed"], reverse=True):
            _rs = _r.get("reason") or "unknown"
            _dist[_rs] = _dist.get(_rs, 0) + 1
            if _r["speed"] > MIN_SPEED:
                _st = "ok"
            elif _r["speed"] > 0:
                _st = "slow(低于阈值)"
            else:
                _st = "fail(%s)" % _rs
            _rows.append("%s\t%s\t%.2f\t%s\t%s\t%d\n" % (
                _r["host"], _r.get("matchType", ""), _r["speed"], _st,
                _rs, len(_r.get("channels") or [])))

        _alive = _dist.get("ok", 0) + _dist.get("ts_fail", 0)
        with open("speed_report.txt", "w", encoding="utf-8") as _sf:
            _sf.write("# 候选 %d 个｜连得上 %d 个（%.1f%%）｜能出源 %d 个\n"
                      % (_scanned, _alive,
                         (100.0 * _alive / _scanned) if _scanned else 0.0,
                         len(valid_results)))
            _sf.write("# 分布：%s\n" % "，".join(
                "%s %d" % (k, v) for k, v in sorted(_dist.items(), key=lambda kv: -kv[1])))
            _sf.write("# no_list=整台连不上，没救；"
                      "ts_fail=频道列表能读但测不出速度，源大概率还能用\n")
            _sf.write("host\tmatchType\tspeed_mb_s\tstatus\treason\tchannels\n")
            _sf.writelines(_rows)
        print("[patch] 测速明细已写入 speed_report.txt（%d 个 host）" % _scanned)
        print("[patch] 结果分布：%s" % "，".join(
            "%s %d 个" % (k, v) for k, v in sorted(_dist.items(), key=lambda kv: -kv[1])))
        if _dist.get("no_list"):
            print("[patch] no_list = %(t).0f 秒内连频道列表都取不到（超时/拒绝/404），"
                  "这类 host 是死的，调大超时也救不回来" % {"t": LIST_TIMEOUT})
        if _dist.get("ts_fail"):
            print("[patch] ts_fail = 频道列表能读、首个 TS 分片拉不动，"
                  "源大概率还能用，只是测不出速度")
    except Exception as _e:
        print("[patch] 写 speed_report.txt 失败: %s" % _e)
    
    final_sources = []
    selected_hosts = set()
    
    # Ensure at least one from each type if available and fast enough
    required_matches = ['txiptv', 'hsmdtv', 'zhgxtv', 'jsmpeg']
    
    for m in required_matches:
        # Find best for this match type
        for res in valid_results:
            if res['matchType'] == m and res['host'] not in selected_hosts:
                final_sources.append(res)
                selected_hosts.add(res['host'])
                break # Only need one per type for now to ensure diversity
    
    # Fill the rest with optimal speed from remaining
    for res in valid_results:
        if len(final_sources) >= TOP_N:
            break
        if res['host'] not in selected_hosts:
             final_sources.append(res)
             selected_hosts.add(res['host'])

    # Re-sort final collection by speed
    final_sources.sort(key=lambda x: x['speed'], reverse=True)
    
    top_sources = final_sources
    
    print(f"Selected top {len(top_sources)} sources.")
    # patched-by-zyjk: 清晰度过滤统计
    print("[patch] 清晰度过滤 HD_ONLY=%s：保留 %d 条频道，丢弃标清 %d 条%s"
          % (HD_ONLY, HD_STATS["kept"], HD_STATS["sd_dropped"],
             ("，丢弃未标注高清 %d 条" % HD_STATS["unlabeled_dropped"])
             if HD_ONLY == "2" else ""))
    if HD_STATS["sd_sample"]:
        print("[patch] 被丢弃的标清样例：%s" % "、".join(HD_STATS["sd_sample"]))

    if len(top_sources) < int(_env("MIN_SOURCES", "1")):  # patched-by-zyjk
        print(f"Not enough sources found ({len(top_sources)} < {_env('MIN_SOURCES', '1')}).")
        
        # Load from file if empty in memory
        if not global_m3u8_content and os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                global_m3u8_content = f.read()
        if not global_txt_content and os.path.exists(TXT_CACHE_FILE):
            with open(TXT_CACHE_FILE, "r", encoding="utf-8") as f:
                global_txt_content = f.read()
                
        update_time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        dummy_name = f"更新时间: {update_time_str}"
        dummy_url = "http://127.0.0.1/"
        
        if global_m3u8_content:
            lines = global_m3u8_content.split('\n')
            for i, line in enumerate(lines):
                if line.startswith("#EXT-X-UPDATED:"):
                    lines[i] = f"#EXT-X-UPDATED: {update_time_str}"
                elif line.startswith('#EXTINF:-1 group-title="Update",更新时间:'):
                    lines[i] = f'#EXTINF:-1 group-title="Update",{dummy_name}'
            global_m3u8_content = "\n".join(lines)
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                f.write(global_m3u8_content)
                
        if global_txt_content:
            lines = global_txt_content.split('\n')
            for i, line in enumerate(lines):
                if line.startswith("更新时间:"):
                    lines[i] = f"{dummy_name},{dummy_url}"
                    break
            global_txt_content = "\n".join(lines)
            with open(TXT_CACHE_FILE, "w", encoding="utf-8") as f:
                f.write(global_txt_content)
                
        is_running = False
        return

    # Collect all entries
    all_entries = []
    
    for idx, source in enumerate(top_sources):
        speed_str = (f"{source['speed']:.2f}MB/s" if source["speed"] > 0 else "未测速")  # patched-by-zyjk
        console_label = f"源{idx+1} {speed_str}"
        m3u8_label = f"源{idx+1}"
        print(f"Processing {console_label}: {source['host']} ({source['matchType']}) {source.get('source', 'N/A')}")
        
        entries = []
        if source['matchType'] == 'txiptv':
            entries = process_txiptv_channels(source['channels'], m3u8_label, idx)
        elif source['matchType'] == 'hsmdtv':
            entries = process_hsmdtv_channels(source['host'], m3u8_label, idx)
        elif source['matchType'] == 'zhgxtv':
            entries = process_zhgxtv_channels(source['channels'], m3u8_label, idx)
        elif source['matchType'] == 'jsmpeg':
            entries = process_jsmpeg_channels(source['channels'], m3u8_label, idx)

        # patched-by-zyjk: 把 host / 速度挂到每条 entry 上，
        # 后面"每节目最多 N 源"要按 host 去重，m3u 标题也要用速度
        for _e in entries:
            _e['_host'] = source['host']
            _e['_speed'] = source['speed']
        all_entries.extend(entries)

    # Group by name
    # We want to keep the order of channel names as much as possible
    grouped_entries = {}
    channel_order = [] 
    
    # Pre-populate order from channel_list.txt if we want specific order for those
    try:
        with open(CHANNEL_LIST_FILE, 'r', encoding='utf-8') as f:
            for l in f.readlines():
                name = l.strip()
                if name:
                    grouped_entries[name] = []
                    channel_order.append(name)
    except:
        pass

    _seen_entry = set()
    for entry in all_entries:
        name = entry['name']
        # patched-by-zyjk: 同名同地址只留一条，避免同一 host 重复刷屏
        _k = (name, entry.get('url'))
        if _k in _seen_entry:
            continue
        _seen_entry.add(_k)
        if name not in grouped_entries:
            grouped_entries[name] = []
        grouped_entries[name].append(entry)

    # patched-by-zyjk: 每个节目最多留 PER_CHANNEL 条源，且尽量来自不同 host
    # （同一台 host 上的同名频道只算一条，否则 3 个源全是一个盒子，一起挂）
    _per_channel_stats = {"keep": 0, "drop": 0}
    if PER_CHANNEL > 0:
        for _name in list(grouped_entries.keys()):
            _lst = grouped_entries[_name]
            if not _lst:
                continue
            _lst.sort(key=lambda x: (x.get('index', 999), -(x.get('_speed') or 0)))
            _picked, _hosts = [], set()
            for _e in _lst:
                _h = _e.get('_host') or _e.get('url')
                if _h in _hosts:
                    continue
                _hosts.add(_h)
                _picked.append(_e)
                if len(_picked) >= PER_CHANNEL:
                    break
            _per_channel_stats["keep"] += len(_picked)
            _per_channel_stats["drop"] += max(0, len(_lst) - len(_picked))
            grouped_entries[_name] = _picked
        print("[patch] 每节目最多 %d 条源：保留 %d 条，裁掉多余 %d 条"
              % (PER_CHANNEL, _per_channel_stats["keep"], _per_channel_stats["drop"]))
    
    # Sort the channel names
    
    # Pre-populate order from channel_list.txt if we want specific order for those
    # Actually, user wants standard sorting now. Let's prioritize standard sorting but also enable manual order if provided.
    
    # Get all unique channel names
    unique_channel_names = list(grouped_entries.keys())
    
    # Custom sort function
    def sort_channels(name):
        return channel_sort_key(name)
    
    unique_channel_names.sort(key=sort_channels)
    
    channel_order = unique_channel_names

    # Insert dummy update time channel at the top
    update_time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    dummy_name = f"更新时间: {update_time_str}"
    dummy_url = "http://127.0.0.1/" # Dummy URL
    
    # Add to m3u8 lines
    m3u8_lines = ["#EXTM3U", f"#EXT-X-UPDATED: {update_time_str}"]
    m3u8_lines.append(f'#EXTINF:-1 group-title="Update",{dummy_name}\n{dummy_url}')
    
    for name in channel_order:
        entries_list = grouped_entries.get(name, [])
        # Sort by source index to ensure 源1, 源2 order
        entries_list.sort(key=lambda x: x['index'])
        if entries_list:  # Only add header if sources exist (optional, m3u8 format usually implies listing)
            for entry in entries_list:
                 if SHOW_SPEED:  # patched-by-zyjk: 标题追加 [源N 速度]
                     _sp = entry.get('_speed') or 0
                     _tag = ("%.2fMB/s" % _sp) if _sp > 0 else "未测速"
                     _title = "%s [源%d %s]" % (name, entry.get('index', 0) + 1, _tag)
                     m3u8_lines.append('#EXTINF:-1 group-title="IPTV",%s\n%s'
                                       % (_title, entry['url']))
                 else:
                     m3u8_lines.append(entry['content'])

    global_m3u8_content = "\n".join(m3u8_lines)
    
    # Save to file (optional, but good for persistence)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        f.write(global_m3u8_content)

    # Generate TXT content
    txt_lines = []
    
    # Add dummy first
    txt_lines.append(f"{dummy_name},{dummy_url}")
    
    # Use grouped_entries to iterate channels in order (same order as m3u8)
    # We iterate channel_order, which contains unique names
    unique_names_processed = set()
    
    for name in channel_order:
        if name in unique_names_processed:
            continue
        unique_names_processed.add(name)
        
        entries_list = grouped_entries.get(name, [])
        # Sort by source index
        entries_list.sort(key=lambda x: x.get('index', 999))
        
        for entry in entries_list:
            if 'url' in entry:
                txt_lines.append(f"{entry['name']},{entry['url']}")
    
    global_txt_content = "\n".join(txt_lines)
    
    # Save to file
    with open(TXT_CACHE_FILE, "w", encoding="utf-8") as f:
        f.write(global_txt_content)
        
    print(f"M3U8 and TXT generation complete at {last_run_time}.")
    is_running = False

@app.route('/txt')
def get_txt():
    global global_txt_content
    # Try to load from file if memory is empty (after restart)
    if not global_txt_content and os.path.exists(TXT_CACHE_FILE):
        with open(TXT_CACHE_FILE, "r", encoding="utf-8") as f:
            global_txt_content = f.read()

    if not global_txt_content:
        return "Not ready yet. Please wait for the first scan.", 503
        
    return global_txt_content, 200, {'Content-Type': 'text/plain; charset=utf-8'}


@app.route('/')
def status():
    return jsonify({
        "status": "running" if not is_running else "updating",
        "last_run": last_run_time,
        "message": "Visit /iptv for m3u8 playlist, /txt for text playlist."
    })

@app.route('/iptv')
def get_m3u8():
    global global_m3u8_content
    # Try to load from file if memory is empty (after restart)
    if not global_m3u8_content and os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            global_m3u8_content = f.read()

    if not global_m3u8_content:
        return "Not ready yet. Please wait for the first scan.", 503
        
    return global_m3u8_content, 200, {'Content-Type': 'application/vnd.apple.mpegurl'}

@app.route('/forceRetest')
def force_retest():
    global is_running
    if is_running:
         return jsonify({"message": "Update already in progress.", "status": "busy"}), 429
         
    threading.Thread(target=scheduled_task).start()
    return jsonify({"message": "Force retest started in background.", "status": "started"})

# Initial run in background on startup (or trigger manually)
def start_scheduler():
    # patched-by-zyjk: 固定时刻更新（默认早 07:00 / 晚 18:00，北京时间），
    # 不再用 interval=12h —— 那样更新时间会随启动时间漂移。
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    _jobs = []
    for _t in UPDATE_TIMES.split(","):
        try:
            _h, _m = [int(x) for x in _t.strip().split(":")]
            if 0 <= _h < 24 and 0 <= _m < 60:
                _jobs.append((_h, _m))
        except Exception:
            print("[patch] 忽略无效的更新时间配置：%r" % _t)

    if _jobs:
        for _h, _m in _jobs:
            scheduler.add_job(func=scheduled_task, trigger="cron", hour=_h, minute=_m)
        print("[patch] 定时更新：%s（Asia/Shanghai）"
              % "、".join("%02d:%02d" % j for j in _jobs))
    else:
        print("[patch] UPDATE_TIMES 无效，回退为每 12 小时一次")
        scheduler.add_job(func=scheduled_task, trigger="interval", hours=12)

    scheduler.start()
    
    # Run immediately in a separate thread to not block startup
    threading.Thread(target=scheduled_task).start()

if __name__ == '__main__':
    print(f"Starting... - Version: {VERSION}")
    start_scheduler()
    app.run(host='0.0.0.0', port=5000)

