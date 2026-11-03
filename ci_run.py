#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CI 入口：跑一轮 IPTV 源扫描，把结果写到 output/，跑完即退出。

与容器版的区别：
  - 不启动 Flask，不起 APScheduler，不常驻
  - app.py 原封不动（只打了数据源/阈值/并发五处补丁），这里只负责
    准备好运行目录、调用一次 scheduled_task()、再把产物搬到 output/

app.py 里的 scheduled_task() 会把结果直接写到当前工作目录的
iptv_sources.m3u8 / iptv_sources.txt，所以这里先 chdir 到临时目录，
避免把中间文件写进仓库根。

环境变量：
  MODES         要跑的模式，逗号分隔，默认 jsmpeg,txiptv,zhgxtv
  MAX_WORKERS   并发数，默认 40
  MIN_SPEED     最低速度 MB/s，默认 0.2
  MIN_SOURCES   至少几个可用源才出结果，默认 1
  OUT_DIR       产物目录，默认 output
  WORK_DIR      临时运行目录，默认 _run
"""
import json
import os
import shutil
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
OUT_DIR = os.path.abspath(os.environ.get("OUT_DIR") or os.path.join(HERE, "output"))
WORK_DIR = os.path.abspath(os.environ.get("WORK_DIR") or os.path.join(HERE, "_run"))

ALL_MODES = ["jsmpeg", "txiptv", "zhgxtv"]

# app.py 需要的文件，从 data/ 复制进运行目录
DATA_FILES = ["jsmpeg.csv", "txiptv.csv", "zhgxtv.csv", "hsmd_address_list.txt"]


def log(msg):
    print("[ci] " + msg, flush=True)


def parse_modes():
    raw = (os.environ.get("MODES") or "").strip()
    if not raw:
        return list(ALL_MODES)
    modes = [m.strip().lower() for m in raw.split(",") if m.strip()]
    bad = [m for m in modes if m not in ALL_MODES]
    if bad:
        log("忽略无效模式 %s（可选：%s）" % (bad, ", ".join(ALL_MODES)))
        modes = [m for m in modes if m in ALL_MODES]
    return modes or list(ALL_MODES)


def prepare_work(modes):
    """建一个干净的运行目录，只放本次要跑的模式对应的 CSV。"""
    for d in (WORK_DIR, OUT_DIR):
        os.makedirs(d, exist_ok=True)

    # 清掉上次的中间产物，避免读到旧结果
    for name in ("iptv_sources.m3u8", "iptv_sources.txt"):
        p = os.path.join(WORK_DIR, name)
        if os.path.exists(p):
            os.remove(p)

    shutil.copy2(os.path.join(HERE, "app.py"), os.path.join(WORK_DIR, "app.py"))

    for name in DATA_FILES:
        src = os.path.join(DATA_DIR, name)
        dst = os.path.join(WORK_DIR, name)
        if os.path.exists(dst):
            os.remove(dst)
        if not os.path.exists(src):
            continue
        if name.endswith(".csv"):
            # 只复制本次启用的模式；app.py 的 fetch_api_data 找不到文件会自行跳过
            if name[:-4] not in modes:
                continue
        shutil.copy2(src, dst)

    # 每种模式实际有多少候选 host
    for m in modes:
        p = os.path.join(WORK_DIR, m + ".csv")
        n = max(0, sum(1 for _ in open(p, encoding="utf-8-sig", errors="ignore")) - 1) \
            if os.path.exists(p) else 0
        log("%-8s 候选 host %d 个" % (m, n))


def run_scan():
    """在运行目录里 import app.py，调用一次 scheduled_task()。"""
    os.chdir(WORK_DIR)
    sys.path.insert(0, WORK_DIR)

    log("导入 app.py ...")
    import app  # noqa: E402  （必须在 chdir 之后）

    log("开始扫描（并发 %s，阈值 %s MB/s，最少源 %s）"
        % (app.MAX_WORKERS, app.MIN_SPEED, os.environ.get("MIN_SOURCES", "1")))
    t0 = time.time()
    app.scheduled_task()
    log("扫描结束，耗时 %.1f 秒" % (time.time() - t0))


def count_entries(text):
    """统计真实频道条目数（排除 # 注释行和 app.py 自带的"更新时间"占位行）。"""
    n = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("更新时间"):
            continue
        n += 1
    return n


def publish():
    """把中间产物搬到 output/，并写一份运行状态。

    返回 True 表示已更新 output/，False 表示本轮没有可用结果。
    """
    src_m3u = os.path.join(WORK_DIR, "iptv_sources.m3u8")
    src_txt = os.path.join(WORK_DIR, "iptv_sources.txt")

    if not os.path.exists(src_m3u) or not os.path.exists(src_txt):
        log("没有产出结果文件——本轮扫描没拿到可用源")
        return False

    m3u = open(src_m3u, encoding="utf-8").read()
    txt = open(src_txt, encoding="utf-8").read()
    n = count_entries(txt)
    log("频道条目 %d 条，m3u %d 字节，txt %d 字节" % (n, len(m3u.encode()), len(txt.encode())))

    # 没有任何真实频道 = 没扫到源，保留上一轮结果，不要把空文件推上去覆盖
    if n < 1:
        log("结果为空，保留 output/ 中的上一轮结果")
        return False

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "iptv.m3u"), "w", encoding="utf-8", newline="\n") as f:
        f.write(m3u)
    with open(os.path.join(OUT_DIR, "iptv.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write(txt)

    # app.py 会把每个 host 的测速结果写到运行目录，搬过来方便核对
    hosts = {}
    src_report = os.path.join(WORK_DIR, "speed_report.txt")
    if os.path.exists(src_report):
        shutil.copyfile(src_report, os.path.join(OUT_DIR, "speed_report.txt"))
        for line in open(src_report, encoding="utf-8").read().splitlines()[1:]:
            col = line.split("\t")
            if len(col) >= 4:
                hosts[col[3]] = hosts.get(col[3], 0) + 1
        log("测速明细：%s" % ", ".join("%s %d 个" % (k, v) for k, v in sorted(hosts.items())))
    else:
        log("警告：没有 speed_report.txt（app.py 未产出）")

    status = {
        "updated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "updated_at_local": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "channels": n,
        "modes": parse_modes(),
        "max_workers": int(os.environ.get("MAX_WORKERS", "40")),
        "min_speed_mb": float(os.environ.get("MIN_SPEED", "0.05")),
        "keep_unknown": os.environ.get("KEEP_UNKNOWN", "1") == "1",
        "top_n": int(os.environ.get("TOP_N", "15")),
        "hosts_by_status": hosts,
        "runner": os.environ.get("GITHUB_RUN_ID", ""),
    }
    with open(os.path.join(OUT_DIR, "status.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
    log("已写入 output/iptv.m3u、output/iptv.txt、output/status.json、output/speed_report.txt")
    return True


def main():
    modes = parse_modes()
    log("模式：%s" % ", ".join(modes))
    prepare_work(modes)
    try:
        run_scan()
    except Exception:
        log("扫描过程异常：")
        traceback.print_exc()
        return 1
    if publish():
        return 0
    # 没扫到源属于业务结果，不算流程失败；workflow 里提交步骤自然无变化
    log("警告：本轮没有任何可用频道，output/ 保持上一轮内容")
    return 0


if __name__ == "__main__":
    sys.exit(main())
