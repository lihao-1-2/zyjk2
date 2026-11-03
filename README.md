# IPTV 源自动更新（GitHub Actions 版）

把 `hkbase/iptvs` 镜像里的扫描逻辑抽出来，去掉 Flask 常驻服务，改成**跑一轮就退出**的脚本，
由 GitHub Actions 定时执行，结果自动提交回仓库，用 raw 链接直接订阅。

思路和 [ZYJK](https://github.com/lihao-1-2/ZYJK) 那套一致：Actions 定时跑 → 产物提交回仓库 → 播放器订阅 raw 地址。

---

## 与容器版的区别

| | 容器版（修复镜像） | 本版（Actions） |
|---|---|---|
| 运行方式 | 常驻，APScheduler 每 12 小时 | 定时触发，跑完即退出 |
| 结果获取 | HTTP 服务 `/iptv`、`/txt` | 仓库里的 `output/iptv.m3u`、`iptv.txt` |
| 需要 NAS 常开 | 是 | **否** |
| 测速出口 | 你家宽带 | GitHub runner（海外） ⚠️ |
| 增量/缓存 | 无 | 无 |

核心代码是同一份 `app.py`（同样 6 处补丁），只是入口不同。

---

## 目录结构

```
.
├── .github/workflows/update.yml   # 定时任务定义
├── app.py                         # 扫描核心（来自镜像，已打补丁）
├── ci_run.py                      # CI 入口：跑一轮 → 写 output/
├── data/
│   ├── jsmpeg.csv                 # 890 个候选 host
│   ├── txiptv.csv                 # 738 个
│   ├── zhgxtv.csv                 # 690 个
│   └── hsmd_address_list.txt      # hsmdtv 频道模板（当前模式未启用）
├── output/                        # 产物，由 workflow 自动提交
│   ├── iptv.m3u
│   ├── iptv.txt
│   └── status.json
└── requirements.txt
```

---

## 快速开始

**1. 新建仓库**，建议**私有**（原因见下面「注意事项」第 3 条）。

**2. 上传这些文件**：把本目录整个内容推到仓库根目录，`.github/` 要在仓库根下。
网页上传时注意 `.github/workflows/update.yml` 的层级不能丢。

用命令行最省事（在 `iptvs-ci` 目录里执行，仓库先在网页上创建成空仓库，不要勾选初始化 README）：

```bash
git init
git add -A
git commit -m "init: IPTV 源自动更新"
git branch -M main
git remote add origin https://github.com/你的用户名/iptv.git
git push -u origin main
```

网页上传也可以，但要拖整个文件夹进去，`.github` 这种以点开头的目录容易漏，
漏了就不会有定时任务——检查一下仓库根目录下能看到 `.github/workflows/update.yml` 再走下一步。

**3. 开写权限**：`Settings → Actions → General → Workflow permissions` 选
**Read and write permissions**，否则提交结果那一步会 403。
（workflow 里已经声明了 `permissions: contents: write`，但仓库级设置也必须是读写。）

**4. 手动跑一次验证**：`Actions → 更新 IPTV 源 → Run workflow`。
约 5–15 分钟。跑完看仓库里 `output/iptv.m3u` 有没有出现频道。

跑成功后，定时任务会自动生效，默认北京 06:00 和 18:00。

---

## 订阅地址

假设仓库是 `你的用户名/iptv`，分支 `main`：

| 地址 | 用途 |
|---|---|
| `https://raw.githubusercontent.com/你的用户名/iptv/main/output/iptv.m3u` | m3u8，播放器订阅 |
| `https://raw.githubusercontent.com/你的用户名/iptv/main/output/iptv.txt` | TXT，DIYP / 影视仓 |
| `https://cdn.jsdelivr.net/gh/你的用户名/iptv@main/output/iptv.m3u` | jsDelivr 加速（国内更稳） |
| `https://cdn.jsdelivr.net/gh/你的用户名/iptv@main/output/iptv.txt` | 同上 |

**私有仓库**的 raw 链接需要 token，播放器一般填不了。想让播放器直接订阅，仓库得公开，
或者用下面「用 Pages 发布」的方式。

### 用 Pages 发布（私有仓库也能有公开链接）

私有仓库的场景可以另开一个公开仓库放产物，或者用 GitHub Pages（Pages 对私有仓库需要 Pro）。
最简单的做法还是：私有仓库只用来跑，另外用一个公开仓库或 Gist 存放 `output/` 里的两个文件。

---

## 参数

在 `Actions → Run workflow` 时可选，或改 `update.yml` 的 `env`：

| 参数 | 默认 | 说明 |
|---|---|---|
| `MODES` | `jsmpeg,txiptv,zhgxtv` | 要跑的模式。想只跑一种就填 `jsmpeg`，最快 |
| `MIN_SPEED` | `0.2` | 低于此速度（MB/s）的源丢弃。原版是 1.5，太严 |
| `MAX_WORKERS` | `40` | 并发数。runner 性能有限，超过 64 意义不大 |
| `MIN_SOURCES` | `1` | 至少几个可用源才出结果。原版是 3，只有 1-2 个时永远不出结果 |

改定时：`update.yml` 里的 `cron` 是 UTC，北京时间减 8 小时。

```yaml
- cron: '0 22 * * *'   # 北京 06:00
- cron: '0 10 * * *'   # 北京 18:00
```

---

## 三个必须知道的限制

### 1. 测速结果不代表你家的播放体验 ⚠️

GitHub 托管 runner 在海外机房。`get_download_speed()` 测的是「runner 到该 host」的速度，
海外线路和国内线路完全是两回事，所以**排序会失真**——可能把国内很慢、海外很快的源排在前面。

两个解法：

- **换 self-hosted runner**（推荐）：找一台国内常开的机器（NAS、树莓派、小主机）装 runner，
  测速就在你的网络环境里进行。步骤：仓库 `Settings → Actions → Runners → New self-hosted runner`，
  按提示装好，把 `update.yml` 的 `runs-on: ubuntu-latest` 改成 `runs-on: self-hosted`。
- **接受失真**：只把 `output` 当"可用源清单"，播放器端还有一次实际加载，慢的源会被自然淘汰。

### 2. 仓库 60 天没活动，定时任务会被自动停用

GitHub 对 `schedule` 触发的 workflow 有这样的策略。本 workflow 每次都会提交 `output/`，
所以运行本身就算活动，一般不会触发；但如果长期扫不到源（没提交），就可能被停。
收到 "This scheduled workflow is disabled" 邮件时，去 Actions 页面点一下 "Enable workflow"。

### 3. 公开仓库有版权与合规问题

`app.py` 来自第三方镜像，镜像里没有 LICENSE 文件，**没有明确的开源授权**。
放到公开仓库属于再分发，风险由你承担；建议保持私有。

另外，这套逻辑是扫描公网上暴露的酒店/宾馆 IPTV 前端，本身属于他人商业系统资源。
GitHub 的 ToS 禁止「通过 Actions 对第三方系统进行扫描或探测」，上游 ZYJK 项目也在 Actions 上跑，
说明实际执行中通常不管，但**存在被限流的可能**。降低频率、压低并发会安全一些。

---

## 常见问题

**Q：任务跑了但 `output/` 没变化？**

看日志有没有 `[ci] 已写入`。如果看到 `结果为空，保留 output/ 中的上一轮结果`，
说明这轮所有 host 都没测通 —— 大概率是 CSV 里的地址已经失效，需要换新 CSV。

**Q：提交步骤 403 / `Permission denied`？**

仓库设置里没给写权限，见「快速开始」第 3 步。

**Q：想换 CSV 怎么办？**

直接替换 `data/` 下三个文件，保持同样的表头（`host,ip,port,...`），`host` 列是 `ip:port` 格式。
来源可以是 [lihao-1-2/ZYJK](https://github.com/lihao-1-2/ZYJK) 的仓库文件。

**Q：能加更多模式吗？**

原 `app.py` 里 `test_host_speed()` 只实现了 `txiptv`、`zhgxtv`、`jsmpeg` 三种。
`hsmdtv` 有生成函数但没有测速分支，属于死代码，所以这里没启用。

---

## 验证情况

已在本机实测（Python 3.13，与镜像基础环境同版本）：

- **正常一轮**：起假酒店前端，`python ci_run.py` 退出码 0，
  `output/` 生成 `iptv.m3u`（450 字节）、`iptv.txt`（254 字节）、`status.json`，
  频道 4 条（正确排除了 app.py 自带的"更新时间"占位行）
- **单模式**：`MODES=jsmpeg` 时 `_run/` 里只复制 `jsmpeg.csv`，另两份不参与，避免白跑
- **空结果保护**：把 host 换成不可达地址，退出码仍为 0，
  `output/` 三个文件 sha256 与上一轮完全一致（不会用空结果覆盖上次的成功产物）
- **补丁**：6 处全部命中，`app.py` 780 行语法校验通过

**未验证**：本机无法真正触发 GitHub Actions，workflow 的实际执行需要你推到仓库后跑一次。

---

## 与容器版共用同一份 app.py

`iptvs-ci/app.py` 与修复镜像里的 `/app/app.py` 内容**完全一致**（同源、同 6 处补丁）。
改了一边记得同步另一边，或者重新跑 `zyjk-docker/iptvs-fix/build_image.py` 打包镜像。
