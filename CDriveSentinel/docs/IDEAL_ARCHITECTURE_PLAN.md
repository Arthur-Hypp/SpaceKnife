# CDriveSentinel 理想架构方案：采集与存储优化（P4 立项建议）

| 项目 | 内容 |
|---|---|
| 文档版本 | v1.0 |
| 日期 | 2026-11-17 |
| 状态 | **方案建议，未实施**；本文不修改任何代码 / 配置 / 数据库 |
| 定位 | 回答「想真正压占用，该优化采集架构而不是纠结窗口样式」——把三个方向整合成可落地的终态方案 |
| 基线 | `docs/DESIGN.md`、`P2_HOTFIX_PLAN.md`、`P2_2_FIX_PLAN.md`、`P2_3_FIX_PLAN.md` + `src/` 现状逐行核对 |
| 硬约束 | 只读采集；绝不写 C 盘；账本语义（FRN 对齐 / AllocationSize / 未归因残差）不得改变；零第三方依赖 |

---

## 0. 缘起与结论先行

之前对话的判断是：

> 想真正压占用，得优化采集架构——比如设计文档里那个还没做的「P1 改 MFT 直读」（现在逐文件扫描太重）、DB 分区和更激进的 retention，而不是纠结窗口样式。

对这句话的三个方向逐项核对现状后，结论是——**三项的价值都成立，但名称与成熟度各不相同，不能当成"现成清单"直接抄**：

| 方向 | 现有文档状态 | 真实差距 | 本文对应章节 |
|---|---|---|---|
| P1 改 MFT 直读 | 有技术路线、明确「工作量大、独立立项」，但**未实现** | P2 已验证 ENUM 能秒级枚举 MFT，但**记录不带大小**，无法直接替 P1 快照；这是要破解的核心 | §3 |
| DB 分区 | **没有任何方案**；只有 `auto_vacuum` + 页回收 | "压缩占用"真实大头是 DB 收敛到约 915 MiB，而不是窗口样式；分区/分表是空白 | §4 |
| 更激进的 retention | 已从 30→7 天并落地（`prune_usn` + 页回收） | 可进一步：把共用键拆成各类独立保留期；但 7 天已是用户拍板，激进有回溯代价 | §5 |

一句话总纲：**先用 MFT 直读把「采集成本」从每 30 分钟 5–9 分钟全盘 `os.scandir` 降到秒级，再用分区/分表 + 独立保留期把「存储成本」锁死在稳态上限。** 窗口样式（托盘 / pythonw / 日报门户）属于显示层，不在压占用主线上（详见 §1.3）。

---

## 1. 现状基线（实测，非估算）

### 1.1 占用与采集成本

| 指标 | 实测值 | 来源 |
|---|---|---|
| 单次全盘扫描耗时 | 297 s / 284 s / 538 s（约 5–9 分钟） | `DESIGN.md` P1 实测 |
| 文件规模 | 约 95 万文件 + 8.2 万硬链接去重 | `DESIGN.md` P1 |
| 采样错位 | 扫描期间卷用量另变 -221.81 MiB ~ -901.78 MiB | 残差账本 |
| 未归因残差（当前） | `[+810725376, -945582080]` 字节 | `diff_run`，多次 verify 未变 |
| DB 稳态估算（7 天） | ≈ 915 MiB（452.5 + 148.1 + 314） | `P2_3_FIX_PLAN.md` §1 |
| USN 日增 | 约 12 万行 / 45 MiB | `P2_3_FIX_PLAN.md` |
| `data_size_alert_mb` | 1280 MiB（为 915 MiB 稳态留余量而提高） | `config/config.json` |

### 1.2 DB 体积构成（`dbstat` 实测）

| 表 / 索引 | 占用 | 性质 |
|---|---|---|
| `file_state` + 2 索引 | 452.5 MiB（70%） | 2 份全量快照，恒定，**不是增长项** |
| `mft_index` + 主键索引 | 148.1 MiB（23%） | 每次 `usn-index` 重建，靠代次管理 |
| `usn_event` + 3 索引 | 44.8 MiB（7%）→ 314 MiB | **唯一无上限增长项** |

**关键判断**：DB 的"大头"是恒定的快照（70%），真正会持续涨的是 USN 事件表。任何"压缩占用"方案都必须先分清这两类，否则会优化错对象。

### 1.3 为什么显示层不是主线

2026-11-17 已经做过 pythonw 静默化（去掉黑色 console），并在记录中明确写下：

> 真正大头是 DB 717MiB→915MiB 收敛 + 每 30min 全盘快照 192 万行，与 UI 外壳无关。

托盘图标、窗口样式、日报门户都属于"看起来省"，不改变采集字节数与写入行数。**本文只处理采集架构与存储架构。**

### 1.4 关键实现事实（逐行核对）

- **P1 扫描**（`p1_snapshot.py:88-172`）：`os.scandir` 深度遍历，每文件一次 `os.stat()` 取 `st_ino`（NTFS FRN），再 `allocated_size()` 按簇对齐（压缩/稀疏走 `GetCompressedFileSizeW`）。慢在"每文件一次 stat + 每簇对齐计算"，且卷用量与逐文件清单不在同一时刻采样。
- **P2 ENUM**（`p2_usn.py:397-441`）：`FSCTL_ENUM_USN_DATA` 顺序游标流式枚举，1,200,146 条已实测落库，`eof=True` 正常收尾。
- **ENUM 记录字段**（`p2_usn.py:169-175`）：`record_length / major / minor / frn / parent_frn / name / usn / time_utc / time_local / reason_mask / reason_names / op / source_info / file_attributes / needs_resolution`——**没有 logical_size / allocated_size**。
- **FRN 一致性**（已验证）：`fsutil` 128 位 ID 低 64 位 == `os.stat().st_ino`，统一 `0x%016x`。
- **保留键**（`p1_db.py:249-276`、`p2_store.py:370-434`）：`retention_days=7` 同时被 `prune()`、`prune_usn()`、`prune_archives()` 消费；`usn_retention_days` 文档里提到但**代码中不存在**。

---

## 2. 设计原则与不变量

任何 P4 改动必须同时满足，违反即判失败：

1. **只读采集**：所有对 C 盘的访问是 `GENERIC_READ`；不删除、不移动、不写入被监控卷任何字节。
2. **产物只落 D 盘**：DB / CSV 归档 / 日志 / 报告全部在 `D:\SpaceKnife-local\CDriveSentinel\data\`。
3. **FRN 对齐**：快照按 FRN 而非路径差分，重命名不得算成一删一增。
4. **AllocationSize 口径**：用分配大小而非逻辑大小；压缩/稀疏用真实分配值；硬链接按 FRN 只计一次。
5. **未归因诚实**：残差保留为独立行，绝不硬摊到某目录。
6. **可比性护栏**：`scope_hash` / `partial` / `pruned` 决定哪些快照能作差分前序。
7. **零第三方依赖**：Python 标准库 + `ctypes` + `sqlite3`，不引入 WizTree 等外部二进制作为**必需**路径（可选加速除外）。
8. **可回退**：改动前打 `Log\backup_YYYYMMDD_HHMM\`，含源码与 DB 一致性快照（项目无 git 仓库）。

---

## 3. 方案 A：P1 改 MFT 直读（核心，工作量大）

### 3.1 核心难题：ENUM 有 FRN 和路径，但没有大小

这是整个方案最关键、也是现有文档没点破的一点。`FSCTL_ENUM_USN_DATA` 返回的 `USN_RECORD` 设计目的是"变更日志 + MFT 枚举定位"，它给出：

- ✅ FRN / 父 FRN / 文件名（可拼完整路径树）
- ✅ 文件属性、时间戳、USN
- ❌ **逻辑大小、分配大小都没有**

而 P1 空间账的每一行都依赖 `allocated_size`（见 `p1_diff.py:45-90`）。所以**不能**简单地把 `FileScanner.rows()` 换成 ENUM 结果——那样所有文件大小都是 0，账本直接失效。

### 3.2 获取大小的四条技术路径

| 路径 | 做法 | 优点 | 代价 / 风险 | 结论 |
|---|---|---|---|---|
| **A1** `FSCTL_GET_NTFS_FILE_RECORD` | 按 FRN 逐条取完整 MFT 记录，解析 `$DATA` 属性拿 Real/Allocated Size | 官方接口、可读最准；微软文档明确 | 约 120 万次 IOCTL，可能仍要数分钟，未必比 `os.stat` 快 | **POC 首选**：先验证可行性 |
| **A2** 直读 `$MFT` 文件 | 打开卷上 `$MFT`，按 1024 字节/记录解析属性 | WizTree 原理，秒级全盘 | 属性解析、重解析点、128 位 ID、resident/压缩文件全要自己处理，高风险 | A1 跑通后作为提速终态 |
| **A3** WizTree CLI CSV | 用 `WizTree64.exe /export` 秒级导出 | 最省事 | **CSV 无 FRN 列**（config 注释已确认），无法做 FRN 对齐差分 → 会误判重命名 | ❌ 不用于 P1 差分；仅可用于 P0 现状盘点 |
| **A4** `GetFileInformationByHandleEx` | 打开每个文件取 `FileStandardInfo.AllocationSize` | 分配大小最真 | 120 万次 `CreateFile`，比 `os.stat` 更重 | ❌ 不解决问题 |

**推荐路线**：**A1 做 POC → A2 做终态**，且保留现有内置扫描作为回退与自检对照。绝不上 A3（FRN 缺失是硬伤）。

### 3.3 目标架构：可插拔的快照源

```
snapshot_source = "builtin" | "mft"          # 配置新增，默认 builtin
                         │
        ┌────────────────┴────────────────┐
        ▼                                 ▼
 FileScanner.rows()                 MftScanner.rows()      ← 统一产出同一套 FIELDS
 (现状，os.scandir)                  (ENUM 枚举 FRN/路径树
                                     + FILE_RECORD 取大小)
        └────────────────┬────────────────┘
                         ▼
              db.bulk_insert_files()  →  diff.diff_snapshots()
```

统一契约（`p1_snapshot.py:31` 的 `FIELDS`）：`frn, parent_frn, name, path, logical_size, allocated_size, mtime_utc, attributes, is_dir`。只要新扫描器产出同样的字典行，`main.py:127-146` 的调用链、`p1_diff`、`p1_db`、`p1_report` **一行都不用改**——这是把改动面压到最小的关键设计。

### 3.4 正确性护栏（必须逐条实现）

1. **FRN 规范化**：FILE_RECORD 的 FRN 低 64 位 → `0x%016x`，与 `os.stat().st_ino` 和 USN V3/V4 一致。
2. **硬链接去重**：同一 FRN 只在 ENUM 中出现一次（MFT 一条记录），天然去重；但路径要选规范路径（沿用 `DESIGN.md` 提到的"规范路径归一化"），避免与 builtin 扫描归属漂移。
3. **重解析点跳过**：`FILE_ATTRIBUTE_REPARSE_POINT (0x400)` 过滤，与现状一致，防止循环与重复计数。
4. **大小口径对齐**：FILE_RECORD 的 `$DATA` 里 Real Size = 逻辑大小，Allocated Size = 分配大小；稀疏/压缩文件要用分配值而非簇对齐估算。切换扫描源前后必须用 A/B 对照证明 `bytes_total` 差异在可接受范围（见 §6.2）。
5. **partial 语义**：ENUM 的 `truncated / win_error / eof` 映射到 `stats["partial"]`，超时/失败快照只做基线不参与差分。
6. **scope 过滤**：`scope_hash` 由 `config.scope_roots()` 决定；MFT 全盘枚举后按 scope 前缀过滤，保证与旧快照可比。
7. **采样一致性**：MFT 直读秒级完成，快照开始/结束的卷用量差趋近于零 → **从结构上消除采样错位类残差**（这是相比 builtin 的最大收益，不只是快）。

### 3.5 权限与降级

- ENUM / FILE_RECORD 读取需要管理员（与 USN READ 同级）；根目录句柄 + `FILE_FLAG_BACKUP_SEMANTICS` 对 QUERY 可行，但读取记录需提权。
- 非管理员环境：`snapshot_source=mft` 应**自动降级**到 builtin 并记一条 `alert`，与 P2 的 `permission_denied` 策略一致，不得静默产出空快照。

### 3.6 验收门槛

- 全盘扫描耗时 < 60 s（对比现状 297–538 s）。
- `file_state` 行数与 builtin 扫描差异 < 0.1%。
- 同一 C 盘 A/B 两次快照，`total_allocated` 与 `change_event` 汇总差异可解释（簇对齐 vs 真实分配的差值需量化并记录）。
- `selftest` 新增 MFT 记录解析用例（见 §6.1）。

---

## 4. 方案 B：DB 分区 / 分表（空白，需新建方案）

### 4.1 先分清两类数据

- **恒定项**：`file_state`（2 份快照，452.5 MiB）——"分区"对它意义有限，它不会涨。
- **增长项**：`usn_event`（7 天约 314 MiB）——分区/分表的主战场。

因此"DB 分区"的准确目标不是把 DB 变小，而是：**让增长项有可预测的上限、可由运维批量淘汰、且删除后文件能真正缩小。**

### 4.2 三种候选（按推荐度排序）

| 方案 | 做法 | 收益 | 代价 |
|---|---|---|---|
| **B1 分表 + 分区视图**（推荐） | `usn_event` 按日/周分表（`usn_event_YYYYMMDD`），上层建 `CREATE VIEW usn_event AS UNION ALL` | 淘汰旧数据用 `DROP TABLE`，O(1) 且无碎片；查询侧按视图无感 | 需改 `p2_store` 全部 SQL；视图跨越多表时查询计划要验证 |
| **B2 冷热分库（ATTACH）** | 热库（近 7 天）主 DB，冷库归档单独文件，`ATTACH DATABASE` 查询 | 主库体积小、备份快；冷库可整文件删除 | 跨库 JOIN 复杂；`verify` 脚本要适配；事务一致性要小心 |
| **B3 分区键 + 范围清理** | 维持单表，但所有查询强制带时间范围索引，清理按范围分批 | 改动最小 | 不解决 SQLite 文件不缩小问题（依赖 `auto_vacuum`） |

**推荐 B1**：与已落地的 `prune_usn()` 分批删除形成互补——日常用分批删保平稳，跨月/跨周用 `DROP TABLE` 一次性回收空间。B2 留作 DB 突破 1 GiB 后的进阶选项。

### 4.3 与 retention 的联动

分区的真正价值在 §5 的保留期分离之后才完全体现：不同表可以有不同保留期，淘汰方式也可以不同（`usn_event` 用 DROP 分表、`change_event` 用时间范围删、`file_state` 用快照份数）。三者互不干扰，才叫"激进但可控"。

### 4.4 不变量

- 账本语义不变：`change_event.delta_bytes` / `diff_run.residual_bytes` / USN 证据升级逻辑一行不动。
- `verify` 与 `report` 通过视图读数据，结果必须逐字节一致（用备份库 A/B 对比）。

---

## 5. 方案 C：更激进的 retention（可做，但有代价）

### 5.1 现状：一个键管四处

`retention_days=7` 同时作用于：

| 消费方 | 数据 | 7 天后果 |
|---|---|---|
| `prune_usn()` | `usn_event` / `usn_poll` | 秒级事件只留 7 天 |
| `p1_db.prune()` | `change_event` / `alert` / `volume_heartbeat` | 空间账明细只留 7 天 |
| `prune_archives()` | `data/snapshots/*.csv.gz` | 每日归档只留 7 天（另有 4 份保底） |
| `file_state` | 不受影响 | 由 `keep_file_state_snapshots=2` 控制 |

### 5.2 理想：按类别拆成独立保留期

```
usn_retention_days      = 3~7     # 秒级事件，最不重要，最短
ledger_retention_days   = 30      # 空间账 change_event，回溯价值高
archive_retention_days  = 30      # 每日 CSV 归档
keep_file_state_snapshots = 2     # 恒定
keep_usn_polls          = 200     # 成功 poll 保底（verify 基线）
keep_snapshot_archives  = 4       # 归档保底
```

要点：

- **账本与 USN 解耦**：`DESIGN.md:619` 已明确「若日后要 USN 7 天、账本 30 天，应新增 `usn_retention_days` 独立键」——本方案就是把这句话落地。
- **更激进 ≠ 更短**：对 USN 可以更激进（3 天），但对账本应更保守（30 天），因为账本是回答"哪天涨了什么"的唯一明细来源。
- **页回收固化**：所有 `prune` 后统一 `PRAGMA incremental_vacuum`；库必须是 `auto_vacuum=INCREMENTAL`（新库建库即设，旧库用 `maintenance` 转换）。
- **可解释的牺牲**：账本明细 30 天但 USN 3 天，意味着 3 天前的事件没有秒级证据（只有 `diff_run` 窗口级残差）——报告要明确标注。

### 5.3 容量测算（拆键后稳态）

| 项 | 保留 | 估算占用 |
|---|---|---|
| `file_state` | 2 份 | 452.5 MiB |
| `mft_index` | 单代 | 148.1 MiB |
| `usn_event` | 3–7 天 | 135–314 MiB |
| `change_event` | 30 天 | 需实测（当前 7 天口径未单列） |
| **合计** | | **约 800–1000 MiB**，仍低于 1280 告警线 |

若配合 §4 分区用 `DROP TABLE` 回收，实际文件体积可进一步低于理论值。

---

## 6. 验证、风险与实施顺序

### 6.1 自检用例（纳入 `src/main.py selftest`，当前 32/32）

新增用例建议编号 F24–F28：

| 编号 | 用例 |
|---|---|
| F24 | MFT FILE_RECORD 解析：V1/V3 记录、FRN 低 64 位规范化、硬链接多路径去重 |
| F25 | 大小口径：$DATA Real/Allocated Size 提取，压缩/稀疏文件走真实分配 |
| F26 | 快照源可插拔：`builtin` 与 `mft` 产出同结构 FIELDS，`diff` 结果可比 |
| F27 | 分表视图：按日分表后 `SELECT` 结果与单表逐行一致 |
| F28 | 保留期分离：三个键各自生效、互不串扰、`incremental_vacuum` 调用到位 |

全部在 D 盘沙箱完成，C 盘零写入（延续现有 selftest 约定）。

### 6.2 A/B 对照（上线前必做）

1. 备份当前 DB → `sentinel_before_p4.db`。
2. 同一 C 盘先后跑 `builtin` 和 `mft` 两次完整快照。
3. 对比行数、`total_allocated`、`change_event` 汇总、`diff_run` 残差，差异必须可解释并记录到 `Log/`。
4. `verify` 必须 OVERALL PASS，`diff_run` 残差账本语义不得被改写。

### 6.3 风险登记册

| 风险 | 后果 | 缓解 |
|---|---|---|
| MFT 属性解析错误（重解析点 / 128 位 ID / resident 文件） | 大小算错 → 账本污染 | A1 官方接口先 POC；builtin 回退；A/B 对照 |
| 权限不足 | 空快照 | 自动降级 + 告警，不静默 |
| 分表后 `verify` / `report` 结果漂移 | 误判账本异常 | 视图 + 备份库逐行 A/B |
| 保留期拆键后回看缺明细 | 用户困惑 | 报告标注证据等级与时间边界 |
| 无 git 仓库 | 改坏难回退 | 每次改动前 `Log\backup_YYYYMMDD_HHMM\` |

### 6.4 实施顺序（推荐）

```
P4.0  POC：A1 按 FRN 取大小 + A/B 对照         → 门槛：耗时 < 60s、行数差 < 0.1%
P4.1  快照源可插拔（builtin 默认 / mft 可选）   → 门槛：selftest F24–F26、真实快照 A/B
P4.2  保留期拆键 + 页回收固化                   → 门槛：F28、verify PASS
P4.3  usn_event 分表 + 分区视图                 → 门槛：F27、结果逐行一致
P4.4  （可选）A2 直读 $MFT 提速 / B2 冷热分库
```

依赖顺序：P4.1 依赖 P4.0；P4.3 依赖 P4.2（先把保留期语义定清楚再分表）。每期结束必须 `verify` PASS 且账本残差语义不变，否则不进下一期。

---

## 7. 不在本文范围

- P3 进程归因（Sysmon / ETW）、告警增强、PySide6 面板——属展示与归因层。
- 任何 C 盘文件的清理、移动、删除——系统永远只读。
- WizTree 作为 P1 必需依赖——A3 已论证其 CSV 无 FRN，不适合差分。

---

## 附录 A：待实测确认的假设

本文基于代码与文档事实写成，以下三点**必须在 P4.0 用真实 C 盘验证**，不得当作既定结论：

1. `FSCTL_GET_NTFS_FILE_RECORD` 在约 120 万文件规模下的实际耗时（决定 A1 是否够快，还是必须上 A2）。
2. FILE_RECORD 提取的 `Allocated Size` 与现有 `allocated_size()` 簇对齐估算的差值分布（决定 A/B 对照的可接受阈值）。
3. 分表视图在 `usn_event` 跨多表 JOIN 时的查询计划是否退化（决定 B1 是否需加时间范围约束）。

## 附录 B：与现有文档的关系

- 本文是**新增建议文档**，不修改 `DESIGN.md` / `P2_*_PLAN.md` 任何正文。
- 若 P4 某期落地，按项目惯例在验收后回填 `DESIGN.md` 状态表与对应章节。
- 术语、口径、不变量完全继承 `DESIGN.md` §0、§8，无新增例外。
