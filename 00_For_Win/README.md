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
② 双击 install.cmd   → 注册两个计划任务（主任务 5 分钟 + watchdog 30 分钟）并跑一次自检
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
py -3 install.py status     :: 两个计划任务的定义 + 最近运行痕迹
py -3 install.py run        :: 立即前台跑一次（看得到输出）
```

**怎么确认它在正常工作**：`py -3 install.py status` 里两个任务都已注册，且
`py -3 watchdog.py status` 判定为健康。

## 文档维护约定

**本文件与代码同步维护，这是硬要求。** 以后任何一次优化调整 —— 逻辑变更、配置新增、
目录移动、默认值改动 —— 都必须**同步更新本 README**（win 版改 `00_For_Win/README.md`，
mac 版改 `00_For_Mac/README.md`；**跨平台的功能改动两份都要改**，
不要只改一侧、留下一条过期的谎言）。同步规则与改完的验证动作见 **第 5 节**。

变更历史不在这里维护：大改动的方案与执行记录见上级 `Plan/`，每日工作日志见项目内 `.workbuddy/memory/`。

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
第 1 步  双击 install.cmd          → 注册两个计划任务（主任务 + watchdog）
第 2 步  双击 doctor.cmd           → 环境诊断，全绿即通
第 3 步  双击 run_now.cmd          → 手动跑一次，看到 JSON 摘要
第 4 步  （可选）双击 wait_token.cmd → 让微信也能收到通知副本
第 5 步  合上电脑，等第二天       → 到点自动跑
```

关于第 1 步：`install.cmd` 会注册计划任务并自动跑一次自检。
看不到输出就是成功（`pythonw.exe` 无控制台是**故意**的，避免闪黑窗）；要看结果就双击 `run_now.cmd`。
注册的是**两个**任务：主任务每 5 分钟、`WorkBuddyRewardWatchdog` 每 30 分钟（见第 4 节末）。

关于第 3 步：`run_now.cmd` 是**幂等**的。今天已经签过就什么都不做，安全，随便跑。

关于第 4 步：**不做也能跑通**，但微信收不到通知。原因见第 6 节「context_token」。

---

## 3. 文件夹地图

```
00_For_Win/
├── catchup.py            ← 入口：闸门 + 补跑 + 通知编排（计划任务跑的就是它）
├── winenv.py             ← ★ 所有 OS 差异的唯一集中地（本版核心新增）
├── install.py            ← 计划任务注册 / 卸载 / 状态 / 立即运行（注册**两个**任务）
├── watchdog.py           ← ★ 存活监控：只查主脚本心跳与计划任务是否还在，异常弹 Toast
├── doctor.py             ← 一键环境诊断（只读，不领积分、不发消息）
├── selftest.py           ← 自检 190 余项（在 mac 上也能跑来验 Windows 逻辑）
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
py -3 install.py install                  :: 注册（默认主任务 5 分钟 + watchdog 30 分钟，共两个）
py -3 install.py install --interval 15    :: 自定义主任务间隔
py -3 install.py install --no-watchdog    :: 只装主任务
py -3 install.py install --dry-run        :: 只生成 XML 不注册（可在 mac 上审阅）
py -3 install.py uninstall                :: 卸载（两个任务一起删，不删文件）
py -3 install.py status                   :: 看两个任务的定义 + 最近运行痕迹
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

所以 `install.py` 会注册**三个**独立任务：

| 任务名 | 触发 | WakeToRun | 干什么 |
|---|---|---|---|
| `WorkBuddyRewardCatchup` | 登录 + 每 5 分钟 | **false** | 真正干活的主脚本 |
| `WorkBuddyRewardWatchdog` | 登录 + 每 30 分钟 | false | 只查 `state.json` 心跳（阈值 90 分钟）与主任务是否还在，异常弹 Toast |
| `WorkBuddyRewardWake` | **每天 07:00 一次** | **true** | 电脑睡着时把它叫醒去签到 —— Windows 上「睡着也能签到」的唯一入口 |

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
| **必须逐字一致** | `scripts/main.py`、`scripts/checkin.py`、`scripts/travel.py`、`scripts/credentials.py`、`runtime/config/notify_config.json`、`runtime/config/renew_config.json` | 改 mac 版后**原样复制**到另一边。它们不含任何平台判断 |
| **必须成对改，允许少量差异** | `catchup.py`、`notify.py`、`renew.py`、`clawbot.py`、`scripts/api_discovery.py`、`scripts/http_client.py` | 逻辑必须同步，但平台相关的**几行**允许不同 |
| **同名但各自实现（不逐字比对）** | `watchdog.py` | 它是「监控主脚本还活着没」，所以**必须独立**：mac 用 `launchctl` + `osascript`，Win 用 `schtasks` + PowerShell Toast。两边的判断逻辑（心跳阈值、告警冷却、问题分类）必须一致，实现各写各的 |
| **各自独立** | `winenv.py`（仅 Win）、`install.py` / `doctor.py` / `selftest.py` / `*.cmd`（仅 Win）、`install.sh`（仅 mac —— 安装时按实际路径现场生成 plist） | 不参与同步 |

### 5.2 硬规则（违反就会累积技术债）

1. **任何新增的平台判断（`sys.platform` / `os.name` / `osascript` / `CREATE_NO_WINDOW` / 盘符路径 / `%APPDATA%`），只许写在 `winenv.py` 里。**
   其它文件只能调用 `winenv.xxx()`。`selftest.py` 第 8 节会**自动检查**这项：
   ```
   ✗ osascript 只出现在 winenv.py（平台差异已收口）
   ```
   一旦你在别处写了 `osascript`，自检立刻报错。

2. **禁止在 Win 版任何文件里出现 mac 用户路径**（`/Users/<name>...`、`.workbuddy/binaries/python`）。
   自检第 8 节会扫全目录。同理，mac 版也不该出现 `C:\` 或 `%APPDATA%`。

3. **解释器路径永远不硬编码**（两侧都是）。mac 版用 `sys.executable`（跑自己的那个），
   Win 版用 `winenv.default_python()`（同语义，另切 python/pythonw）。这样整个文件夹
   拷到任何机器都能直接跑。
   （2026-09-18 之前 mac 版 `catchup.py` 顶部写死过一条机器专属的绝对路径，已改掉，
   所以这条现在对两版同时成立。）

### 5.3 改完后的验证动作（照着做，两分钟）

```bash
# 在 mac 上就能做，不需要 Windows 机器

cd ".../00_Workbuddy自动签到领积分/00_For_Win"

# ① 自检：必须 0 失败（项数随环境浮动，约 218 项）
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
| 微信收不到通知 | 三种可能：**context_token 缺失** / **投递被拒** / 登录会话失效 | 先看 `runtime/logs/catchup.log` 里的 `notify[...] sent=… reason=…` 与 `py -3 notify.py status` 的 `last_send_error` |
| 微信收不到且报 `ret=-2 prepare failed` | **投递被拒**：服务端拒绝为主动消息建立会话。**成因未确认**（见下） | ① 在微信里给机器人发任意一条消息（如 `1`）；② 让 WorkBuddy 桌面端保持运行。二者都可恢复，**不需要重新扫码** |
| 微信收不到且报 `errcode=-14` | 登录会话过期 | `login.cmd` 扫码，或直接在 WorkBuddy 里重连「微信助理」 |
| 提示「接口疑似已变更」 | 腾讯改了接口路径 | 看 `runtime/cache/api_endpoints.json` 的 `history`；`doctor.py` 会显示现读到的端点。本版已做前置校验，正常情况下会自动跟上 |
| 提示「当前无活动」 | 可能是真无活动，也可能是接口返回全零 | 本版已把这种情形判为 `suspect` 而非 `no_activity`，并**主动告警**，不会静默漏签 |
| 路径找不到（settings / asar） | 客户端装在了非标准位置 | 跑 `doctor.cmd` 看它找过哪些路径 → 建 `win_paths.json` 填写正确路径 |
| 双击 `.cmd` 闪一下就没了 | 脚本报错但窗口关太快 | 从 cmd 里手动运行同名 `.py`，或在文件夹里开 cmd 跑 `install.cmd` |

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

## 7. 四个 Windows 特有的坑（本版已处理，记录备查）

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

另外：`.cmd` 文件**内容是纯 ASCII**（中文注释只出现在 `.py` 和本 README 里）。
原因同上——cmd.exe 读 UTF-8 中文会乱码，写英文注释反而更可靠。

---

## 8. 安全与不干扰承诺

- **不写入文件夹以外的任何数据**（唯一的系统侧痕迹是计划任务注册项，`uninstall.cmd` 可清除）
- **不修改 `../00_For_Mac/` 的 mac 版任何文件**，两套完全独立运行
- 凭据只从本机 `settings.json` 读取，**不打印、不写日志、不进代码**
- 所有网络请求只发往 WorkBuddy 官方接口
- 沙箱/只读命令（`doctor.py`、`main.py status`、`notify.py status`）**不领积分、不发消息**

---

## 9. 一次性完整验证（交付前做过）

自检脚本在 macOS 上跑 Windows 代码路径，**216 项全部通过**（项数随环境浮动，判据是 0 失败）。它实际验证了：

- 计划任务 XML 结构合法（命名空间、触发器、重复间隔、几个关键布尔值、UTF-16 编码往返）；
  并单独钉死「**轮询主任务 `WakeToRun=false` / 唤醒任务 `WakeToRun=true` 且不带重复触发**」
  这一对形态约束（第 2 节）
- `read_asar_version()` 用**真实 mac asar** 交叉验证能解出版本号（Windows 上同一函数可用）
- 模拟 `win32` 平台下的全部路径候选（settings / 游标目录 / asar）与 `win_paths.json` 覆盖优先级
- 闸门逻辑、当日去重、状态文件往返
- 模块导入链完整，6 个 `.cmd` 入口可运行
- 非 Windows 上 `install` **明确报错**而不是静默假装成功
- 没有硬编码 mac 路径泄漏进 Windows 版
- **用户可见文案的平台适配**（第 8b 节）：假装是 Windows 后，把通知与报错的正文
  真正渲染出来，断言里面**不出现 `python3`**、且指向 `login.cmd` / `doctor.cmd`
- **watchdog 的独立性**（第 8d 节）：用 `ast` 查它真实的 `import` 语句，断言不依赖任何项目模块；
  并注入 `job_loaded` 覆盖「心跳新鲜 / 心跳过期 / state 缺失 / 任务未注册 / 两者并存 / 未超阈值」
  六种结论，外加**静默期与当日收工后不误报、而真故障照报**两种（2026-09-19 新增）
- **静默判据两版同口径**（第 8d 节末尾）：直接断言 `catchup._day_finished` 与
  `watchdog._day_finished` 一致 —— 前者决定「要不要写心跳」，后者据此判活，判错就会互相打架
- **去重两表制的语义**（第 8e 节）：仅本机送到 → 放行微信补发；已送微信 → 拦一切；
  超窗口 → 都不拦；7 天清理。**写入策略**（第 8f 节）：整个 `notify.py` 只有一处写 `sent` 指纹，
  且必须被 `if wechat_delivered` 守住 —— 直接断言不变量本身，而不是跑一遍网络

### 为什么单列「文案适配」这一节

这类问题是**静默的**：代码逻辑完全正确，只有真出故障、用户照着提示去执行时才发现命令跑不通 ——
而那正是最需要它管用的时候。所以用反向断言把它钉死，而不是靠人工检查字符串。

---

*配套 mac 版文档见 `../00_For_Mac/README.md`。*
