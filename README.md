# gdrive-agent — 面向 Agent 的 Google Drive 存储服务

> 给无任何上下文的接手者(人或 agent)。创建:2026-08-31。

## 是什么

把用户自己的 Google Drive(5 TiB)变成 Agent 可自主调用的存储层:随时上传/下载/管理文件,
把本机硬盘当临时中转,并能注册周期性任务。

**核心约束:本地可用约 150G,Drive 可用约 5 TiB,比例 1:33。**
数据可能整个比服务器硬盘还大,所以"绝不写爆根分区"是本服务的第一设计目标——
根分区写满会直接搞死 quantcheck 这个线上业务。

## 三种用法,按场景选

| 你是 | 用什么 | 怎么用 |
|---|---|---|
| Claude Code / MCP agent | **MCP 工具**(16 个) | 已全局注册,直接调用 `drive_df` / `drive_upload` 等 |
| 脚本、systemd、人 | **CLI `gdrive`** | `gdrive df`、`gdrive ls`,加 `--json` 得机器可读输出 |
| 想直接读远端数据 | **只读挂载** | `/mnt/gdrive/...`,pandas 可直接 `read_parquet` |

## 架构

```
MCP server (stdio, 16 工具)  ┐
CLI /usr/local/bin/gdrive    ├→ gdrive_agent 核心(护栏+策略+任务)
只读挂载 /mnt/gdrive         ┘         ↓
                          rclone rcd @127.0.0.1:5572  ← 传输引擎(异步 job/进度/重试/限速)
                                       ↓
                          Google Drive: gdrive:agent-data/
```

传输、重试、进度、限速全部由 rclone 负责,本服务不自造轮子,只补 rclone 不提供的三件事:
**安全护栏、形态自适应策略、任务注册**。

**与备份系统完全隔离**:`rclone-restic.service`(:8787)是每日备份专用,本服务用独立的
`gdrive-rcd.service`(:5572)。Agent 再怎么折腾也不会影响备份。

## Drive 命名空间

```
gdrive:vps-backup/    ← restic 备份仓库。本服务【永久禁止访问】,路径层硬拒绝
gdrive:agent-data/    ← 服务根,所有路径都相对于它
    ├ datasets/       长期数据集
    ├ artifacts/      产出物
    └ scratch/        临时区
```

路径规则:相对服务根,**不接受 `..` 分量,也不接受 `gdrive:` 前缀**。`datasets/foo` ✓,`../vps-backup` ✗。

## "数据比硬盘大"怎么办 —— 四道机制

1. **下载前强制磁盘预检**。`drive_download` 先算远端体量,对比 `本地free - 20G保留水位`,
   不够就**拒绝**并列出替代方案,而不是传一半写爆盘。
2. **暂存区自动回收**。不指定 `local` 时落到 `/var/lib/gdrive-agent/staging/`,
   24h 后自动清,总量超 100G 按 LRU 淘汰。**正在用的数据要 `staging_manage(action='lease')` 钉住**。
3. **只读挂载 `/mnt/gdrive`**。VFS 缓存上限 20G,可以直接读 TB 级数据集而本地永不超 20G。
   实测:从 200MB 远端文件读 1MB 耗时 0.5s,缓存只占 2.1M。
4. **流式窥探 `drive_cat`**。不落盘看文件头,先判断值不值得下。

**空间不够时的处理顺序**:`staging_manage(clean)` 腾空间 → `drive_download` 带 `subset` 分批取 →
改用 `/mnt/gdrive` 挂载读 → `drive_cat` 只看头部。

## 形态自适应(不需要你声明数据长什么样)

上传前自动扫描目录,选策略并在返回的 `reason` 里说明理由:

| 探测到 | 策略 |
|---|---|
| 少量大文件(均值 ≥64MB) | 直传,128M 分块 / 低并发 |
| 海量小文件(>5000 个且均值 <1MB) | **自动 tar 分卷打包**(~2G/卷)+ JSON 索引 |
| 混合 | 分治:大文件直传,小文件子树打包 |

为什么要打包:Drive 每个文件都有固定 API 开销,裸传 1 万个小文件可能要几小时,
打成几个大包只要几分钟。打包保留**可检索索引**,取回时能只解需要的文件,不必下整包。

## 常用操作

```bash
gdrive df                          # 先看这个:Drive/本地/暂存区三处空间
gdrive ls datasets                 # 列目录
gdrive size datasets/big           # 递归统计体量(下载大目录前必做)
gdrive cat some.csv --limit 500    # 不落盘看文件头

gdrive upload /data/foo --remote datasets/foo        # 异步,返回 job_id
gdrive upload /data/foo --remote datasets/foo --wait # 阻塞到完成(周期任务用这个)
gdrive download datasets/foo --local /tmp/foo        # 含磁盘预检
gdrive download datasets/foo --subset '*2024*'       # 只取子集
gdrive job status 12                                 # 轮询进度/速度/ETA

gdrive rm datasets/old -r --confirm    # 递归删除必须 --confirm,默认进回收站
gdrive staging list                    # 暂存区现状
gdrive staging clean                   # 立刻回收过期条目
```

MCP 工具与 CLI 一一对应(`drive_df`↔`gdrive df`、`drive_upload`↔`gdrive upload`……)。

## 周期任务

声明式定义存 `/etc/gdrive-agent/tasks/*.json`,渲染成 systemd service+timer(复用 vps-backup 的成熟模式)。

```bash
gdrive task create nightly-data --type push \
  --schedule 'daily' --local /data/quant --remote datasets/quant
gdrive task list                  # 含下次执行时间、上次结果
gdrive task run nightly-data      # 手动跑一次,不影响定时
gdrive task logs nightly-data
gdrive task delete nightly-data   # 会一并清理 systemd unit
```

类型:`sync`(镜像,**会删远端多余文件**)/ `push`(只增不删)/ `pull`(Drive→本地)/ `script`(任意命令)。
`--schedule` 用 systemd OnCalendar 语法:`hourly`、`daily`、`*-*-* 03:00:00`、`Mon *-*-* 06:00:00`。

## 安全护栏(都是拒绝优先)

- **备份仓库隔离**:任何解析到 `vps-backup` 的路径一律拒绝;路径含 `..` 直接拒绝(不静默改写)
- **磁盘预检**:下载超过 `free - 20G` 一律拒绝,且拒绝时零写入
- **删除确认**:递归删除必须显式 `confirm=true`;默认进 Drive 回收站,`permanent=true` 才彻底删
- **本机认证**:rcd 绑 127.0.0.1 且启用 HTTP Basic,凭据在 `/etc/gdrive-agent/rc-token`(600)
- **审计**:所有变更操作记 `/var/log/gdrive-agent/audit.log`
- **带宽保护**:UTC 13:30–21:00(北京 21:30–05:00,美股时段)限速 8M,避免拖累 quantcheck

## 运维

```bash
systemctl status gdrive-rcd.service        # 传输引擎,必须 active
systemctl status gdrive-mount.service      # 只读挂载
systemctl list-timers 'gdrive-*'           # 暂存区 GC + 各周期任务
tail -f /var/log/gdrive-agent/rcd.log      # 传输日志
cat /var/log/gdrive-agent/audit.log        # 变更审计
claude mcp list | grep gdrive              # MCP 注册状态
```

| 现象 | 处理 |
|---|---|
| MCP 工具调用报"无法连接 rclone rcd" | `systemctl restart gdrive-rcd.service` |
| 下载被拒(磁盘不足) | 按上面"空间不够时的处理顺序"依次尝试 |
| `/mnt/gdrive` 访问卡住或报错 | `systemctl restart gdrive-mount.service` |
| 传输很慢 | 正常:美股时段限速 8M。改 `/etc/gdrive-agent/config.toml` 的 `[bandwidth]` 后重启 rcd |
| 海量小文件传输极慢 | 应该走 pack;检查是否被 `--strategy` 强制成 direct |
| 暂存区满 | `gdrive staging clean`;若条目被 lease 钉住,先 `staging release` |

## 配置

`/etc/gdrive-agent/config.toml`(600)。常调项:`disk.reserve_gb`(保留水位)、
`disk.staging_cap_gb`、`disk.ttl_hours`、`transfer.pack_min_files`、`bandwidth.timetable`。
改完 `systemctl restart gdrive-rcd.service`。

**凭据复用备份系统的 `/root/.config/rclone/rclone.conf`(自有 OAuth client_id)。不要改它——
那是每日备份的命脉。** 详见 `/root/backup/BACKUP-SYSTEM.md`。

## 文件位置

```
/opt/gdrive-agent/           core(gdrive_agent/)+ mcp_server.py + .venv
/usr/local/bin/gdrive        CLI 入口
/etc/gdrive-agent/           config.toml、rc-token、tasks/
/var/lib/gdrive-agent/       staging/(暂存区)、leases/
/var/log/gdrive-agent/       rcd.log、mount.log、audit.log、task-*.log
/etc/systemd/system/         gdrive-rcd / gdrive-mount / gdrive-staging-gc / gdrive-task-*
```

## Google Drive 的硬性配额(会真的撞上)

- **每天最多上传 750 GB。** 超了之后 24 小时内无法再上传。服务已加
  `--drive-stop-on-upload-limit`,配额耗尽时 job 会**明确失败**并在错误里写明原因,
  而不是无限重试假装还在跑。大数据集要分多天传,或先在本地压缩/打包减少体量。
- 单文件最大 5 TB。
- 每文件都有固定 API 开销,所以海量小文件必须走 pack 策略(服务会自动判断)。

撞到 750G 上限时:`gdrive job status <id>` 会显示失败,日志里有 `uploadLimitExceeded`。
等 24 小时后重跑同一条命令即可续传(rclone 会跳过已传完的文件)。

## 已知限制

- 单一后端(Google Drive)。核心是后端无关的,加 S3/B2 只需扩 `config.toml` 的 `[remote]`
- 挂载是只读的。写入一律走 `drive_upload`,避免 VFS 写缓存把盘吃满
- 任务失败无外部通知,靠 `gdrive task list` 的 `last_result` 巡检
- pack 模式的索引存在本地暂存区,若要长期保留需一并上传
