# CDriveSentinel P2.3 方案：USN 留存、索引代次与持续运行

- 版本：v1.1
- 日期：2026-11-16 18:10
- 状态：**已实施（F14–F20 全部落地）；管理员侧 A8–A12 待执行**
- 实施记录：`Log\2026-11-16_P23_fix_记录.md`；复测清单：`Log\P2_3_管理员复测清单.md`
- 实施结果速览：`selftest` **32/32**、`prune --dry-run` 报 `mft_index=36579`、账本与 10 张表行数零变化、`verify` 10 项中 8 PASS / 1 预期 FAIL（单代次）/ 1 WARN（cursor lag）
- 修订：v1.1 将 `retention_days` 由 30 改为 **7 天**（用户 2026-11-16 18:07 决定），并同步重算全文所有 30 天口径的容量与判定数字
- 依据：18:00 只读实查（进程 / 计划任务 / `dbstat` 各表占用 / `usn_event` 增长速率 / `mft_index` 代次分布）、`src/p1_db.py` `prune()`、`src/p2_store.py`、`src/main.py` 逐段核对
- 适用文件：`src/p2_store.py`、`src/main.py`、`config/config.json`、`Log/verify_p2_hotfix.py`（共 4 个）
- 基线文档：`docs/P2_2_FIX_PLAN.md`（保持原样，不修改）

## 0. 工作区说明

项目**无 git 仓库**，回退靠 `Log\backup_20261116_1626\`（P2.2 前 4 个源文件 + SQLite 一致性快照）。本包是 P2.3，改动落在同一批文件上，**动手前需再打一份 `backup_20261116_18xx`**，不能只依赖 P2.2 的备份（那份已不含 P2.2 补丁）。

当前会话 `IsUserAnAdmin=0`：`usn-index` / `usn-poll` / 计划任务注册均需提升权限，本包只做「非管理员可验证」部分，管理员侧列清单交付。

## 1. 结论先行

P2.2 已把「能不能算对」解决完了（`selftest 27/27`、管理员侧 `verify` 7/7 PASS）。18:00 体检发现的是另一类问题：**系统算得对，但撑不住长期运行**。

| 编号 | 缺陷 | 直接后果 | 实测证据 |
|---|---|---|---|
| D8 | `prune()` 只清 `file_state` / `volume_heartbeat` / `change_event` / `alert`，**完全不碰 `usn_event` / `usn_poll` / `mft_index`** | USN 事件表无上限增长，`retention_days` 对该表是空配置 | 今天 117,232 行 / 44.8 MiB → 每天约 12 万行 / 45 MiB；7 天约 **82 万行 / 314 MiB** |
| D9 | `save_mft_index()` 用 `INSERT OR REPLACE` 且开扫前不清表 | 表内混入上一轮索引的陈旧行，FRN 被复用时可能解析成**已失效的旧路径** | 全表 1,232,623 行 vs `usn.index_size=1,196,044`，差额 **36,579 行** `updated_at=16:17` |
| D10 | `resolve()` 的 unresolved note 写死「run usn-index as Administrator」 | 实际成因是瞬态文件（CREATE 后立刻 DELETE）不在索引快照里，与权限无关，误导排障 | poll#4 的 1,553 条 unresolved，FRN **全部不在** `mft_index`，`frn_present=0` |
| D11 | 采集进程未常驻；`usn_max_records_per_poll` 固定 20,000、无追平逻辑 | 游标持续落后，journal 满即 `gap` → resync 全盘快照 + 中间事件永久丢失 | 无 python 进程、计划任务 `CDriveSentinel` **未注册**、最后心跳 15:24、最后 poll 17:01 |

数据库体积构成（`dbstat` 实测，总计 649 MiB）——**大头是 P1 快照，不是 USN**：

| 表 / 索引 | 占用 | 性质 |
|---|---|---|
| `file_state` + 两个索引 | 452.5 MiB（70%） | 保留 2 份快照，恒定，**不是本次目标** |
| `mft_index` + 主键索引 | 148.1 MiB（23%） | 每次重建，代次残留是 D9 |
| `usn_event` + 三个索引 | 44.8 MiB（7%） | **唯一无上限增长项**，是 D8 的靶点 |

> 口径提示（按 7 天重算）：稳态总量 ≈ `file_state` 452.5 + `mft_index` 148.1 + `usn_event` 314 ≈ **915 MiB**，距 `data_size_alert_mb=1024` 只剩约 110 MiB 余量；若库未转 `auto_vacuum=INCREMENTAL`，删除后的碎片会把实际文件顶到阈值以上。故本包同时把 `data_size_alert_mb` 提到 **1280**（见 F20），否则 7 天留存仍会常年 `self_growth` 告警。14:52 那条 531.7 MiB 的告警是旧阈值文案，当前代码阈值 1024 MiB、现用量 694 MiB，暂不会重复触发。

## 2. 第 0 步：先止血（不写代码，且有时间窗）

方案落地前必须先让采集持续运行，否则下面所有改动的验证都会落在「数据已断流」的状态上。

journal `MaximumSize=146.2 MiB`，实测写入速率 **0.231 MiB/min**（15:42→17:01，`first` 前进 18.25 MiB / 79 min），即约 **10.5 小时**覆盖一轮。17:01 的游标 `46117329368` 之后若不再追平，**次日凌晨约 3:30 前**会落入 `cursor_state` 的 `gap` 分支。

```powershell
# 常驻（推荐，管理员）：install_task.ps1 已配好 AtLogOn + RunLevel Highest + 无执行时限
cd D:\SpaceKnife-local\CDriveSentinel
powershell -ExecutionPolicy Bypass -File .\install_task.ps1
Start-ScheduledTask -TaskName CDriveSentinel

# 或先临时追平一次
python src\main.py usn-poll --max-records 200000
```

判定：`python src\main.py usn-status` 应看到 `status=active`、`journal next` 与 `stored state last_usn` 的差 < 1 MiB、`last poll` 时间在数分钟内。

## 3. 修改清单（F14–F20，编号延续 P2.2 的 F9–F13）

### F14 · USN 表留存（D8）

文件 `src/p2_store.py` 新增 `prune_usn(conn, cfg, dry_run=False)`：

```python
def prune_usn(conn, cfg, dry_run=False):
    days = int(cfg.get("retention_days", 7))
    keep_polls = int(cfg.get("keep_usn_polls", 200))
    cutoff = (datetime.datetime.now()
              - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    ...
```

**`retention_days` 的影响面（本包一并降为 7 天，需知情）**：这个键不止管 USN，`p1_db.prune()` / `prune_archives()` 也在用，改成 7 后四处同时缩短：

| 消费方 | 受影响的数据 | 7 天后的后果 |
|---|---|---|
| `prune_usn`（本包新增） | `usn_event` / `usn_poll` | USN 事件只留 7 天，DB 稳定在约 915 MiB |
| `p1_db.prune()` | `change_event` / `alert` / `volume_heartbeat` | **空间账本明细只留 7 天**，`report --day` 回看超过 7 天将没有明细行（`diff_run` 窗口级残差不受影响） |
| `prune_archives()` | `data/snapshots/*.csv.gz` | 每日归档只保留最近 7 天各一份（另有 `keep_snapshot_archives=4` 保底最近 4 个） |
| `file_state` | 不受 `retention_days` 影响 | 仍由 `keep_file_state_snapshots=2` 控制，恒为 2 份 |

> 若你希望「USN 只留 7 天、但空间账本留 30 天」，正确做法是给 USN 单独一个 `usn_retention_days` 键，而不是共用 `retention_days`。当前按你的指示共用 7 天；要分离请说一声，F14 加 3 行即可。

要点：

- **分批删除**，绝不一次 `DELETE` 全表。SQLite 的 `DELETE` 不带 `LIMIT`，用 rowid 子查询循环：
  ```sql
  DELETE FROM usn_event WHERE id IN (
      SELECT id FROM usn_event WHERE time_local < ? LIMIT 50000)
  ```
  每批 5 万行、每批 `commit()`，避免长事务锁库并让 `incremental_vacuum` 有回收机会。
- **`usn_poll` 双条件**：既按 `ts_local < cutoff`，又**至少保留最近 `keep_usn_polls` 条成功 poll**（默认 200）。理由：若长期停跑后第一次启动就触发留存，不能把唯一的 `status=ok` 记录删光，否则 `verify` 与 `usn-status` 会失去基线。
- **`mft_index` 按代次清**（见 F15），不按时间。
- **不删** `change_event` / `diff_run` / `snapshot_meta` / `file_state`——账本与差分基线不归本函数管，`p1_db.prune()` 已各自负责。
- 返回 `{"events": n, "polls": m, "index_rows": k, "cutoff": cutoff, "dry_run": bool}`；`dry_run=True` 时把 `DELETE` 换成 `SELECT COUNT(*)`，供手动确认。
- 删除后调用 `PRAGMA incremental_vacuum`（与 `p1_db.prune()` 一致，非 auto_vacuum 库上是 no-op，不报错）。

**触发时机**（三处，都不高频）：

| 位置 | 频率 | 说明 |
|---|---|---|
| 新增 `python src\main.py prune [--dry-run]` 子命令 | 手动 | 运维入口，先干跑看数再实删 |
| `do_snapshot()` 内 `db.prune()` 之后（`main.py:175` 附近） | 每 30 分钟 | 跟随既有留存节奏，不额外增加调度 |
| `cmd_daemon()` 内每轮检查，距上次 ≥ 1 小时才执行 | 每小时 | 防止无快照时留存永不触发 |

### F15 · `mft_index` 代次（D9）

文件 `src/p2_store.py` 与 `src/main.py`：

- `init_schema()` 给 `mft_index` 补列 `index_run TEXT`（`PRAGMA table_info` 检查后 `ALTER TABLE`，与现有 `EXTRA_CHANGE_COLUMNS` 同一套路，向后兼容旧库）。
- `save_mft_index(conn, records, index_run=None)` 增加第三参数，写入该列；缺省 `None` 表示旧数据。
- `cmd_usn_index` 开扫前生成 `run_id = "%Y%m%d%H%M%S"`，**先不删任何行**（保证索引中途失败时旧数据仍可兜底）。
- 成功分支（`win_error==0` 且 `total>0`）之后才清理：
  ```sql
  DELETE FROM mft_index WHERE index_run IS NULL OR index_run <> ?
  ```
  并把清理行数写进日志：`USN index: dropped N stale rows from previous runs`。
- 失败分支**不清理**，只记 `usn.index_error`，行为与 P2.2 一致。
- `_from_mft()` 查询**不加代次条件**（避免每次父链查询多一次子查询），靠写入端保证表内只有单一代次。

**安全性论证**：清理条件只在成功后触发；若本次索引行数异常偏小（例如 < 上一轮的 80%），跳过清理并记一条 `alert`，避免「扫到一半成功」误删可用索引。这个下限写进 F19 自检用例。

### F16 · unresolved 文案（D10）

文件 `src/p2_store.py` 的 `PathResolver.resolve()` 两处：

| 位置 | 原文案 | 新文案 |
|---|---|---|
| `name` 存在但父链解析失败 | `path unresolved: run usn-index as Administrator` | `path unresolved: transient file, not in index snapshot` |
| V4 无内联文件名且父链失败 | `V4 record without name; run usn-index as Administrator` | `V4 record without name; transient file, not in index snapshot` |

约束：**必须保留 `path unresolved` 前缀**，因为 `save_events()` 用 `if "path unresolved" in note` 决定是否写进 `note` 列（`p2_store.py:243`）。改文案时同步检查这处判定仍成立。

「索引过旧」的诊断提示不放在每条事件上，改由**报告层**处理：日报 USN 章节在 unresolved 比例 > 20% 时追加一行「索引快照可能过旧，建议管理员重跑 usn-index」。这样每条事件的 note 只描述事实，运维建议只出现一次。

### F17 · 追平逻辑与轮询上限（D11 代码侧）

文件 `src/main.py`：

- `run_usn_poll()` 的成功返回值已含 `journal` 与 `cursor`（`main.py:441-443`），`cmd_daemon()` 直接算 `lag = journal["next_usn"] - cursor`。
- 新增纯函数便于自检：
  ```python
  def needs_catch_up(lag_bytes, threshold=4194304):   # 4 MiB
      return lag_bytes > threshold
  ```
- daemon 循环里，poll 返回后若 `needs_catch_up(lag)` 且连续追平轮次 < 10，**跳过 `time.sleep(beat)` 立即再 poll 一轮**；达到 10 轮或 lag 低于阈值即恢复 30s 节奏，并记一行日志说明追平了多少轮。防止异常情况下死循环。
- `config.json`：`usn_max_records_per_poll` 由 `20000` 提到 **`60000`**。依据：实测 31 raw/s × 30s ≈ 930 条/轮，20000 对稳态足够，但对冷启动补读偏小；60000 可让单轮覆盖约 30 分钟突发，同时把 10 轮追平上限的总量控制在 60 万条以内。
- `config.json` 新增两项：`keep_usn_polls: 200`、`usn_catch_up_mib: 4`。

### F18 · 手动留存子命令

`src/main.py` 新增 `cmd_prune` 与 argparse 子命令 `prune`：

```powershell
python src\main.py prune --dry-run     # 只报告将删除多少行，不改库
python src\main.py prune               # 实际执行
```

输出沿用 `report.human()` 风格，形如：

```
prune: cutoff=2026-10-17 18:03:00  usn_event=0 rows  usn_poll=0 rows  mft_index=0 rows  (dry-run)
```

### F19 · 自检与验收脚本

`src/main.py` 的 `cmd_selftest` 新增 5 项（纯内存库 / D 盘沙箱，C 盘零写入）：

1. **留存只删旧行**：内存建 12 万条 `usn_event`（一半 10 天前、一半今天，`retention_days=7`），`prune_usn` 后断言旧行清零、新行完整、分批次数 > 1。
2. **poll 保底**：构造 300 条 `usn_poll` 全部超期，断言仍保留最近 `keep_usn_polls` 条。
3. **代次清理**：`mft_index` 造两代各 100 行，成功清理后断言只剩最新代 100 行。
4. **偏小保护**：新代行数 < 旧代 80% 时断言**不清理**且返回告警标记。
5. **追平判定**：`needs_catch_up(5 MiB) is True`、`needs_catch_up(1 MiB) is False`。

selftest 由 **27 项扩到 32 项**。

`Log/verify_p2_hotfix.py` 新增 3 项检查（保持单一验收入口，不另建脚本）：

| 检查 | 判定 |
|---|---|
| `usn_event` 无超期行 | `MAX(time_local)` 与 `MIN(time_local)` 跨度 ≤ `retention_days(7) + 1` = **8 天** |
| `mft_index` 单代次 | `SELECT COUNT(DISTINCT index_run)` ≤ 1（旧库 `NULL` 视为单代） |
| `usn_poll` 留存生效 | `MIN(ts_local) >= cutoff`，或行数 ≤ `keep_usn_polls` |

保持原有 7 项不变，总计 **10 项**；仍要求 `0 hard failures`。

### F20 · 告警阈值与会话口径对齐

文件 `config/config.json`：

- `retention_days`：`30` → **`7`**（已改）。
- `data_size_alert_mb`：`1024` → **`1280`**。依据：7 天稳态约 915 MiB（452.5 `file_state` + 148.1 `mft_index` + 314 `usn_event`），1024 只剩 110 MiB 余量，sqlite 碎片或一次 `mft_index` 重建就会越线，导致 `self_growth` 变成常态噪声而不是异常信号。1280 留出约 35% 余量，真正涨到 1.28 GiB 才告警。
- 不改 `alert_free_gb_below` / `alert_growth_gb_per_hour` / `usn_max_records_per_poll`（后者归 F17 改）。

判定：`python src\main.py status` 或 `report` 顶部不再因容量本身触发 `self_growth`；真正异常（例如 `usn_event` 增速翻倍）仍能在 1280 MiB 触发。

## 4. 明确不改的部分（不变式）

| 约束 | 说明 |
|---|---|
| **只读 C 盘** | 不新增对被监控卷的任何写入；全部产物仍在 `D:\SpaceKnife-local\CDriveSentinel` |
| **零第三方依赖** | 仅 Python 3.13 标准库 + `ctypes` + `sqlite3` |
| **空间账本语义** | `change_event` 的字节增量、`diff_run` 残差、P1 差分逻辑一行不动；`prune_usn` 不触碰这三张表 |
| **FRN 口径** | 继续 `0x%016x`；`PathResolver` 解析优先级（snapshot → history → mft → parent）不变 |
| **不回填历史** | 已落库的 117,232 条 `usn_event` 不改写；留存只按时间删整行，不重算 |
| **留存天数** | `retention_days` 由 30 降为 **7**（用户 2026-11-16 决定）；本包只读取该值，代码里不再二次调整，也不为 USN 另设独立键 |
| **已知牺牲** | 7 天留存意味着 `change_event` 明细只保留 7 天，`report --day` 回看更早日期将无明细（`diff_run` 窗口级残差仍在）；如需分离，后续加 `usn_retention_days` |
| **P2.2 基线** | `docs/P2_2_FIX_PLAN.md`、`docs/P2_HOTFIX_PLAN.md` 保持原样 |

## 5. 验证方案

### 5.1 非管理员侧（本会话执行）

| 步骤 | 命令 / 动作 | 预期 |
|---|---|---|
| V0 | `py_compile` 四个改动文件 | 无语法错误 |
| V1 | `python src\main.py selftest` | **32/32 通过**，沙箱在 D 盘、C 盘零写入 |
| V2 | `python src\main.py prune --dry-run` | 打印 cutoff 与三个计数；**不修改数据库** |
| V3 | 只读不变式断言 | `diff_run` 两行残差仍 `810725376 / -945582080`；`change_event` 行数不变；`runtime_state` 的 `usn.*` 未被改动 |
| V4 | `python Log\verify_p2_hotfix.py` | 10 项；管理员侧动作未做时，新 3 项中的「单代次」应报 FAIL（`mft_index` 仍有 36,579 条 `index_run IS NULL`），属如实的预期失败 |

> V4 的预期失败要在文档里写明，避免又出现「修了代码但没重跑就以为坏了」的误判。

### 5.2 管理员侧（需用户在提升的 PowerShell 执行）

```powershell
cd D:\SpaceKnife-local\CDriveSentinel
python src\main.py prune --dry-run          # 先看要删多少，应为 0（数据仅 1 天，未达 7 天）
python src\main.py usn-index                # 重建索引并清理陈旧代次
python src\main.py prune --dry-run          # 再跑，mft_index 应已无陈旧行
python src\main.py usn-poll --max-records 200000
python D:\SpaceKnife-local\Log\verify_p2_hotfix.py
```

| 步骤 | 期望 | 失败时看什么 |
|---|---|---|
| A8 `prune --dry-run` | `usn_event=0 rows`（现有数据仅 1 天，未超 7 天）、`mft_index=36579 rows` | 若 `mft_index` 不是 36,579，说明期间又跑过索引 |
| A9 `usn-index` | 日志出现 `dropped N stale rows`，N ≈ 36,579；`win_error=0, eof=True` | 若未清理，检查 `index_run` 列是否加上、成功率判定是否命中 |
| A10 `prune --dry-run`（第二次） | `mft_index=0 rows`，`usn_event` 仍为 0 | 若仍 > 0，说明清理条件写错 |
| A11 `usn-poll` | 游标前进、`path sources` 正常、无 truncated | — |
| A12 `verify` | **OVERALL PASS（10 项）** | 新 3 项里「单代次」由 A9 满足；「留存生效」在数据未满 7 天时按 `MIN(ts_local) >= cutoff` 判 PASS |

### 5.3 长跑验收（D11 的真正验证）

| 用例 | 动作 | 通过标准 |
|---|---|---|
| 用例 4（DESIGN 第 9 章「验收用例」第 4 条） | 停 daemon 2 小时后重启 | 重启后 3 分钟内 lag 回落 < 4 MiB（F17 追平逻辑生效），期间快照兜底不丢空间结论 |
| 增长观测 | daemon 连续跑 24 小时 | `usn_event` 日增 ≈ 12 万行 / 45 MiB；`file_state` 稳定在 2 份；数据总量向 915 MiB 收敛，不突破 `data_size_alert_mb=1280` |

## 6. 回退

**动手前**先建 `Log\backup_20261116_18xx\`（4 个源文件 + `sentinel_before_p23.db` 一致性快照）。

回退：关闭占用 `sentinel.db` 的进程 → 用备份覆盖 `src\` 与 `Log\` → 如需连数据回退用 `sentinel_before_p23.db`。

F14–F19 彼此独立，可逐项回退。**注意**：F14 一旦实删过数据，回退代码不会把行找回来——所以首次执行必须走 `--dry-run` 确认，且保留 `sentinel_before_p23.db`。

## 7. 风险与边界

| 风险 | 影响 | 缓解 |
|---|---|---|
| 留存误删过多 | 历史事件不可恢复 | 7 天留存是用户明确选择；`prune_usn` 只按时间删整行、不按比例；首次必须 `--dry-run`；`keep_usn_polls` 保底；若要保留更久，改 `retention_days` 即可，不必改代码 |
| 分批删除锁库 | daemon 轮询被阻塞 | 每批 5 万行、每批 commit；只在快照后或每小时触发，不与 poll 同轮 |
| 代次清理误删可用索引 | 路径解析率骤降 | 只在 `win_error==0 且 total>0` 且新行数 ≥ 旧行 80% 时清理，否则记 alert 并跳过 |
| 追平逻辑变成忙循环 | CPU / journal 压力 | 连续追平上限 10 轮，超过即强制 sleep；每轮仍受 `max_records` 约束 |
| `incremental_vacuum` 无效 | 删除后文件不缩小 | 库需先经 `maintenance` 转为 `auto_vacuum=INCREMENTAL`；本包文档中写明前置条件 |
| 管理员侧未复测 | 新 3 项验收为空 | 交付清单，不伪造；V4 明确标注预期失败项 |

## 8. 未闭合 / 后续

1. **P1 全盘扫描改 MFT 直读**：消除 297s/284s/538s 的采样错位，是 +773 / -902 MiB 残差的结构性来源。工作量大，单独立项。
2. **`tools/WizTree64.exe` 死配置**：`config.json` 引用该路径但 `tools\` 为空；当前代码不调用它，建议下一包删配置或补说明。
3. **P3 进程归因**：Sysmon EID 11/26 窄规则 + 可选 PySide6 面板，回答「哪个进程在吃盘」。
4. **真实 1 GiB 验收用例 1/2**：目前仅 MiB 级沙箱等价验证。
5. **`file_state` 452 MiB 的压缩**：可考虑把 `path` 拆成 `parent_id + name` 或按前缀字典化，属优化项，非缺陷。

> 实施顺序：备份 → 本文档确认 → F14 → F15 → F16 → F17/F18 → F19 → F20 → V0–V4 → 交付管理员清单 → 用户回填 A8–A12 → 长跑验收 → 回填 DESIGN.md 与工作日志。
