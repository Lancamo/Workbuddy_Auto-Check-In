# WorkBuddy 积分自动签到 · Windows 版

> **这个文件夹就是交付物。** 拷到新 Windows 机器的任意位置即可用；
> 所有运行时文件（state / 日志 / 凭据）都写在文件夹内部，**不往系统别处写东西**（计划任务注册除外，见下）。

## 简介

**每天自动替你在 WorkBuddy 里签到领积分、派遣旅行并领取奖励，结果推到微信；漏跑自动补，做完即停。**
与 `../00_For_Mac/` 的 macOS 版**功能等价、逻辑同源**，但完全不依赖 mac 环境，也没有任何 mac 路径。

它具体替你做的事：

- **签到**：每天 `07:00` 后自动完成 Buddy 加油站签到（+100 积分）。
- **旅行**：同一时刻把猫派出去；**猫到达后自动领取奖励**（+7 积分）—— 不设固定兜底时刻，到点才领。
- **通知**：每笔到账各推一条微信（签到、旅行分开推，不合并）；Windows Toast 作为本机兜底。
- **补跑**：错过的触发在开机 / 唤醒后由计划任务的 `StartWhenAvailable` 自动补上。
- **安静且免费**：当天做完后，后续触发全部空转、**零网络请求**；不走 WorkBuddy 自动化，**不消耗任何额度**。

## 安装

前置：Windows 10/11 + Python **3.10 或更高**（安装时勾选「Add python.exe to PATH」）+ 已登录
WorkBuddy 桌面端。**不需要管理员权限，也不需要 `pip install` 任何东西。** 完整清单见 **第 1 节**。

三步：

```
① 把整个 00_For_Win 文件夹拷到 Windows 任意位置（路径含中文/空格都行）
② 双击 install.cmd   → 注册四个计划任务（主任务 + watchdog + 每日唤醒 + 白天唤醒）并跑一次自检
③ 双击 doctor.cmd    → 环境诊断，全绿即通
```

`install.cmd` 用 `pythonw.exe` 跑，**无控制台、不闪黑窗，看不到输出就是成功**；
要看结果就双击 `run_now.cmd`。完整五步说明见 **第 2 节**。

## 使用

装好后**不需要你日常操作**。日常入口都是双击：

| 想做什么 | 双击 |
|---|---|
| 立即跑一次、看 JSON 结果 | `run_now.cmd` |
| 环境 / 配置诊断 | `doctor.cmd` |
| 让微信也能收到通知副本 | `wait_token.cmd` |

要精细控制就在文件夹里开 PowerShell / cmd 用命令行 —— 完整命令表见 **第 4 节**，
最常用的两条：

```bat
py -3 install.py status     :: 四个计划任务的定义 + 最近运行痕迹
py -3 install.py run        :: 立即前台跑一次（看得到输出）
```

**怎么确认它在正常工作**：`py -3 install.py status` 里四个任务都已注册，且
`py -3 watchdog.py status` 判定为健康。

`doctor.cmd` 里只有 `FAIL` 才表示会阻塞签到；未绑定微信、缺少 `context_token` 等
只影响推送，属于 `WARN`。计划任务的关键设置优先从 `schtasks` XML 读取；若某个
Windows 版本省略了可选的 XML 字段，`doctor.py` 会自动用 PowerShell 回读有效设置，
避免把“字段未输出”误判成“设置错误”。

## 文档维护约定

**本文件与代码同步维护，这是硬要求。** 以后任何一次优化调整 —— 逻辑变更、配置新增、
目录移动、默认值改动 —— 都必须**同步更新本 README**（win 版改 `00_For_Win/README.md`，
mac 版改 `00_For_Mac/README.md`；**跨平台的功能改动两份都要改**，
不要只改一侧、留下一条过期的谎言）。同步规则与改完的验证动作见 **第 5 节**。

变更历史不在这里维护：大改动的方案与执行记录见上级 `Plan/`，每日工作日志见项目内 `.workbuddy/memory/`。

### 关于本项目的 skill 与日志：**只放项目内，不装全局**（2026-09-20 起）

- 技能放仓库根目录 `.workbuddy/skills/`；
- 工作日志与长期约定放仓库根目录 `.workbuddy/memory/`；
- **不要把本项目的技能安装到全局 `~/.workbuddy/skills/`** —— 本项目自包含，
  换机器时整个文件夹拷走就应该带着它的技能与日志。
  （全局目录里原有的无关技能不受影响，不要顺手清理。）

---

## 0. 与 macOS 版的关系（先看这个）

| | macOS 版（`../00_For_Mac/`） | Windows 版（本目录） |
|---|---|---|
| 定时器 | LaunchAgent `com.workbuddy.wb-reward-catchup`（由 `00_For_Mac/install.sh` 生成） | 计划任务 `WorkBuddyRewardCatchup` |
| 触发 | `RunAtLoad` + `StartInterval=300` | `LogonTrigger`（延迟 1 分钟）+ 每 5 分钟重复 |
| 错过补跑 | launchd 唤醒后自动补发 | `StartWhenAvailable=true` |
| 解释器 | `sys.executable`（跑自己的那个，由 `install.sh` 写进 plist） | `winenv.default_python()`（同一语义，另切 python/pythonw） |
| 客户端版本 | 读 `Info.plist` | 直接解 `app.asar` 头部 |
| 本地通知兜底 | `osascript display notification` | PowerShell Windows Toast |
| 入口 | 命令行 / launchd | 双击 `.cmd` |

**两套的目标不是"一模一样"，而是"同名文件尽量逐字一致，差异集中在一处"。** 见第 5 节。

---

## 1. 前置条件

| 项 | 要求 | 说明 |
|---|---|---|
| Windows | 10 / 11 | 计划任务用 `schtasks`，Win7 未验证 |
| Python | **3.10 或更高** | 纯标准库，**不需要 pip install 任何东西** |
| WorkBuddy 桌面端 | 已安装 | 签到接口的凭据来自它的 `settings.json` |
| 微信 ClawBot | 在 WorkBuddy 里已绑定 | 推送通道。不绑也能跑，只是没有微信通知 |
| 管理员权限 | **不需要** | 计划任务注册在当前用户下 |

装 Python 时**务必勾选** 「Add python.exe to PATH」。
另外：别用 Microsoft Store 版 Python（那是个跳转别名，计划任务里会出问题）。

---

## 2. 拷机后五步

把整个 `00_For_Win` 文件夹拷到 Windows 任意位置（路径含中文/空格都没问题），然后：

```
第 1 步  双击 install.cmd          → 注册四个计划任务（主任务 + watchdog + 每日唤醒 + 白天唤醒）
第 2 步  双击 doctor.cmd           → 环境诊断，全绿即通
第 3 步  双击 run_now.cmd          → 手动跑一次，看到 JSON 摘要
第 4 步  （可选）双击 wait_token.cmd → 让微信也能收到通知副本
第 5 步  合上电脑，等第二天       → 到点自动跑
```

关于第 1 步：`install.cmd` 会注册计划任务并自动跑一次自检。
看不到输出就是成功（`pythonw.exe` 无控制台是**故意**的，避免闪黑窗）；要看结果就双击 `run_now.cmd`。
注册的是**四个**任务：主任务每 5 分钟、`WorkBuddyRewardWatchdog` 每 30 分钟、
每天 07:00 的 `WorkBuddyRewardWake`，以及白天每小时一次的
`WorkBuddyRewardDayWake`（07:00–23:00 各一个唤醒点，见第 4 节末）。
后两个是**能唤醒睡眠中电脑**的任务，前两个刻意不开唤醒（见第 4 节第 4 条）。

关于第 3 步：`run_now.cmd` 是**幂等**的。今天已经签过就什么都不做，安全，随便跑。

关于第 4 步：**不做也能跑通**，但微信收不到通知。原因见第 6 节「context_token」。

---

## 3. 文件夹地图

```
00_For_Win/
├── catchup.py            ← 入口：闸门 + 补跑 + 通知编排（计划任务跑的就是它）
├── winenv.py             ← ★ 所有 OS 差异的唯一集中地（本版核心新增）
├── notify_card.py        ← ★ 右下角通知卡片（点 ✕ 才消失；由 winenv 用 pythonw 拉起，
│                            是窗口子系统程序，**不会冒出 PowerShell / 控制台黑框**）
├── install.py            ← 计划任务注册 / 卸载 / 状态 / 立即运行（注册**四个**任务）
├── watchdog.py           ← ★ 存活监控：只查主脚本心跳与计划任务是否还在，异常弹 Toast
├── doctor.py             ← 一键环境诊断（只读，不领积分、不发消息）
├── selftest.py           ← 自检（项数随环境浮动，0 失败为准；Windows 真机最近 347 项；
│                            Windows / mac 都能跑，缺 mac 版时自动跳过跨版本比对）
│                            排除 `._*` 残留、不假设本机是 mac、不注册任何计划任务，
│                            且把日志隔离到 `.selftest_logs/`（跑完即删，绝不写 runtime/logs）
│
├── notify.py             ← 通知编排（微信直推 → 本地 Toast 降级）
├── clawbot.py            ← 腾讯 iLink 微信直推（零第三方依赖）
├── renew.py              ← 会话续期守护（只读桌面端游标，提前预警失效）
│
├── scripts/              ← 引擎，与 mac 版同名同结构
│   ├── main.py           ← 子命令入口（all / preflight / checkin / travel / status）
│   ├── api_discovery.py  ← ★ 接口前置校验（每次运行现读本机客户端，防接口漂移）
│   ├── checkin.py        ← 签到
│   ├── travel.py         ← 旅行派遣与领奖
│   ├── credentials.py    ← 凭据装载
│   └── http_client.py    ← HTTP（UA 跟随真实客户端版本）
│
├── install.cmd / uninstall.cmd / doctor.cmd
├── run_now.cmd / login.cmd / wait_token.cmd      ← 双击入口（纯 ASCII）
│
├── paths.py              ← runtime 路径与日志裁剪的唯一收口
├── win_paths.json        ← （可选）路径覆盖，仅当自动探测找不到时创建
│
└── runtime/              ← 运行时生成（可安全删除，会自动重建；不要放进 git）
    ├── config/
    │   ├── notify_config.json    通知配置（通道、每日上限、去重窗口、三档熔断）
    │   └── renew_config.json     续期守护配置
    ├── state/
    │   ├── state.json            当日去重状态
    │   ├── state.bak.json        state.json 的自动备份（损坏时回落）
    │   ├── notify_state.json     推送去重与配额计数（sent / local_sent 两张表）
    │   ├── renew_state.json      续期提醒节流
    │   ├── clawbot_state.json    微信 context_token / 自建登录凭据
    │   └── watchdog_state.json   watchdog 告警冷却记录
    ├── cache/
    │   ├── api_endpoints.json    接口发现结果缓存
    │   ├── task.xml              注册时提交的 XML 原文
    │   └── task_watchdog.xml     watchdog 注册 XML 原文
    └── logs/
        ├── catchup.log           运行日志
        ├── watchdog.log          watchdog 自己的日志（含通知投递失败的原文兜底）
        └── stdio.log             计划任务（无控制台）下的 stdout 兜底
```

---

## 4. 日常命令

双击 `.cmd` 就行。想精细控制就用命令行（在文件夹里开 PowerShell / cmd）：

```bat
:: 计划任务
py -3 install.py install                  :: 注册（默认主任务 + watchdog + 每日唤醒 + 白天唤醒，共四个）
py -3 install.py install --interval 15    :: 自定义主任务间隔
py -3 install.py install --no-watchdog    :: 只装主任务
py -3 install.py install --dry-run        :: 只生成 XML 不注册（可在 mac 上审阅）
py -3 install.py uninstall                :: 卸载（四个任务一起删，不删文件）
py -3 install.py status                   :: 看四个任务的定义 + 最近运行痕迹
py -3 install.py run                      :: 立即前台跑一次
py -3 install.py trigger                  :: 让计划任务立即执行一次（验证任务本身能跑）
py -3 install.py enable / disable         :: 启用 / 停用（不删除）

:: 诊断
py -3 doctor.py                           :: 全量诊断
py -3 selftest.py                         :: 自检（含平台文案适配断言）
py -3 watchdog.py status                  :: 看主脚本的存活判定（只读，不弹通知）

:: 引擎（只读，安全）
py -3 scripts\main.py status              :: 看积分状态 / 接口是否漂移
py -3 scripts\main.py preflight           :: 只跑接口前置校验

:: 通知通道
py -3 notify.py status                    :: 看通道与今日配额（不发消息）
py -3 notify.py                           :: 真发一条测试通知到微信
py -3 notify.py localtest                 :: 只测本机 Toast 这一级（不碰微信、不耗配额）
py -3 clawbot.py status                   :: 看凭据 / token / 游标
py -3 renew.py where                      :: 看它到底读了哪个游标文件
```

### 为什么除了主任务还有两个额外的计划任务

`catchup.py` 的**所有告警**都建立在「它自己跑起来了」这个前提上。如果计划任务被停用/删除、
Python 被卸载、文件夹被移走 —— 主脚本会**连告警机制一起静默死掉**，你不会有任何感知。
这是整套系统唯一的、无法靠主脚本自己解决的盲区。

所以 `install.py` 会注册**四个**独立任务：

| 任务名 | 触发 | WakeToRun | 干什么 |
|---|---|---|---|
| `WorkBuddyRewardCatchup` | 登录 + 每 5 分钟 | **false** | 真正干活的主脚本 |
| `WorkBuddyRewardWatchdog` | 登录 + 每 30 分钟 | false | 只查 `state.json` 心跳（阈值 90 分钟）与主任务是否还在，异常弹 Toast |
| `WorkBuddyRewardWake` | **每天 07:00 一次** | **true** | 电脑睡着时把它叫醒去签到 —— Windows 上「睡着也能签到」的第一个入口 |
| `WorkBuddyRewardDayWake` | **每天 07:00–23:00，每小时一个唤醒点** | **true** | 补上面那个够不着的白天时段：本机实测**空闲 1~2 分钟就自己睡了**，白天睡过去后除它之外没人叫醒（2026-09-21） |

> ★ `WorkBuddyRewardDayWake` 的 17 个唤醒点是 **17 个并列的 `CalendarTrigger`**，
> **不是**「一个触发器 + `Repetition PT60M`」。后者任务照样按时跑、XML 也挑不出错，
> 但 `powercfg /waketimers` 里查无此项，电脑睡着后**一个都叫不醒**（实测睡了 48 分钟
> 直到用户按电源键才醒）。原因：`WakeToRun` 只对触发器**自己的计划起始时刻**生效，
> `Repetition` 派生出来的重复时刻不会各自去武装唤醒定时器。自检第 2 节钉死了这条。

`watchdog.py` **不 import 任何项目模块**（连发通知都自己内联实现），免得同一个故障把
主脚本和监控一起干掉。这条约束由自检第 8d 节用 AST 检查真实的 `import` 语句来守着。
它自己无人监控 —— 这是原理性的（无限套娃），它只能把「最可能发生的静默死亡」变成
一条你看得见的本地通知。

#### 为什么唤醒器必须单独一个任务，而不是给主任务开 WakeToRun

`<WakeToRun>` 位于任务 XML 的 `<Settings>`，**作用于该任务的所有触发器**。
主任务带 `Repetition PT5M`，给它开唤醒等于让笔记本**每 5 分钟被叫醒一次** ——
比不设还糟，且与「减少对系统的打扰」正好相反。所以这件事只能交给一个
「每天只触发一次」的任务来做。

唤醒时刻定在 **07:00**，与 `catchup.py` 静默期的结束时刻（`QUIET_FROM`）、
签到闸门的开门时刻（`WINDOW1`）同值 —— 醒来的那一瞬间就是能干活的时刻，
不浪费这次唤醒。

不想让它叫醒电脑：`py -3 install.py install --no-wake`。代价是睡眠期间不会签到，
得等机器自然唤醒后才补跑。

### 静默期：00:00–07:00 与「当日收工后」完全不做事

从 2026-09-19 起，`catchup.py` 与 `watchdog.py` 都带一条静默规则，两版语义一致：

| 情形 | 主脚本行为 | watchdog 行为 |
|---|---|---|
| **00:00–07:00** | 只读一次系统时间就退出，**连锁都不抢、日志都不写** | 不做 stale 判定（此时心跳陈旧是设计预期） |
| **当日签到与旅行奖励都到手后** | 之后每次触发同样立即退出，不做续期检查、不做端点探测 | 同上，不报 stale |
| 其他时间 | 照常全量运行 | 照常判定 |

**为什么要有这条**：电脑睡眠时，系统的维护唤醒（mac 的 Power Nap / Windows 的唤醒器）
会定期把机器叫起来几秒，计划任务随之被触发。这些触发里绝大多数根本无事可做，
照常跑完一套流程就是纯空转。静默把它压成「启动解释器 → 看时间 → 退出」。

**静默只省开销，不改任何闸门逻辑** —— 签到与领取的判据一个字没动。

> 注意：静默**不减少系统自身的唤醒次数**，只是让我们在那些唤醒窗口里不干活。
> Windows 的唤醒次数本身就由上面那个每日唤醒器决定（每天最多 1 次）。

> 关于命令前缀：**Windows 上用 `py -3`**。脚本内部那些与 mac 版逐字一致的共享文件
> （主要是 `scripts/main.py`）的文档字符串里写的是 `python3`，那是 mac 版原文 ——
> 在 Windows 上等价于 `py -3`，照着上面这张表用即可。
> 另外：**通知与报错文案里的操作指引是按平台生成的**，Windows 上会直接告诉你
> 「双击 login.cmd / doctor.cmd」，不会给你一条跑不通的命令。

---

## 5. ★ Mac 版 ↔ Win 版 同步规则

> 用户要求：**以后任何优化，两套一起改**。这一节就是给未来的自己（或 AI）看的操作手册。

### 5.1 分层：哪些文件必须成对改，哪些可以不一样

| 类别 | 文件 | 改动要求 |
|---|---|---|
| **必须逐字一致** | `scripts/main.py`、`scripts/checkin.py`、`scripts/travel.py`、`runtime/config/notify_config.json`、`runtime/config/renew_config.json` | 改 mac 版后**原样复制**到另一边。这些文件**不含任何平台判断**（已核实） |
| **必须逐字一致（但自带平台分支，属刻意设计）** | `scripts/credentials.py`、`paths.py` | 它们**必须**在两侧逐字一致，而两侧又都要跑 → 所以平台分支写在文件**内部**（`sys.platform` / `os.name` 三选一），**不能**改调 `winenv.xxx()` —— `winenv.py` 只存在于 Win 版，改调它就让 mac 版直接 import 失败。把三平台分支收在同一个文件里，正是避免两版分叉的手段 |
| **必须成对改，允许少量差异** | `catchup.py`、`notify.py`、`renew.py`、`clawbot.py`、`scripts/api_discovery.py`、`scripts/http_client.py` | 逻辑必须同步，但平台相关的**几行**允许不同 |
| **同名但各自实现（不逐字比对）** | `watchdog.py` | 它是「监控主脚本还活着没」，所以**必须独立**：mac 用 `launchctl` + `osascript`，Win 用 `schtasks` + PowerShell Toast。两边的判断逻辑（心跳阈值、告警冷却、问题分类）必须一致，实现各写各的。**连操作系统的判断都只能自己写**（它不许 import 任何项目模块，见 5.2 第 1 条） |
| **各自独立** | `winenv.py`（仅 Win）、`install.py` / `doctor.py` / `selftest.py` / `*.cmd`（仅 Win）、`install.sh`（仅 mac —— 安装时按实际路径现场生成 plist） | 不参与同步 |

### 5.2 硬规则（违反就会累积技术债）

1. **任何新增的平台判断（`sys.platform` / `os.name` / `osascript` / `CREATE_NO_WINDOW` / 盘符路径 / `%APPDATA%`），只许写在 `winenv.py` 里。**
   其它文件只能调用 `winenv.xxx()`。`selftest.py` 第 8 节会**自动检查**这项：
   ```
   ✗ osascript 只出现在 winenv.py（平台差异已收口）
   ```
   一旦你在别处写了 `osascript`，自检立刻报错。

   > **两条明列的例外**（2026-09-20 补齐说明，此前文档只写了规则、没写例外，读起来与代码事实相反）：
   > 1. **`scripts/credentials.py` 与 `paths.py`** —— 它们是「必须逐字一致」的共享文件，
   >    mac / Win 两侧都要跑，所以平台分支写在文件内部（见 5.1 表第 2 行）。
   >    在这里改成 `winenv.xxx()` 会直接让 mac 版 import 失败，**那不是优化，是把两版拆了**。
   > 2. **`watchdog.py`** —— 它刻意不 import 任何项目模块（免得同一个故障同时干掉主脚本与监控），
   >    所以只能用 `sys.platform` 自己判断；这正是它独立性的代价。
   >    注意它是唯一一处**故意**用 `winenv.IS_WIN` 而不调 `platform()` 的地方之一
   >    （另一处是 `winenv.pid_alive`，理由见那边注释：Windows 上 `os.kill(pid,0)`
   >    会真的杀进程，这里必须反映**真实**操作系统、不能被测试桩翻过去）。
   >
   > 除这两类之外，新写的平台判断一律进 `winenv.py`。

2. **禁止在 Win 版任何文件里出现 mac 用户路径**（`/Users/<name>...`、`.workbuddy/binaries/python`）。
   自检第 8 节会扫全目录。同理，mac 版也不该出现 `C:\` 或 `%APPDATA%`。

3. **解释器路径永远不硬编码**（两侧都是）。mac 版用 `sys.executable`（跑自己的那个），
   Win 版用 `winenv.default_python()`（同语义，另切 python/pythonw）。这样整个文件夹
   拷到任何机器都能直接跑。
   （2026-09-18 之前 mac 版 `catchup.py` 顶部写死过一条机器专属的绝对路径，已改掉，
   所以这条现在对两版同时成立。）

### 5.3 改完后的验证动作（照着做，两分钟）

> **在 Windows 交付机上也能跑。** `selftest.py` 与 `doctor.py` 都不假设本机是 mac：
> 路径候选、平台文案、计划任务形态都会按**真实平台**断言（2026-09-20 修，此前会
> 因为「假设本机是 mac」而恒报假失败）。下面 ② 那条 `diff` 只在有 mac 版时才有意义 ——
> **独立交付副本（没有 `../00_For_Mac/`）会自动跳过跨版本比对，不会因此判失败。**

```bash
# 在 mac 上就能做，不需要 Windows 机器

cd ".../09_Workbuddy自动签到领积分/00_For_Win"

# ① 自检：必须 0 失败（项数随环境浮动；Windows 真机最近基线为 347 项）
python3 selftest.py

# ② 差异清单：逐字一致的那 6 个文件差异必须是 0
#    （mac 版在同级的 00_For_Mac/，2026-09-18 整理目录后如此）
for f in scripts/main.py scripts/checkin.py scripts/travel.py \
         scripts/credentials.py ../00_For_Mac/runtime/config/notify_config.json \
         ../00_For_Mac/runtime/config/renew_config.json; do
  echo "$(diff "../00_For_Mac/$f" "$f" | grep -c '^[<>]')  $f"
done
# 期望输出全部为 0

# ③ 编译校验
python3 -m py_compile *.py scripts/*.py && echo "COMPILE OK"
```

`selftest.py` 第 8 节末尾会检查两套的**文件配对**（用字节比对分出「逐字一致」与「存在差异」两组，
并打印两组的文件名），但它**不打印差异行数** —— 行数要照上面 ② 的循环自己跑。

> **从 mac 拷贝后先清一遍 `._*`。** macOS 往 U 盘 / 网络共享 / exFAT 上写文件时，会为每个
> 文件额外生成一个 `._同名` 的 AppleDouble 伴生文件（二进制，头 4 字节 `00 05 16 07`）。
> 它们不参与运行，但会让所有「按后缀扫描」的检查炸掉（自检的 `.py 可编译` / `.cmd 纯 ASCII`
> 都踩过），也让交付物看起来不干净。清理：`find . -name '._*' -delete`；
> 自检第 8 节现在会**主动点出**残留数量并给出这条命令。

### 5.4 只改 mac 版时，怎么判断 Win 版要不要动？

问三个问题：

1. **改的是纯逻辑吗？**（接口路径、判定条件、文案）
   → 要动，而且两个版本改法一样。
2. **改的是平台相关的东西吗？**（通知、路径、编码、进程）
   → Win 版改 `winenv.py`，mac 版改它自己那几行，**逻辑对齐、实现分开**。
3. **只跟 mac 的 launchd / plist 有关吗？**
   → Win 版**不动**，但要检查 Win 版对应语义是否已有等价物（见表格第 0 节）。

### 5.5 已知的允许差异（不必强行抹平）

| 文件 | 差异内容 | 原因 |
|---|---|---|
| `catchup.py` | 解释器取值、`setup_stdio()`、通知文案里的操作指引、锁的残留判定（`winenv.pid_alive()`） | 平台 |
| `notify.py` | 本地通知调用（`winenv.native_notify` vs `osascript`）、`status` 里的本机通知自测实现 | 平台 |
| `renew.py` | 游标目录候选、`status` 里解释器路径显示 | 平台 |
| `clawbot.py` | `settings.json` 路径候选、登录引导文案、HTML 字体栈 | 平台 |
| `api_discovery.py` | asar 路径候选、客户端探测 | 平台 |
| `http_client.py` | UA 版本探测 | 平台 |

**如果哪天发现差异行数暴涨**（比如 `catchup.py` 从 154 行涨到 300 行），说明逻辑开始分叉了，
应当回头把公共部分抽出来，而不是继续各改各的。

> 参考基线（2026-09-19 复测，本次加静默逻辑后刷新）：`catchup.py` 154、`notify.py` 190、
> `renew.py` 149、`clawbot.py` 83、`scripts/api_discovery.py` 55、`scripts/http_client.py` 17。
> **口径必须一起写**，就是上面 5.3 节 ② 那条命令（`diff | grep -c '^[<>]'`）。
> 同一组文件换个口径能差出好几成 —— 用 `diff | wc -l` 数出来是 215 / 254 / 181。
> 历史上正是因为没写口径，同一个项目里出现过两套互相矛盾的基线（103 / 92 与 142 / 193），
> 谁也不知道该信哪个，这个预警就废了。**刷新数字时同一行写上口径和日期。**
> 这些数字应该**缓慢**增长（每加一处平台差异才涨几行），跳跃式上涨就是分叉的信号。
> 反过来说：**纯业务逻辑的改动不该让这些数字涨** —— 若发现某次「两边都同步改了」
> 却让数字变大了，多半是两边措辞没对齐（本次就踩过：`catchup.py` 一度涨到 165，
> 把注释与 docstring 逐字对齐后回落到 154）。

---

## 6. 排错表

| 现象 | 原因 | 处理 |
|---|---|---|
| `install.cmd` 报「Python 3 was not found」 | 没装或没勾 PATH | 重装 Python 并勾选 Add to PATH，然后重开窗口 |
| 计划任务存在但不跑 | ① 笔记本默认「仅交流电启动」② `StartWhenAvailable` 未开 | 本版的 `install.py` **已显式关掉这两个坑**；若任务是被别人手动改过，重跑 `install.cmd` 覆盖 |
| 到点没签到 | 闸门未开（`≥07:00` 签到；领取要等猫到达，**无固定时刻兜底**），或当日已完成 | `run_now.cmd` 手动验证；看 `runtime/logs/catchup.log` 与 JSON 摘要里的 `claim_open` |
| 微信收不到通知 | 三种可能：**context_token 缺失** / **投递被拒** / 登录会话失效 | 先看 `runtime/logs/catchup.log` 里的 `notify[...] sent=… wechat=… reason=…` 与 `py -3 notify.py status` 的 `last_send_error` |
| 微信收不到且报 `ret=-2 prepare failed` | **投递被拒**：服务端拒绝为主动消息建立会话。**成因未确认**（见下） | ① 在微信里给机器人发任意一条消息（如 `1`）；② 让 WorkBuddy 桌面端保持运行。二者都可恢复，**不需要重新扫码** |
| 微信收不到且报 `errcode=-14` | 登录会话过期 | `login.cmd` 扫码，或直接在 WorkBuddy 里重连「微信助理」 |
| 提示「接口疑似已变更」 | 腾讯改了接口路径 | 看 `runtime/cache/api_endpoints.json` 的 `history`；`doctor.py` 会显示现读到的端点。本版已做前置校验，正常情况下会自动跟上 |
| 提示「当前无活动」 | 可能是真无活动，也可能是接口返回全零 | 本版已把这种情形判为 `suspect` 而非 `no_activity`，并**主动告警**，不会静默漏签 |
| 签到/旅行报 `Connection refused` 或 `self-signed certificate in certificate chain` | 常见原因是系统代理端口未启动，或代理开启了 TLS 拦截。积分接口默认**绕过系统代理直连**；确需走系统代理时设置 `WORKBUDDY_PROXY_MODE=system` |
| 路径找不到（settings / asar） | 客户端装在了非标准位置 | 跑 `doctor.cmd` 看它找过哪些路径 → 建 `win_paths.json` 填写正确路径 |
| `doctor.cmd` 报「找不到 WorkBuddy 客户端」，但客户端确实装了 | 旧版只靠 `%PROGRAMFILES%` 找安装目录，而该变量在某些启动上下文里**根本不存在** | 2026-09-20 已内置 `C:\Program Files` / `C:\Program Files (x86)` 固定兜底（见第 7 节第 5 条）。若仍找不到，按上一行用 `win_paths.json` 显式指定 |
| 提示「接口已自动切换」但你什么都没改 | 旧版把「客户端临时读不到而退回内置兜底表」也判成了端点变化 | 2026-09-20 已修：只有两次都是权威扫描才判定变化（见第 9 节末）。看到 `runtime/cache/api_endpoints.json` 里的 `degraded_scan_at` 就说明那几次只是**没读到**，不是端点变了 |
| 双击 `.cmd` 闪一下就没了 | 脚本报错但窗口关太快 | 从 cmd 里手动运行同名 `.py`，或在文件夹里开 cmd 跑 `install.cmd` |
| **已登录**却报「读不到登录态」 | 旧版 `credentials.py` 在 Windows 上只找 `%APPDATA%`（Roaming），而桌面端实测把明文登录态写在 `%LOCALAPPDATA%`（Local） | 2026-09-20 已修：两个布局都列、逐个探测。自查：`dir "%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth"` 看有没有 `workbuddy-desktop.info` |
| **把文件夹挪了位置之后就不再签到了** | 计划任务里存的是**绝对路径**，挪动文件夹后它仍指向旧位置；此后每次触发都以 `0x8007010B`（目录名无效）**静默失败** —— `pythonw` 没有控制台，旧位置的日志也不会再被写 | 在新位置**重跑一次 `install.cmd`** 覆盖注册（2026-09-21 本次搬迁就是这么修的）。`py -3 install.py status` 会显示任务实际指向哪个脚本；现在 `doctor.cmd` 会直接报「计划任务指向的是旧路径」，`watchdog` 也会报 `wrong_path` / `task_failed` |
| **任务显示「已启用」、`下次运行时间` 也在往后滚，但从来没真正跑过一次** | ★ 计划任务 XML 的 `<Repetition>` 少了 `<Duration>` —— 省略它**不等于「无限重复」**，而是该触发器每次都被记成「错过的运行」而跳过 | 2026-09-20 已修（第 7 节第 6 条）。自查：`powershell -c "Get-ScheduledTaskInfo -TaskName WorkBuddyRewardCatchup \| fl NextRunTime,LastRunTime,NumberOfMissedRuns"`，**若 `LastRunTime` 一直不动、`NumberOfMissedRuns` 一直涨**，就是这个毛病 |
| `catchup.log` 显示 `sent=True` 但你没收到微信 | `sent` 的语义是「**某个**通道送到了」（微信**或**本机弹窗），只有 `wechat=True` 才代表手机微信真收到了 | 看同一条日志里的 `wechat=`。`sent=True wechat=False channel=native` = 只弹了本机窗、微信没送到。2026-09-20 修：此前两个降级分支连 `sent` 都不置真，日志会自相矛盾 |
| 日志里写 `无可用微信通道（请在 WorkBuddy 绑定 ClawBot…）` | 本机既没绑定 ClawBot，也没配 pushplus / serverchan —— **积分照领，只是收不到微信通知**（会降级成本机 Toast） | `py -3 notify.py status` 看 `channel_order`（为 `[]` 就是没通道）。恢复二选一：① 在 WorkBuddy 里绑定「微信助理 / ClawBot」；② 在 `runtime/config/notify_config.json` 填 `pushplus_token` 或 `serverchan_key` |
| **明明在 WorkBuddy 里绑定过微信，工具却说「未绑定」** | 工具要的是 `settings.json` 里的 **ClawBot bot 凭据**（`botToken` + `userId`），而且桌面端在不同版本里写过**两种结构**：`claw.users.<uid>.channels.weixinClawBot` 与顶层 `claw.channels.weixinClawBot`。旧实现只读前者 | 2026-09-20 已改成**两种都认**（自检第 8l 节钉住）。自查：`py -3 clawbot.py status` 看 `found` 与 `settings_used`；若 `found: false` 而你以为绑过，多半是：① 绑的是「微信公众号 webhook」而不是「微信助理 / ClawBot」（`claw.channels.wechatmp` **不算**）；② 绑完没重启桌面端，配置还没落盘 |
| `watchdog` 报 `healthy` 但主脚本其实早停了 | ① 任务被 `/Change /DISABLE` 或删除；② PowerShell 被策略禁用，状态判不了 | 2026-09-20 修：改用 PowerShell 的 `State` 枚举判定（此前中文系统上**任务真被停用也报健康**）。看 `runtime/logs/watchdog.log` 里有没有「无法判定计划任务状态」——有就说明两条路都读不到，它只能保守放行 |
| **本机通知一直挂在屏幕上，要点叉才消失** | 2026-09-21 起这是**设计行为**：微信通道不可用时本机 Toast 是唯一可见通道，而签到结果偏偏弹在没人盯屏幕的时刻，所以默认**常驻**（`scenario="urgent"` + `duration="long"`） | 想恢复「几秒后自动消失」：设环境变量 `WORKBUDDY_TOAST_PERSISTENT=0`（手动运行立即生效；计划任务需重跑一次 `install.cmd` 或在任务里带上该变量）。若系统拒绝常驻写法，工具会**自动退回普通 Toast**，不会因此一条都弹不出来 |
| **早上没看到通知弹窗** | 先分清「投递」与「显示」：**显示器关闭 / 没人在屏前时 Windows 不弹横幅**，通知只进通知中心（07:00 你多半还没坐到屏幕前） | 按 `Win + N` 看通知中心；或用本 README「通知投出去了，但屏幕上没看到」一节里的 `History.GetHistory` 命令查投递记录。**只有通知中心里也没有**才是真故障 → `py -3 notify.py status` 看 `local_notify_check`。开关清单与试用建议见同节 |
| **睡醒 / 解锁后没有重新弹出通知** | 补弹要满足两个条件：① 投递那一刻判定「你不在场」（键鼠默认 5 分钟无操作）；② 你回来之后计划任务触发一次（≤5 分钟）才会补弹 | 看 `runtime/state/notify_reshow.json` 有没有积压、`catchup.log` 里有没有「补弹 N 条」；确认 `WORKBUDDY_RESHOW` 没被设成 `0`。熄屏/锁屏期间**不会**当场弹横幅（Windows 行为），补弹发生在那之后 |
| **电脑睡眠时没有按时被唤醒去签到** | 先分清它到底是**睡眠(S3)**还是**休眠(S4)** —— 唤醒定时器**叫不醒休眠**。实测 `rundll32 powrprof.dll,SetSuspendState 0,1,0` 在本机进的是 S4 | 看事件 42 的 `TargetState`（**4=S3 / 5=S4**）：`Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Microsoft-Windows-Kernel-Power'} -MaxEvents 5`；再 `powercfg /waketimers` 确认唤醒任务的定时器已武装；`powercfg -q SCHEME_CURRENT SUB_SLEEP RTCWAKE` 的交流/直流都必须为「启用」 |
| **白天睡过去了没人叫醒（只有 07:00 会醒）** | 2026-09-21 起由 `WorkBuddyRewardDayWake` 补上：07:00–23:00 每小时一个唤醒点。**若它的定时器不在** `powercfg /waketimers` **里，就是白装了** —— 典型原因是又退化成「一个触发器 + `Repetition`」（重复实例不武装唤醒定时器，见第 4 节 4b） | `powercfg /waketimers` 应该看到 `WorkBuddyRewardDayWake`；没有就重跑 `install.cmd`。想改密度：`python install.py install --daywake-every 30`（每 30 分钟一个点）或 `--daywake-points "07:30,12:00,19:30"`；不想要就 `--no-daywake` |

### 微信推送不出去的两种故障（恢复动作完全不同，别混）

| | 登录会话失效 | 投递被拒 |
|---|---|---|
| 响应 | `errcode=-14 / session timeout` | `ret=-2 / errmsg=prepare failed` |
| 含义 | 登录态没了 | 登录态**正常**，但服务端拒绝为这条主动消息建立会话 |
| 恢复 | **重新扫码**（`login.cmd`） | **发一条消息** / **保持桌面端运行**（都不需要扫码） |
| 验证 | `py -3 clawbot.py probe` 会失败 | `probe` **正常**（`getconfig` / `sendtyping` 都返回 `ret:0`，只有 `sendmessage` 被拒） |

### ⚠️ 不要把 `prepare failed` 说成「会话窗口 N 小时过期」（2026-09-18 更正）

早先把它归因为「距你上次给机器人发消息超过会话窗口时长」，写下过「9 小时内一定能发、
32 小时后发不出」。**当天下午的数据就把这个模型否掉了：**

| 时刻 | 距上次用户发消息 | 结果 |
|---|---|---|
| 09-17 09:30 | 9.1 小时 | ✅ 送达 |
| 09-18 09:14 | 32.8 小时 | ❌ `prepare failed`（**刚过夜休眠醒来**） |
| 09-18 11:20 | 34.9 小时 | ❌ `prepare failed` |
| 09-18 15:12 | **38.7 小时** | ✅ **送达** |

**距上次发消息更久反而发成功了**，所以「按时间过期」站不住。两个观测有个被忽略的共同点：
失败那两次都在机器**过夜休眠醒来后不久**（`catchup.log` 里 00:42 → 09:14 有 8.5 小时空档）；
而成功那次，桌面端游标文件（`%USERPROFILE%\.workbuddy\claw-state\weixin\*.cursor.json`）正在被
**持续刷新** —— `renew.py` 的说明里写着「桌面端正常轮询间隔约 18 秒」。

**目前最合理的推测**：能不能发取决于**本机客户端是否在持续轮询这个 bot**（服务端那边这个
「会话」还是不是活的），而不是「你多久没发过消息」。样本仍太少，这仍是推测，不是结论。

**实践含义**：保持 WorkBuddy 桌面端运行，比「记得偶尔发条消息」更可能让推送持续可用；
排查时别拿「多久没发消息」当判据。代码里的熔断时长（120/60/20 分钟）只是防止把 iLink
每日配额烧在注定失败的请求上的工程取舍，**不代表我们已知道成因**。

### 去重记两张表（2026-09-18 修的真实缺陷）

**旧行为**：推送失败 → 降级成本机 Toast → **但去重指纹照样被记成「已送达」**。
后果是：等你后来发消息把会话窗口重新打开，那条消息**再也不会被尝试推送** ——
「降级过一次」变成了「永久放弃」。积分确实到手了，但那一条到账消息你永远看不到。

**现在的语义**：指纹按**通道**分开记，两张表各管一件事 ——

| 表 | 什么时候写 | 拦住什么 | 放行什么 |
|---|---|---|---|
| `sent`（已送微信） | **只有真的送到微信**时 | 拦住所有后续重试（含本机，避免重复弹） | — |
| `local_sent`（仅本机） | 只弹过本机 Toast 时 | 只拦住本机重复弹窗 | **放行微信通道** → 通道恢复后自动补发 |
| （都不写） | 微信和本机**都失败**时 | — | 留给下一次运行继续补发 |

一句话：**只有真的送到微信，才配记「已送达」。** `py -3 notify.py status` 里的
`dedupe_records` 会把两张表的条数分开显示。

另外，熔断期内**只要你给机器人发了一条消息**，冷却会立刻失效（而不是傻等满 60/120 分钟）。

### 本机通知：微信不可用时的唯一可见通道（2026-09-21）

微信通道当前不可用时，**本机通知就是唯一能让你看到结果的通道**，而它恰恰最容易被忽略：

- 普通 Toast 只在屏幕上停留几秒就缩进「通知中心」，可签到结果偏偏弹在
  **没人盯着屏幕的时刻**（07:00 唤醒后、猫到达那一刻）—— 没看到就等于没通知。
- 所以 2026-09-21 起，**默认通道改成"必须点掉的弹窗"**（见下一节），
  Toast 退居兜底并尽力做到"常驻"（`scenario="urgent"` + `duration="long"`）——
  实测它在这台机器上**仍会被系统自动收走**，这也是为什么主力通道要换成弹窗。
- 自查：`py -3 notify.py localtest` 只测本机这一级（不碰微信、不耗配额）；
  `py -3 notify.py status` 的 `verdict` / `fallback_log` 说明它当前是否弹得出来
  （投递失败时正文会落到 `logs/notify_fallback.log`，保证信息不丢）。

### 让通知「一定会被你看到」的三层设计（2026-09-21，用户要求）

背景：**通知投递成功 ≠ 你看到了**。实测两件事：
① 熄屏 / 锁屏 / 睡眠期间 Windows **不弹横幅**，通知只进通知中心（07:00 的签到通知就是这样"没人看到"的）；
② **Toast 即使写了 `scenario="urgent"` + `duration="long"`，在这台机器上依然"过一会儿就自己收走"**。

所以本机通知**默认走"必须点掉的弹窗"**，Toast 与补弹队列是它的兜底：

| 层 | 通道 | 行为 | 何时启用 |
|---|---|---|---|
| ① | **右下角卡片（默认）** | 无边框卡片贴在**右下角**工作区边缘，置顶，**点右上角 ✕ 才消失**（不自动消失）；由 `winenv` 另起一个 **`pythonw`** 进程弹出 —— **不出现 PowerShell / 控制台黑框**，也绝不阻塞每 5 分钟一次的主脚本 | 默认通道（`WORKBUDDY_ALERT_STYLE=dialog`） |
| ② | 常驻 Toast | `scenario="urgent"` + `duration="long"`（尽力而为：实测仍可能被系统自动收走） | 弹窗不可用时自动退回；或手动设 `WORKBUDDY_ALERT_STYLE=toast` |
| ③ | 补弹队列 | **走 Toast 时**如果"你不在场"（键鼠 5 分钟无操作），记下原文；等你回到机器前再原样弹一次 | 只在第 ② 层生效时使用（走弹窗则不补弹，避免同一条弹两次） |

弹窗最多同时堆 **3 个**（再多就退回 Toast + 补弹队列），免得离开几天回来被几十个窗口糊满；
每个弹窗在 `runtime/state/notify_dialog_<pid>.json` 留一个标记，进程退出时自删，
残留标记按 PID 存活清理（不会越积越多）。

补弹队列文件：`runtime/state/notify_reshow.json`（运行时文件，不进交付物），
上限 **8 条 / 72 小时 / 每次最多补 3 条**，损坏或读不到一律当作空队列（绝不抛异常）。

```bat
set WORKBUDDY_ALERT_STYLE=dialog   :: 默认：必须点掉的弹窗
set WORKBUDDY_ALERT_STYLE=toast    :: 换回"角落小横幅"（会自动消失的那种）
:: 下面三个只对 toast 模式有效
set WORKBUDDY_TOAST_PERSISTENT=0   :: 放弃"常驻"，几秒后自动消失
set WORKBUDDY_RESHOW=0             :: 关掉补弹层
set WORKBUDDY_RESHOW_IDLE_SEC=600  :: 「不在场」判定阈值（秒，默认 300 = 5 分钟）
```

> **踩过的坑（已修，2026-09-21）**：弹窗进程的 `Popen` 同时传了显式 `creationflags`
> 和 `**subprocess_flags()`（里面也有这个键）→ `TypeError` 被 `except` 吞掉 →
> **静默退回 Toast**：弹窗永远不出现，而日志、退出码、自检全都看不出来。
> 现在合并成一个 `creationflags`，并在自检里**直接钉住 `Popen` 的入参**（第 8o 节）。

> **为什么补弹阈值是 5 分钟**：短暂离开（<5 分钟）时横幅/弹窗本来还在屏幕上，
> 不需要再补；真正会漏的是熄屏/锁屏/睡眠这类长离开。阈值太小 → 重复打扰，太大 → 漏看。

> **对静默期的一处微调**：补弹检查放在静默判定**之前**（夜里回到机器前也该看到积压的通知）。
> 代价极小 —— 正常情况下队列文件不存在，只多做一次文件存在性检查；
> 静默期的其余行为（不抢锁、不写日志、不读 state、不联网）**完全不变**。

### 「通知投出去了，但屏幕上没看到」——先分清投递与显示（2026-09-21 实测）

**不是一回事。** `notify.py` 里的 `sent=true` 只代表**投出去了**，不代表屏幕上闪过一条横幅。
2026-09-21 的实例：07:00 的签到通知与 08:05 的领奖通知**都成功投递**（通知中心里查得到），
但用户没看到横幅 —— 原因是**那时显示器是关闭的 / 没人在看屏幕**：
Windows 在显示器关闭时不弹横幅，通知只进「通知中心」。**这是系统行为，不是本项目故障。**

**怎么确认「到底送到没送到」（不依赖肉眼）**：按 `Win + N` 打开通知中心，或直接查历史——

```powershell
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
[Windows.UI.Notifications.ToastNotificationManager]::History.GetHistory($appId) |
    ForEach-Object { ($_.Content.GetXml() -replace '<[^>]+>', ' ' -replace '\s+', ' ').Trim() }
```

**显示层的开关**（2026-09-21 已逐项审计并**显式打开**；审计结论：原配置里**没有任何抑制项**）：

| 开关 | 位置 | 现状 |
|---|---|---|
| 全局通知总开关 | `HKCU\...\PushNotifications` → `ToastEnabled` | **设为 1** |
| 全局通知（新键） | `HKCU\...\Notifications\Settings` → `NOC_GLOBAL_SETTING_TOASTS_ENABLED` | **设为 1** |
| 应用级（本项目的通知来自 PowerShell 的 AppID） | `...\Settings\{1AC14E77-…}\WindowsPowerShell\v1.0\powershell.exe` → `Enabled` / `ShowInActionCenter` / `ShowBanner` | **设为 1** |
| 锁屏上也显示 | `...\Settings` → `NOC_GLOBAL_SETTING_ALLOW_TOASTS_ABOVE_LOCK`、`..._CRITICAL_...` | **设为 1**（07:00 机器通常还在锁屏） |
| 勿扰（专注助手） | CloudStore `default$windows.data.notifications.quiethourssettings` | **键不存在 = 从未开过勿扰**（若你手动开过，请看「设置 → 系统 → 通知」） |

> ⚠️ `Windows.UI.Shell.FocusSessionManager` 在本机版本上不可用，
> 所以「勿扰是否开着」**没有**稳定的程序化读法（`notify.py status` 里的 `focus: unknown`
> 是如实说明，不是偷懒）。

### 试用建议（第一次在真机上跑时按这个顺序看）

1. 装好后**先手动跑一次** `run_now.cmd`（当天已签到它是幂等的），
   当场确认你能不能看到通知；看不到就按 `Win + N` 看通知中心里有没有。
2. **早上起来没看到弹窗 ≠ 失败**：先按 `Win + N`。07:00 时你多半还没坐在屏幕前，
   （甚至显示器是关的）横幅不弹是正常的。
3. 只有**通知中心里也没有**才是真问题：跑 `py -3 notify.py status` 看
   `local_notify_check.verdict` / `toast_enabled_registry` / `fallback_log`，
   并检查「设置 → 系统 → 通知」里 `Windows PowerShell` 这一项的开关。
4. `toast_enabled_registry` 应为「开」。若显示「关（本机通知会被系统丢弃）」，
   说明系统通知总开关被关了 —— 此时**所有**本机兜底都会静默消失（微信也没配的话就彻底没感知）。
5. 没绑 ClawBot 时，**本机通知是唯一可见通道**，不要关掉它；
   想彻底不依赖屏幕，就去绑「微信助理 / ClawBot」。

### context_token 专项（微信收不到通知的头号原因）

主动推送**必须**带 `context_token`。没有它的请求，服务端照样返回 `message_id`、
照样消耗每日配额，但**消息根本不会出现在微信里** —— 这是最容易误判成"通道正常"的坑。

token 只在**你给 bot 发消息的那一刻**随长轮询下发，纯轮询永远拿不到。所以：

1. 打开微信，进入 WorkBuddy / ClawBot 会话
2. 发任意一条消息（比如 `1`）
3. **立刻**双击 `wait_token.cmd`（长轮询 60 秒捕获）

捕获后写入 `clawbot_state.json`。token 是临时的，失效后重复上面三步。
判断有没有：`py -3 clawbot.py status` 看 `context_token` 字段。

---

## 7. 六个 Windows 特有的坑（本版已处理，记录备查）

1. **计划任务默认「仅在使用交流电时启动」**
   → 笔记本一拔电源，任务被**静默跳过且不报错**。`install.py` 显式设 `false`。

2. **计划任务默认不补跑错过的触发**（`StartWhenAvailable` 默认 false）
   → 不打开就退化成"必须卡着点开机"。`install.py` 显式设 `true`，对齐 mac 版 launchd 的补发能力。

3. **任务 XML 的编码**
   → XML 用 **UTF-16** 写出（`schtasks /XML` 要求），彻底绕过 cmd 代码页把中文打成乱码的问题。
   解释器与脚本路径**一律带引号**（路径常含空格与中文）。
   执行体用 `pythonw.exe`（无控制台）→ 不闪黑窗，输出落到 `runtime/logs/stdio.log`。

4. **想让电脑睡着也能签到，不能直接给主任务开 `WakeToRun`**（2026-09-19 新增）
   → 直观做法是 `<WakeToRun>true</WakeToRun>`，但这个开关位于 `<Settings>`，
   **作用于该任务的所有触发器**；主任务带 `Repetition PT5M`，开了它 = 每 5 分钟把电脑
   叫醒一次，比不设还糟。正确做法是单独一个**每天只触发一次**的唤醒任务
   （`WorkBuddyRewardWake`，07:00，`WakeToRun=true`）。
   自检第 2 节把「主任务必须 false / 唤醒任务必须 true」两条都钉死了，防止日后被
   「顺手统一一下」改坏。

4b. **想「白天每 N 分钟唤醒一次」，不能用一个触发器加 `Repetition`**（2026-09-21 实测新增）
   → 第一版 `WorkBuddyRewardDayWake` 写成了「07:00 开窗 + `Repetition PT3M`
   + `Duration PT17H` + `WakeToRun=true`」。它**看起来完全正确**：任务确实每 3 分钟
   跑一次、`NextRunTime` 也对、`Result=0`。于是让它睡着验证 —— 结果 **48 分钟一次
   都没被叫醒**，最后是用户按电源键才醒的（`powercfg /lastwake` = 电源按钮）。
   根因：注册后 `powercfg /waketimers` 里**只有 07:00 那个任务**，根本没有它。
   → **`WakeToRun` 只对触发器的计划起始时刻生效；`Repetition` 派生出的重复时刻
   不会各自武装唤醒定时器。** 正解是把每个时刻写成**独立的 `CalendarTrigger`**
   （07:00、08:00 … 23:00 共 17 个），改完再测：`waketimers` 立刻出现该任务，
   睡眠 → 定时器到点 → 自动唤醒，`lastwake` 显示「唤醒源 = 唤醒计时器，
   Windows 将执行 `NT TASK\WorkBuddyRewardDayWake`」。
   自检第 2 节新增「**不带 `Repetition`**」「每个唤醒点一个触发器」两条断言守住它。

5. **`%PROGRAMFILES%` 可能根本不存在，不能只靠环境变量找客户端**（2026-09-20 新增）
   → 原来 `winenv.programfiles()` 只读 `PROGRAMFILES` / `PROGRAMFILES(X86)` /
   `ProgramW6432` 三个环境变量。**但实测在一台正常装了 WorkBuddy 的 Windows 上，
   这三个变量全都是空的**（精简启动器 / 沙箱 / 计划任务上下文都可能只传一小撮变量）。
   后果是一条连锁：客户端明明在 `C:\Program Files\WorkBuddy` 却探测不到 →
   `doctor.py` 报「找不到客户端」→ `api_discovery` 退回内置端点表、拿不到 App 里的
   权威端点 → 版本号读不到、User-Agent 退化。而 `C:\Program Files` 与
   `C:\Program Files (x86)` 是 Windows 的**固定约定**，不依赖任何变量。
   现在两者都列上（环境变量在就优先用它，支持自定义系统盘）。
   自检第 3 节会直接断言「在本机定位到了客户端」。

6. **★★ `<Repetition>` 省略 `<Duration>` = 该触发器永不触发**（2026-09-20 新增，最严重的一个）
   → 直觉（以及本文件旧版注释）都写着「不写 Duration = 无限重复」。**实测是错的。**
   省略 Duration 后，Windows 每次到点都把它记成一次「错过的运行」而**跳过不执行**，
   而且**极具欺骗性**：

   | 观察点 | 看起来 | 实际 |
   |---|---|---|
   | `schtasks /Query /V` | 「重复: 每: 5 分钟」 | 显示正常 |
   | `NextRunTime` | 每 5 分钟往后滚一格 | 一直在滚，像在工作 |
   | `LastRunTime` | — | **永远停在注册之前** |
   | `NumberOfMissedRuns` | — | **一直往上涨** |
   | `LastTaskResult` | — | 恒为 `267011`（`SCHED_S_TASK_HAS_NOT_RUN`） |

   实测对照（用 4 个探针任务，间隔 `PT1M`，各观察 3.5 分钟，任务只做「追加一行时间戳」）：

   | 变体 | Repetition 写法 | 触发次数 |
   |---|---|---|
   | ① | 不写 Duration + 有 LogonTrigger | **0** |
   | ② | 不写 Duration + 无 LogonTrigger | **0** |
   | ③ | `<Duration>P1D</Duration>` | **3** ✅ |
   | ④ | `Duration=PT2H` + `StopAtDurationEnd=true` | **0**（窗口已过期，下次运行变成次日 00:00） |

   **当时的实际后果**：主任务的「每 5 分钟轮询」与 watchdog 的「每 30 分钟心跳」
   **双双静默失效** —— 两个任务都只在**登录时**被 `LogonTrigger` 带起过一次。
   于是「猫到达即领」退化成「下次登录时才领」（当天实测**推迟了 59 分钟**），
   而 watchdog 这个「防止主脚本静默死亡」的唯一保险，自己也是死的。

   **为什么能活这么久**：自检第 2 节原本写着
   ```python
   check("重复无终止（未写 Duration = 无限重复）",
         root.find(".//t:Repetition/t:Duration", NS) is None)
   ```
   —— **测试把这个 bug 断言成了期望行为**，所以它一路通过自检。
   这条已改成反向断言（`Duration` 必须存在）。

   **取值也有坑**：两个 Task Scheduler 前端的上限**不一致** ——
   PowerShell/CIM 对「无限期」给的序列化值是 `P99999999DT23H59M59S`，
   但 **`schtasks.exe` 会直接拒绝**它（「任务 XML 包含格式不正确或超出范围的值」）。
   本项目用 `schtasks /Create /XML` 注册，实测上限在 `P9999D` 附近
   （`P1D`/`P30D`/`P365D`/`P1000D`/`P3650D`/`P9999D` 通过，`P36500D`/`P99999D` 被拒）。
   代码里是 `install.REPETITION_DURATION = "P9999D"`（≈27 年，对一个签到工具即「无限期」）。

另外：`.cmd` 文件**内容是纯 ASCII**（中文注释只出现在 `.py` 和本 README 里）。
原因同上——cmd.exe 读 UTF-8 中文会乱码，写英文注释反而更可靠。

---

## 7b. ★ 什么情况下会跑、什么情况下不会（2026-09-20 实测）

问题：「除了关机，睡眠 / 息屏这些情况还能签到吗？」——逐层依赖如下，
**任何一层不满足，`WakeToRun` 都会被静默忽略**（不报错、不提示）：

| 情形 | 会不会跑 | 依据 / 实测 |
|---|---|---|
| **息屏 / 锁屏** | ✅ **正常跑** | 息屏只是显示器断电，会话仍是 Active，计划任务毫无影响。实测：会话 `Active` 期间 `PT5M` 轮询每 5 分钟准时触发 |
| **睡眠（S3 待机）** | ✅ **会被唤醒**（07:00 + 白天每小时） | ① 两个唤醒任务 `WakeToRun=true`；② `powercfg /waketimers` 显示定时器**已实际武装**；③ 电源计划「允许使用唤醒定时器」AC/DC **均为启用**；④ 本机固件支持 S3。四层全通，且已真机实证：睡着 → 定时器到点自动醒 → 跳过的那次 2 秒内补跑 |
| **休眠（S4）** | ⚠️ **通常不会被唤醒** | 唤醒定时器对 S4 一般无效。等自然开机后由 `StartWhenAvailable` 补跑 |
| **关机（S5）** | ⚠️ 不会唤醒，但**开机后自动补跑** | 今天已实证：16:35 与 17:38 两次开机后约 1 分钟，任务都自动跑了一次，其中 17:39 那次把挂着的旅行奖励领了 |
| **注销登录 / 切到别的用户** | ❌ **不跑** | 任务用 `LogonType=InteractiveToken`（「只在用户登录时运行」）。**注销 ≠ 锁屏** —— 锁屏照跑，注销不跑 |
| 睡眠中的**轮询** | — | 只有两个唤醒任务会叫醒机器：07:00 那一次 + 白天 07:00–23:00 每小时一次；深夜不唤醒（这是故意的）。主任务/看门狗**不带** `WakeToRun` —— 给 5 分钟轮询开唤醒会每 5 分钟叫醒电脑一次 |
| **断网** | ✅ 会跑，然后失败并退避重试 | `RunOnlyIfNetworkAvailable=false`（不让系统替我们跳过），失败后在 20 分钟后重试 |

一句话：**「息屏/锁屏」完全不受影响；「睡眠」靠每天 07:00 的唤醒器；「关机」靠开机后的补跑；
只有「注销登录」是真盲区。**

> 本机电源计划的实测值：`STANDBYIDLE` 交流=0（永不自动睡眠）、直流=900s（15 分钟）；
> `HIBERNATEIDLE` 均为 0（永不自动休眠）。所以现实里最常见的就是「合盖 → S3 睡眠 → 07:00 被唤醒」。

---

## 7c. 代理 / VPN 的影响（2026-09-20 实测）

| 配置 | 对脚本的影响 | 实测证据 |
|---|---|---|
| **无代理、无 VPN** | ✅ 直连正常 | 一次真实请求即可完成签到/领奖 |
| **Windows 系统代理（注册表 `ProxyEnable`）** | ✅ **默认被绕过**，不影响 | 代码只在 `WORKBUDDY_PROXY_MODE=system` 时才调 `getproxies()`（会读注册表）；默认走 `getproxies_environment()` |
| **环境变量 `HTTP_PROXY` / `HTTPS_PROXY`** | ⚠️ **默认会被使用** | 指向一个没在监听的端口时，请求报 `WinError 10061 由于目标计算机积极拒绝，无法连接` |
| **只设 `ALL_PROXY`** | ⚠️ **实际不生效**（静默直连） | 实测正常连通 —— `ProxyHandler` 不认 `all` 这个 key。方向是安全的（等于直连），但「设了以为走代理其实没走」容易误判 |
| **VPN（改路由的那种，如 OpenVPN/WireGuard）** | ⚠️ **会有影响，且脚本绕不过** | 「绕过系统代理」绕的是代理，VPN 改的是 IP 路由。必须保证 VPN 允许访问 `codebuddy.cn`。本机两个 TAP-Windows 网卡当前均 Disconnected，无默认 VPN 路由 |
| **TLS 拦截型代理** | ❌ 会失败 | 报 `self-signed certificate in certificate chain`；属于代理侧配置问题 |

> **要不要开 `WORKBUDDY_PROXY_MODE=system`？** 默认建议**不开**：系统代理端口一关，
> 走系统代理反而会连不上，而直连通常更稳。只有在「必须经代理才能出网」时才开。
>
> **踩过的一个坑（已修）**：中文 Windows 的报错是**全本地化**的，
> `Connection refused` 那句中文里**不含英文 `connection`**，于是失败提示会被归到「未知」分支，
> 用户拿到的是最没用的一句「跑一次 run_now.cmd」。现在分类同时认中英文措辞与 `WinError` 号。

---

## 8. 安全与不干扰承诺

- **不写入文件夹以外的任何数据**（唯一的系统侧痕迹是计划任务注册项，`uninstall.cmd` 可清除）
- **自检自身不改动系统**：`selftest.py` 不注册 / 不删除任何计划任务，也不发消息
  （2026-09-20 修：此前它是靠「以子进程跑一次真正的 `install`」来验证非 Windows 守卫的，
  在任何 Windows 机器上都会**真的注册一个计划任务** —— 现在改为在进程内翻转平台断言守卫）
- **不修改 `../00_For_Mac/` 的 mac 版任何文件**，两套完全独立运行
- 凭据只从本机 `settings.json` 读取，**不打印、不写日志、不进代码**
- 所有网络请求只发往 WorkBuddy 官方接口
- 沙箱/只读命令（`doctor.py`、`main.py status`、`notify.py status`）**不领积分、不发消息**

---

## 9. 一次性完整验证（交付前做过）

自检脚本**在 Windows 与 macOS 上都能跑**（不再假设本机是 mac），Windows 真机最近基线
**347 项全部通过**（项数随环境浮动，判据是 0 失败）。它实际验证了：

- 计划任务 XML 结构合法（命名空间、触发器、重复间隔、几个关键布尔值、UTF-16 编码往返），
  并单独钉死「**轮询主任务 `WakeToRun=false` / 唤醒任务 `WakeToRun=true` 且不带重复触发**」
  这一对形态约束（第 2 节）；
- Windows 命令输出的 UTF-8 / UTF-16 / GB18030 解码，以及 XML 省略可选字段时的
  PowerShell 回读判定；
- `read_asar_version()` 能解出版本号（本机用 `C:\Program Files\WorkBuddy` 的 195MB 真 asar 验证）
- 模拟 `win32` 平台下的全部路径候选（settings / 游标目录 / asar）与 `win_paths.json` 覆盖优先级
- 闸门逻辑、当日去重、状态文件往返
- 模块导入链完整，6 个 `.cmd` 入口可运行
- 非 Windows 上 `install` **明确报错**而不是静默假装成功（在进程内翻转平台断言，**不真的注册任务**）
- 没有硬编码 mac 路径泄漏进 Windows 版；`.cmd` 为纯 ASCII；**没有 `._*` 拷贝残留**
- **用户可见文案的平台适配**（第 8b 节）：真机平台、假装 Windows、再翻回 mac 三段都验，
  把通知与报错的正文真正渲染出来，断言里面**不出现 `python3`**、且指向 `login.cmd` / `doctor.cmd`
- **watchdog 的独立性**（第 8d 节）：用 `ast` 查它真实的 `import` 语句，断言不依赖任何项目模块；
  并注入 `job_loaded` 覆盖「心跳新鲜 / 心跳过期 / state 缺失 / 任务未注册 / 两者并存 / 未超阈值」
  六种结论，外加**静默期与当日收工后不误报、而真故障照报**两种（2026-09-19 新增）
- **静默判据两版同口径**（第 8d 节末尾）：直接断言 `catchup._day_finished` 与
  `watchdog._day_finished` 一致 —— 前者决定「要不要写心跳」，后者据此判活，判错就会互相打架
- **去重两表制的语义**（第 8e 节）：仅本机送到 → 放行微信补发；已送微信 → 拦一切；
  超窗口 → 都不拦；7 天清理。**写入策略**（第 8f 节）：整个 `notify.py` 只有一处写 `sent` 指纹，
  且必须被 `if wechat_delivered` 守住 —— 直接断言不变量本身，而不是跑一遍网络
- **端点变化检测的语义**（第 8g 节，2026-09-20 新增）：只有「本次与上次都是权威扫描」才允许
  判定端点变化 —— 客户端临时定位不到而退回兜底表时**不得**误报「接口已切换」，
  兜底结果也**不得覆盖**权威基线；同时反向断言**真正的端点变化照报**，
  确保这次收紧没有连带把告警关掉
- **通知的「送达」口径与告警记账**（第 8h 节，2026-09-20 新增）：
  微信不可用但本机弹成功 → `sent=True`；两者都失败 → `sent=False` 且两张表都不记；
  **告警未送达时不得写「今日已告警」**（否则一次投递失败就把告警吞掉一整天）；
  两类告警各自留额度；状态文件被改坏时 `send()` 也不抛异常
- **续期提醒不得「没送到也记账」**（第 8i 节，2026-09-20 新增）：
  提醒未送达 → 不写 `last_remind_date`（下一轮继续试）；送达 → 记账且当天不重复
- **登录态候选路径覆盖 Windows 两种布局**（第 4b 节，2026-09-20 新增）：
  按**真实平台**断言 Windows 下候选必须同时含 `%APPDATA%` 与 `%LOCALAPPDATA%`
  （实测明文只写在后者，旧实现因此漏读）
- **ClawBot 凭据的两种 `settings.json` 结构**（第 8l 节，2026-09-20 新增）：
  `claw.users.<uid>.channels.weixinClawBot` 与顶层 `claw.channels.weixinClawBot`
  都要能解析；只有 `wechatmp`（公众平台 webhook）时**不得**误判成已绑定
- **中文报错分类与命令输出解码**（第 8k 节，2026-09-20 新增）：
  中英两种网络报错措辞都必须归到「网络」档；`winenv.decode_console`
  对 gb18030 / UTF-16 / UTF-8 三种编码都要还原正确，且对任意字节**永不抛异常**
- **watchdog 能识别「任务被停用 / 被删除」**（第 8j 节，2026-09-20 新增）：
  把真实形态的 `schtasks /FO LIST /V` 输出样本按 **gb18030 / UTF-16 / UTF-8** 三种编码
  喂进解析函数，断言「启用 / 停用」结论一致；并附一条**反证**——
  整串子串匹配在「启用」样本上也会命中「已禁用」，所以那种写法必然误报
- **watchdog 还会看「任务指向哪个文件」与「上次跑成没成」**（第 8j 节，2026-09-21 新增）：
  注入「指向旧路径」+ `0x8007010B` 断言两条新判据都会报；注入信息码 `267011`（未曾运行）
  / `267009`（正在运行）/ `0` 断言**不误报**；并单独钉住 `_result_is_error` 的边界
  （`0x413xx` 信息码不算失败、`>= 0x80000000` 算失败，含以负数给出的等价写法）
- **watchdog 不许在「静默期边界」和「刚开跑」这两个时刻误报**（第 8j 节，2026-09-21 新增）：
  07:00 首次采样（state 还是昨天的）不报 `stale` 且 `first_run_grace=true`；
  07:15 同样条件**照报**；state 是今天的则宽限期不适用；
  刚开跑 5 秒时不下 `task_failed` 结论、已开始 10 分钟则照报；
  并完整**复现** 2026-09-21 07:00 那条真实假告警的场景，断言现在判定为健康
- **本机通知必须「看得见」**（第 8m 节，2026-09-21 新增）：
  默认常驻、`WORKBUDDY_TOAST_PERSISTENT=0` 可退回「几秒后自动消失」、
  投递脚本里确实写了 `scenario=urgent` / `duration=long`，
  以及**常驻被系统拒绝时会退回普通 Toast 再试一次**（不许为了「更显眼」把唯一通道弄哑）；
  并钉住 `notify._reg_get` 对 `reg query` 的 `0x1` / `0x0` / `REG_SZ` 三种输出都能正确归一 ——
  判错会让「系统通知被关掉」这**唯一**要抓的场景静默放行
- **必须点掉的卡片通道**（第 8o 节，2026-09-21 新增）：
  默认通道是 dialog、可切 toast；卡片脚本 `notify_card.py` 必须作为**真实文件**随交付物搬走；
  「另起进程 + 用 pythonw + `creationflags` 只传一次且含 DETACHED」，
  且**不许经过 powershell**（否则会闪一个控制台黑框）；
  标题正文走环境变量；弹窗后留「待处理」标记；已有 3 个待处理弹窗时不再堆积；
  以及组合语义（走弹窗就不补弹 / 弹窗不可用才退回 Toast 并进补弹队列）
- **通知补弹状态机**（第 8n 节，2026-09-21 新增）：
  在场不排队 / 不在场才排队；人没回来不补弹（不许每 5 分钟糊一次）；
  人回来则补弹**且仍是常驻**、补完出队（同一条只补一次）；
  补弹投递失败**不消费队列**（下一轮继续试，不丢消息）；
  队列上限与 72 小时过期裁剪；队列文件损坏当空队列；`WORKBUDDY_RESHOW=0` 可整层关闭
- **自检对「干净副本」中立，且不污染被诊断对象的证据**（第 8 节，2026-09-21 新增）：
  断言运行时配置的**生成能力在代码里**（`runtime/config/*.json` 不再是「必备文件」，
  它们在干净副本上本来就不存在）、已存在的配置必须是合法 JSON、
  并且 `catchup.LOG` / `renew.LOG` 的落点已被隔离到 `.selftest_logs/`
- **doctor 解析计划任务里的脚本路径**（第 8 节末，2026-09-21 新增）：
  带引号 / 带参数 / 空值三种输入都要取对，`_same_file` 同路径判等、异路径判不等
  —— 这是「搬迁后任务指向旧路径」那条 FAIL 判据的基础

### 为什么单列「文案适配」这一节

这类问题是**静默的**：代码逻辑完全正确，只有真出故障、用户照着提示去执行时才发现命令跑不通 ——
而那正是最需要它管用的时候。所以用反向断言把它钉死，而不是靠人工检查字符串。

### 2026-09-20 一轮实测修复清单（Windows 真机）

在 Windows 11 上真机跑通后的修复。**按性质分两类**，第二类更值得看：

**A. 交付物/自检本身的问题（在交付机上必然踩到）**

1. `selfcheck_hint()` / `login_hint()` 读的是模块级常量 `IS_WIN`（导入时固定），
   而本模块自己的约定是「内部逻辑一律调 `platform()`，这样测试才能把平台翻过去」——
   于是「文案是否按平台生成」这条不变量**根本无法被验证**，自检只能长期报假失败。已改成 `platform()`。
2. `doctor.cmd` 结尾写死 `exit /b 0`，把 `doctor.py` 的退出码丢了 ——
   诊断出 FAIL 也返回 0，脚本化调用完全看不出失败。已改为透传退出码。
3. `uninstall.cmd` 的手动删除指引只提了 1 个任务，而当时 `install` 实际注册**三个** ——
   照它做会漏删 watchdog 与唤醒任务，调度仍在跑。已改为列全任务（含自定义任务名的提示）；
   后续又新增 `WorkBuddyRewardDayWake`，当前为**四个**。
4. 从 mac 拷过来的 66 个 `._*` AppleDouble 残留在所有「按后缀扫描」的检查里炸掉，
   已清理（临时备份未纳入公开仓库），
   并新增一条会主动点出残留的检查。

**B. 逻辑缺陷（会影响真实行为，且多数是「静默失效」）**

5. **`winenv.programfiles()` 只读环境变量** → 本机 `PROGRAMFILES` 根本不存在
   （实测三个变量全空），于是客户端明明装在 `C:\Program Files\WorkBuddy` 却探测不到，
   连锁导致 `doctor` 报「找不到客户端」、`api_discovery` 退回内置端点表、版本号读不到。
   已加 `C:\Program Files` / `C:\Program Files (x86)` 固定兜底（见第 7 节第 5 条）。
6. **`api_discovery` 把「客户端读不到」误判成「端点变了」** → 一次读取抖动就推一条
   「接口已自动切换」给用户，还会反向再误报一次。已改为只在两次都是权威扫描时判定变化，
   且兜底结果不覆盖权威基线（见第 8g 节）。
7. **`watchdog._job_loaded()` 双重失效**（本项目唯一「监控」的命门）：
   既用 `utf-8` 读 GBK 输出导致中文永远读不出来（**任务真被停用也报健康**），
   又用整串子串匹配导致「空闲时间: 已禁用」等无关字段必然命中（**修好编码就会每 30 分钟误报**）。
   两个 bug 互相掩盖。已改为 PowerShell 的 `State` 枚举为主路径（语言无关 + 纯 ASCII 输出）、
   按控制台代码页解码且只认「任务状态」那一行为兜底（见第 8j 节）。
8. **告警「先记账后发送」**（`notify._alert_channel_expired`）：本机 Toast 发不出去时
   照样写「今天已告警」→ 这条告警被永久吞掉一整天。已改为**送达才记账**。
9. **`notify.send()` 两个早退分支漏置 `sent=True`**：本机弹窗明明成功了，
   `catchup.log` 里却记 `sent=False … reason=已降级为本机通知` —— 日志自相矛盾，
   按第 6 节排错表去看会得出「根本没送到」的错误结论。已置真。
10. **`renew` 的续期提醒未送达也写「今日已提醒」** → 当天不再重试，
    而它提醒的恰恰是「ClawBot 会话已停摆」这种漏掉就再没人管的事。已改为送达才记账。
11. **`watchdog._job_loaded` / `renew.log` / `install.cmd` 的编码**：`renew.log` 未指定编码，
    在非中文 Windows 上写中文会抛 `UnicodeEncodeError`（**不是 OSError**，
    逃过 `except OSError`，破坏「永不抛异常」的承诺）。已显式 UTF-8 + `errors="replace"`。
12. **`http_client._parse_json` 可能返回非 dict**：接口返回顶层为数组/标量的合法 JSON 时，
    `checkin` / `travel` 的 `body.get("code")` 会抛 AttributeError，而它们只捕
    `HttpTransportError`，异常穿透成 traceback → 当天签到空转。已保证只返回 dict。
13. **「缺 context_token」被当成网络抖动**，按 20 分钟短冷却反复重试 ——
    每次服务端都受理并扣 iLink 每日配额，但消息永远到不了微信，
    结果是配额被白烧光、连本该能送达的通知也只能降级。已归入「需人工动作」那一档（60 分钟）。
14. `install.py` 的平台守卫：原本用冻结常量 `IS_WIN`、且位置在「XML 已写出、正要调 schtasks」
    之后。后果有二 —— 非 Windows 上会先落盘一堆 XML 才报错；自检本想验证这条守卫，
    却因为机器真在 Windows 上而**真的注册/覆盖了一个计划任务**。已抽成统一的
    `windows_guard()` 并提前到函数开头（见下方「自检本身也是被审的对象」第 3 条）。
15. `install.py` 的 `task_settings_note` 把 `interval` 拼成 `PTPT5MM`
    （XML 里已经是完整形式 `PT5M`，又套了一层 `PT{}M`）—— 一条**照着核对必然对不上**的
    误导性提示。已修正。
16. `catchup._hint_for` 里硬编码了 `py -3 scripts\api_discovery.py`（本文件另有 4 处同类
    指引都走 `winenv.selfcheck_hint()`）。既违反「平台差异只写在 winenv.py」，
    也违背 README「不会给你一条跑不通的命令」的承诺（`py` 启动器是可选组件，
    且它指向的解释器未必是跑本脚本的那个）。已统一。
17. `doctor.py` 里用局部变量 `paths` 遮住了顶部的 `import paths` —— 目前无害，
    但只要以后在同一函数里写一句 `paths.log_path(...)` 就会静默变成「dict 没有该属性」。
    已改名 `exe_paths`。

18. **`scripts/credentials.py` 在 Windows 上只找 `%APPDATA%`（Roaming）** ——
    而桌面端实测把新版明文登录态写在 **`%LOCALAPPDATA%`（Local）**：
    `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info`。
    后果是**明明已登录却报「读不到登录态」**，签到与领取全部空转，
    而报错文案还把用户引向「请先登录 WorkBuddy 桌面端」这个完全错误的方向。
    已改为两个布局都列、逐个探测（旧版 `state.vscdb` 同理）。
    实测：修复后当天签到成功 +100，旅行派遣成功。
    自检新增第 4b 节钉住「Windows 候选必须同时覆盖 Roaming 与 Local」。

19. **★★ `install.build_task_xml` 的 `<Repetition>` 没写 `<Duration>`** ——
    本轮最严重的一个：主任务与 watchdog **双双从未按周期运行过**，
    只被 `LogonTrigger` 在登录时带起；「猫到达即领」实测被推迟 59 分钟。
    根因、实测对照、取值上限、以及「自检第 2 节把 bug 当期望」的经过，见 **第 7 节第 6 条**。
    已修：`REPETITION_DURATION = "P9999D"`，并把自检那条反向断言改成正确的方向。

20. **`notify._reg_get` 用 `subprocess.run(text=True)` 读 `reg` 输出** ——
    该写法按**会漂移的 locale** 解码（本机 `PYTHONUTF8=1` + `LANG=C.UTF-8` → utf-8），
    而 `reg.exe` 在中文系统上输出 GBK → reader 线程抛 `UnicodeDecodeError`。
    后果：`notify.py status` 打印一段吓人的 traceback，而 Toast 开关值被静默当成「读不到」。
    已抽出共用的 `winenv.decode_console()`（BOM → UTF-16 → 严格 UTF-8 → gb18030 → locale
    → replace，**永不抛异常**），`notify._reg_get` 与 `winenv.pid_alive` 改用它，
    `install.decode_process_output` 也委托到它（去掉一份重复实现）。
    顺带把 `scripts/credentials.py` 里那处 `text=True` 补上显式 `encoding="utf-8"`。

21. **`catchup._hint_for` 的失败分类只认英文措辞** ——
    中文 Windows 的报错是**全本地化**的：端口没人监听时给的是
    「`<urlopen error [WinError 10061] 由于目标计算机积极拒绝，无法连接。>`」，
    里面**没有**英文 `connection`，于是落进「未知」分支，
    用户拿到最没用的一句提示 —— 而排错表专门为这个场景写了处置办法，等于没送到。
    已把中文措辞（积极拒绝 / 无法连接 / 连接超时 / 远程主机 / 名解析）
    与 `WinError` 号（10061/10060/10054/10053/11001/11002）补进网络类关键词，
    并把提示文案改成能直接指路的版本。
    自检新增第 8k 节钉住「中英两种措辞都必须归到网络档」。

22. **`clawbot._channel_from_settings` 只认一种 `settings.json` 结构** ——
    它只在 `claw.users.<uid>.channels.weixinClawBot` 里找 bot 凭据，
    而桌面端**在不同版本/分支里写过顶层结构** `claw.channels.<name>`：
    本机实测看到的 `claw.channels.wechatmp`（公众平台 webhook）就摆在**顶层**。
    后果是「用户确实绑定成功、工具却一直说未绑定」，而且没有任何报错 —— 又一次静默失效。
    已改成**两种结构都认**（顶层放在用户结构之后，保持原有优先级），
    通道名同时兼容 `weixinClawBot` / `clawbot` / `clawBot` / `weixin_claw_bot`，
    并兼容 `botToken`/`bot_token`、`baseUrl`/`base_url` 等写法。
    自检新增第 8l 节钉住「两种结构都要能解析，且只有 `wechatmp` 时**不得**误判成已绑定」。

### 自检本身也是被审的对象（2026-09-20）

上面这些断言曾经自己就有三个 Windows 相关的缺陷，导致**自检在交付机上必然报假失败、
而且崩溃中断**（只跑完 15 节里的 4 节）：

1. 还原环境变量时用 `os.environ.clear()` + `update(整份快照)` —— Windows 的环境块有
   32767 字符硬上限，本机实测存在一个 28 万字符的变量，于是直接 `ValueError` 崩在第四节，
   **后面 10 节再也跑不到**。现在只回写自己动过的那几个键。
2. 断言「本机是 mac」的两处（`selfcheck_hint` 给 mac 命令、「项目路径本身含中文」）——
   在 Windows / ASCII 路径下恒假失败。现在改成按**真实平台**断言 + 用确定性的中文路径
   现场跑一遍创建 / 写入 / 读回。
3. 「非 Windows 上 install 必须失败」是用**真的注册一次计划任务**来间接验证的 ——
   在任何 Windows 机器上都会留下系统痕迹。现在改为在进程内翻转 `winenv.platform` 断言守卫。

顺带把 `._*` 残留从所有「按后缀扫描」的检查里排除，并新增一条会主动点出残留的检查。

> **为什么这些能溜进来：自检原本是「只在开发机（macOS）上跑」的。**
> 一旦它真的被拿到 Windows 交付机上跑，所有「本机是 mac」的隐含假设同时暴露，
> 而环境变量的 32767 上限还把问题从「报几条假失败」升级成「**静默跳过 10 节**」。
> 教训：**自检的断言必须对「运行环境」保持中立，或者显式声明依赖** ——
> 否则它给的绿灯是假的。

---

### 2026-09-21 复核一轮的修复（Windows 真机 · 从离线 bundle 克隆出的新副本）

**起因**：文件夹挪到新位置后，计划任务仍指向已被删掉的旧路径，每次触发都以
`0x8007010B`（目录名无效）失败 —— 而任务列表里它们「已注册、已启用」，
`doctor` 只把执行体打印出来、**从不比对**，`watchdog` 只看「任务在不在」，
于是**两个监控同时哑掉却一路绿灯**。这一轮除了在新位置重注册，把这三个盲点一并补上：

23. **`selftest` 把运行时生成物列进「必备文件」** → 干净副本（克隆下来、一次都没跑过）上
    恒报 `失败 1 项：缺失 runtime/config/notify_config.json`；而 `install.cmd` 结尾就会跑自检，
    **开箱第一次安装看起来像失败了**（已实测复现）。已改为断言「生成它们的能力在代码里」
    （`notify.ensure_config` + `DEFAULT_CONFIG`），并新增「已存在的运行时配置必须是合法 JSON」。
24. **`selftest` 把自己的记录写进交付物的 `runtime/logs/catchup.log`** ——
    实测干净副本首次 `install.cmd` 之后该日志前 10 行全是自检写的，其中
    `[renew] stale reminder #1 sent=True age=99.0h` 与**真实告警完全无法区分**
    （`99.0h` 是第 8i 节的桩数据）。已把 `catchup.LOG` / `renew.LOG` / `watchdog.SELF_LOG`
    隔离到 `.selftest_logs/`（跑完即删），并加一条断言钉住隔离本身。
    教训：**自检的产物不许混进被诊断对象的证据里。**
25. **`doctor.py` 只打印执行体、不比对路径** → 搬迁后任务指向旧路径时全程 OK。
    已新增 FAIL 级检查「任务指向当前目录」；解析不出路径时降级成 WARN（不误报）。
26. **`watchdog.py` 只查「任务在不在」** → 任务「已注册、已启用」但每次都失败时仍报
    `healthy`。已新增两条判据：`wrong_path`（任务指向的文件 ≠ 当前目录）与
    `task_failed`（`LastTaskResult` 是 `>= 0x80000000` 的错误码）；
    信息码（未运行过 / 正在运行 / 已排队）**不算失败**，避免刚装完就误报。
    摘要里同时输出 `task_script` 与 `task_last_result`，事后核对判据不用猜。
27. **本机通知改成「常驻、不自动消失」**（`winenv._PS_TOAST`：`scenario="urgent"` +
    `duration="long"`）。理由：微信通道不可用时它是**唯一**能让用户看到结果的通道，
    而签到结果偏偏弹在没人盯着屏幕的时刻。同时加了兜底 ——
    **常驻写法被系统拒绝时退回普通 Toast 再试一次**，不许为了「更显眼」把通道弄哑。
    想恢复旧行为：`WORKBUDDY_TOAST_PERSISTENT=0`。

28. **watchdog 的两条新判据第一次真机运行就暴露了两个假报警** ——
    2026-09-21 上午通知中心里那条「⚠️ WorkBuddy 积分脚本可能已停摆」就是它，
    而当天签到其实**已经成功**（07:00:01 签到 +100，08:05 领到旅行奖励 +9）：
    ```
    [2026-09-21 07:00:05] PROBLEM stale,task_failed
        主脚本已 432 分钟没有运行（心跳阈值 90 分钟）
        主任务最近一次运行以错误码 0x800710E0 结束
    ```
    两个根因，都不是真故障：
    - **`stale`**：静默期（00:00–07:00）内主脚本**故意不写心跳**，机器又整夜空转，
      于是 07:00 的第一次采样心跳必然是「昨晚的」—— 再撞上当天第一次运行还没写完
      `state.json`，就被判成「已停摆」。
      → 新增 `first_run_grace`：静默期结束后 10 分钟内、**且盘上的 state 还是前一天的**，
      视为预期静默。07:10 之后照报，**也不会**放过「今天跑过又停了」。
    - **`task_failed`**：`0x800710E0`（「操作员或管理员拒绝了请求」）是**过渡值** ——
      07:00:00 唤醒任务与主任务同时拉起 `catchup.py`，此刻 `LastTaskResult` 还没稳定。
      → 只有「这次运行已开始 ≥ 2 分钟」才判失败，并把 `LastTaskResult` 与 `LastRunTime`
      一起取回来；**常驻失败**（如搬迁后的 `0x8007010B`）照样在下一轮采样被抓住。
    摘要里新增 `task_last_run` 与 `first_run_grace`，事后核对判据不用猜。

29. **`notify._reg_get` 读不出「自己刚设好的开关」** —— `reg query` 对 DWORD 输出的是
    `0x1` / `0x0` 形式，而调用方拿返回值去查 `{"1": "开", "0": "关（本机通知会被系统丢弃）"}`。
    后果：`0x1`（开）显示成「读不到（按默认开处理）」，更糟的是 **`0x0`（**已被关掉**）
    也落进「读不到」** —— 而这条检查存在的唯一目的就是抓住「系统通知被关掉、
    本机兜底全部静默消失」这个场景，判错就等于它根本不存在。
    2026-09-21 把开关**显式设为 1** 之后才暴露出来（之前该值不存在，返回 None 反而是对的）。
    已归一（`int(raw, 0)`；非 DWORD 仍返回原串），并加自检钉住 `0x1` / `0x0` / `REG_SZ` 三种输入。

30. **「投递成功」不等于「你看到了」** —— 熄屏 / 锁屏 / 睡眠期间 Windows **不弹横幅**，
    通知只进通知中心（实测 07:00 签到、08:05 领奖两条都是这样"没人看到"的）。用户要求
    「不管什么情况通知都要弹窗、点叉才消失；之前睡眠/休眠的，重新打开即弹窗」，
    于是加了两道保险（详见第 6 节「让通知一定会被你看到的两道保险」）：
    - **横幅常驻**：`scenario="urgent"` + `duration="long"`（第 27 条），并保留「被拒则退回普通 Toast」；
    - **回来时补弹**：用 `winenv.idle_seconds()`（`GetLastInputInfo`）判断投递时**用户是否在场**，
      不在场就把这条**原文**记进 `runtime/state/notify_reshow.json`；`catchup.py` 每次触发
      （每 5 分钟）问一句"人回来了吗"，一回来就把积压的**原样再弹一遍**（依旧常驻），
      同一条**只补一次**。队列上限 8 条 / 72 小时 / 每次最多补 3 条，文件损坏当空队列。
    开关 `WORKBUDDY_RESHOW=0`，阈值 `WORKBUDDY_RESHOW_IDLE_SEC`（默认 300 秒 = 5 分钟：
    短暂离开时横幅本就还挂着，不需要重复弹）。
    顺带把补弹检查放在静默判定**之前**（夜里回到机器前也要能看到积压），
    静默期其余行为（不抢锁、不写日志、不读 state、不联网）**不变**。
    自检新增第 8n 节（12 条）钉住这套状态机；并做过真机端到端验证：
    阈值 0 模拟"不在场"投递 → 队列 1 条 → `idle=1` 模拟"人回来了" → `resent: 1` 且出队。

31. **Toast 的「常驻」在本机不成立 → 默认通道换成"必须点掉的弹窗"** ——
    用户实测反馈：那几条通知"看得到，但过一会儿就自己消失了"。也就是说
    `scenario="urgent"` + `duration="long"`（第 27 条）只兑现了一半：Windows 接受了属性
    （还给我们记了 `AllowUrgentNotifications=1`），但横幅依然按时被系统收走。
    要真正做到「点叉才消失」，只有**原生弹窗**这条路：

    - `winenv._notify_windows_dialog()`：由 `pythonw` 拉起 **`notify_card.py`**
      （tkinter 无边框窗口，贴在**右下角**工作区边缘），**点右上角 ✕ 才消失** ——
      主脚本（计划任务每 5 分钟一次）绝不能被一个没人点的窗口卡住；
    - 最多同时堆 `_DIALOG_MAX = 3` 个，再多就退回 Toast + 补弹队列
      （免得离开几天回来被几十个窗口糊满）；每张卡片在
      `runtime/state/notify_dialog_<pid>.json` 留标记，进程退出自删、残留按 PID 存活清理；
      多张同时存在时按 `WB_DLG_SLOT` **往上叠**，不会互相盖住；
    - **走弹窗时不再进补弹队列**（同一条消息不该弹两次）；弹窗不可用才退回 Toast 并补弹；
    - 开关 `WORKBUDDY_ALERT_STYLE=dialog|toast`（**默认 dialog**）。

    ★ 过程中踩到一个**静默降级**：弹窗进程的 `Popen` 同时传了显式 `creationflags`
    和 `**subprocess_flags()`（两者都有这个键）→ `TypeError` 被 `except` 吞掉 →
    **静默退回 Toast**：弹窗永远不出现，而日志、退出码、自检全都看不出异常
    （实测就是先发出一条"看起来成功"的 Toast）。已合并成一个 `creationflags`，
    并把 `Popen` 收进 `winenv._spawn()` —— **自检只替换这个函数，不去动全局
    `subprocess.Popen`**（替换全局模块属性会连带影响进程里的其它调用）。
    自检第 8o 节直接钉住入参，含"creationflags 只能传一次"这一条。

    ★★ **中途走过一次 PowerShell + WinForms 的弯路，两个坑都实测复现了**（留在文档里防重蹈）：
    ① 那时用户反馈"弹窗的同时桌面出现了一个 PowerShell 窗口" —— `powershell.exe`
    是**控制台程序**，`-WindowStyle Hidden` + `CREATE_NO_WINDOW` 也压不干净；
    ② PowerShell 的**相对路径基准不是脚本所在目录**，传相对路径时标记文件被写到别处，
    于是"卡片还开着"被误判成"已关闭"（计数恒为 0）。
    → 最终回到 **pythonw（窗口子系统程序，天生没有控制台）+ tkinter（路径一律用绝对路径）**。
    ★★ 另一个实测：**父进程拿到的 `Popen.pid` 不一定是那个窗口进程**（经代理层/中间进程
    起进程时会错位），所以标记改由**卡片自己**写入自己的 PID，父进程只在 spawn **之前**
    写占位 —— 顺序写反了同样会把真实 PID 覆盖掉。

**这一轮顺带实测确认的事实**（写下来省得下次再试）：


- **系统代理开着不影响**：本机 Clash Verge 监听 `127.0.0.1:7897`、`ProxyEnable=1`，
  工具默认走 `getproxies_environment()`（等于直连），实测 `https://www.codebuddy.cn/` 200；
  **环境变量** `HTTP(S)_PROXY` 则**会被使用**（指向死端口即 `WinError 10061`）；
  VPN 改的是 IP 路由，脚本绕不过。
- **`rundll32 powrprof.dll,SetSuspendState 0,1,0` 在本机会进 S4 休眠**
  （Kernel-Power 事件 42 的 `TargetState=5` = `PowerSystemHibernate`），
  而**唤醒定时器不会把机器从休眠叫醒**（唤醒事件里 `ProgrammedWakeTime` 为空）。
  所以要验证「睡眠唤醒」，必须先确认真的是 S3（`TargetState=4`）；
  否则会把「测试方法不对」误判成「唤醒定时器坏了」。

---

*配套 mac 版文档见 `../00_For_Mac/README.md`。*
