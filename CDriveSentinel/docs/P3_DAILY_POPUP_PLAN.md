# CDriveSentinel P3 方案：登录进桌面自动用浏览器打开前一天日报

- 版本：v1.1（已按用户确认的"全部按建议"实施）
- 日期：2026-11-23 方案 / 2026-11-24 实施
- 状态：**代码已完成并实测（selftest 37/37、dry-run、真实弹窗、去重均通过）；计划任务注册脚本已就绪，待用户在管理员/普通 PowerShell 执行注册并实测登录触发**
- 依据：`src/main.py`（daemon / report / 入口）、`src/p1_config.py`、`src/p1_report.py`、`src/p1_db.py`、`install_task.ps1`、现有 `report.last_auto_day` 实测行为
- 已确认的需求定义：
  - **触发时机 = 登录进桌面的时刻**（Windows 的 `AtLogOn`），不是通电开机那一刻。停留在登录界面时不会触发，用户登录进桌面后才弹——这正是用户想要的效果。
  - **呈现方式 = 系统默认浏览器打开网页版日报**（HTML），不做系统通知、不做独立桌面窗口。

---

## 1. 结论先行

能实现，而且属于**小改动**：最难的部分（采集、差分、日报生成、登录自启）早就跑通了，本次只补"登录时判断并打开"这最后一步。

现有基础已经覆盖 80%：

| 已有能力 | 落点 | 本次用途 |
|---|---|---|
| 登录自启计划任务 | `install_task.ps1` → 任务 `CDriveSentinel`，`AtLogOn` + `Interactive` + `Highest`，`pythonw.exe daemon` | 证明"登录触发"机制已就绪；本次**另建一个独立任务**专门弹窗 |
| 每天自动生成昨天日报 | `cmd_daemon()` 里 8 点后为昨天生成 txt+html+门户，状态键 `report.last_auto_day` 防重复 | 弹窗内容来源；也提供"文件不在就现场补生成"的现成函数 |
| 日报 HTML 文件 | `reports/daily/YYYY-MM-DD.html` | 浏览器直接打开的目标 |
| 门户页 | `reports/index.html` | 顺手刷新，保证从日报能回到最新列表 |
| 状态存储 | `runtime_state` 表 + `db.get_state()` / `db.set_state()` | 用来记住"今天已经弹过了" |

需要新增的只有三件事：一个"今天弹过没"的开关、一个"打开网页"的入口、一个独立的登录触发任务。

---

## 2. 现状基线（验收对照用）

| 指标 | 当前值 | 来源 |
|---|---|---|
| 计划任务 | `CDriveSentinel`，`AtLogOn`，`Interactive`，`RunLevel Highest`，`pythonw src\main.py daemon`，`MultipleInstances=IgnoreNew`，`Hidden` | `install_task.ps1:51-57` |
| 日报自动触发 | `now.hour >= daily_report_hour`（默认 8）时为**昨天**生成，写入 `report.last_auto_day`；守护进程重启不重复 | `main.py:305-323` |
| 日报文件命名 | `reports/daily/<YYYY-MM-DD>.html` + 同名 `.txt` | `p1_report.py:519-529` |
| 门户文件 | `reports/index.html`，列最近 7 天 | `p1_report.py:665-668` |
| 状态键 | `report.last_auto_day`（已有）；本次新增 `report.popup_day` | `p1_db.py:163-170` |
| 报告生成入口 | `report.write_report(conn, cfg, paths, day, since, until, top, make_html=True)` → 含 `html_path` | `p1_report.py:519` |
| CLI 入口 | `main(argv)` 的 `handlers` 字典，当前 12 个命令 | `main.py:1168-1185` |
| 数据目录 | `D:\SpaceKnife-local\CDriveSentinel`（`data_root="."`，D 盘，与 C 盘隔离） | `config/config.json` |

> 关键时序事实：日报是**登录之后**、过了 8 点才由常驻守护进程生成的。所以"登录瞬间就弹昨天日报"会遇到"文件还没生成"的情况——见决策 3 的处理。

---

## 3. 三个关键设计决策

### 决策 1：新建独立计划任务，不塞进现有 daemon

**选独立任务 `CDriveSentinel-OpenReport`（普通权限，不 Highest）。**

理由：

1. **浏览器权限坑**：现有 daemon 是 `RunLevel Highest` 运行的高权限常驻进程。从高权限进程直接打开浏览器，浏览器会继承管理员权限，Chrome/Edge 会弹"以管理员身份运行不受支持"的警告甚至拒绝启动。独立任务用普通权限，浏览器正常打开。
2. **职责隔离**：daemon 是长期常驻、只被启动一次的采集器；"每天弹一次"是一次性短任务。混在一起会让"每天一次"的判断和采集循环纠缠，改坏了还影响采集。
3. **风险可控**：新任务失败只影响弹窗，采集与日报生成完全不受影响。

代价：需要用户用**管理员 PowerShell** 再跑一次新的安装脚本来注册这个任务（一次性操作）。注意区分：**注册**任务要管理员权限（写系统任务存储），但任务**运行时**是普通权限（浏览器才能正常启动）。

> 2026-11-24 实测补充：普通 PowerShell 注册会报 `0x80070005 拒绝访问`，必须 Run as Administrator。另 PATH 里的 `python.exe` 是 3.7（已 EOL，打不开 WAL 数据库报 `attempt to write a readonly database`），安装脚本已改为精确定位 **Python 3.13**（py-launcher `-3.13` 或 `%LOCALAPPDATA%\Programs\Python\Python313`），并在注册前后做失败检测与任务存在性校验，杜绝"失败却打印成功"。

### 决策 2：用 `runtime_state` 的 `report.popup_day` 保证每天只弹一次

流程：**登录 → 读 `report.popup_day` → 若等于今天则直接退出 → 否则弹窗 → 写 `report.popup_day = 今天`**。

- 键名建议 `report.popup_day`，值形如 `2026-11-23`（表示"11 月 23 日已经弹过了"）。
- 这样同一天注销再登录、重启再登录，都不会重复弹。
- 第二天登录时，键里的值是昨天，于是弹"前天"……准确说：11 月 24 日登录 → 弹 11 月 23 日日报 → 键写 `2026-11-24`。符合"弹前一天日报"。
- 沿用现成的 `db.get_state()` / `db.set_state()`，不引入新表、不改 schema。

### 决策 3：昨天日报还没生成时，现场补生成再弹

因为日报要等 8 点后由 daemon 生成，早间登录时昨天的 HTML 可能不存在。处理顺序：

1. 算出昨天日期；
2. 检查 `reports/daily/<昨天>.html` 是否存在；
3. **不存在** → 现场调用现成的 `report.write_report(..., make_html=True)` 补生成昨天日报（昨天已过去、数据已定稿，安全），并调用 `report.write_portal()` 刷新门户；
4. 存在 → 直接跳过生成；
5. 用默认浏览器打开该 HTML；
6. 最后才写 `report.popup_day = 今天`。

**成功才写标记**：只有真正打开了浏览器才写 `popup_day`。如果生成或打开失败，标记不写，下次登录会重试——避免"标记写了但没弹成"的静默失败。

边界情况：如果昨天电脑关机、守护进程没跑，昨天几乎没有采集数据，日报会如实显示空内容。这是**数据缺失，不是故障**，不在本次范围内伪造数据。

---

## 4. 修改清单

| 编号 | 文件 | 改动 |
|---|---|---|
| F1 | `src/main.py` | 新增 `cmd_open_report(cfg, paths, args)`：按决策 2/3 实现"判断 → 补生成 → 打开 → 写标记"，每步写 `run.log` |
| F2 | `src/main.py` | `main()` 的 `handlers` 增加 `"open-report"`；argparse 增加 `--dry-run`（只走判断和生成、不实际打开浏览器，供验证用） |
| F3 | `src/main.py` | `cmd_selftest()` 增加纯逻辑用例：日期计算、去重判断、"昨天"跨月边界（如 3 月 1 日 → 2 月 28/29 日） |
| F4 | 新建 `install_popup_task.ps1` | 注册 `CDriveSentinel-OpenReport`：`AtLogOn` + 当前用户 `Interactive` + **不** Highest + `pythonw src\main.py open-report` + `StartWhenAvailable` + `MultipleInstances=IgnoreNew`；附 `-Uninstall` |
| F5 | 新建 `docs/P3_DAILY_POPUP_PLAN.md` | 本方案文档 |
| F6 | `docs/DESIGN.md` | 附录 B 进度勾选补一行"登录自动弹日报"，并注明独立任务而非 daemon 内嵌 |

**可选（待确认）**：

| 编号 | 文件 | 改动 |
|---|---|---|
| F7 | `config/config.json` + `src/p1_config.py` | 新增 `daily_popup_enabled`（默认 `true`）开关。加了它用户以后能一键关掉弹窗；不加则代码内置常量。倾向于加，因为它不影响任何现有键。 |

> 打开浏览器的实现：优先用 `os.startfile(path)`（标准库、走系统文件关联，等价于双击 HTML）。`--dry-run` 时跳过这一步。

---

## 5. 明确不改的部分（不变式）

| 约束 | 说明 |
|---|---|
| **只读** | 不新增任何对 C 盘的写入；所有新文件仍落在 D 盘项目目录 |
| **零第三方依赖** | 仅 Python 3.13 标准库 + 现有模块，不引入包 |
| **不动采集/差分** | 不改 `p1_snapshot` / `p1_diff` / `p2_*` 的任何逻辑 |
| **不动报告内容** | 不改 `p1_report.py` 的模板、字段、红涨绿跌配色；只复用其 `write_report` / `write_portal` |
| **不动现有任务** | `CDriveSentinel`（daemon 任务）保持原样，不覆盖、不删改 |
| **不改数据库 schema** | 只在 `runtime_state` 增加一个键，沿用 `get_state` / `set_state` |
| **不改已有 config 默认值** | 除可选 F7 新增一个开关键外，`config.json` 现有键一律不动 |

---

## 6. 验证方案

### 6.1 改动前备份（项目无 git，必须手动；非管理员终端即可）

```powershell
cd D:\SpaceKnife-local\CDriveSentinel
New-Item -ItemType Directory -Force .\backup\20261123_0950 | Out-Null
Copy-Item .\src\main.py .\backup\20261123_0950\
Copy-Item .\config\config.json .\backup\20261123_0950\
Copy-Item .\data\sentinel.db .\backup\20261123_0950\
```

### 6.2 我这边可独立执行（非管理员）

| 步骤 | 命令 | 预期 |
|---|---|---|
| S1 | `python src\main.py selftest` | 全部通过（原有用例 + 新增日期/去重用例）；沙箱仍在 D 盘、C 盘零写入 |
| S2 | `python src\main.py open-report --dry-run` | 日志显示"昨天日报缺失→现场补生成"或"已存在→跳过"，最后打印"would open: ...html"；**浏览器不弹出**；`report.popup_day` 未写入 |
| S3 | 只读 DB 断言 | `runtime_state` 中 `report.last_auto_day` 等原有键值与基线一致；`report.popup_day` 不存在（dry-run 不写标记） |
| S4 | 再跑一次真实 `open-report`（非 dry-run） | 默认浏览器打开 `reports/daily/<昨天>.html`；DB 出现 `report.popup_day=今天` |
| S5 | 紧接着再跑一次 `open-report` | 日志显示"already popped today, skip"；不再打开浏览器，`popup_day` 不变 |

### 6.3 需用户实测（登录后）

1. 管理员 PowerShell 跑 `install_popup_task.ps1` 注册任务；
2. 注销并重新登录（或重启电脑再登录）；
3. 预期：进桌面后默认浏览器自动打开前一天日报；
4. 同一天再注销登录一次：预期**不再**弹出；
5. 检查 `data\run.log` 有 `open-report` 的完整日志（弹/跳过都有记录）。

### 6.4 回退

- 代码：用 `backup\20261123_0950\` 里的 `main.py` / `config.json` 覆盖回去。
- 弹窗任务：`powershell -ExecutionPolicy Bypass -File .\install_popup_task.ps1 -Uninstall`。
- 状态键：`report.popup_day` 可留可删（删掉后当天登录会再弹一次）。

---

## 7. 风险与边界

| 风险 | 影响 | 缓解 |
|---|---|---|
| 浏览器被高权限启动 | Chrome/Edge 拒绝运行 | 决策 1：独立任务用普通权限（非 Highest） |
| 早间登录时昨天日报未生成 | 打开失败 | 决策 3：现场补生成，成功才写标记 |
| 同一天多次登录重复弹 | 打扰用户 | 决策 2：`report.popup_day` 去重 |
| 打开动作失败但标记已写 | 静默不再弹 | 决策 3：只有成功才写标记，失败留待下次登录重试 |
| 任务注册需管理员 | 一次性操作 | 脚本内置提权检查并给出明确提示，附带 `-Uninstall` |
| 昨天电脑关机导致空日报 | 报告内容为空 | 如实展示，不伪造数据；属数据缺失，不在本次范围 |
| 默认浏览器被改/文件关联异常 | `os.startfile` 失败 | 捕获异常写日志，不写标记；日志给出明确文件路径供手动打开 |

---

## 8. 待确认事项

1. 是否同意**新建独立计划任务**（决策 1），而不是塞进现有 daemon？
2. 是否同意新增 `daily_popup_enabled` 开关（F7），还是写死在代码里？
3. 弹窗时是否顺手刷新门户页 `reports/index.html`？（建议：是，这样从日报能跳回最近 7 天列表。）
4. 去重标记失败时不写、下次登录重试（决策 3 末条）——是否接受"极端情况下可能连弹两天"这一取舍？

> 确认后进入实现：改 `main.py` + 新建安装脚本 → 跑 S1–S5 → 交付管理员侧命令清单 → 用户注销/重启实测 → 回填 `DESIGN.md` 与当日工作日志。
