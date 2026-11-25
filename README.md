# SpaceKnife

C 盘空间变化实时监测与归因工具集。回答一个问题：**C 盘的哪个文件夹下的什么内容，在哪天的哪个时间段，占用了多少空间。**

> ⚠️ **本系统严格只读**：只采集、记录和报告，绝不自动删除、移动或修改 C 盘上的任何文件。任何清理动作必须由用户自行确认后单独执行。

## 目录结构

- **`CDriveSentinel/`** — 主项目：C 盘空间变化监测（P0 基线 → P1 快照差分 → P2 USN 事件流，均已实现）
  - `src/` — 统一入口 `main.py` 及各期模块
  - `config/config.json` — 配置（数据目录、扫描范围、USN、告警、日报等）
  - `docs/` — 方案设计与各期修复计划
  - `run_p0.ps1` / `run_p1.ps1` / `install_task.ps1` — PowerShell 启动器与计划任务安装脚本
  - `build.py` / `CDriveSentinel.spec` / `pack_release.py` — 打包与发布归档脚本
- **`signpath/`** — SignPath 免费开源代码签名配置与设置指南
- **`.github/workflows/build-release.yml`** — 打 `v*` tag 时自动构建、送签、发布 Release

## 特性

- **只读采集**：逐文件扫描 NTFS，按分配大小（`AllocationSize`）计量，压缩/稀疏文件用 `GetCompressedFileSizeW`，硬链接按 NTFS 文件 ID 去重。
- **快照差分**：FRN 对齐的前后快照流式归并，精确到字节地分类 CREATE / EXTEND / DELETE / RENAME，诚实保留「未归因」残差。
- **USN 事件流**：直读 USN Journal（V2/V3/V4），秒级时间与操作原因，按 FRN 关联快照事件升级置信度；瞬态事件单列，不注入字节账本。
- **日报与告警**：每日 txt/HTML 报告、门户页，磁盘余量与增长速度告警。
- **零第三方依赖**：Python 3.7+ 标准库（`ctypes` / `sqlite3` / `gzip` / `csv`），无需 pip 安装任何包。
- **可移植**：程序与数据分离——安装版数据在 `%PROGRAMDATA%\CDriveSentinel`，源码运行在项目文件夹，放一个 `portable.txt` 则回到 exe 旁的绿色版布局；数据若在监控卷上会自动排除该子树，避免自激。

## 平台与权限

- 仅支持 **Windows 10 / 11 + NTFS** 卷（依赖 Win32 API、`fsutil`、`vssadmin`，Linux/macOS 不适用）。
- 只读扫描无需管理员；USN 深读、MFT 直读、VSS 查询需管理员权限，建议以管理员身份运行以获得完整功能。

## 快速开始

在 `CDriveSentinel/` 目录下执行：

```powershell
python src\main.py setup --yes   # 首次设置：档位 + 监控范围（不加 --yes 则交互问答）
python src\main.py init-db       # 建库 + 首个心跳
python src\main.py snapshot      # 手动跑一次快照 + 差分
python src\main.py daemon        # 常驻：心跳 / USN / 快照 / 日报
python src\main.py report --day 2026-11-20 --top 20   # 生成某天日报
python src\main.py status        # 数据库概况（含控制通道状态）
python src\main.py paths         # 程序 / 数据 / 配置实际落点
python src\main.py scan-now      # 请求守护进程立刻扫一次（不需管理员）
python src\main.py pause         # 暂停调度（正在跑的扫描会中止）
python src\main.py resume        # 恢复调度
python src\main.py selftest      # 端到端自检（只读沙箱，不碰 C:）
python src\main.py usn-status    # USN 权限 / journal / 游标诊断
```

也可用 PowerShell 启动器：`powershell -ExecutionPolicy Bypass -File .\run_p1.ps1 status`。

将守护进程注册为开机计划任务（需管理员）：`powershell -ExecutionPolicy Bypass -File .\install_task.ps1`（卸载加 `-Uninstall`）。

### 控制通道（托盘 ↔ 守护进程）

托盘界面：`python gui\tray.py`（`--window` 启动即展开面板，`--smoke` 自检，`--wizard` 打开首次设置）。界面只读数据库，**只写 `runtime/`**，不需要管理员权限。

**首次设置的三个选择**（`setup` 命令或向导里都一样）：监控范围（整盘 / 只监控用户文件夹）、占用档位（极简 320 / 标准 550 / 完整 800 MB）、是否开机自启。之后一切自动运行。`config.json` 里 `setup_complete: true` 表示已经配好，向导不再打扰；想重开向导用 `--wizard`。

```json
{ "paused": false, "request_scan": 1799999999.0, "by": "tray" }
```

- 请求用持久化的 `runtime.last_scan_request` 消费，**守护进程重启不会重放旧点击**。
- 暂停会让正在跑的扫描在数秒内中止，快照如实标记为 `PARTIAL(cancelled)`，并且**不会**成为下次差分的基线。
- 界面与守护进程必须指向同一数据目录，否则便条写到了没人看的地方。

## 配置

编辑 `CDriveSentinel/config/config.json`。常用项：

| 字段 | 含义 |
|---|---|
| `volume` | 监控卷，默认 `C:` |
| `data_root` | 数据目录，相对「数据根」解析（安装版为 `%PROGRAMDATA%\CDriveSentinel`，源码运行为项目文件夹），默认 `.` |
| `scope` | 扫描范围，空数组表示整卷 |
| `exclude_paths` | 永不扫描的目录列表（绝对路径，或相对「数据根」）。数据目录若在监控卷上会自动加入；写进去的目录既不参与快照扫描，也不会进 USN 事件流 |
| `skip_paths` | `exclude_paths` 的旧名字，仍然生效（两者合并使用） |
| `snapshot_minutes` / `heartbeat_seconds` / `usn_poll_seconds` | 调度间隔 |
| `max_scan_seconds` | 单次全盘扫描的时间预算（默认 3000）。**必须大于真实扫描时长**：超时的扫描在 L3 下不落库（partial 不能当基线），等于这个窗口没有数据。守护进程每轮按最近 5 次完整扫描的中位数 ×1.5 自动上调（只升不降），正常不需要手改 |
| `data_budget_mb` | **自身占盘硬上限**（默认 800）。超出时自动按「损失最小的先砍」收缩：旧归档 → USN 明细 → 可重建索引 → 清单 → 账本；`0` 关闭 |
| `usn_retention_days` | USN 事件明细保留天数（默认 2）。报告只需要聚合数字，不需要 7 天逐条 |
| `archive_retention_days` | 每日 gzip 归档保留天数（默认 2）。清单和账本已覆盖当前状态与每次变化，归档只是给外部工具的时间点副本 |
| `mft_index_enabled` | 是否保留完整 MFT 索引（默认 `false`）。清单现在含目录行，父目录路径可直接解析；这份索引只在「两次扫描之间生灭的文件」上有用，实测为此花掉 159 MB |
| `frn_path_map_enabled` | 是否保留「这个文件以前叫什么」的历史（默认 `true`，约 30 MB）。极简档关掉它，代价是两次扫描之间生灭的文件更容易落到 `unresolved` |
| `alert_free_gb_below` / `alert_growth_gb_per_hour` | 告警阈值 |
| `daily_report_hour` / `daily_report_top` | 日报生成时间与 Top N |

**占盘说明**：清单（`file_state`）只存 `(目录 id, 文件名)`，实测 **57 B/行**；目录字符串单独存一份 `dir_path`（已去掉 UNIQUE 索引，省 38 MB）。从 L3 起只有**一份**清单：扫描先把结果写进 `scan_stage`，扫完再原地合并进 `file_state`，因此 1.23M 行（103 万文件 + 20 万目录）约 86 MB，而不是原先两份的 205 MB。清单同时记录每个目录的 FRN，父目录路径不再需要 `mft_index`——这砍掉了 159 MB。实测：存活数据约 **400 MB**，加上归档与日志约 **450 MB**；扫描期间暂存区会让数据库文件临时多出约 80 MB 空闲页，每轮 `reclaim` 归还，1–2 轮内归零。用 `python src\main.py status` 可看到实时占用与 `reclaimable` 拆分；`maintenance` 做一次完整收缩（**需先停掉守护进程**，VACUUM 要排他锁）。要继续压体积只能缩短历史：USN 明细 2→1 天（−92 MB）、账本 7→3 天（−55 MB）、关掉 `frn_path_map`（−30 MB）、归档 2→1 天（−20 MB），四项全开约 250 MB。

**重要**：所有产物应避开监控卷。源码运行时有第二块盘会自动把 `data_root` 迁过去，单盘时落到监控盘的 `CDriveSentinelData/`；安装版固定使用 `%PROGRAMDATA%\CDriveSentinel`，若它在监控卷上则整目录排除扫描。排除清单属于扫描范围的一部分：改动它会开启一个新的基线，因此不会把被排除目录里的历史文件误报成"删除"。

## 打包为独立 exe（Windows）

无需 Python 环境即可分发，产出 `dist/CDriveSentinel/` 文件夹：

```powershell
cd CDriveSentinel
python -m pip install pyinstaller   # 仅构建时需要
python build.py
```

把整个 `dist/CDriveSentinel/` 文件夹拷到目标电脑即可运行（无需安装 Python）。里面是**两个程序，共用一份 `_internal\` 运行时**：

| 程序 | 形态 | 用途 |
|---|---|---|
| `SentinelTray.exe` | 窗口程序 | 双击即用：托盘图标 + 面板（可用空间、今日净增、Top 10、告警、7 天趋势、"立即扫描/暂停"） |
| `CDriveSentinel.exe` | 控制台 | 采集进程与维护命令（见下） |

日常使用只需要 `SentinelTray.exe`；它不需要管理员权限，也从不写数据库，只通过 `runtime\state.json` 向采集进程发通知。

```
CDriveSentinel.exe status
CDriveSentinel.exe daemon
CDriveSentinel.exe selftest
```

- 采用 onedir 模式。**程序与数据分离**：安装后程序在 `C:\Program Files\CDriveSentinel\`，配置与数据在 `%PROGRAMDATA%\CDriveSentinel\`（首次运行自动生成 `config\config.json`，可编辑）。
- 源码运行不受影响：配置与数据仍然写在项目文件夹内，路径与以前完全一致。
- **界面与采集进程共用同一套落点规则**（`SENTINEL_HOME` → `portable.txt` → ProgramData → 项目目录），所以托盘读到的必然是采集进程写入的那份库。安装版不再"探测 exe 旁边有没有 `data/`"——那曾让界面读到一份已经停止写入的历史库。
- 想做成"绿色版"（数据随程序走）：在 `CDriveSentinel.exe` 同目录放一个空的 `portable.txt`，数据就会退回 exe 旁边的 `config\`、`data\`。
- 数据目录若落在被监控的卷上（典型是只有 C: 单盘的笔记本），该子树会被扫描排除，避免把工具自身的写入算成 C 盘增长。
- 查看当前实际使用的目录：`CDriveSentinel.exe paths`。
- 把守护进程注册为开机计划任务（需管理员）：在 exe 目录内运行 `powershell -ExecutionPolicy Bypass -File .\install_task.ps1`；卸载加 `-Uninstall`。脚本会自动识别 exe 版并以无窗口方式运行。
- `CDriveSentinel.spec` / `build.py` 为构建配置；不需要的第三方模块已在 spec 中排除，未启用 UPX 压缩以避免杀毒误报。
- 重复构建时旧产物不会被删除，而是改名为 `CDriveSentinel.prev-<时间戳>`（`build.py` 末尾会打印路径），确认新构建无误后自行删除即可。

## 代码签名（SignPath Foundation）

发布版通过 [SignPath Foundation](https://signpath.org/) 免费开源签名消除 SmartScreen 拦截。签名私钥托管在 SignPath 的 HSM，证书私钥不落地；每次发布由 GitHub Actions 在托管 runner 上构建、送签、人工批准后发布。

- 配置步骤（申请、GitHub App、Secrets/Variables、审批）见 [`signpath/README.md`](signpath/README.md)，签名策略见 [`signpath/CODE_SIGNING_POLICY.md`](signpath/CODE_SIGNING_POLICY.md)。
- 发布：打一个 `v*` tag 并 push，workflow 自动完成构建 → 送签 → 校验 → 发布带签名的 Release（含 SHA-256）。
- 尚未配置 SignPath 变量时，同样流程会发布未签名 Release，可先用它作为开源签名申请素材。
- 验证：`signtool verify /pa CDriveSentinel.exe`。

## 技术细节

完整的计量口径、数据源分工、差分算法、USN 结构处理与实测记录见 [`CDriveSentinel/docs/DESIGN.md`](CDriveSentinel/docs/DESIGN.md)。

## 许可

MIT License — 详见 [LICENSE](LICENSE)。
