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
- **可移植**：数据目录默认相对项目文件夹解析；有第二块盘时自动把数据放过去；真单盘机器会落到监控盘的专用子目录并从扫描中排除，避免自激。

## 平台与权限

- 仅支持 **Windows 10 / 11 + NTFS** 卷（依赖 Win32 API、`fsutil`、`vssadmin`，Linux/macOS 不适用）。
- 只读扫描无需管理员；USN 深读、MFT 直读、VSS 查询需管理员权限，建议以管理员身份运行以获得完整功能。

## 快速开始

在 `CDriveSentinel/` 目录下执行：

```powershell
python src\main.py init-db       # 建库 + 首个心跳
python src\main.py snapshot      # 手动跑一次快照 + 差分
python src\main.py daemon        # 常驻：心跳 / USN / 快照 / 日报
python src\main.py report --day 2026-11-20 --top 20   # 生成某天日报
python src\main.py status        # 数据库概况
python src\main.py selftest      # 端到端自检（只读沙箱，不碰 C:）
python src\main.py usn-status    # USN 权限 / journal / 游标诊断
```

也可用 PowerShell 启动器：`powershell -ExecutionPolicy Bypass -File .\run_p1.ps1 status`。

将守护进程注册为开机计划任务（需管理员）：`powershell -ExecutionPolicy Bypass -File .\install_task.ps1`（卸载加 `-Uninstall`）。

## 配置

编辑 `CDriveSentinel/config/config.json`。常用项：

| 字段 | 含义 |
|---|---|
| `volume` | 监控卷，默认 `C:` |
| `data_root` | 数据目录，相对项目文件夹解析，默认 `.` |
| `scope` | 扫描范围，空数组表示整卷 |
| `snapshot_minutes` / `heartbeat_seconds` / `usn_poll_seconds` | 调度间隔 |
| `alert_free_gb_below` / `alert_growth_gb_per_hour` | 告警阈值 |
| `daily_report_hour` / `daily_report_top` | 日报生成时间与 Top N |

**重要**：所有产物必须避开监控卷。有第二块盘时程序会自动把 `data_root` 迁过去；单盘时数据落在监控盘的 `CDriveSentinelData/` 子目录并从扫描中排除。

## 打包为独立 exe（Windows）

无需 Python 环境即可分发，产出 `dist/CDriveSentinel/` 文件夹：

```powershell
cd CDriveSentinel
python -m pip install pyinstaller   # 仅构建时需要
python build.py
```

把整个 `dist/CDriveSentinel/` 文件夹拷到目标电脑即可运行（无需安装 Python）：

```
CDriveSentinel.exe status
CDriveSentinel.exe daemon
CDriveSentinel.exe selftest
```

- 采用 onedir 模式，配置与数据都在 exe 旁边：首次运行自动生成 `config/config.json`（可编辑），数据写入 `data/`（或自动迁到非监控盘）。
- 把守护进程注册为开机计划任务（需管理员）：在 `dist/CDriveSentinel/` 内运行 `powershell -ExecutionPolicy Bypass -File .\install_task.ps1`；卸载加 `-Uninstall`。脚本会自动识别 exe 版并以无窗口方式运行。
- `CDriveSentinel.spec` / `build.py` 为构建配置；不需要的第三方模块已在 spec 中排除，未启用 UPX 压缩以避免杀毒误报。

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
