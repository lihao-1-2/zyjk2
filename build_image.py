#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""由原镜像 hkbase/iptvs(latest).tar 构造一个"已修复"的新镜像 tar。

不需要 Docker、不需要构建上下文：直接按 OCI 镜像布局追加一层，
层内是修补后的 /app/app.py 与三份候选 host CSV，然后重新生成
config / manifest / index，输出可被 `docker load` 直接导入的 tar 包。

修补内容与 iptvs-fix/boot.py 一致，但这里是**固化进镜像**，
所以容器每次启动都不再需要打补丁，也不依赖 heredoc 粘贴。
"""
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import time

SRC_TAR = os.environ.get("SRC_TAR", r"D:/360极速浏览器X下载/hkbase_iptvs(latest).tar")
WORK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_build")
# 输出路径可用环境变量或第一个命令行参数覆盖
OUT_TAR = os.environ.get("FIXED_TAR_OUT") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "hkbase_iptvs_fixed.tar"
)
CSV_SRC = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "vendor"))

# 原镜像里的固定摘要
OLD_CONFIG = "e06e585dcbf3de0431b5f79c88021629c5cac839f8e0c1b1fc0fda0fced87628"
OLD_MANIFEST = "45e08f48bab610991e77d67782331547245a84b552b2fd35e8de4bfa9691f138"
APP_LAYER = "bdc219e079aef129e6c2b9b7ae328c2c55c9160c15e3195af1444cb04f15f3d1"

FIXED_MTIME = 1767225600  # 2026-01-01 00:00:00 UTC，固定值让构建可复现

MARK = "# patched-by-zyjk"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------ 补丁
NEW_CHECK_UPDATE = '''def check_for_updates():
    """patched-by-zyjk: 自更新已彻底禁用。

    原逻辑：远端 /message 报 4.2.1 > 内置 1.0.0，就去 /latest 把返回内容
    直接写进 __file__。而 /latest 现在返回的是 JSON 数据（不是 Python 代码），
    app.py 会被覆盖成 JSON，容器下次重启直接语法错误起不来。
    """
    print("[patch] 自更新已禁用（避免 app.py 被远端 JSON 覆盖）")
    return
'''


NEW_FETCH = '''def fetch_api_data():
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
    return {"results": out}

'''


def patch_app(src):
    hits = {}

    src, n = re.subn(
        r"def check_for_updates\(\):.*?(?=\n# Set Timezone)",
        NEW_CHECK_UPDATE + "\n",
        src,
        count=1,
        flags=re.S,
    )
    hits["禁用自更新"] = n

    src, n = re.subn(
        r"def fetch_api_data\(\):.*?(?=^def get_download_speed\()",
        NEW_FETCH,
        src,
        count=1,
        flags=re.S | re.M,
    )
    hits["数据源改本地CSV"] = n

    src, n = re.subn(
        r"^MAX_WORKERS = 20$",
        'MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "40"))  ' + MARK + "\n"
        'MIN_SPEED = float(os.environ.get("MIN_SPEED", "0.05"))  ' + MARK + "\n"
        'KEEP_UNKNOWN = os.environ.get("KEEP_UNKNOWN", "1") == "1"  ' + MARK,
        src,
        count=1,
        flags=re.M,
    )
    hits["并发/阈值可配置"] = n

    # 原阈值 1.5 MB/s 太严，大量酒店前端实测在 0.3-1 MB/s，会被全部过滤
    src, n = re.subn(r"r\['speed'\] > 1\.5", "r['speed'] > MIN_SPEED", src, count=1)
    hits["速度阈值"] = n

    # 原来不足 3 个源就直接放弃，导致只剩 1-2 个可用源时 /iptv 永远 503。
    # 连同下面那句写死 "< 3" 的日志一起改，否则判断已可配置、文案还写着 3，日志会误导
    src, n = re.subn(
        r"^(\s*)if len\(top_sources\) < 3:$",
        '\\1if len(top_sources) < int(os.environ.get("MIN_SOURCES", "1")):  ' + MARK,
        src,
        count=1,
        flags=re.M,
    )
    hits["最少源判断"] = n

    src, n = re.subn(
        r'Not enough sources found \(\{len\(top_sources\)\} < 3\)\.',
        "Not enough sources found ({len(top_sources)} < "
        "{os.environ.get('MIN_SOURCES', '1')}).",
        src,
        count=1,
    )
    hits["最少源日志"] = n

    # 原来写死只保留 5 个 host（TOP_N=5），每个频道能列几条源完全取决于留下几个 host
    src, n = re.subn(
        r"^TOP_N = 5$",
        'TOP_N = int(os.environ.get("TOP_N", "15"))  ' + MARK,
        src,
        count=1,
        flags=re.M,
    )
    hits["候选host上限"] = n

    # 关键：原来 speed <= 0 的 host 全被丢掉。GitHub runner 在海外，
    # 从海外拉国内酒店前端的首个 TS 分片经常超时（speed=-1），
    # 但这些 host 明明能取到频道列表——在国内其实是能看的。
    src, n = re.subn(
        r"^([ \t]*)if speed > 0:$",
        '\\1if speed > 0 or KEEP_UNKNOWN:  ' + MARK,
        src,
        count=1,
        flags=re.M,
    )
    hits["保留测速失败"] = n

    # 阈值过滤同样放行 speed<=0 的，排序里它们自然排到最后
    src, n = re.subn(
        r"^([ \t]*)valid_results = \[r for r in results_with_speed if r\['speed'\] > MIN_SPEED\]$",
        '\\1valid_results = [r for r in results_with_speed '
        'if r["speed"] > MIN_SPEED or (KEEP_UNKNOWN and r["speed"] <= 0)]  ' + MARK,
        src,
        count=1,
        flags=re.M,
    )
    hits["放行未测速源"] = n

    # 标签别把 -1.00MB/s 印出来
    src, n = re.subn(
        r"""speed_str = f"\{source\['speed'\]:\.2f\}MB/s\"""",
        'speed_str = (f"{source[\'speed\']:.2f}MB/s" if source["speed"] > 0 '
        'else "未测速")  ' + MARK,
        src,
        count=1,
    )
    hits["未测速标签"] = n

    # 把每个 host 的测速结果落盘，CI 再复制到 output/，否则用户无从判断测没测速
    SPEED_REPORT = '''
    # patched-by-zyjk: 落盘测速明细，供 CI 复制为 output/speed_report.txt
    try:
        with open("speed_report.txt", "w", encoding="utf-8") as _sf:
            _sf.write("host\\tmatchType\\tspeed_mb_s\\tstatus\\n")
            for _r in sorted(results_with_speed, key=lambda x: x["speed"], reverse=True):
                if _r["speed"] <= 0:
                    _st = "fail(测速超时)"
                elif _r["speed"] <= MIN_SPEED:
                    _st = "slow(低于阈值)"
                else:
                    _st = "ok"
                _sf.write("%s\\t%s\\t%.2f\\t%s\\n" % (
                    _r["host"], _r.get("matchType", ""), _r["speed"], _st))
        print("[patch] 测速明细已写入 speed_report.txt（%d 个 host）" % len(results_with_speed))
    except Exception as _e:
        print("[patch] 写 speed_report.txt 失败: %s" % _e)
'''
    # 注意：re.subn 的替换串里 \t \n 会被当成转义处理，把报告里的制表符/换行搞乱，
    # 所以这里用函数式替换，原样插入
    def _insert_report(mo):
        return mo.group(0) + "\n" + SPEED_REPORT.rstrip("\n")

    src, n = re.subn(
        r"^[ \t]*# Sort by speed descending\n[ \t]*valid_results\.sort\(key=lambda x: x\['speed'\], reverse=True\)$",
        _insert_report,
        src,
        count=1,
        flags=re.M,
    )
    hits["测速明细落盘"] = n

    return src, hits


# ------------------------------------------------------------------ 工具
def build_layer(files):
    """files: [(arcname, bytes, mode)] -> 未压缩层 tar 的字节"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tf:
        for name, data, mode in files:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            ti.mode = mode
            ti.mtime = FIXED_MTIME
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = "root"
            ti.type = tarfile.REGTYPE
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def main():
    if not os.path.exists(SRC_TAR):
        print("找不到原镜像:", SRC_TAR)
        return 2
    if not os.path.isdir(CSV_SRC):
        print("找不到 CSV 目录:", CSV_SRC)
        return 2

    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)

    # 1) 取出原 app.py
    print("[1/6] 从原镜像抽出 /app/app.py ...")
    app_src = None
    with tarfile.open(SRC_TAR) as t:
        f = t.extractfile("blobs/sha256/" + APP_LAYER)
        inner = tarfile.open(fileobj=io.BytesIO(f.read()))
        app_src = inner.extractfile("app/app.py").read()
    print("    原始 app.py %d 字节" % len(app_src))

    # 2) 打补丁
    print("[2/6] 应用补丁 ...")
    text = app_src.decode("utf-8")
    patched, hits = patch_app(text)
    for k, v in hits.items():
        flag = "OK" if v == 1 else "未命中(危险)"
        print("    %-14s %s" % (k, flag))
    if any(v != 1 for v in hits.values()):
        print("关键补丁未命中，终止")
        return 3
    try:
        compile(patched, "app.py", "exec")
    except SyntaxError as e:
        print("补丁后语法错误:", e)
        return 3
    print("    语法校验通过，%d 行" % patched.count("\n"))

    # 3) 组装层内容
    print("[3/6] 组装新层 ...")
    files = [("app/app.py", patched.encode("utf-8"), 0o644)]
    total_rows = 0
    for name in ("jsmpeg.csv", "txiptv.csv", "zhgxtv.csv"):
        p = os.path.join(CSV_SRC, name)
        data = open(p, "rb").read()
        rows = max(0, data.count(b"\n") - 1)
        total_rows += rows
        files.append(("app/" + name, data, 0o644))
        print("    app/%-12s %8d 字节  %5d 行" % (name, len(data), rows))
    print("    候选 host 合计 %d 行" % total_rows)

    layer = build_layer(files)
    layer_digest = sha256(layer)
    print("    新层 %d 字节  sha256:%s" % (len(layer), layer_digest[:16]))

    # 4) 生成新 config
    print("[4/6] 生成 config ...")
    with tarfile.open(SRC_TAR) as t:
        cfg = json.loads(t.extractfile("blobs/sha256/" + OLD_CONFIG).read())
        man = json.loads(t.extractfile("blobs/sha256/" + OLD_MANIFEST).read())

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # 校验 history 与 diff_ids 的对应关系，避免 provider 报错
    non_empty = [h for h in cfg["history"] if not h.get("empty_layer")]
    assert len(non_empty) == len(cfg["rootfs"]["diff_ids"]), (
        "history 非空层 %d != diff_ids %d" % (len(non_empty), len(cfg["rootfs"]["diff_ids"]))
    )

    env = [e for e in cfg["config"].get("Env", [])
           if not e.split("=", 1)[0] in ("MAX_WORKERS", "MIN_SPEED", "NO_PROXY", "MIN_SOURCES",
                                         "TZ", "TOP_N", "KEEP_UNKNOWN")]
    env += [
        "MAX_WORKERS=40",
        "MIN_SPEED=0.05",
        "MIN_SOURCES=1",
        # 海外/异地测速对国内源经常超时，保留这些 host 否则每频道只剩 1 条源
        "KEEP_UNKNOWN=1",
        "TOP_N=15",
        # 宿主机若设了 HTTP_PROXY，requests 会把 内网/公网 IP 也走代理，探测全超时
        "NO_PROXY=*",
        "TZ=Asia/Shanghai",
    ]
    cfg["config"]["Env"] = env
    cfg["created"] = now
    cfg["rootfs"]["diff_ids"].append("sha256:" + layer_digest)
    cfg["history"].append({
        "created": now,
        "created_by": "patched-by-zyjk: /app/app.py 禁用自更新 + 数据源改本地 CSV + "
                      "阈值可配 + 并发可配，随层写入三份候选 host CSV",
        "comment": "fix: remote API empty & self-update overwrites source with JSON",
    })

    cfg_bytes = json.dumps(cfg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    cfg_digest = sha256(cfg_bytes)
    print("    新 config %d 字节 sha256:%s" % (len(cfg_bytes), cfg_digest[:16]))

    # 5) 生成新 manifest / index
    print("[5/6] 生成 manifest / index ...")
    man["layers"].append({
        "mediaType": "application/vnd.oci.image.layer.v1.tar",
        "digest": "sha256:" + layer_digest,
        "size": len(layer),
    })
    man["config"] = {
        "mediaType": "application/vnd.oci.image.config.v1+json",
        "digest": "sha256:" + cfg_digest,
        "size": len(cfg_bytes),
    }
    man_bytes = json.dumps(man, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    man_digest = sha256(man_bytes)

    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [{
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": "sha256:" + man_digest,
            "size": len(man_bytes),
            "annotations": {
                "io.containerd.image.name": "docker.io/hkbase/iptvs:latest",
                "org.opencontainers.image.ref.name": "latest",
            },
        }],
    }
    index_bytes = json.dumps(index, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    layer_digests = [l["digest"].split(":")[1] for l in man["layers"]]
    docker_manifest = [{
        "Config": "blobs/sha256/" + cfg_digest,
        "RepoTags": ["hkbase/iptvs:latest", "hkbase/iptvs:fixed"],
        "Layers": ["blobs/sha256/" + d for d in layer_digests],
    }]
    dm_bytes = json.dumps(docker_manifest, separators=(",", ":")).encode("utf-8")

    repositories = {"hkbase/iptvs": {"latest": layer_digest, "fixed": layer_digest}}
    repo_bytes = json.dumps(repositories, separators=(",", ":")).encode("utf-8")

    # 6) 打包
    print("[6/6] 打包 %s ..." % os.path.basename(OUT_TAR))
    want_blobs = set(layer_digests[:-1])  # 原镜像的 7 层

    def add_bytes(tf, name, data):
        ti = tarfile.TarInfo(name)
        ti.size = len(data)
        ti.mode = 0o644
        ti.mtime = FIXED_MTIME
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = "root"
        tf.addfile(ti, io.BytesIO(data))

    if os.path.exists(OUT_TAR):
        os.remove(OUT_TAR)

    copied = 0
    with tarfile.open(OUT_TAR, "w", format=tarfile.GNU_FORMAT) as out:
        add_bytes(out, "index.json", index_bytes)
        add_bytes(out, "manifest.json", dm_bytes)
        add_bytes(out, "oci-layout", b'{"imageLayoutVersion": "1.0.0"}')
        add_bytes(out, "repositories", repo_bytes)
        # 新产生的小 blob
        add_bytes(out, "blobs/sha256/" + layer_digest, layer)
        add_bytes(out, "blobs/sha256/" + cfg_digest, cfg_bytes)
        add_bytes(out, "blobs/sha256/" + man_digest, man_bytes)

        # 原镜像的层，流式复制
        with tarfile.open(SRC_TAR) as src:
            for m in src:
                if not m.name.startswith("blobs/sha256/"):
                    continue
                d = m.name.rsplit("/", 1)[-1]
                if d not in want_blobs:
                    continue
                f = src.extractfile(m)
                if f is None:
                    continue
                ti = tarfile.TarInfo(m.name)
                ti.size = m.size
                ti.mode = 0o644
                ti.mtime = FIXED_MTIME
                ti.uid = ti.gid = 0
                ti.uname = ti.gname = "root"
                out.addfile(ti, f)
                copied += 1

    size = os.path.getsize(OUT_TAR)
    print("    复制原层 %d/%d" % (copied, len(want_blobs)))
    print("\n完成: %s (%.1f MB)" % (OUT_TAR, size / 1024 / 1024))
    print("镜像标签: hkbase/iptvs:latest 与 hkbase/iptvs:fixed")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        OUT_TAR = sys.argv[1]
    sys.exit(main())
