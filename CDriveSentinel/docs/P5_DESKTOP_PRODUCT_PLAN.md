# CDriveSentinel P5 方案：从「能跑的脚本」到「用户能直接用的 Windows 应用」

> 分支：`feature/2611/softwareModify-hyp`　基线提交：`b3231c3`
> 目标：一个从没装过 Python 的普通用户，下载一个安装包 → 双击 → 一路下一步 → 重启后自动在后台工作 → 托盘能看到状态 → 随时能看到「昨天 C 盘哪里胖了」→ 卸载干净。

---

## 0. 结论先行

1. **exe 形态不是问题，已经验证过了。** 现成 `dist/CDriveSentinel/`（onedir，23 MB，主程序 2.0 MB）实测可启动；代码零第三方依赖，CI 已能自动出包。**真正的门槛不是"能不能打包"，而是下面四件事。**
2. **四个真门槛**：① 安装后往哪写数据（装进 `Program Files` 会写不进去）；② 提权与计划任务（USN 深读要管理员，但 UI 不能提权）；③ 全盘扫描要 23–25 分钟、数据库已 2.0 GB；④ 没有签名会被 SmartScreen 拦。
3. **推荐路线**：保留现有 CLI 内核不动，外面加「安装器 + 托盘 GUI 壳」，采集进程继续以计划任务提权运行，GUI 永不需要管理员。**M1 做完（3–5 天）用户就能"直接装、直接用"**；性能达标取决于是否做 MFT 直读（P4，另立项）。

---

## 1. 现状体检（实测，非估算）

| 项 | 实测值 | 对产品化的含义 |
|---|---|---|
| 代码规模 | `src/` 11 个模块，≈5,200 行 | 内核成熟，值得包壳而不是重写 |
| 第三方依赖 | **零**（`ctypes` / `sqlite3` / `gzip` / `csv`） | 打包包袱极小，是最大优势 |
| 已构建产物 | onedir 23 MB，exe 2.0 MB，`status` 可正常输出 | exe 可行性已证明 |
| 全盘快照 | 82–103 万文件 / 14.5–20 万目录，**1366–1505 s（23–25 分钟）** | 用户机器上"几乎一直在扫盘" |
| 超时情况 | 8 次采样中 **3 次 `PARTIAL(timeout)`**，1500 s 预算不够 | 结果会不完整，且用户无感知 |
| 数据体积 | `sentinel.db` **2.03 GB**，`data/` 合计 **2.7 GB** | 装完就吃掉用户 3 GB |
| 调度 | `snapshot_minutes=30` | 扫 25 分钟、歇 5 分钟 ≈ 持续 I/O |
| 交互 | 13 个 CLI 子命令 + 2 个 PS1 安装脚本 | 普通用户不可能完成 |
| 提权 | `install_task.ps1` 必须管理员 PowerShell 手工跑 | 安装体验断点 |
| 呈现 | 每日 HTML/TXT + 门户 `index.html`，浏览器打开 | 可复用，是"界面"的现成底子 |
| 目录 | 程序与数据同目录（exe 旁 `config/`、`data/`） | **装到 Program Files 必失败** |
| 分发 | 手拷文件夹；SignPath CI 已配但需人工审批 | 无安装、无卸载、无更新 |

### 1.1 两个容易被忽略的坑

- **`skip_paths` 是"假配置"**：它只在 `p1_diff.is_whitelisted()` 里过滤差异事件，**不参与扫描排除**。用户以为写了就能跳过目录、省扫描时间，实际不生效。产品化必须统一语义。
- **数据是否被自己统计**，目前只靠 `paths["root"]` 一个排除项兜住（`do_snapshot` 里的 `exclude_roots`）。一旦按产品化把数据搬到 `%PROGRAMDATA%`，这条防线就失效了 —— 必须改成"排除清单"并写进自检。

---

## 2. 目标与验收

### 2.1 用户旅程（唯一验收主线）

```
下载安装包 → 双击 → 1 次 UAC → 首启向导（3 个选择，30 秒）
   → 后台静默采集（托盘图标常驻）
   → 双击托盘：今天/昨天哪里胖了、Top 目录、趋势
   → 右键托盘：立即扫描 / 暂停 / 打开报告 / 设置 / 退出
   → 卸载：任务、程序、数据（可选保留）都清干净
```

### 2.2 硬指标

| 指标 | 目标 |
|---|---|
| 安装 | ≤ 3 次点击，**1 次** UAC，全程中文 |
| 待机开销 | CPU 均值 < 1%，常驻内存 采集 < 150 MB / 含 GUI < 250 MB |
| 扫描体感 | 扫描期间用户操作不卡（I/O 优先级 IDLE） |
| 单次周期 | 目标 ≤ 90 s；首扫 ≤ 5 min（需 MFT 直读或范围模式） |
| 磁盘写入 | < 200 MB/天稳态 |
| 卸载 | 无残留计划任务 / 无残留服务 / 数据可选择保留 |
| 信任 | 安装包与 exe 均带有效签名，无 SmartScreen 红屏 |

---

## 3. 四个必须先定的架构决策

### D1 · 进程与权限模型

| 方案 | 说明 | 取舍 |
|---|---|---|
| **A. 采集进程独立 + GUI 壳**（推荐） | `CDriveSentinel.exe daemon` 仍由计划任务提权跑；新增 `SentinelTray.exe` 普通权限 | 崩溃隔离；UI 不提权，浏览器/通知才正常；CLI 与 `selftest` 全部保留，回归风险最小 |
| B. 单进程 GUI 内含调度线程 | 像 Puzi 那样一个窗口里跑线程 | 简单，但 GUI 崩溃＝采集中断；提权 GUI 会带来 UAC 与 `os.startfile` 失败 |
| C. Windows 服务 | `sc create` + 服务控制 | 最"正规"，但服务↔UI 要 IPC，安装器/调试成本最高，P3 已证明提权与桌面交互易踩坑 |

**结论：A。** 一句话原则：**提权只发生在采集进程，界面永不需要管理员。**

### D2 · 程序 / 数据 / 配置三目录分离（必做，否则装不上）

现状 `p1_config.app_dir()` 把三者绑在 exe 旁边，装进 `Program Files` 后普通用户改不了 `config.json`、写不了 `data/`。

| 类型 | 位置 | 理由 |
|---|---|---|
| 程序 | `C:\Program Files\CDriveSentinel\` | 符合 Windows 习惯，卸载/升级清晰 |
| 数据 | `C:\ProgramData\CDriveSentinel\data\` | 全局可写（安装时授权），不随用户切换丢失 |
| 配置 | `C:\ProgramData\CDriveSentinel\config\config.json` | GUI 设置页写入，带校验与备份 |

- 数据落在监控盘 C: 上 → 进入"单盘模式"，**必须**把整个 `C:\ProgramData\CDriveSentinel` 加入扫描排除（替换现在只排除 `paths["root"]` 的做法）。
- **取消"自动迁到第二块盘"的隐式行为**：产品里静默换盘会让用户找不到数据。改为默认固定位置 + 设置页里显式提供"更改数据目录"。
- 老版本（exe 旁 `data/`）要有一条迁移路径：首启检测到旧目录 → 提示"导入历史数据" → 拷 DB 后保留旧目录不动（只读）。

> 状态：已在 M1-1 落地（见 10.2）。

### D3 · 界面技术路线

| 方案 | 体积增量 | 观感 | 工时 | 说明 |
|---|---|---|---|---|
| **PySide6 托盘 + 主窗口**（推荐） | +40–70 MB | 原生、专业 | 中 | 托盘/通知/开机自启成熟；你已有 PySide6 经验 |
| 本地 Web UI（127.0.0.1 + 浏览器/WebView2） | ≈0 | 现代，图表好做 | 中 | 直接复用现有 HTML 报告；要处理端口占用、防火墙、token 鉴权 |
| tkinter | 0 | 朴素 | 低 | 零依赖但卖相差，不适合对外推广 |

**结论：PySide6 做托盘与"控制台页"（状态 / 今日 Top / 趋势），"详细报告"继续交给默认浏览器打开现有 HTML。** 这样 UI 轻、报告页零成本复用。
> 许可证注意：PySide6 为 LGPL，动态链接 + 不修改即可商用分发。

### D4 · 性能策略（不做就是给用户装了个"硬盘烤箱"）

| 手段 | 效果 | 成本 |
|---|---|---|
| ① 首启选"快速模式/全盘模式"（快速＝`C:\Users`、`ProgramData`、`Windows\Temp` 等高频区） | 首扫从 25 min → 2–4 min | 1 天 |
| ② 自适应节拍 `interval = max(config, 2 × 上次耗时)` + 默认 60 min | 消除"扫 25 歇 5" | 半天 |
| ③ 采集进程/线程 I/O 优先级降到 IDLE + 电池/全屏/空闲暂停 | 用户不再觉得卡 | 半天 |
| ④ retention 收紧 + 每周 `incremental_vacuum` | 3 GB → 目标 < 500 MB | 1 天 |
| ⑤ **MFT 直读（P4 方案 A）** | 25 min → 1–2 min，根治 | 5–10 天，另立项 |

**结论：先做 ①②③④（M2 内完成），⑤ 单独立项，不阻塞"能用"。**

---

## 4. 目标运行形态

```
┌──────────────────────────── 用户会话（普通权限） ────────────────────────────┐
│  SentinelTray.exe                                                            │
│   ├─ 托盘：状态灯 / 今日增量 / 立即扫描 / 暂停 / 打开报告 / 设置 / 退出       │
│   ├─ 主窗口：概览 · Top 目录 · 趋势图 · 设置页（写 config.json）              │
│   └─ 只读 DB + 写"命令位"（runtime_state）→ 不需要管理员                      │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │ SQLite（只读）+ 命名事件唤醒
┌───────────────────────────────────┴──────────────────────────────────────────┐
│  计划任务 CDriveSentinel（AtLogOn, RunLevel Highest, RestartOnFailure）       │
│  CDriveSentinel.exe daemon  ← 提权，独占写 DB / 扫盘 / USN / 生成报告         │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
        %PROGRAMDATA%\CDriveSentinel\data  ← 整个目录加入扫描排除（自激防护）
```

关键不变量（改代码时逐条对照）：

1. 监控卷**只读**，绝不删除/移动用户文件；
2. 提权只在采集进程，UI 绝不提权；
3. 自产数据目录必须被扫描排除（新增目录＝同步新增排除项＋自检）；
4. 全部 CLI 子命令与 `selftest` 保留，作为回归基线。

---

## 5. 修改清单（编号延续 F1–F20）

| 编号 | 内容 | 涉及文件 |
|---|---|---|
| **F21** | 程序/数据/配置三目录分离：`app_dir()` 拆为 `install_dir()` / `data_dir()` / `config_dir()`；冻结态走 `%PROGRAMDATA%`，源码态保持原样 | `p1_config.py` |
| **F22** | 扫描排除清单化：新增 `exclude_paths`，默认含自产数据目录；`skip_paths` 语义统一为"扫描排除 + 差异忽略"（已交付，见 10.3） | `p1_config.py` / `p1_snapshot.py` / `p1_diff.py` / `p2_store.py` |
| **F23** | Inno Setup 安装器：per-machine 安装、注册两个计划任务、写 `ProgramData` 权限、卸载清理、升级保留数据 | `installer/*.iss`（新增） |
| **F24** | 托盘壳 `SentinelTray.exe`（PySide6）：状态、Top 目录、趋势、通知、右键菜单、单实例互斥 | `gui/`（新增） |
| **F25** | GUI→采集进程控制通道：`runtime/state.json` 命令位 + 命名事件（立即扫描/暂停/恢复），GUI 零提权（已交付，见 10.4） | `p1_control.py` / `gui/control_client.py` / `main.py` |
| **F26** | 首启向导 + 设置页：监控盘、数据目录、快速/全盘模式、开机自启、日报开关；写盘带校验与 `.bak` 回滚 | `gui/` + `p1_config.py` |
| **F27** | 性能四件套：范围预设、自适应节拍、I/O 优先级 IDLE、电池/空闲暂停 | `main.py` / `p1_snapshot.py` |
| **F28** | 体积：单份清单原地更新（L3）、目录行进清单后取消 `mft_index`、归档保留收紧、按类拆分 retention、每周 `incremental_vacuum`（已交付，见 10.7） | `p1_db.py` / `p1_diff.py` / `main.py` |
| **F29** | 可靠性：计划任务失败自动重启、单实例、心跳停摆检测（> 2×interval 通知） | `installer` / `gui` |
| **F30** | 诊断：一键导出诊断包（run.log + status + 脱敏 config + 版本信息） | `gui/` / `main.py` |
| **F31** | 分发：CI 产出安装包 + SHA-256、安装包签名、启动时"检查更新 → 打开下载页" | `.github/workflows` / `signpath` |
| **F32** | 升级/卸载路径：旧版 exe 旁 `data/` 迁移、卸载三选一（保留数据/删除数据/完全清除） | `installer` / `p1_config.py` |

---

## 6. 分期路线

| 阶段 | 内容 | 产出（用户可见） | 建议工期 |
|---|---|---|---|
| **M1 能装能用** | F21 F22 F23 F24(简) F25 F26(简) F27① | 安装包 → 托盘 → 双击看今日 Top → 自动开机采集 | 3–5 天 |
| **M2 不伤机器** | F27②③ + F28 F29 F30 | 扫描不再持续占盘，占盘从 3 GB 降到 <500 MB，停摆会提示 | 3–5 天 |
| **M3 像个产品** | F26 全量（设置页/趋势/通知）+ 报告内嵌 | 设置全可视化，趋势图与通知 | 3–5 天 |
| **M4 能放心发** | F31 F32 + 签名 + 下载页与使用说明 | 签名安装包、升级不丢数据、SMART 无红屏 | 2–3 天 |
| **M5 性能根治**（可选） | P4 MFT 直读 | 25 min → 1–2 min | 5–10 天 |

**M1 结束即可对外小范围试用；M4 结束才适合面向陌生用户公开发布。**

---

## 7. exe 形式可行性评估（结论：可行，已部分落地）

| 形式 | 体积 | 启动 | 写目录/权限 | 杀软误报 | 更新 | 结论 |
|---|---|---|---|---|---|---|
| **onedir + 安装器** | 23 MB + 壳 | 快 | 安装器一次授权解决 | 低（未开 UPX，正确） | 安装器覆盖安装 | ✅ **推荐** |
| onefile | ≈25 MB | 慢 2–5 s（每次解压 temp） | `_MEIPASS` 路径陷阱多 | 较高 | 同 | ❌ 不建议 |
| MSI / WiX | 同 | 快 | 标准 | 低 | `msiexec` | 可用，但写 WiX 脚本工时高 |
| MSIX | 同 | 快 | 容器化，**计划任务受限** | 最低 | 商店式 | ❌ 与本工具形态冲突 |
| 绿色 zip | 23 MB | 快 | 用户自选，需手动注册任务 | 中 | 手动替换 | ✅ 保留为高级用户选项 |

**判断依据**：本工具的价值 = 常驻 + 提权 + 计划任务 + 每日报告。MSIX 的容器化会切断计划任务；onefile 会让每次启动解压 23 MB 且更易被误报；onedir + Inno 是唯一能同时满足"体积小、启动快、可注册任务、可干净卸载"的组合。

---

## 8. 风险登记册

| 风险 | 触发条件 | 规避 |
|---|---|---|
| 装进 Program Files 后写不进数据 | 沿用当前"数据在 exe 旁" | F21 三目录分离，M1 必做 |
| 自产数据被自己统计（自激） | 数据目录变更但排除清单没跟着改 | F22 清单化 + 写进 `selftest` |
| 提权进程启动浏览器失败 | 从提权进程 `os.startfile` | 已有 P3 教训：报告打开交给普通权限的 GUI/独立任务 |
| 计划任务被杀软/组策略拦截 | 企业环境 | 安装器检测 + 降级为"仅手动模式"并提示 |
| 升级覆盖 config / 数据 | 安装器直接覆盖目录 | 安装器分离程序与数据 + 升级前备份 config |
| 单盘机器 200 万文件首扫 1 小时 | 无 D 盘的大容量笔记本 | 快速模式默认开，首扫可后台可取消 |
| PySide6 体积/许可 | 打包后 +40–70 MB | LGPL 动态链接合规；采集内核保持零依赖 |

---

## 9. 已确认决策（2026-11-24）

| # | 决策项 | 结论 |
|---|---|---|
| D3 | 界面形态 | **PySide6 托盘 + 主窗口**；详细报告仍交给默认浏览器打开 |
| D2 | 安装范围 | **per-machine 装 `C:\Program Files\CDriveSentinel`**，数据 `C:\ProgramData\CDriveSentinel`，安装时一次 UAC |
| — | 实施顺序 | **先做 M1「能装能用」**，性能与体积（M2）紧随其后 |

仍未定：**发布目标**（只给朋友用可暂缓签名；面向陌生用户公开发布则 M4 的签名与下载页为必须项）。

---

## 10. M1 详细工作分解（进行中）

| 步骤 | 内容 | 验收 |
|---|---|---|
| M1-1 | `p1_config` 三目录分离：新增 `install_dir()` / `data_dir()` / `config_dir()`；冻结态读 `%PROGRAMDATA%\CDriveSentinel`，源码态行为不变 | `selftest` 全绿；源码态路径与现在完全一致 |
| M1-2 | 扫描排除清单化：`exclude_paths` 默认含数据目录；修正 `skip_paths` 语义（扫描排除 + 差异忽略） | 自检新增"自产数据目录不被计入"用例 |
| M1-3 | 旧数据迁移：检测 exe 旁 `data/`，首启提示导入（只读拷贝，不删旧目录） | 旧机器升级后历史快照仍在 |
| M1-4 | `SentinelTray.exe`（PySide6）：托盘菜单、状态灯、今日 Top 10、单实例互斥、右键"打开报告/立即扫描/暂停/退出" | 双击托盘 3 秒内出今日数据；重复启动不产生第二实例（已打包进 dist，见 10.8） |
| M1-5 | 控制通道：GUI 写 `runtime/state.json` + 命名事件唤醒 daemon；GUI 零提权 | 点"立即扫描"能在 30 秒内触发，且不弹 UAC |
| M1-6 | 首启向导（简版）：监控盘、数据目录、快速/全盘模式、开机自启 | 三项选择后即可全自动工作（已交付，见 10.9） |
| M1-7 | Inno Setup 安装器：per-machine、注册两个计划任务、授权 `ProgramData`、卸载清理 | 干净 Win10/11 虚拟机上装完重启即自动采集 |
| M1-8 | CI 增加安装包产出 + SHA-256 | 打 tag 后 Release 里有 `Setup.exe` 与校验和 |

### 10.1 M1-4 原型已交付（2026-11-24）

| 文件 | 作用 |
|---|---|
| `gui/datafeed.py` | 只读数据层。`mode=ro` + `PRAGMA query_only`，不 import `src/`，即使 `src/` 正在重构也不受影响 |
| `gui/tray.py` | 托盘 + 面板。PySide6，含状态图标、今日 Top 10、告警、7 天趋势、单实例互斥 |
| `docs/assets/m1-4-preview.png` | 用真实数据渲染的预览图 |

运行：

```powershell
cd CDriveSentinel
python gui\tray.py            # 正式运行（托盘 + 面板）
python gui\tray.py --window   # 启动即打开面板
python gui\tray.py --smoke    # 自检：托盘可用性 + 数据加载 + 状态
python gui\tray.py --shot out.png   # 离屏截图（开发用）
```

已实现：实时读取容量/今日净增/最近快照/USN/告警/趋势、Top 10 条形对比、托盘图标随状态变色（运行中绿 / 扫描中蓝 / 停止琥珀 / 出错红）、tooltip 摘要、双击托盘开面板、关闭窗口收进托盘、单实例。

**故意留空（M1-5 才做）**：「立即扫描」「暂停监控」当前只弹说明框，不写库、不碰计划任务——原型全程只读。

已知限制：`datafeed.load()` 单次约 0.6–0.7 s，因此放在 `QThreadPool` 里跑；M1-2 加了扫描排除与索引后再优化到 100 ms 以内。

### 10.2 M1-1 三目录分离已交付（2026-11-24）

| 位置 | 源码运行（开发） | 安装版（默认） | 绿色版（`portable.txt`） |
|---|---|---|---|
| 程序 `install_dir()` | 项目文件夹 | `C:\Program Files\CDriveSentinel` | exe 所在目录 |
| 数据 + 配置 `data_dir()` | 项目文件夹（**与改前完全一致**） | `%PROGRAMDATA%\CDriveSentinel` | exe 所在目录 |
| 覆盖方式 | — | 安装器（M1-7）授权写权限 | 手动放置 `portable.txt` |

改动清单：

| 文件 | 改动 |
|---|---|
| `src/p1_config.py` | 新增 `install_dir()` / `data_dir()` / `config_dir()` / `layout_info()`；`app_dir()`、`PROJECT_ROOT` 保留为兼容别名；`data_root` 改为相对「数据根」解析；冻结态**取消自动迁盘**，改为 `same_volume` 标记 + 整目录排除；`SENTINEL_HOME` 环境变量可整体重定向 |
| `src/main.py` | 新增 `paths` 子命令打印实际落点；崩溃兜底日志改用 `data_dir()` |
| `src/p1_report.py` | 报告与门户里的「数据目录」说明由写死改为动态显示真实路径 |
| `build.py` / `README.md` | 使用说明与文档同步为「程序与数据分离」 |

验证结果（全部实测）：

| 检查 | 结果 |
|---|---|
| `python src/main.py paths`（源码态） | 安装/数据/配置均为 `D:\SpaceKnife-local\CDriveSentinel`，与改前一致 |
| 布局断言：冻结 + ProgramData | 数据落 `%PROGRAMDATA%\CDriveSentinel`，`config.json` 写在 ProgramData 而非 exe 旁，无自动迁盘 |
| 布局断言：`portable.txt` | 数据退回 exe 目录，`portable=True` |
| 布局断言：`SENTINEL_HOME` | 整体重定向生效 |
| 布局断言：源码态 | 路径与改前逐项相同 |
| `python src/main.py selftest` | **37/37 通过** |
| `status` / `report --day` | 正常，报告文案已显示真实数据目录 |

已知边界：

- `%PROGRAMDATA%` 的写权限由安装器（M1-7）授予。未授权时（非提权进程先跑）配置只能读，缺失时回落到内存默认值并记录 `_config_missing`——不会崩，但会与提权进程的配置不一致。安装器落地前不要手工往 ProgramData 部署。
- 旧版「exe 旁 `data/`」的迁移仍是 M1-3。
- 扫描排除已由 M1-2 清单化（见 10.3），不再只依赖 `paths["root"]`。

### 10.3 M1-2 扫描排除清单化已交付（2026-11-24）

**问题**：排除逻辑只有一个来源——`paths["root"]`，而且只在单盘回退时启用。`skip_paths` 看着像"跳过目录"，实际只在差异事件上加一句 `note="whitelisted"`，既不省扫描时间也不影响统计。用户按文档写了目录，发现毫无作用。

现在统一成一份清单，扫描与差异共用：

| 来源 | 是否默认启用 | 说明 |
|---|---|---|
| 自身数据目录 | 在监控卷上时自动 | 自激防护。放在另一块盘时不加，免得污染指纹 |
| `exclude_paths` | 否（用户填） | 面向用户的正式键，支持绝对路径和相对数据根的路径 |
| `skip_paths` | 否（用户填） | 旧键，合并进同一份清单，老配置终于名副其实 |

三个实现细节值得记录：

1. **排除发生在 `os.scandir()` 之前**，不是扫完再过滤——被排除的子树连一次 `stat()` 都不会发生。`FileScanner` 新增 `excluded_skipped` 计数器，日志里能看到跳过了几项。
2. **排除清单进入 `scope_fingerprint`**。这是最容易漏的一步：如果指纹不变，加一个排除目录后，上一份快照里那些文件会全部被判为 `DELETE`，报告上凭空出现一个几个 GB 的"净减少"。纳入指纹后，改动排除清单等价于开启新基线，最多损失一个对比窗口。
3. **清单会自我校验**：任何包含扫描根的条目都会被丢弃。否则用户同时写 `scope=["C:\\Users"]` 和 `exclude_paths=["C:\\"]`，扫描结果会是空，而界面上只会显示"今天没有任何变化"。

另外把 `is_whitelisted()` 的前缀匹配改成了边界安全：原来是 `startswith("C:\\Temp")`，会把 `C:\Temperature` 一起命中。

改动清单：

| 文件 | 改动 |
|---|---|
| `src/p1_config.py` | 新增 `_norm_prefix()` / `_exclude_candidates()` / `exclude_prefixes()` / `is_excluded()`；`paths()` 返回 `exclude`；`scope_fingerprint()` 接受排除清单；`layout_info()` 暴露 `exclude` |
| `src/p1_snapshot.py` | `exclude_roots` 去重排序；新增 `excluded_skipped` 计数；文档说明排除发生在扫描前 |
| `src/p1_diff.py` | `is_whitelisted` → 边界安全的 `is_under`（旧名保留）；事件备注改为"排除目录漏进差异"的告警式说明 |
| `src/p2_store.py` | `save_events()` 接受 `exclude`，排除目录的 USN 事件直接丢弃并计入 `stats["excluded"]` |
| `src/main.py` | `do_snapshot()` 统一使用 `paths["exclude"]`；`run_usn_poll()` 传入排除清单；`paths` 子命令打印清单；自检新增 7 个用例 |
| `config/config.json`、`dist/.../config*.json`、`README.md` | 补 `exclude_paths` 字段与语义说明 |

验证结果：

| 检查 | 结果 |
|---|---|
| `selftest` | **46/46 通过**（新增 7 项：可枚举性对照、清单归一化、指纹变化、无 `file_state`、无 `change_event`、跳过计数、基线重建、扫描根保护、USN 丢弃） |
| `paths`（源码态） | `exclude: (none)`，scope 指纹仍是 `59d75dd6f26789f6`——**开发机没有被强制重置基线** |
| 冻结态模拟（数据落 `%PROGRAMDATA%`，监控 `C:`） | 清单自动含 `c:\...\cdrivesentinel`，`db`/`logs` 均判定为排除，指纹由 `59d7…` 变为 `477a…` |
| 实测排除生效 | 被排除目录内的文件：`file_state` 0 行、`change_event` 0 行、`excluded_skipped` ≥ 1 |
| `status` / `report --day` | 正常 |

已知边界：

- 排除清单目前只能改 `config.json`，设置页在 M3。
- 历史数据不做回填：升级前已经计入的自产数据事件仍在库里，等 `retention_days` 自然过期。要清干净需要一条一次性 SQL，属于可选项。
- 排除粒度是"目录"，不支持通配符（如 `*.tmp`）。按文件名过滤会让 `attributed` 与 `used_delta` 的对账口径变得含糊，暂不引入。

### 10.4 M1-5 控制通道已交付（2026-11-24）

**约束**：采集进程必须提权（要读 `C:\System Volume Information`、`$Recycle.Bin` 等目录），界面必须不提权（P3 踩过一次：从提权进程拉浏览器，Chrome/Edge 直接拒绝启动）。所以界面**不能调用**采集进程，只能"留个便条"。

协议（单文件、后写覆盖，不做队列）：

```json
// <data>/runtime/state.json
{ "paused": false, "request_scan": 1799999999.0,
  "updated_local": "2026-11-24 16:40:13", "by": "tray" }
```

| 决定 | 理由 |
|---|---|
| 状态文件而非数据库表 | 界面打开 `sentinel.db` 是 `mode=ro` + `query_only`，**采集进程是唯一写者**这条纪律比省一张表值钱 |
| 后写覆盖而非命令队列 | 没有孤儿文件要清、没有 TTL 要过期，连点两次自然合并成一次扫描 |
| 用持久化的 `runtime.last_scan_request` 消费请求 | 重启幂等：已经服务过的点击不会被重放 |
| 命名事件 `Global\CDriveSentinel.Wake` 只做加速 | 跨权限/跨会话可能开不了句柄；**正确性不依赖它**，守护进程最多每 5 秒回看一次文件，30 秒指标仍可保证 |
| 暂停也走同一个文件 | 一处状态、一处读取，界面与守护进程不会各说一套 |

**顺带解决一个体验坑**：原计划的 M1-5 只能"下一轮生效"，而这台机器单次全盘扫描 25 分钟、间隔 30 分钟，等于 80% 时间都在扫 —— 点"暂停"要等 20 多分钟才生效，等于没做。所以给 `FileScanner` 加了 `should_stop` 钩子（按目录检查，节流 5 秒读一次状态），暂停时把正在跑的扫描**当场中止**，并如实记成 `PARTIAL(cancelled)`（partial 永不作为 diff 基线）。

改动清单：

| 文件 | 改动 |
|---|---|
| `src/p1_control.py`（新增） | 状态文件读写（临时文件 + `os.replace` 原子替换）、`request_scan` / `set_paused` / `read_state`、命名事件 create/signal、`wait_for_request`（事件 + 分片轮询双保险） |
| `src/p1_snapshot.py` | `FileScanner(should_stop=...)`，按目录检查并置 `cancelled`；`stats()` 增 `cancelled` |
| `src/main.py` | `scan_decision()` 纯函数（可自检）；daemon 每轮读状态、暂停则跳过快照与 USN、命中请求则立即扫、`time.sleep` 改为 `wait_for_request`；新增 `scan-now` / `pause` / `resume` / `runtime` 子命令；`status` 显示通道状态 |
| `gui/control_client.py`（新增） | 界面侧最小客户端：读状态、写请求、signal 事件。**不 import `src/`**，与 `datafeed.py` 同一纪律 |
| `gui/datafeed.py` | `Dashboard.paused`；`state` 新增 `paused`；首启无心跳时改用 `shutil.disk_usage` 取真实容量（否则新装首屏是红色"0 B 可用 / 低于告警线"） |
| `gui/tray.py` | 立即扫描/暂停真正接通；新增"暂停/恢复"按钮；暂停时状态灯变琥珀、副标题说明、"立即扫描"置灰；菜单与状态同步 |

验证结果（真实进程，非模拟）：

| 检查 | 结果 |
|---|---|
| 点"立即扫描"→ 守护进程开始扫描 | **0.8 秒**（命名事件命中，日志 `daemon woken by event`） |
| 暂停中请求扫描 | 被拦截，**不扫描**；恢复后补做那一次（`manual scan requested ... (11s ago)`） |
| 扫描中暂停 | 扫描在 **5 秒内**中止：`files=104792 ... PARTIAL(cancelled)`，快照标记 `partial=1`，`diff status: partial` |
| 重启后旧请求不重放 | 由 `state` 表里的 `runtime.last_scan_request` 判定 |
| `selftest` | **57/57 通过**（新增 11 项：目录落点、默认值、请求落盘、暂停往返、原子写、损坏文件降级、三个调度决策、中止即 PARTIAL） |
| 事件不可用的降级 | 守护进程未启动时 CLI 只报"will pick it up within 5s"，不报错 |

已知边界：

- 事件能否跨权限签名取决于会话与 DACL；签名失败不影响功能（切片轮询兜底），日志会如实写 `event not signalled`。
- 暂停期间心跳继续写（界面才能分辨"暂停"与"进程死了"），只有快照与 USN 轮询停。
- 界面与守护进程若指向**不同的数据目录**（例如把安装版装进源码目录），便条会写到一个没人看的地方——这是 M1-3/M1-7 要一起收口的路径一致性问题。

### 10.5 L1 体积止血已交付（2026-11-24）

**触发点**：用户指出"会用这个工具的人 C 盘本来就没空间，你写 1 GB 进去是在帮倒忙"。这是对的，而且工具当时正在**为自己报警**（`自身数据 2.63 GB，超过上限 1.76 GB`）。

实测把 2.63 GiB 拆开（`dbstat`，非估算）：

| 对象 | 占用 |
|---|---|
| `usn_event` + 3 索引 | 672 MiB（8 天 178 万条） |
| `file_state` + 2 索引 | 568 MiB（2 份清单） |
| `mft_index` + 索引 | 159 MiB（可重建缓存） |
| `change_event` | 81 MiB |
| `-wal` | **646 MiB**（扫描被掐断留下的） |
| 空闲页（可回收） | 380 MiB |

**关键判断：问题不是"存哪"，是"存成什么"。** 同一份 88 万条清单，存 gzip CSV 是 **20 MB**，存 SQLite 行 + 索引是 **284 MB**——13 倍。所以"把 1.9 GB 搬到别的盘"是伪命题。

L1 只做不动数据模型的部分：

| 改动 | 效果 |
|---|---|
| `PRAGMA journal_size_limit=32MB` | WAL 有了上界，不再保留高水位（646 MiB → 实测 1.3 MiB） |
| `db.prune` 分批删除 + 尾部 `wal_checkpoint(TRUNCATE)` | 一次删 100 万行不再让 WAL 吃掉几百 MB，也不需要那么多空闲空间才能执行 |
| partial 清单只在"比所有完整快照都新"时才留 | 旧 partial 既不能当基线、也不是路径解析读的那份，实测白占 **880,117 行（约 390 MB）** |
| `usn_retention_days`（默认 2）与 `retention_days`（7）拆开 | 178 万条 → 45.5 万条；报告只要聚合，不需要 7 天逐条 |
| `data_budget_mb`（默认 800）硬预算 + 分级降级 | 超出时按"损失信息最少"的顺序自动收缩，并落一条 12 小时去重的告警 |
| `db.reclaim()`：分批 `incremental_vacuum` + `wal_checkpoint(TRUNCATE)` | 每份快照末尾回收 20000 页（约 80 MB），不长时间占写锁 |
| `prune` 命令升级为"立即应用保留策略 + 全量回收" | 改完保留参数不必等下一轮；`maintenance` 打印收缩前后对比 |

预算的分级降级顺序（越往下损失越大）：

1. 旧 gzip 归档（保留最新一份，它才是可导出的副本）
2. `usn_event` 明细 → 1 天
3. `mft_index` / `frn_path_map`（可重建缓存，重跑 `usn-index` 即可）
4. 每份清单只留 1 份（**代价是下次差分重新建基线**，日志会明说）
5. `change_event` → 1 天（最后手段）

关于默认值的一个诚实说明：**800 MB 是当时数据模型的下限附近，不是目标**。L1 阶段 1 份 103 万文件的清单在旧 schema 下就要约 285 MB，两份就 570 MB。L2（见 10.6）把它压到 67 B/行，两份清单落到约 205 MB，但 200 MB 总预算仍不现实——那份账在 10.6 末尾重新算过。

验证结果：

| 检查 | 结果 |
|---|---|
| `selftest` | **65/65 通过**（新增 8 项：prune 2+1 分离、无用 partial 被丢弃、分批删除计数、WAL 截断、`data_usage` 统计归档、预算先删归档、预算 0 关闭） |
| 真实库：`-wal` | 646.90 MiB → **1.28 MiB** |
| 真实库：`usn_event` | 1,779,162 行 → **455,713 行** |
| 真实库：`file_state` | 2,278,258 行 → **1,590,701 行**（丢掉 109 号那个无用的 partial 清单） |
| 真实库：页内空闲 | 259,462 页（**1.01 GiB 可归还**） |
| 一致副本上 VACUUM 实测 | **1.88 GiB → 0.83 GiB（59 秒）** |

预算判定用的是**存活数据**而不是文件大小：一个刚删掉一百万行的库在磁盘上仍然很大，若把空闲页算成"占用"，预算就会去删有用的清单来解决一个 VACUUM 能解决的问题（`db_free_bytes()` 就是为此存在）。`status` 会把两者分开显示。

已知边界：

- `incremental_vacuum` 需要短暂的排他锁；守护进程正在扫描时会被挡住（实测只会回收 20 MB）。因此真正的文件收缩由 `maintenance`（VACUUM）在空闲窗口完成，日常靠每轮 20000 页慢慢归还。
- L1 只止血，不减数据模型；2 份清单仍是 ~570 MB 的硬底。下一步是 L2（见 10.6）。

### 10.6 L2 数据模型瘦身已交付（2026-11-24）

L1 之后清单仍是最大的单项（2 份 × 284 MB）。原因不是索引写错了，而是**每行都存了一遍完整路径**：`C:\Users\15493\AppData\Local\Programs\...\file.dll` 里的目录部分，在同一份清单里被重复了上百万次。

新模型把路径拆开，只存一次：

```sql
-- 目录字符串全局唯一，与快照无关（删除的树也能还原路径）
CREATE TABLE dir_path (id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE);

CREATE TABLE file_state (
  snapshot_id INTEGER, frn INTEGER, dir_id INTEGER, name TEXT,
  logical_size, allocated_size, mtime INTEGER, attributes, is_dir,
  PRIMARY KEY (snapshot_id, frn)
) WITHOUT ROWID;          -- 主键即索引，不再需要 idx_fs_snap + autoindex
```

三处配套改动：

| 改动 | 说明 |
|---|---|
| `frn` TEXT → INTEGER | NTFS 文件引用是无符号 64 位，高位经常置 1；SQLite 只支持有符号，故存二补码并在 `frn_to_text()` 还原（`sort` 顺序仍一致，merge-join 不受影响） |
| `mtime_utc` TEXT → `mtime` INTEGER | 省掉每行 19 字节的 ISO 字符串 |
| 路径按需重建 | 差分只为**真正产生事件**的行（每次几千行）重建路径，`dir_path` 命中缓存；不再为 100 万行准备路径 |

实测每行开销（`dbstat`，沙箱里扫 68.7 万个真实文件）：

| 模型 | 每行 |
|---|---|
| 旧（路径 + 2 个索引） | 186 B 表 + 84 B 索引 = **270 B** |
| 新（dir_id + name，PK 即索引） | **67.2 B** |
| `dir_path` 摊销 | + 4 B/行（70k 目录，含 UNIQUE 索引） |

**约 4 倍。** 真实库的结果（`maintenance` 后实测）：

| 阶段 | 占用 |
|---|---|
| L1 结束（VACUUM 后） | 1.03 GiB |
| L2 迁移 + VACUUM（清单已清空，待重建） | **574.86 MiB**（db 462 + 归档 100 + 日志 11） |
| 两份新清单重建后（预估） | ~660 MB |

迁移方式：`init_schema()` 检测到旧的 `file_state`（含 `path` 列）就整表丢弃重建。清单是派生数据，下次扫描即可重建；代价是**一个窗口重新建基线**。不采用逐行转换：200 万行转换要一两分钟，收益只有"少丢一个窗口的差分"，不划算。

**升级顺序有硬约束**：旧的守护进程写的是旧列名，schema 一换就会写失败。必须先停旧守护进程，再跑新代码。本次实操中我违反了这一点，直接被现实验证了代价（见下）。

顺带修掉两个真实缺陷：

1. **VACUUM 把采集进程挤死了。** 17:17 我执行 `maintenance`，采集进程的下一次心跳撞上排他锁，`db.add_heartbeat()` 是循环里**唯一没有 try/except** 的写操作，异常直接终结了整个 daemon（最后心跳 17:16:39）。已给心跳与 age 查询加保护——单次数据库争用不该让采集进程退出；这也正是 F29「计划任务失败自动重启 / 停摆检测」要解决的。
2. **孤儿快照会挤掉真正的基线。** `file_count IS NULL` 且早已超过扫描预算的行是"扫描被打断"，它永远不能当基线（`previous_comparable` 要求 `file_count > 0`），但旧逻辑按"最新 N 份"把它当完整快照保留。现在按年龄识别为 orphan，直接标记 `pruned=1` 并回收其清单。

验证结果：

| 检查 | 结果 |
|---|---|
| `selftest` | **66/66 通过**（新增"废弃扫描被退休"用例；差分 CREATE/EXTEND/DELETE/RENAME/硬链接在新 schema 下全部一次通过） |
| 真实库迁移 | `integrity_check=ok`，schema `p1.2`，`dir_path`/`file_state` 清空待重建 |
| 真实库体积 | 1.13 GiB → **574.86 MiB**（含 VACUUM） |
| `report` / `status` / 界面自检 | 正常（报告已改为按 `dir_id` 聚合再上卷） |

已知边界与下一步：

- **200 MB 仍然做不到。** 两份清单本身就是 ~205 MB（1.03M 文件 × 2 × 67 B + 目录表）。要再砍一半只有一条路：**只保留一份清单**，把差分改成"扫描时逐条与现有清单比对并原地更新"（省掉第二份 + 省掉 merge-join，代价是需要一个已见 FRN 集合或标志列）。那属于 L3，改的是差分算法而不是表结构，要重写 `diff_snapshots` 的核心循环。
- `mft_index`（159 MB）现在是第二大项，而它的职责（FRN→路径）已被新清单覆盖大半；下一轮可以把它降级为"仅覆盖清单里没有的 FRN"或直接砍掉。
- `dir_path` 的 UNIQUE 索引占 218 B/目录，若目录数达到 20 万，这一项约 45 MB；改成路径哈希主键还能再省，但需要处理碰撞，暂不做。

### 10.7 L3 单份清单 + 目录行取代 mft_index 已交付（2026-11-24）

L2 把每行压到 67 B，但差分要「上一次 + 这一次」两份完整清单，1M 文件就是 205 MB 的硬底。L3 把它拆掉，走三步：

**1. 只有一份清单，扫描先进暂存区。**
`file_state` 现在描述「截至快照 X 的卷面状态」（X 记在 `runtime_state.inventory.snapshot_id`），扫描结果先写 `scan_stage`，扫完再原地合并。

暂存区不是"多一张表"的洁癖，而是**正确性要求**：如果边扫边改清单，一次被暂停或断电的扫描会让"半个卷"变成基线，下一次差分就把没扫到的那一半报成删除。有了暂存区，取消/超时的扫描对清单零影响（用例 `L3 a cancelled scan leaves the inventory and baseline untouched` 就是钉这一条）。

**2. 分类从 Python 搬到 SQL。**
旧实现把两份清单读进 Python 做归并，2M 行逐行判等；现在五个桶由 SQL 完成有序扫描，Python 只看到真正变化的行（每窗口几千行）：

| 桶 | 条件 | 事件 |
|---|---|---|
| 新增 | stage 有、inventory 无 | `CREATE` |
| 消失 | inventory 有、stage 无 | `DELETE` |
| 变大/变小 | 同 FRN，`allocated_size` 不同 | `EXTEND` / `TRUNCATE` |
| 改名/移动 | 同 FRN、同大小，`(dir_id, name)` 不同 | `RENAME` |
| 原地改写 | 全同、`mtime` 不同 | `OVERWRITE`（计数不落库） |

清单侧只需三条语句：`INSERT` 新增、`INSERT OR REPLACE` 变更、`DELETE ... WHERE NOT EXISTS` 消失。合并末尾校验 `inventory 行数 == 暂存行数`，不等就写 WARNING——这条不变量一旦破了，之后每个窗口都在错的基础上做差分。

**3. 目录行进入清单，`mft_index` 下线。**
扫描现在为每个目录产出一行（`is_dir=1`、大小为 0）。它不产生任何事件（按字节记账的 P1 不该为新建文件夹记一条），但它让「事件的父目录 FRN」直接在清单里解析到真实路径。于是那份 1.2M 行、159 MB 的 MFT 全量副本没有存在意义了：`mft_index_enabled` 默认 `false`，`prune_usn` 会把它释放掉。

这不是"省空间的妥协"，实测解析质量反而更好（见下表）：`parent` 从依赖 MFT 父链改成查清单里的目录行，命中更准。

**顺带：`dir_path` 的 UNIQUE 索引被删掉。** 实测每个目录占 346 B，其中 UNIQUE 索引一项就 38 MB——它是每条长路径的**第二份拷贝**，而暂存时本来就把整表读进字典，从不做 path→id 查询。新目录直接 `INSERT` 取 `lastrowid`。旧库由 `_migrate_dir_path()` 原地重建（保留 id，清单引用不断）。

#### 实测：体积（同一天、同一份历史）

| 阶段 | 数据目录 |
|---|---|
| L2 结束时 | 631.53 MiB（db 500.95 + wal 19.69 + 归档 99.73 + 日志 11.16） |
| v1.3 迁移 + 清 41,056 条孤儿目录 + VACUUM | 579.54 MiB（db 468.65） |
| 释放 `mft_index`（1,200,146 行）+ VACUUM | 420.23 MiB（db 309.34） |
| 首次全盘扫描重建清单（1,230,718 行） | 621.01 MiB（db 532.66） |
| `dir_path` 去索引 + VACUUM | 487.17 MiB（db 399.02） |
| 归档 4→2 份 | **447.24 MiB**（db 399.02 + 归档 37.98 + 日志 11.16） |
| 跑完一个完整窗口（扫描 + 合并） | 554.47 MiB（db 493.20，其中 **81.79 MiB 是暂存区刚释放的空闲页**） |

最后一行是暂存区的固有代价，值得说清：每轮扫描都要往 `scan_stage` 写一遍完整清单（1.23M 行 ≈ 85 MB），合并后删掉，于是主库里留下一批待回收的空闲页。`reclaim` 每轮归还 2 万页（≈82 MB），一到两轮就归零——它是**有界的、自愈的**，不是增长。界面上 `reclaimable` 那行就是它。

清单本身：`file_state` 67.37 MiB / 1,230,718 行（含 199,077 个目录行）= **57 B/行**；`dir_path` 19.07 MiB / 173,336 个目录 = 115 B/目录。合计 **86.4 MiB**，替代了 L2 的两份共 205 MiB。

#### 实测：耗时（1,230,718 行）

| 动作 | 耗时 |
|---|---|
| 扫描写入暂存区（顺序插入） | **1.74 s** |
| 合并：无变化窗口 | **7.7 s** |
| 合并：2,000 改写 + 300 删除（2,300 事件） | **9.4 s** |
| 基线采用（真实全盘扫描结束） | 16.8 s |
| **生产实况**：`#114 → #115`，1,170 事件 / +609 −458 ~674 | **27.2 s** |
| 全盘扫描本体（103 万文件，含每目录一次 `os.stat`） | 约 1,340 s（22 分钟） |

生产实况比基准慢（27.2 s vs 9.4 s），原因是真实的暂存区是边扫边写的（页分布零散，缓存命中差），而基准里是从同一张表顺序拷贝出来的。即便按 27 s 算，它也只占 30 分钟窗口的 1.5%；瓶颈始终是那 22 分钟的目录遍历。

同一窗口的记账仍然自洽：`events=1,170  attributed=+94.97 MiB  used_delta=+78.27 MiB  residual=−16.70 MiB`——归因略大于实测增长，差值如实进 residual，没有被摊平。

#### 实测：路径解析质量（同一时段的 USN 轮询）

| 时刻 | snapshot | parent | mft | unresolved |
|---|---|---|---|---|
| L3 前（`mft_index` 在位，清单为 L2 双份） | 0 | 5,486 | 532 | 806（11.5%） |
| 清单重建前（mft 已删、清单为空） | 0 | 0 | 0 | 3,931（48%） |
| 清单重建后（mft 仍然缺） | 963 | 2,687 | 0 | **176（2.3%）** |

中间那行是这个改动唯一的代价，也是它唯一的窗口：删掉 mft 到清单重建之间（本次约 22 分钟）解析会退化。生产上不会出现——升级时会先跑一次 `snapshot` 建基线，再切 `mft_index_enabled`。

#### 上线后立刻暴露的一个耦合：扫描必须跑完

L3 上线一小时后，`#116` 在 1500 秒预算内只扫到 90.6 万文件（1.03M 的 88%）就超时，于是 108 万行暂存数据被整份丢弃、清单停在 `#115`——**一个窗口的差分直接没了**。

这是 L3 带来的新耦合：以前 partial 扫描至少还能留下一份"能用于路径解析的清单"，现在 partial 一律不落库（否则半个卷会成为基线）。所以预算必须**大于真实扫描时长**，否则系统会安静地停止更新。两处修正：

- `max_scan_seconds` 1500 → **2400**（本机全盘 22–27 分钟，留 45% 余量）。默认值也从 900 提到 2400——100 万文件的 C 盘上 900 秒只是必然超时。
- 超时日志不再只有 `PARTIAL(timeout)`，而是明确说清后果与改法：
  `NOTE the scan budget is too short for this volume: only 906,196 files in 1500s and nothing was stored (a partial pass cannot be a baseline). Set max_scan_seconds to at least 2250s (current 1500s) ...`

同一个耦合还有第二面：清单陈旧时（没有一次成功的全量扫描），未进清单的新文件会解析成 `unresolved`——因为 `mft_index` 已经不在了。上线初期那 35 分钟里 `unresolved` 从 2.3% 涨到 16%，清单更新后立刻回落。结论不是"把 mft_index 加回来"，而是**扫描预算必须够**，这是前置条件。

#### 顺带修掉的缺陷

- **`log()` 会因为 stdout 消失而杀死采集进程。** `print(..., flush=True)` 在管道被关闭（`... | head`）或控制台被分离时抛 `OSError: [Errno 22]`，而这是未捕获异常——守护进程直接退出。现在吞掉该异常，只丢一行日志。
- **扫描期间的报告可靠性。** 清单在合并前始终是"上一份完整状态"，报告/界面不会读到半份清单（L2 时靠"取最新已完成快照"规避，现在结构上就不可能）。

#### 边界：为什么停在 447 MB，而不是 250 MB

剩下的 400 MB 里没有"浪费"，只有"保留多久"：

| 对象 | 占用 | 削减方式 | 代价 |
|---|---|---|---|
| `usn_event` + 3 索引 | 185 MiB | `usn_retention_days` 2 → 1 | 今天/昨天的 USN 明细对比缺一天（−92） |
| `change_event` + 4 索引 | 99 MiB | `retention_days` 7 → 3 | 界面 7 天趋势缩到 3 天（−55） |
| 清单（file_state + dir_path） | 86 MiB | 无（就是当前状态本身） | 再压要牺牲路径可读性 |
| `frn_path_map` + 索引 | 30 MiB | 停用（历史路径） | "这个 FRN 以前叫什么"失去依据（−30） |
| 归档 | 38 MiB | `archive_retention_days` 2 → 1 | 少一个时间点副本（−20） |

四项全开约 **250 MB**。我不把这些设成默认值：每一条都在减少这个工具"能回答的问题"，而 447 MB 对一个 1M 文件的磁盘监控来说是合理的。M3 设置页应该给三档（极简 200 / 标准 450 / 完整 800），由用户按 C 盘余量选。

其它已知边界：

- **升级顺序仍是硬约束**：先停守护进程 → 再换代码。本次又验证了一次（旧的 `p1.2` 进程写 `p1.3` 表会失败）。
- **预算判定用存活数据**（`dbstat` 口径），不是文件大小；否则一个刚删了百万行的库会被判"超标"然后去删有用的清单。
- `scan_stage` 是真实表而非临时表，因此崩溃后残留的行需要 `prune` 清理；`prune` 会删掉所有非最新快照的暂存行（id 只增，旧行永远等不到合并）。
- 目录行只影响路径解析，不参与字节统计：`total_allocated` 与 `used_delta` 的口径没有变化。
- **`max_scan_seconds` 必须大于真实扫描时长**：partial 扫描在 L3 下不落库，超时等于这个窗口没有数据。换机器/换磁盘后第一件事是看 `run.log` 里单次扫描的 `elapsed`，把它乘以 1.5 设成预算。

### 10.8 分发物补齐：托盘第一次被打包（2026-11-24）

M1-4 的界面此前只存在于源码态（`python gui/tray.py`）。`CDriveSentinel.spec` 里只有一个 `EXE`，所以 `dist/CDriveSentinel/` 只有 `CDriveSentinel.exe`——**装完是一个看不见的后台进程加一个不存在的托盘图标**。这是"装完不能用"，优先级高于 M1-6/M1-7，所以先做。

#### 两个入口，一份运行时

| 程序 | 形态 | 理由 |
|---|---|---|
| `CDriveSentinel.exe` | `console=True` | `status` / `report` / `selftest` 必须能打印 |
| `SentinelTray.exe` | `console=False` | 登录时弹黑框不可接受 |

不合成一个 exe 的原因：`console=False` 会让 CLI 失去输出，`console=True` 会让托盘每次登录闪一个黑窗口。两个 Analysis 交给同一个 `COLLECT`，所以 PySide6 与 Python 运行时**只有一份**——实测目录 100 MiB，其中 PySide6 74 MiB，没有翻倍。

采集进程那份 Analysis 仍然 `excludes=["PySide6"]`：一个提权守护进程不该加载 GUI 工具包。

#### 落点规则对齐（防的是"界面和后台对不上"）

旧 `find_db()` 的顺序是「exe 旁边有 `data/sentinel.db` 就用它」。装到 Program Files 后这个分支通常不命中，但**只要一次手动测试在程序目录留下 `data/`，界面就会一直读那份已经停止写入的历史库**，而守护进程写的是 ProgramData。表现是"屏幕上的数字不动了"，极难定位。

`find_config()` 同理，且更危险：`dist/` 里本来就带 `config/`，于是界面读程序旁那份、采集进程读 ProgramData 那份，**两边用不同的阈值和保留策略**。

现在两边共用同一套四步规则（`SENTINEL_HOME` → `portable.txt` → ProgramData → 项目目录），并逐种情形对拍过：

| 情形 | 采集进程 `data_dir()` | 界面 `home()` | 一致 |
|---|---|---|---|
| 源码运行 | `D:/.../CDriveSentinel` | 同 | ✅ |
| 源码 + `SENTINEL_HOME` | 该目录 | 同 | ✅ |
| 安装版（frozen） | `%PROGRAMDATA%\CDriveSentinel` | 同 | ✅ |
| 安装版 + `portable.txt` | 程序目录 | 同 | ✅ |

界面不再"探测"程序旁的 `data/`：安装版只认约定位置。

顺带修掉 `app_root()` 的冻结态错误。`Path(__file__).parent.parent` 在源码态正确，但打包后 `__file__` 指向 `_internal/`（甚至 `base_library.zip` 内部），会算错一层。现在 frozen 时用 `sys.executable` 的父目录。

#### 顺带修掉两个"第一次打开就吓人"的缺陷

打包后的托盘第一次渲染出来是这样（数据库还不存在，等价于全新安装）：

| 缺陷 | 原因 | 现在 |
|---|---|---|
| 可用空间 **红色 `0 B` + "低于告警线"** | `load()` 在 `db.is_file()` 为假时直接 return，容量停在全零默认值 | 早退前先向 Windows 取真实容量（15.3 GB），红色标签消失 |
| 右上角 **红色"读取失败"** | 库不存在被当成错误 | 新增 `pending` 状态：显示灰色"等待数据"，副标题说明"尚未开始采集 · 守护进程完成首次扫描后会自动显示数据" |
| 今日净增 `0 B` | 同上 | 显示 `--` + "首次扫描完成后开始统计" |

注意这是**第二条路径**：上一轮修的是"库存在但还没有心跳"，这次是"库根本不存在"。首启的两种半成品状态都要照顾。

#### 窗口程序没有 stdout 会崩

PyInstaller 的 `console=False` 启动时把 `sys.stdout` / `sys.stderr` 设成 `None`。任何 `print` —— 包括异常处理里的 —— 都会抛 `AttributeError: 'NoneType' object has no attribute 'write'`，于是一次本该被记录的崩溃变成进程静默消失。

现在入口处先 `_ensure_streams()`：把两个流重定向到 `<数据目录>\logs\tray.log`（失败则 devnull），并安装 `sys.excepthook` 把未捕获异常写进同一份日志。双击启动的托盘从此有线索可查。

#### 构建脚本不再递归删除

`build.py` 原先 `shutil.rmtree(DIST)`。这有两个问题：不可恢复（`DIST` 一旦被改成别的路径就是灾难），以及约 100 个文件的递归删除会触发批量删除保护，导致脚本"第二次运行必失败"。

改成 `_retire()`：把旧产物改名为 `CDriveSentinel.prev-<时间戳>` / `build.prev-<时间戳>`，构建结束打印路径，由人决定何时删。

#### 实测

```
[3/5] verifying executables ...
      CDriveSentinel.exe     ok
      SentinelTray.exe       ok
      folder size           100 MiB
      PySide6 (one copy)    74 MiB

冻结态落点：data_root = C:/ProgramData/CDriveSentinel   ✔
空库首启  ：等待数据 / 15.3 GB 真实容量 / 今日净增 --   ✔
真实库    ：+769 MB、Top 10、4 条告警、7 天趋势图        ✔
命令行    ：status / runtime 正常                        ✔
selftest  ：71/71
```

#### 边界

- 安装器（M1-7）尚未存在，`%PROGRAMDATA%\CDriveSentinel` 目录与其 ACL 还没人创建。当前非提权运行 `SentinelTray.exe` 且从未安装时，会停在"等待数据"——正确但需要安装器收尾。
- `install_task.ps1` 目前只注册采集进程的计划任务，**托盘的登录自启是 M1-7 的事**。
- 托盘日志没有轮转：`logs/tray.log` 只在崩溃时增长，量级可忽略；若要长期运行再加大小上限。
- 旧产物改名而非删除，`dist/` 会随构建次数堆积（每次约 100 MiB + `build/` 工作目录）。`build.py` 末尾会列出这些路径。

### 10.9 M1-6 首启向导 + 扫描预算自标定已交付（2026-11-25）

M1-6 的验收标准是"三项选择后即可全自动工作"。所以它不是一个多步向导，而是**一页三个问题**：监控哪里、占多少 C 盘空间、要不要开机自启。

#### 分成三层，只有一层碰 GUI

| 层 | 文件 | 为什么放这里 |
|---|---|---|
| 配置语义 | `p1_config.PROFILES` / `apply_profile()` / `save_config()` | 可被 selftest 覆盖，无 GUI 依赖 |
| 命令 | `main.py setup`（交互 / `--profile --area --yes`） | 安装器将来可以静默调用 |
| 界面 | `gui/setup_wizard.py` + `gui/setup_client.py` | 只做布局，配置由子进程写 |

界面**不自己写 config.json**。它调 `CDriveSentinel.exe setup --profile X --area Y --yes`，和托盘"不 import src/"、"不写数据库"是同一条边界。否则档位表、原子写、scope 规则会在两个地方各有一份，然后慢慢不一致。

#### 三个档位（预算与保留期绑定）

| 档位 | 预算 | USN 明细 | 变化账本 | 归档 | 实测稳态占用 |
|---|---|---|---|---|---|
| 极简 | 320 MB | 1 天 | 3 天 | 1 天 | ~253 MB |
| 标准 | 550 MB | 2 天 | 7 天 | 2 天 | ~447 MB |
| 完整 | 800 MB | 3 天 | 14 天 | 3 天 | ~647 MB |

预算是"上限"不是"目标"，所以每一档都留了余量——否则档位会常年贴着自己的上限、不停触发降级。`apply_profile()` 只改档位相关的 7 个键，`scope`、`exclude_paths`、告警线、调度间隔一律不动（用例 `M1-6 profile … keeps user settings` 钉住这条）。

极简档顺带加了 `frn_path_map_enabled`（默认 `true`）。那张表是"这个文件以前叫什么"的历史，约 30 MB；关掉后路径解析少一个来源，两次扫描之间生灭的文件会更容易落到 `unresolved`——这是极简档明确的交换。

#### 扫描预算：放在 daemon 里，不放在向导里

`#116` 那次事故（1500 秒预算只扫到 88%，108 万行暂存全丢、清单停在上一份）的根因是**预算小于真实扫描时长**，而这个数字在扫描跑完之前谁都不知道。向导里做"标定扫描"意味着让用户等 25 分钟，不可接受。

所以改成**每轮快照后自检**（`calibrate_scan_budget`）：

- 取最近 5 次**完整**扫描的 `elapsed_seconds` 中位数；
- 中位数 × 1.5 向上取整到 5 分钟；若大于当前预算就抬上去；
- 只写 `max_scan_seconds` 一个键（读磁盘最新配置再写，手改的其他键不受影响）；
- 只升不降——一次偶发变慢（杀毒软件、另一个备份在跑）不该缩小预算。

`partial` 行被明确排除：超时的扫描什么都没测到，用它的耗时当依据会让预算越调越大。用例 `M1-6 partial scans do not inflate the budget` 就是钉这一条。

#### 实测

```
M1-6 profile full/minimal/standard applies and keeps user settings   PASS
M1-6 an unknown profile is rejected                                 PASS
M1-6 config save is atomic and drops runtime keys                   PASS
M1-6 budget is raised above the median real scan                    PASS  (2050s 中位 → 3300s)
M1-6 budget calibration is idempotent                               PASS
M1-6 partial scans do not inflate the budget                        PASS
selftest: 79/79

端到端（源码态）：is_configured False → run_setup("full","full") → True
                  config: profile=full budget=800 usn=3 ledger=14 scope=[]
                  run_setup("nope") → False，错误来自 argparse 的 choices
端到端（打包态）：CDriveSentinel.exe setup --profile minimal --area full --yes
                  → 写入探针目录，setup_complete=True
```

#### 界面上踩过的坑（记录下来免得再踩）

`QRadioButton::indicator` 用 QSS 自绘时，**只要同时给 `QCheckBox::indicator` 写样式**，Qt 会把两种按钮的指示器都交给统一自绘路径，radio 于是被画成**圆角方框**——看上去和复选框一样，用户分不清。试过 `border-radius` 写进每个状态、写过 `10px`/`12px` 都没有用。

最后的解法是给向导单独 `setStyle(QStyleFactory.create("Fusion"))`，让 Fusion 自己画圆，QSS 只负责颜色和间距。原生 Windows 风格在套了 QSS 之后反而把指示器画成几乎不可见的白点。

#### 边界

- **开机自启仍依赖 `install_task.ps1`**。向导勾选后会 `ShellExecuteW runas` 提权运行它；用户拒绝 UAC 时向导如实提示"后台任务未注册，之后可手动运行"。Inno Setup（M1-7）落地后这一步会变成安装器的一部分，向导不再需要提权。
- 向导只写配置，**不做首次扫描**（只写一条 `request_scan` 请求）。若守护进程没在跑，这次请求会在它下次启动时被消费——`runtime.last_scan_request` 是持久化的。
- 源码态若 `SENTINEL_HOME` 指向被监控的卷，`data_dir()` 会把它自动重定位到另一块盘（实测会出现在 `C:/CDriveSentinelData`）。这是 M1-1 的既有行为，不是向导引入的，但在"数据放哪"这件事上确实容易让人困惑，值得在 M3 设置页里显式给出实际落点。
- 已经配好的机器不该被向导打断：`config.json` 里 `setup_complete=true` 即为已配置。本机现有配置被标记为 `setup_profile: "custom"`（手工调过，不属于任何预设），向导不会自动弹出，需要时可用 `--wizard` 手动打开。

### 10.10 M1-7 安装器已交付（2026-11-25）

M1-7 的验收主线是"装完重启即自动采集"。落地时发现一个此前被推迟的决定必须先定：**数据放哪**。工具的存在意义是查清 C: 被什么占掉，若它把自己的 300–600 MB 数据写进 C:，就成了它要解释的那个答案的一部分。所以采用 option B —— 数据目录指针。

#### 数据目录指针（option B）

- 安装器询问"数据保存位置"，默认选第一个非 C: 盘上的 `CDriveSentinelData`（`make_installer.py`/`install.ps1` 与 `.iss` 的 `DefaultDataDir()` 用同一规则）。
- 选择写入 `%PROGRAMDATA%\CDriveSentinel\home.txt`（一行绝对路径）。采集进程 `p1_config.data_dir()`、托盘 `gui/datafeed.home()` 用同一套规则解析：`SENTINEL_HOME` → **指针** → `portable.txt` → ProgramData → 项目目录。托盘**不 import `src/`**，把指针读取逻辑镜像了一份，注释里写明两侧必须同步。
- 指针存在但不可用（空 / 相对路径 / 目录不存在）是**错误**，明确报出而不是回落到默认位置——回落会让界面读到一份空库然后重新开始。
- `CDriveSentinel.exe set-home --to D:\...` / `--clear` 供装机后改盘；只写指针、建目录，**不搬数据**（搬活库是丢数据的经典方式），命令会提示用户自己拷 `data\`/`config\` 再重启任务。
- 指针指向的目录仍在监控卷上时，照旧整目录排除扫描；`paths` / `layout_info()` 会打印指针路径与错误。

#### 两个计划任务（按验收标准补齐）

F23 与 M1-7 验收都写"注册两个计划任务"，此前只做了采集任务，本轮补齐第二个：

| 任务 | 安装脚本 | 权限 | 触发 | 作用 |
|---|---|---|---|---|
| `CDriveSentinel` | `install_task.ps1 -Start` | Highest | AtLogOn | 提权采集 + USN 深读，装完立即启动首扫 |
| `CDriveSentinel-OpenReport` | `install_popup_task.ps1` | 普通（不 Highest） | AtLogOn +15s | 每天在用户会话弹昨天日报（提权进程开浏览器会被 Chrome/Edge 拒绝） |

- 两条路径（脚本安装器 `install.ps1`、Inno `CDriveSentinel.iss`）都注册这两个任务、都在卸载时清理这两个任务（`UninstallRun` 用两个 `RunOnceId`：`RemoveCollectorTask` / `RemovePopupTask`）——避免"卸载后残留一个每天弹窗的孤儿任务"。
- `install.ps1` 的 `Stop-Running` 同时停两个任务；`build.py` 现在把两个任务安装脚本都打进 dist。
- 顺带修掉 `.iss` 里 `install_task.ps1` 被列两次（一次来自 dist、一次来自源码树）的重复 Source 行——编译出的 Setup.exe 只应依赖 dist 产物。

#### 权限、自启与卸载

- `data\runtime`、`data\logs` 给 `BUILTIN\Users` 修改权限（`install.ps1` 用 SID `*S-1-5-32-545` 避免本地化名问题；`.iss` 用 `[Dirs] Permissions: users-modify`）——托盘不提权也能写"立即扫描/暂停"便条，否则按钮静默失效。
- 托盘自启走 HKLM `Run`（per-machine，每个账号一个图标；托盘自身有单实例互斥）。
- 卸载默认**保留**历史数据，删除数据需 `-PurgeData` 或安装器里确认；升级/重跑安装保留现有 `sentinel.db` 与其配置。

#### 实测

```
selftest（源码态）           84/84   （新增 6 项 M1-7 指针用例：无指针/有效/相对拒绝/不存在/空/报错）
PowerShell 三脚本语法解析     3/3 OK
python build.py              100 MiB，两个 exe + 两个任务脚本 + install.ps1 + setup.cmd 齐全
打包 exe paths                frozen / ProgramData / home_pointer 行正确；排除清单含 programdata 子树
打包 exe selftest            83/84  （唯一失败项见下方边界，6 项 M1-7 用例全过）
README.md / 使用说明.txt     已同步一键安装、两个任务、set-home 入口
```

#### 边界

- **安装包已在本机编译成功（2026-11-25）**：`winget install --id JRSoftware.InnoSetup -e --silent` 装了 Inno Setup 6.7.3（用户目录 `%LOCALAPPDATA%\Programs\Inno Setup 6\`，未动系统 PATH/服务）；`make_installer.py` 经注册表找到 ISCC，`CDriveSentinel.iss` 零错误编译通过。产物 `dist\CDriveSentinel-0.2.0-Setup.exe`，**27.5 MiB**，SHA-256 `9e1f7fdc57e32f03e4333fe7b568d37b9b1cc87463227c6c980e17c91a7a0287`。注意：这仍是**非商业用途**编译（ISCC 输出 "Non-commercial use only"），商用发布需购买 Inno Setup 许可证。
- **干净虚拟机端到端未实测**：本机只编译了安装包，没有真的运行 `Setup.exe` 安装（那会注册计划任务、写 ProgramData、改 HKLM Run——属于本机系统变更，未擅自做）。"装完重启即自动采集"这条验收仍需在一台干净 Win10/11 上跑一遍安装包确认。
- **打包态 selftest 83/84**：唯一失败项是既有的环境假设——沙箱取自 `paths["root"]/data/selftest`，打包态 root 即 `%PROGRAMDATA%`（在监控盘 C: 上），触发"沙箱须在监控盘外"的断言。这是 selftest 自身的路径约定问题，与 M1-7 改动无关；源码态（root 在 D:）84/84 全绿。修法应让 selftest 在打包态用可重定向的临时根目录，留待后续。
- **两个任务的 principal 归属**：`install_popup_task.ps1` 按"安装时的当前用户"注册（`USERDOMAIN\USERNAME`）。per-machine 安装若由管理员账号执行、日常登录是另一账号，日报弹窗任务会挂在管理员账号下而不在日常会话触发。多账号机器需在目标账号下重跑该脚本，或改为 per-user 触发——留给 M3 设置页处理。
- 脚本安装器 `install.ps1` 与 Inno `.iss` 是两套并行实现，行为对齐但需各自维护；长期应统一到一套生成的安装定义。
- M1-8（CI 产出安装包 + SHA-256）尚未开始，依赖 ISCC 先在 CI 环境可用。

### 10.11 面板「刷新」可解释化（2026-11-26）

M1-4 的面板底部有四个按钮，其中「刷新」此前只是 `Dashboard.refresh()`：后台重跑一次只读查询再 `_render()`。真实库 318 MB / 124.8 万文件，`datafeed.load()` 实测 **0.05–0.07 s**，所以点下去几乎必然"页面没任何变化"，用户无从判断这一下做了什么（15 s 自动刷新走同一路径，同样无提示）。本轮把它改成"有解释、有结果"的交互，自动刷新保持安静。

#### 改法

| 位置 | 改动 |
|---|---|
| `gui/tray.py` 按钮 | `clicked` 从 `self.refresh` 改为 `self._manual_refresh`；补 tooltip「重新读取本机数据库（只读，不触发扫描）」 |
| `_manual_refresh()` | 记下点击时刻与点击前的 `Dashboard`，再走原有 `refresh()`；若计时器的读取正在跑（`_loading`），不排队第二次查询，由那次 `_on_loaded` 一并汇报 |
| `_finish_manual_refresh()` | 读取完成后算真实耗时，并对比点击前后的数据 |
| `_build_refresh_dialog()` | QMessageBox：标题「刷新做了什么」，正文说明"只重读本机数据库、不触发扫描、不写文件"，随后逐行给结果（读取耗时 / 心跳与最近完成快照 / 今日净增与事件数 / 与上次刷新相比的变化 / 面板每 15 s 自动刷新） |
| 「不再提示」 | QSettings `ui/refresh_hint_hidden`（HKCU\Software\CDriveSentinel\Tray）；勾选后不再弹窗，改为底栏 `_note_flash` 一行「已刷新 HH:MM:SS · 数据 HH:MM:SS · 读取 x.xx 秒」 |
| 失败路径 | `_on_failed` 里若正在手动刷新，底栏闪现「刷新失败：…」，不再静默 |

#### 顺带修掉的两个真问题

1. **深色模式下弹窗深底深字**：QSS 只给 `QWidget` 设了字色（`#1D2129`），没有任何对话框底色规则。Windows 11 深色模式下 Qt 6 给 QDialog/QMessageBox 的底色是 `#1E1E1E`，实测渲染出来就是深底深字、几乎不可读——受影响的不止新弹窗，既有的「暂无日报」提示也一样。QSS 补 `QDialog, QMessageBox { background: #FFFFFF }`、`QMessageBox QLabel` 与 `QCheckBox` 字色，并给无 objectName 的 `QPushButton` 补一套浅色样式（`#primary`/`#ghost` 的 ID 选择器优先级更高，不受影响）。
2. **PySide6 的 `setCheckBox()` 不接管 Python 对象所有权**：`remember = QCheckBox(...)` 是 `_build_refresh_dialog` 的局部变量，函数返回后包装对象被回收，`QMessageBox.checkBox()` 随即返回悬空指针——实测直接 **access violation（0xC0000005）**，同一路径还表现为"复选框根本不显示"。修法是让对话框自己持有一份引用（`box._remember = remember`）。这条只在"先 build 再 exec"的写法下暴露；原先 exec 时局部变量还活着，所以是隐性坑。

#### 实测

```
offscreen 功能测试（.tmp-ui/refresh_test.py，未入库）
  dialogs        2   （手动一次 + "计时器读取中又点一次" 竞态一次）
  button         '知道了'
  checkbox->     True （勾选后写入 setting，第二次点击只闪底栏）
  note_only_2nd  True
  race_loading   True （竞态只弹一次、不排队第二次查询）
  fs_unchanged   True （sentinel.db 与 reports/ 的 mtime_ns 前后一致 → 弹窗不写任何文件）
  lines_*        四个分支文案：首次 / 无变化 / 有变化 / 数据库尚未创建
真实字体渲染（WA_DontShowOnScreen，不打扰屏幕）  502×237（150% DPI 下 753×356）
tray --smoke（指向 D:\CDriveSentinelData 活库）   tray_available=True，dashboard=loaded，state=scanning
```

#### 边界

- 本机活库读取约 0.06 s，弹窗几乎瞬时出现；库更大或机械盘上读取变慢时，弹窗会晚一点出现，但"本次读取 x.xx 秒"会把这件事说清楚。
- 弹窗只解释「刷新」，「打开日报 / 暂停 / 立即扫描」仍用底栏提示；「打开日报」的当日口径在 10.12 单独处理。
- QSettings 键在 `HKCU\Software\CDriveSentinel\Tray`，卸载不清理（用户级偏好，重装后保留"不再提示"是合理的）。

### 10.12 面板「打开日报」改为打开当日（2026-11-26）

「打开日报」原先打开 `reports\daily\` 里文件名最大的 HTML，看似"最新"，实际几乎总是**昨天**：守护进程只在每天 8 点后为昨天生成整日版，从不为当天写文件。CLI 的登录弹窗本来就要"看前一天 24 小时"，这个口径没错；但把它搬进面板后，面板上半屏是今日实时数字、按钮却打开昨天的报告，读起来像 bug。

#### 方案 B（已实现）：采集进程滚动生成当日版

| 位置 | 改动 |
|---|---|
| `src/p1_report.py` | 新增 `rolling_ready()`（当日必须已有完成的差分窗口，否则不出）与 `write_rolling_report()`：`since=今天 00:00`、`until=现在`，写**同名**文件（`2026-11-26.html/.txt`），"当日无窗口返回 None" |
| `src/p1_report.py` 渲染 | `write_report(..., rolling=True)` 由滚动版入口显式传入（**不**由 `until` 非空推断，手写 `report --since/--until` 查历史区间不加横幅）；txt 在区间行下加「版本：滚动版（数据截至生成时刻…次日整日版生成后覆盖本文件）」，HTML 加同义 `.note`，避免与次日整日版混淆 |
| `src/main.py` | 快照成功（`scanned`）后调 `write_rolling_report()`；另外每次 daemon 启动做**一次**补偿（`rolling_seeded`），中途重启不会让面板继续指昨天。都包在 try/except 里，失败只记日志 |
| `src/p1_config.py` | 新增 `rolling_report_enabled`（默认 true） |
| `gui/tray.py` | 当日文件存在时按钮变成「打开今日日报」，tooltip 写明文件名；只有旧日报时按钮仍叫「打开日报」，tooltip 说明"今天还没有日报，将打开最近一份 X.html"；打开后在底栏闪「已打开今日日报 2026-11-26（11:20 更新）」或「已打开 2026-11-25 的日报（今天还没有生成…）」 |
| CLI / 登录弹窗 | **完全不动**：`cmd_open_report` 仍固定打开昨天 |

时序上自洽：今天每次快照覆盖 `今天.html`（滚动版）；次日 8 点后整日版覆盖**同一文件**，于是它自然变成"昨天的完整版"；托盘只会取文件名最大的那份，不需要额外判断。

#### 顺带的性能修正

`build_report_data` 的"当前占用 Top"原先 `file_state JOIN dir_path ... GROUP BY f.dir_id`，SQLite 要逐行回查目录路径：**7.5 s**（124.5 万行 × 17.4 万目录）。改成先 `GROUP BY f.dir_id` 再在 Python 里映射 id→path：**1.7–2.7 s**，与旧查询逐项一致（236 个 top_dir 键、总量 146,396,222,492 B 完全相同，Top 20 一一相等）。滚动版单次总耗时实测 **1.8 s**（`.tmp-ui/rolling_test.py`）。

#### 实测

```
rolling_test.py（活库只读 + 临时输出目录）
  current_top 与旧 JOIN 逐项相同        20 vs 20 rows
  breakdown                            build 1.73s + text/html 渲染 <0.01s（HTML 175 KB）
  write_rolling_report                 1.75s → 2026-11-26.html / .txt
  文本横幅                              统计区间 00:00:00 ~ 11:08:35 + 「版本：滚动版…」
  整日版（明天 8 点写的那份）            无「版本：滚动版」行，区间 00:00:00 ~ 23:59:59
  旧库（最近 diff_run 11-25）            rolling_ready=False → 返回 None，不生成今日文件
daemon 集成（SENTINEL_HOME=.tmp-ui/home-roll，活库副本）
  rolling report updated for 2026-11-26 (html=2026-11-26.html, 1.8s)
  reports\index.html 同步重写（门户含今日行）
ui_report_test.py（offscreen）
  当日文件存在 → 按钮「打开今日日报」，tooltip 带 2026-11-26.html
  只有昨天文件 → 按钮「打开日报」，tooltip「今天还没有日报，将打开最近一份 2026-11-25.html」
```

打包态（0.3.5，`dist\CDriveSentinel-0.3.5-Setup.exe`，11:18 构建）：

```
CDriveSentinel.exe selftest（frozen）  90/90（含 5 项滚动版 + 1 项「历史区间不加横幅」）
.tmp-ui/verify_frozen_035.py          daemon/cli/tray 三份归档都含新代码；tray 仍不含 p1_report
uia_report_test.ps1 -Expect today     home 里有今日文件 → 按钮「打开今日日报」，点击后底栏「已打开今日日报 2026-11-26（11:09 更新）」
uia_report_test.ps1 -Expect fallback  活库（只有 11-25）→ 按钮「打开日报」，底栏「已打开 2026-11-25 的日报（今天还没有生成；09:15 更新）」
```

注：Qt 不把 `setToolTip()` 文本暴露成 UIA 的 HelpText（读回来是空串），tooltip 文案的验证来自 offscreen 的 `ui_report_test.py`；UIA 只用来证明打包版按钮文字与点击行为。

#### 边界

- 当天还没有任何完成的差分窗口时（刚装好、或刚过零点第一轮扫描前），不出滚动版；按钮保持「打开日报」并打开最近一份旧日报，底栏说明"今天还没有生成"。
- 滚动版是**当日累计视图**，不是"增量事件清单"：跨零点的差分窗口按 `diff_run.ts_local`（窗口结束时刻）归属到结束日，因此今日报告里会出现昨晚开的那个窗口，这是原有口径，未改。
- 如果 daemon 被暂停，滚动版停在最后一次快照，底栏的"已打开…（HH:MM 更新）"会显示这个时间。
