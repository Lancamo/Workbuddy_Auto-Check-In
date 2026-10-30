#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 积分补跑器（带闸门 · 幂等 · 当日去重）—— Windows 版

自包含：引擎是同级 `scripts/` 下的 6 个脚本（checkin / travel / api_discovery /
credentials / http_client / main），**不依赖任何技能、不依赖任何第三方包**。

唯一触发者：**Windows 计划任务**（Task Scheduler，任务名 `WorkBuddyRewardCatchup`）
  - 用户登录时跑一次（LogonTrigger）
  - 此后每 5 分钟一次（重复间隔 PT5M）
  - `StartWhenAvailable=true` → **睡眠/关机期间错过的触发，恢复后立即补跑**
    （这是 launchd「唤醒后补发过期任务」的 Windows 等价物）
  - `DisallowStartIfOnBatteries=false` → 笔记本用电池时也照跑（默认会跳过，必须显式关掉）

  为什么是 5 分钟而不是 30 分钟：脚本周期的**唯一**价值是「事件发生后多快被发现」。
  闸门未开时这次触发只做本地判断（读/写 state.json）就退出，**零网络请求**，
  所以把频率提高的代价只是每天多几百次极轻量的本地唤醒，换来的是
  「07:00 签到」和「猫到达就领」都收敛到 5 分钟以内。

闸门（只在「当日该事项未完成」且「该做它了」时才真正调用 main.py）：
  - 签到：>= 07:00 完成签到（领取 100 积分）+ 旅行派遣
  - 领取：**猫到达即开窗**。到达时间由接口的 arrive_at 给出、落盘在 state.json，
          到点才去领；不知道到达时间时先问一次接口补齐（唯一的「主动问」例外）。
          **没有固定时刻兜底** —— 只按「猫真的到了」这个事实触发。
当日两项都完成后，之后每次触发都直接空转退出、零网络请求。

通知：**签到到账与旅行到账各自独立推送**，谁成功推谁，不合并成一条。
推送发生在事件成功的那一次运行里（事件驱动，不做定时汇总）。

运行时文件（state.json / catchup.log / logs/stdio.log）写在脚本所在目录。
通知：成功 / 失败 / 无活动 一律经 notify.py 推送（默认走 ClawBot 微信直推 —— 复用
WorkBuddy 已绑定的凭据，零配置、免实名；不可用时按 pushplus → Server酱 顺序降级，
全不可用则降级**本机桌面通知**（Windows 弹窗 / Toast）；
凭据见 notify_config.json）；并向 stdout 输出一行 JSON 摘要。

★ 本文件里几处「Windows 特有」的处理（其余为纯业务逻辑）
  1. 解释器用 `winenv.default_python()`（另做 python.exe ↔ pythonw.exe 的切换）——
     整个文件夹拷到任何机器都能直接跑，不需要改任何常量。
  2. 启动即调 `winenv.setup_stdio()`：把输出调成 UTF-8，并在「无控制台」时
     （计划任务 + pythonw.exe）把 stdout/stderr 落到 `logs/stdio.log`，避免
     中文乱码 / `sys.stdout is None` 崩溃 / 无输出无法排障。
  3. 子进程（调 main.py）显式用 UTF-8 解码 + 不弹黑窗。
  4. 顶层异常兜底：任何未捕获异常都报警一次，不留「静默失效」。
"""
import datetime
import json
import os
import pathlib
import subprocess
import sys
import time

DIR = pathlib.Path(__file__).resolve().parent
DIR.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(DIR))            # winenv.py 在本目录
sys.path.append(str(DIR / "scripts"))   # 引擎脚本在 scripts/（api_discovery 也在那边）

import paths   # noqa: E402  runtime / logs / credentials path contract
import winenv  # noqa: E402  跨平台适配层

# ★ Windows 差异 1：解释器 = 正在运行本脚本的解释器（不再硬编码绝对路径）
PY = winenv.default_python()
MAIN = str(DIR / "scripts" / "main.py")   # 内联引擎（原 Skill 的脚本），不依赖技能
LOG = paths.log_path("catchup.log")
STATE = paths.state_path("state.json")
BAK = paths.state_path("state.bak.json")     # state.json 的副本：损坏/被删时用它恢复（见 _save_state）
LOCK = paths.state_path("catchup.lock")      # 排它锁：防止计划任务与手动触发并发（见 _acquire_lock）

# scripts/ 子目录加入模块搜索路径（api_discovery 在那边）。
# 用 append 而非 insert(0)：避免 scripts/ 里的同名文件意外遮蔽根目录的 notify / renew。

import api_discovery  # noqa: E402  接口前置校验（从本机客户端现读端点）
import notify        # noqa: E402  同目录通知模块（微信推送 + 本机通知兜底）
import renew         # noqa: E402  同目录续期守护（ClawBot 会话快到期时提醒用户发消息）

# ── 五个可选模块（2026-10-28 从 Mac 侧同步）──────────────────────────────
# 逐个独立 try/except，理由一致：它们都不是「每天必拿的那部分」。
# 签到与旅行坏了才是事故；这五个坏了只该少一笔收益，不能连累主流程起不来。
# 顶部裸 import 一旦抛错会让整个 catchup 挂掉，所以一律兜住并记下原因。
try:
    import ima       # noqa: E402  ima 每日签到（独立账号体系，见 scripts/ima.py）
    IMA_IMPORT_ERROR = None
except Exception as _e:  # noqa: BLE001
    ima = None
    IMA_IMPORT_ERROR = repr(_e)

try:
    import tasks     # noqa: E402  成长任务自动接受/领取（见 scripts/tasks.py）
    TASKS_IMPORT_ERROR = None
except Exception as _e:  # noqa: BLE001
    tasks = None
    TASKS_IMPORT_ERROR = repr(_e)

try:
    import growth    # noqa: E402  成长空间收益兑现（见 scripts/growth.py）
    GROWTH_IMPORT_ERROR = None
except Exception as _e:  # noqa: BLE001
    growth = None
    GROWTH_IMPORT_ERROR = repr(_e)

try:
    import activity_watch    # noqa: E402  晚间活跃看门狗（见 scripts/activity_watch.py）
    ACTIVITY_IMPORT_ERROR = None
except Exception as _e:  # noqa: BLE001
    activity_watch = None
    ACTIVITY_IMPORT_ERROR = repr(_e)

# 积分余额（见 scripts/credits.py）：快照末尾那一行「积分余额：」用。
# 同样可选 —— 查不到余额只是少一行字，绝不能让整条快照发不出去。
try:
    import credits    # noqa: E402
    CREDITS_IMPORT_ERROR = None
except Exception as _e:  # noqa: BLE001
    credits = None
    CREDITS_IMPORT_ERROR = repr(_e)

WINDOW1 = 700       # 07:00 签到 + 派遣（「≥ 时间」语义，过了点补跑）
QUIET_FROM = 700    # 07:00 —— 在此之前完全静默（见 _quiet_now）
MAX_TRIES = 12      # 单事项最多真实尝试次数（业务层失败：401 / 接口错误等）
MIN_RETRY_GAP_MIN = 20   # 两次真实尝试之间的最小间隔（分钟）
NET_RETRY_GAP_MIN = 30   # 网络层失败的重试间隔（分钟）—— 网络失败不烧配额，低频重试到当天结束
PROBE_GAP_MIN = 30       # 业务预算耗尽后的只读探测间隔（分钟）—— 探测恢复
TASKS_GAP_MIN = 60       # 成长任务巡检间隔（分钟）—— 见 _maybe_run_tasks
GROWTH_GAP_MIN = 60      # 成长空间兑现间隔（分钟）—— 见 _maybe_run_growth

# ── 本地环境未就绪（2026-10-19 加）────────────────────────────────────────
# Windows 侧 accessToken 要靠**扫 WorkBuddy 进程内存**取 at-rest 密钥才能解开，
# 客户端没运行就一定解不出来。而计划任务的「唤醒后触发」总比 Electron 客户端
# 先起来几十秒 —— 2026-10-19 实测：任务 11:27:21 触发，客户端 11:28:21 才启动，
# 差 60 秒，于是报了一次「登录态读取失败」并弹窗，20 分钟后退避期满又自己好了。
#
# 这跟「401 / 接口报错」性质完全不同：它并没有真的去请求，也不烧服务端配额，
# 只是**开机竞态**。所以单独归类为 local：
#   · 不计入 MAX_TRIES（否则一天几次唤醒就能把 12 次预算耗光）
#   · 用 LOCAL_RETRY_GAP_MIN 短间隔重试（客户端就绪后下一个 tick 即可成功）
#   · 前 LOCAL_ALERT_AFTER-1 次完全静默，第 LOCAL_ALERT_AFTER 次才告警一次
#     （防「用户真的登出了 / 登录态文件没了」被永久静默）
LOCAL_RETRY_GAP_MIN = 5   # local 失败（未达告警阈值）的重试间隔（分钟）
LOCAL_ALERT_AFTER = 3     # 连续第 N 次仍失败才告警（≈ 10–15 分钟）
LOCAL_REALERT_EVERY = 12  # 越过阈值后每 N 次再提醒一次（20 分钟 × 12 ≈ 4 小时）
# ★ 2026-10-21：同类别失败的**全局**告警节流（对所有 fail_kind 生效）。
#   以前只有 local / net 有降噪，biz 失败是「每次失败都报一条」——而重试间隔
#   20 分钟，一天最多 12 次预算，就是 12 条通知。10-20 连发 12 条、10-21 连发
#   11 条（外加 8 条补弹），用户直接被打到关掉通知。这才是真正的「通知方案」
#   缺陷：降噪只覆盖了两类失败，漏掉了最常见的那一类。
FAIL_ALERT_EVERY_MIN = 180   # 同类别失败最多每 3 小时提醒一次（首次仍立即报）
                          # 为什么需要：若用户真的登出了，local 会一直失败且不消耗
                          # MAX_TRIES（没有上限）。只报一次会永久静默，每 tick 都报
                          # 又会一天几十条 —— 折成「一次 + 每 4 小时一次」。
LOCK_STALE_SEC = 600     # 超过 10 分钟未释放的锁视为残留（正常一次运行只需几秒）

# 为什么需要 MIN_RETRY_GAP_MIN：触发频率从 30 分钟提到 5 分钟后，若不加间隔，
# MAX_TRIES=12 会在 1 小时内烧完，把「重试」变成「刷请求」。加 20 分钟间隔后，
# 12 次尝试 ≈ 4 小时，节奏与原来的 30 分钟触发基本一致。
#
# 为什么不做「固定时刻兜底」：领取的唯一正当理由是**猫真的到了**。
# 时间兜底会凭空制造一次请求，而且「到点电脑恰好睡着」反而是它最不可靠的时候。
# 到达时间由接口给出（travel 的 arrive_at）；缺失时下面的 need_arrive_info 会去问一次。
#
# ── 静默（2026-09-19 加）────────────────────────────────────────────────
# 触发由系统维护唤醒带起时，很多次触发根本无事可做（凌晨、或当天早已领完）。
# （★ Windows 上没有系统的周期维护唤醒，只有 install.py 注册的每日 07:00 唤醒器。）
# 这些触发若照常跑完整流程，就要付出「解释器启动 + 读 state + 续期检查 + 端点
# 探测 + 写日志写状态」的全套开销，纯属空转。两条静默规则把它们压到最小：
#   ① 00:00–07:00         → 连锁都不抢，零落盘（_quiet_now）
#   ② 当天已完成          → 不再做任何事（_day_finished）
# 静默只省开销，**不改闸门逻辑**；设计与取证见 Plan/2026-09-19-静默期与唤醒策略.md。


MAX_LOG_BYTES = 512 * 1024   # 日志超过这个大小就裁剪（触发频率提到 5 分钟后需要）


def log(msg: str) -> None:
    paths.rotate_log(LOG, max_bytes=MAX_LOG_BYTES, keep_lines=800)
    # ★ Windows 差异：显式 UTF-8（Windows 默认 GBK，中文日志会乱码/报错）
    with LOG.open("a", encoding="utf-8", errors="replace") as f:
        f.write(f"[{datetime.datetime.now():%F %T}] {msg}\n")


def _log_once_a_day(key: str, msg: str) -> None:
    """同一个「良性状态」每天只记一次日志。

    为什么需要：触发频率是 5 分钟一次（288 次/天）。像「ima 未配置凭据」这种
    **正常状态**若每轮都写一行，一天 288 行，`rotate_log` 只留 800 行 ——
    两天就把真正有用的历史挤没了，排查时反而看不到东西。
    只用于「持续存在的良性状态」；**真错误仍然每轮照写**，那是要看清楚的信号。
    """
    today = datetime.date.today().isoformat()
    try:
        flag = paths.cache_path("once_{}.day".format(key))
        if flag.exists() and flag.read_text(encoding="utf-8").strip() == today:
            return
        flag.write_text(today, encoding="utf-8")
    except OSError:
        pass          # 写标记失败就照常记（宁可多一条，也不要漏掉状态）
    log(msg)


def _load_state() -> dict:
    """读 state.json；读不到或损坏时回退 state.bak.json。

    为什么要有备份：state.json 一旦损坏/被误删，`arrive_at` 就丢了 ——
    那是「到达即领」的唯一判据。虽然 need_arrive_info 会去接口补问一次，
    但补问受 MAX_TRIES 限制，多花一次请求。一行备份能把这类损失降到 0。
    """
    for i, p in enumerate((STATE, BAK)):
        try:
            if p.exists():
                d = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    if i:      # 走的是备份 → 值得记一笔
                        log("state.json 不可用，已从 state.bak.json 恢复")
                    return d
        except Exception:  # noqa: BLE001 — 损坏就当它不存在，继续试下一个
            continue
    return {}


def _save_state(state: dict) -> None:
    """写 state.json，并同步一份 state.bak.json。"""
    payload = json.dumps(state, ensure_ascii=False)
    STATE.write_text(payload, encoding="utf-8")
    try:
        BAK.write_text(payload, encoding="utf-8")
    except OSError:
        pass
    paths.secure_runtime_files()


def _acquire_lock(now_ts: float) -> bool:
    """抢排它锁，防止「计划任务」与「手动 run_now」并发跑同一件事。

    为什么需要：两条路径都会「读 → 改 → 写」state.json，并发时后写的覆盖先写的 ——
    典型后果是 checkin_tries 被退回 0（当天多打几次接口），或者 arrive_at 被旧值覆盖。
    签到接口本身幂等，所以危害有限；但本地状态是真的会被写坏，排查起来极难。
    用 O_CREAT|O_EXCL 原子创建；若发现残留锁（持锁进程已死 或 超过 LOCK_STALE_SEC）则抢占。

    ★ 存活判断走 `winenv.pid_alive()` —— Windows 上不能直接 os.kill（见那边的说明）。
    """
    try:
        fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        pass
    except OSError as e:  # noqa: BLE001 — 锁机制本身出问题不应阻断主流程
        log(f"创建锁失败（忽略，继续运行）：{e!r}")
        return True

    try:
        age = now_ts - LOCK.stat().st_mtime
        pid_txt = LOCK.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return False
    alive = False
    if pid_txt.isdigit():
        alive = winenv.pid_alive(int(pid_txt))
    if alive and age < LOCK_STALE_SEC:
        return False
    log(f"接管残留锁（pid={pid_txt} 存活={alive} 已存在 {age:.0f}s）")
    try:
        LOCK.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        return False
    return True


def _release_lock() -> None:
    try:
        LOCK.unlink()
    except OSError:
        pass


# 失败原因的措辞 → 该让用户做什么。
# 为什么必须分类：以前所有失败都附同一句「打开一次 WorkBuddy 桌面端刷新登录态」，
# 但这句话只对「登录态过期」成立 —— 接口变了 / 网络不通时让用户去点桌面端纯属误导。
_AUTH_HINTS = ("401", "403", "unauthorized", "登录", "凭据", "令牌", "token 失效")
# ★ 网络类的关键词必须**同时覆盖中英两种措辞 + WinError 号**。
#   2026-09-20 实测的坑：Windows 的中文报错是**全本地化**的，不含英文 "connection" ——
#     端口没人监听时会给出
#       「<urlopen error [WinError 10061] 由于目标计算机积极拒绝，无法连接。>」
#   原列表只有英文 "connection"，于是这句落进「未知」分支，
#   拿到的提示是「跑一次 run_now.cmd 看详细报错」—— 而 README 排错表专门为这个
#   场景（系统代理端口未启动 / TLS 拦截）写了处置办法，等于没传到用户手里。
#   WinError 号（10061 等）与界面语言无关，是最可靠的一路信号。
_NET_HINTS = ("timeout", "timed out", "超时", "网络", "connection", "ssl", "proxy", "代理",
              "积极拒绝", "无法连接", "连接被", "连接超时", "远程主机", "名解析",
              "winerror", "10061", "10060", "10054", "10053", "11001", "11002")
_DRIFT_HINTS = ("404", "405", "不存在", "not found", "接口", "endpoint", "路由")


def _hint_for(reasons: list[str]) -> str:
    """按失败原因给出对应的处理动作（返回一句给用户看的话）。"""
    low = " ".join(str(r or "") for r in reasons).lower()
    if any(k in low for k in _DRIFT_HINTS):
        # ★ 这里必须走 winenv.selfcheck_hint()，不能自己写命令。
        #   本文件另有 4 处同类「自查端点」的指引都用的它，只有这一处漏了、
        #   硬编码了 `py -3 scripts\api_discovery.py`。后果有二：
        #     ① 违反「平台差异只许写在 winenv.py」的硬规则；
        #     ② README 承诺「Windows 上不会给你一条跑不通的命令」——
        #        而 `py -3` 依赖 py 启动器（可选组件），且它指向的解释器未必是
        #        跑本脚本的那个（本机 py -3 = 3.14，实际跑的是 3.11）。
        #   统一后文案变成「双击本文件夹下的 doctor.cmd」，与别处一致且必然可用。
        return ("像是接口变了，脚本已尝试自动跟随。自查方式：{}；"
                "若仍失败，多半需要等客户端升级。".format(winenv.selfcheck_hint()))
    if any(k in low for k in _AUTH_HINTS):
        # ★ 开机竞态专属提示（2026-10-19）：Windows 侧令牌靠扫客户端进程内存才能解，
        #   客户端没起来就一定失败。这句话比「刷新登录态」更贴近真实原因。
        if "未能解出密钥" in low or "**正在运行**" in low or "正在运行" in low:
            return ("像是 WorkBuddy 客户端还没启动完 —— 开机/唤醒后脚本会比客户端早跑"
                    "几十秒，读不到加密的登录态。脚本会每 5 分钟自动重试，"
                    "客户端起来后即可成功；若持续失败，请确认客户端已打开并处于登录状态。")
        return "打开一次 WorkBuddy 桌面端即可刷新登录态，之后脚本会自动重试。"
    if any(k in low for k in _NET_HINTS):
        return ("像是网络问题，脚本会在 20 分钟后自动重试。若持续失败，"
                "检查网络或代理 —— 本工具默认绕过**系统代理**直连；"
                "若挂着 VPN / 代理，请确认它能访问 codebuddy.cn，"
                "并确认没有残留的 HTTP_PROXY/HTTPS_PROXY 指向已关闭的端口。")
    return "脚本会在 20 分钟后自动重试；若连续失败，手动跑一次 run_now.cmd 看详细报错。"


def _notify(title: str, content: str, level: str) -> dict:
    """发通知并把结果写进日志。

    为什么必须记：notify 的失败**不会抛异常**，只在返回值里说明原因。以前这里
    直接把返回值丢掉，导致「微信没收到」时日志里看不出任何异常 —— 排查时只能靠猜。
    """
    r = notify.send(title, content, level)
    # wechat 与 sent 必须分开记：sent=True 只说明"某个通道送到了"（可能是本机弹窗），
    # wechat=True 才说明手机微信真的收到了。2026-09-18 就是因为只有 sent 而误判通道正常。
    log("notify[{}] sent={} wechat={} channel={} reason={}".format(
        level, r.get("sent"), r.get("wechat"), r.get("channel") or "-", r.get("reason") or "-"))
    return r


def _act(c: dict) -> str:
    """签到结果里的活动名后缀（如「（高校新生攻略）」），无则空串。"""
    name = c.get("activity") or ""
    return "（{}）".format(name) if name else ""


# ★ 2026-10-21：local 失败在通知里只说人话。
#   原始 reason 是给 doctor 看的（含 C:\Users\...\workbuddy-desktop.info 这种路径
#   和 WORKBUDDY_ATREST_KEY 这种环境变量名），塞进通知卡片会变成两屏技术细节。
#   用户第一眼需要的是「客户端没开」，不是「哪条路径读不到」。
_LOCAL_REASON_SHORT = "读不到登录态：WorkBuddy 客户端没有在运行"


def _short_reason(res: dict) -> str:
    """通知里展示的失败原因（短版）。详细原因仍在日志与 doctor 里。"""
    raw = str(res.get("reason") or "未知原因")
    if res.get("fail_kind") == "local":
        return _LOCAL_REASON_SHORT
    return raw if len(raw) <= 160 else raw[:160] + "…"


def _fmt_checkin(c: dict, done: bool = False, month_days: int | None = None) -> str:
    """签到的**正文行**（不含「1、WorkBuddy 签到：」标题，标题由 `_compose` 加）。

    2026-10-28：这里的「连续 N 天」改成**本月**连登天数（`month_days`，来自成长空间的
    streak 接口）——连登兑换（7/14/28 天档）与补登卡都按这个口径算，正文放同一个数，
    用户才能对上账。跨月累计的连签成绩挪到消息标题（见 `_snapshot_title`）。
    两个数不是一回事：签到接口的 streak_days 是累计（10-28 报 29），
    成长空间的连登是本月的（同日 28），每月 1 日清零。
    """
    st = c.get("status")
    if not st:                      # 本轮没去查签到（只有旅行要处理）
        return "今日已到账（本轮未重查）" if done else "待领取（07:00 后自动签到）"
    # done：state 里已记为完成，本次 failed 只是整批调用被网络故障拖挂，不代表没领到
    if done and st == "failed":
        return "今日已到账（本次查询失败，积分不受影响）"
    if st == "success":
        body = "成功，+{} 积分{}".format(
            c.get("credit") if c.get("credit") is not None else "?", _act(c))
        if month_days:
            body += "，本月连续签到 {} 天".format(month_days)
        return body
    if st == "already_checked":
        return "今日已领过{}".format(_act(c))
    if st == "no_activity":
        return "当前没有进行中的活动（空档期不计积分）"
    if st == "suspect":
        return "⚠️ 接口返回的数据全为零，无法确认活动状态"
    if st == "failed":
        # ★ Win 保留 `_short_reason`：Mac 直接印原始 reason，而 Windows 的 local 失败
        #   reason 里带 C:\Users\...\workbuddy-desktop.info 与 WORKBUDDY_ATREST_KEY，
        #   塞进通知就是两屏技术细节。分类逻辑不变，只换展示文案。
        return "失败 —— {}".format(_short_reason(c))
    return "待领取（自动重试中）"


def _duty_tag(duty: dict | None, note: str = "") -> str:
    """在岗猫的短标记：` · 暴富喵（UR）`；取不到就返回空串。

    为什么要显示：旅行是「哪只猫回家带信」的唯一可见反馈，而派哪只由服务端按
    「当前在岗猫」决定。不写出来的话，用户换了猫、或猫被换掉，消息里完全看不出来。

    这个函数要同时吃两种形状（刻意在这里归一，而不是让两边各自记着自己的形状）：
      · 派遣结果里的 duty：{"status":…, "current":{name,rarity}, "previous":…}
      · state 里落盘的 duty：{"name":…, "rarity":…}（只为「领取时补名字」而存）

    note：附加说明（如「换猫失败」），与品级一起放进括号，避免多打一层括号。
    """
    d = duty or {}
    cur = d.get("current") or (d if d.get("name") else {})
    name = cur.get("name")
    if not name:
        return ""
    # 品级与技能用「·」连，说明文字（note）另用「，」分隔 —— 三者语义不同层次。
    # 技能只有已知名单里才显示（见 buddy.SKILLS），没有就不显示，不占位。
    head = "·".join(x for x in (cur.get("rarity"), cur.get("skill")) if x)
    inner = "，".join(x for x in (head, note) if x)
    return " · {}{}".format(name, "（{}）".format(inner) if inner else "")


def _fmt_travel(t: dict, done: bool = False, duty: dict | None = None) -> str:
    """旅行奖励的正文行（标题由 `_compose` 加）。"""
    st = t.get("status")
    # 派遣那一次的结果里带着当次要派的猫；领取那一次不带（claim 不查桌宠），
    # 所以用 state 里落盘的 duty（派遣时写入）来补上名字。
    td = t.get("duty") or {}
    tag = _duty_tag(td or duty)
    if not st:                      # 本轮没去查旅行（只有签到要处理）
        return "今日已领取（本轮未重查）" if done else "待领取（07:00 后自动派猫）"
    # done：奖励已领取，本次 failed 只是整批调用被网络故障拖挂，不代表没领到
    if done and st == "failed":
        return "奖励已领取（本次查询失败，积分不受影响）"
    if st == "claimed":
        return "猫已到达{}，奖励领取 +{} 积分".format(
            tag, t.get("reward_credit") if t.get("reward_credit") is not None else "?")
    if st == "departed":
        # ① 换猫失败：出门的是「不是目标的猫」，必须写出来 —— 静默失败正好是
        #    本项目最忌讳的那种「看着正常，其实没做到」。
        # ② 纠偏成功：说清「换回来了」，否则用户不知道发生过替换。
        if td.get("status") == "failed":
            tag = _duty_tag(td, "换猫失败")
            fixed = ""
        elif td.get("status") == "switched":
            prev = (td.get("previous") or {}).get("name")
            fixed = "｜已从 {} 换回".format(prev) if prev else ""
        else:
            fixed = ""
        return "猫已出发{}，到达后自动领取{}".format(tag, fixed)
    if st == "traveling":
        return "待领取（猫还在路上）"
    if st == "daily_limit_reached":
        return "今日已达上限（已领过）"
    if st == "failed":
        return "失败 —— {}".format(_short_reason(t))
    return "待领取（自动重试中）"


def _snapshot_title(state: dict, ima_item: dict | None,
                    streak_days: int | None = None) -> str:
    """快照消息的标题：只报「今日办完几件」，不报金额。

    金额分散在三条不同的账里（WorkBuddy 积分 / 旅行积分 / ima 算力），
    标题里相加是错的、分行写出又太长 —— 金额交给正文第一行。

    2026-10-28：标题带上「连续 N 天」作为**长期成就**展示（用户要求）。
    这个数是签到接口的跨月累计连签（10-28 是 29 天），与正文里「本月连续签到」
    （成长空间的连登，同日 28 天）不是同一个口径 —— 前者是成绩，后者决定档位。

    Win 侧补充：`streak_days` 取不到时（already_checked 那轮接口不回这个字段）
    由调用方传 state 里上次记下的值，见 `_report`。
    """
    done = [bool(state.get("checkin_done")), bool(state.get("claim_done"))]
    if ima_item:
        done.append(bool(ima_item.get("done")))
    today = "{}/{} 完成".format(sum(1 for x in done if x), len(done))
    if streak_days:
        return "每日签到：连续 {} 天 · {}".format(streak_days, today)
    return "每日签到：" + today


def _compose(state: dict, c: dict, t: dict, ima_item: dict | None, stamp: str,
             month_days: int | None = None, brief: bool = False,
             balance: int | None = None) -> str:
    """把三项结果拼成一条统一格式的正文（2026-10-22 用户指定，全站一致）：

        1、WorkBuddy 签到：
        成功，+100 积分，本月连续签到 23 天
        2、WorkBuddy 旅行：
        猫已到达，奖励领取 +5 积分
        3、ima 算力：
        成功，+50 算力，本周已签 7 天（满签✓）

        今日进度：签到 ✓ / 旅行 ✓ / ima ✓
        积分余额：2599
        时间：2026-10-22 08:02

    未到账的事项照同一格式写「待领取（…）」，不换一种排版 —— 三种状态（到账 /
    待领取 / 失败）长得一样，才一眼看得出「今天还差哪件」。

    ima_item 为空（模块未接入 / 未配置凭据 / 本轮没拿到结果）时，第 3 项与进度里的
    ima 一并省略，不写「未接入」这种自曝内部结构的话。

    brief=True（同一天的第二条及以后，2026-10-28 加）：**只列本轮真有新结果的项**。
    第一条已经把「签到成功」「猫已出发」「ima 已签」都说过了，第二条再说一遍
    「今日已领过」只是把消息撑长；用户要的是「这条消息带来了什么新东西」。
    「今日进度」与「时间」仍然保留 —— 它们说明的是当天整体到哪一步，不受本条影响。

    balance（2026-10-30 加）：当前可用积分，压在「今日进度」与「时间」之间。
    None = 本轮没查到（网络 / 登录态问题）→ **整行不显示**，不写「暂不可用」占位：
    一行永远在的占位反而让人以为余额一直是坏的，而实际只是那一轮没取到。
    """
    # ── 哪些项要出现 ─────────────────────────────────────────────────────
    show_checkin = (not brief) or c.get("status") == "success"
    show_travel = (not brief) or t.get("status") in (
        "claimed", "departed", "daily_limit_reached", "failed")
    show_ima = bool(ima_item) and ((not brief) or bool(ima_item.get("new")))

    items = []
    if show_checkin:
        items.append(("WorkBuddy 签到",
                      _fmt_checkin(c, bool(state.get("checkin_done")), month_days)))
    if show_travel:
        items.append(("WorkBuddy 旅行",
                      _fmt_travel(t, bool(state.get("claim_done")), state.get("duty"))))
    marks = ["签到 " + ("✓" if state.get("checkin_done") else "…"),
             "旅行 " + ("✓" if state.get("claim_done") else "…")]
    if ima_item:
        marks.append("ima " + ("✓" if ima_item.get("done") else "…"))
        if show_ima:
            items.append(("ima 算力", ima_item.get("body") or "待领取（自动重试中）"))

    if not items:
        # brief 过滤后一条都不剩：宁可退回完整版，也不发一条只有进度和时间的空消息
        return _compose(state, c, t, ima_item, stamp, month_days, brief=False,
                        balance=balance)

    lines = []
    for idx, (head, line) in enumerate(items, 1):
        lines.append("{n}、{h}：".format(n=idx, h=head))
        lines.append(line)
    # 空行把「三项清单」和「进度/时间」分开（2026-10-22 用户要求）。
    # 这里的空串经 join 变成连续两个换行，投递层再转成两个 U+2028 ——
    # 微信里就是一个真正的空行；单个 U+2028 只会换行不留空。
    lines.append("")
    lines.append("今日进度：" + " / ".join(marks))
    if balance is not None:
        lines.append("积分余额：{}".format(balance))
    lines.append("时间：" + stamp)
    return "\n".join(lines)



def _fail_signature(failed: list, c: dict, t: dict) -> str:
    """同类失败的稳定标识：失败事项 + 失败类别。

    **刻意不含时间**：指纹若带时间戳，每次失败都是「不同内容」，去重永远不命中
    —— 这正是 10-20/10-21 连发 11–12 条的直接机制。用「事项 + 类别」做键，
    同一类故障在时间窗内就只报一次，恢复后键自然消失。
    """
    kinds = sorted({str(v.get("fail_kind") or "biz")
                    for v in (c, t)
                    if isinstance(v, dict) and v.get("status") == "failed"})
    return "{}|{}".format("+".join(failed), "+".join(kinds) or "biz")


def _fail_alert_due(state: dict, sig: str, now_ts: float):
    """这类失败现在该不该真发通知。返回 (是否发, 距上次的分钟数)。"""
    if state.get("fail_alert_key") != sig:
        return True, 0
    last = float(state.get("fail_alert_ts") or 0)
    if last <= 0:
        return True, 0
    gap = (now_ts - last) / 60.0
    return gap >= FAIL_ALERT_EVERY_MIN, int(gap)


def _report(c: dict, t: dict, state: dict, pf: dict | None = None,
            ima_item: dict | None = None) -> bool:
    """按结果推送通知。

    **一条消息说完三项**（2026-10-28 与 Mac 侧统一，原为签到、旅行各推一条）：
    用户要的是「今天办到哪一步」的一张快照，而不是三次独立的到账流水。所以到账与
    待领取合并成同一条编号消息，格式见 `_compose`；日志里仍逐项记录，排查不受影响。

    **失败仍然单独推**：失败是可操作的告警，塞进快照里会被当成日常进度划过去；
    而且失败信息要自带处理建议，长度也不是快照能容纳的。

    **第二条只列变化**：同一天第二次发消息时走 `brief=True`（见 `_compose`）。
    判据是 state 里的 `msg_first_sent`，跨日重置时自然清掉。

    ima_item：ima 当次结果（见 `scripts/ima.py: run_daily()` 的 `item`），
    为空表示本轮没有 ima 结果（未配置凭据 / 未到点 / 模块不可用）。
    返回值：ima 那一项是否已经跟着消息发出去了（False 时调用方要自己补发）。
    """
    pf = pf or {}
    # 只要本轮有「确实好起来了」的迹象，就把失败节流的窗口清零 ——
    # 否则修复后再次出问题，会被上一轮的 3 小时窗口误伤成「静默」。
    if (c.get("status") in ("success", "already_checked")
            or t.get("status") in ("claimed", "daily_limit_reached")):
        if state.get("fail_alert_key"):
            log("故障已恢复（fail_alert_key={}），重置告警节流".format(
                state.get("fail_alert_key")))
        state["fail_alert_key"] = None
        state["fail_alert_ts"] = 0
    checkin_gain = c.get("status") == "success" and c.get("credit") is not None
    travel_gain = t.get("status") == "claimed" and t.get("reward_credit") is not None
    ima_gain = bool(ima_item) and bool(ima_item.get("new"))

    done_map = {"签到": bool(state.get("checkin_done")),
                "旅行": bool(state.get("claim_done"))}
    # 只把**真正待办**的事项计入失败：已完成的事项即便本次调用返回 failed 也不该报。
    # 2026-09-26 的教训 —— 08:23 签到已成功，10:23 网络故障让 main.py all 整批返回
    # failed，告警却写「签到、旅行 失败」，与实际状态不符，白造一次误报。
    failed = [n for n, v in (("签到", c), ("旅行", t))
              if v.get("status") == "failed" and not done_map[n]]
    suspect = c.get("status") == "suspect"
    no_activity = c.get("status") == "no_activity"
    # 前置校验异常：只剩兜底端点可用，或探活全失败
    pf_bad = bool(pf) and (pf.get("probed") and not pf.get("ok"))
    # 端点相对上次发生了变化（客户端升级导致接口换名/换前缀）—— 这是要主动报的
    ep_changed = bool(pf.get("changed"))
    # 交叉比对（查 vs 领）发现的漂移线索 —— checkin / travel 各自可能给出
    drift = c.get("drift_suspect") or t.get("drift_suspect")

    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    # 2026-10-28 两个口径分开：
    #   正文「本月连续签到」= 成长空间的连登（决定兑换档位与补登），读一次 streak 接口；
    #   标题「连续 N 天」    = 签到接口的跨月累计（成绩展示），优先用本轮结果，
    #                         already_checked 那轮拿不到就用上次记下的。
    month_days = growth.current_streak_days() if growth is not None else None
    # 积分余额（2026-10-30 加）：只读查询，短超时不重试。取不到就 None ——
    # 少显示一行，不影响本条的成败判定（余额不是今天要做的事）。
    bal = None
    if credits is not None:
        _r = credits.query_balance()
        bal = _r.get("total")
        if bal is None:
            log("credits: 余额未取到 —— {}".format(_r.get("__error__")))
    if c.get("streak_days"):
        state["checkin_streak_days"] = c["streak_days"]
    streak_days = c.get("streak_days") or state.get("checkin_streak_days")
    # 今天已经发过快照 → 本条只列新变化（见 _compose 的 brief 说明）
    brief = bool(state.get("msg_first_sent"))

    def _add_ep_change(text: str) -> str:
        """端点刚被自动切换时，在正文尾部补一段说明（Win 侧原有行为，保留）。"""
        if not ep_changed:
            return text
        old = ((pf.get("changed_from") or {}).get("status_paths") or [])[:2]
        return text + "\n\n🔄 接口已切换（客户端 {}）：\n  旧：{}\n  新：{}".format(
            pf.get("client_version") or "?", "\n      ".join(old) or "(无)",
            pf.get("resolved_status") or pf.get("preferred_status") or "?")

    def _merged_same_root() -> str:
        """签到与旅行「同因失败」时的合并正文（★ 2026-10-21 Win 侧修复，必须保留）。

        两者都要读同一份登录态，读不到就一起失败、reason 逐字相同。原来会把同一段
        300 字技术细节连印两遍（10-21 的通知卡片就是两屏几乎一样的文字），用户读完
        还不知道该干什么。合并后一行说清「两件事都没做，原因是一个」。
        """
        return "\n".join([
            "签到 / 旅行：都未执行 —— {}".format(_short_reason(c)),
            "",
            "今日进度：签到 {} / 旅行 {}".format(
                "✓" if state.get("checkin_done") else "…",
                "✓" if state.get("claim_done") else "…"),
            *(["积分余额：{}".format(bal)] if bal is not None else []),
            "时间：{}".format(stamp),
        ])

    c_reason, t_reason = str(c.get("reason") or ""), str(t.get("reason") or "")
    same_root = (c.get("status") == "failed" and t.get("status") == "failed"
                 and c_reason and c_reason == t_reason
                 and not done_map["签到"] and not done_map["旅行"])

    body = _add_ep_change(_compose(state, c, t, ima_item, stamp, month_days,
                                   brief=brief, balance=bal))

    if failed or suspect or pf_bad:
        # 告警必须完整：brief 版会省掉「今日已领过」，而故障时用户正是要对照全部三项
        if same_root and not suspect and not pf_bad:
            body = _add_ep_change(_merged_same_root())
        else:
            body = _add_ep_change(
                _compose(state, c, t, ima_item, stamp, month_days, brief=False,
                         balance=bal))
        # ★ 2026-10-21：失败告警节流（兜底闸，对所有 fail_kind 生效）。
        #   上游的 _local_quiet / net_only 只覆盖 local 与 net；biz 失败以前是
        #   「每次失败一条」，20 分钟一跳、12 次预算 = 12 条。
        #   这里按「事项 + 类别」做稳定键，同一类故障 FAIL_ALERT_EVERY_MIN 内只报
        #   一次，首次仍立刻报（这样既不漏消息，也不会把通知栏刷满）。
        #   suspect / pf_bad 不节流：它们是接口层故障，出现频率极低，且必须马上处置。
        if failed and not suspect and not pf_bad:
            sig = _fail_signature(failed, c, t)
            due, gap = _fail_alert_due(state, sig, time.time())
            if not due:
                log("失败告警节流：同类别（{}）{} 分钟前已报过，本次只记日志"
                    "（窗口 {} 分钟）".format(sig, gap, FAIL_ALERT_EVERY_MIN))
                return False
            state["fail_alert_key"] = sig
            state["fail_alert_ts"] = time.time()
        if drift and failed:
            # 漂移优先于「失败」：这类故障的处置是「核对端点」，
            # 不是「等重试」也不是「去刷登录态」，标题必须先把性质说清楚。
            title = "WorkBuddy 积分：接口疑似已变更（" + "、".join(failed) + "）"
        elif failed:
            # ★ 2026-10-21：标题先说人话。全是 local 失败（客户端没开）时，
            #   用「没签到」比「补跑失败」更能让人判断要不要管 —— 后者听起来
            #   像脚本坏了，实际只是客户端还没启动、脚本会自己重试。
            local_only = all(
                str(v.get("fail_kind") or "") == "local"
                for v in (c, t) if v.get("status") == "failed")
            if local_only:
                title = "WorkBuddy 客户端没开，今天还没签到"
            else:
                title = "WorkBuddy 积分补跑失败：" + "、".join(failed)
        else:
            title = "WorkBuddy 积分：接口可能已变更，需要处理"
        extra = "\n\n处理建议：" + _hint_for(
            [str(c.get("reason") or ""), str(t.get("reason") or "")])
        if suspect:
            extra = ("\n\n⚠️ 接口返回了一份**合法但全零**的数据（活动名/结束时间/累计积分/"
                     "签到日期全为空），因此无法确认到底有没有活动。\n"
                     "这正是 2026-09-17 旧接口失效时的特征 —— 多半是活动接口又换了。\n"
                     "自查方式：{}".format(winenv.selfcheck_hint()))
        elif pf_bad:
            extra = ("\n\n⚠️ 前置校验未能确认可用端点：{} 个候选都没探到可信数据。\n"
                     "自查方式：{}".format(len(pf.get("tried") or []),
                                        winenv.selfcheck_hint()))
        if drift:
            extra += "\n\n🔄 交叉比对（查 vs 领）发现的漂移线索：{}".format(drift)
        _notify(title, body + extra, "failure")
    elif checkin_gain or travel_gain or ima_gain:
        _notify(_snapshot_title(state, ima_item, streak_days), body, "success")
        # 首条已发：之后同一天的消息一律走 brief（只列新变化）
        state["msg_first_sent"] = True
    elif no_activity and state.get("no_activity_streak", 0) >= 3:
        # 防「静默失效」保险：连续多日报无活动时，多半是接口/活动判定出了问题
        # （2026-09-17 就因旧接口恒返回 active=false，连续静默了整整一轮活动）。
        _notify(
            "WorkBuddy 积分：连续 {} 天报「无活动」，建议人工核对".format(
                state.get("no_activity_streak")),
            body + "\n\n⚠️ 请打开 WorkBuddy 桌面端确认积分页是否真有活动。\n"
            "若页面有活动而脚本报无活动，说明活动接口又变了，"
            "自查方式：{}。".format(winenv.selfcheck_hint()),
            "failure")
    elif no_activity:
        _notify(
            "WorkBuddy 积分：当前没有进行中的活动",
            body + "\n\n说明：活动空档期不产生积分，脚本会继续每日巡检，活动开启后自动恢复。",
            "warning")
    else:
        # 常规巡检：但若端点刚变过，提升为 warning 并改标题，确保不被当成噪音划过去
        _notify(
            "WorkBuddy 积分：接口已自动切换" if ep_changed
            else _snapshot_title(state, ima_item, streak_days),
            body + ("\n\n说明：端点已自动改用客户端当前使用的新路径，不用你操作。"
                    if ep_changed else ""),
            "warning" if ep_changed else "info")

    return bool(ima_item)


def run_all(tag: str) -> dict:
    log(f"run ({tag})")
    try:
        # ★ Windows 差异 3：显式 UTF-8 解码（Windows 管道默认按 GBK 解，中文 JSON 会碎）
        #   + 不弹黑窗（计划任务里调 python.exe 子进程会闪一下）
        p = subprocess.run(
            [PY, MAIN, "all"], capture_output=True, text=True, timeout=180,
            encoding="utf-8", errors="replace",
            cwd=str(DIR), env=winenv.subprocess_env(), **winenv.subprocess_flags(),
        )
    except Exception as e:  # noqa: BLE001
        log(f"exec error: {e!r}")
        return {}
    out = (p.stdout or "").strip()
    if p.stderr:
        log("stderr: " + p.stderr.strip()[:500])
    log("stdout: " + out[:500])
    try:
        return json.loads(out.splitlines()[-1])
    except Exception:  # noqa: BLE001
        return {}


def run_status() -> dict:
    """只读探测：调用 main.py status（不领取 / 不派遣），用于「探测恢复」。

    当业务预算（MAX_TRIES）耗尽、但当天该事项仍未完成时，不再做正式尝试，
    改为低频只读查询一次状态 —— 一旦外部条件恢复（登录态刷新 / 网络恢复 /
    活动重开），状态查询会返回 ok，据此重置预算、恢复正式签到。
    """
    log("run (probe status)")
    try:
        # ★ Windows 差异：显式 UTF-8 解码 + 不弹黑窗（与 run_all 一致）
        p = subprocess.run(
            [PY, MAIN, "status"], capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace",
            cwd=str(DIR), env=winenv.subprocess_env(), **winenv.subprocess_flags(),
        )
    except Exception as e:  # noqa: BLE001
        log(f"probe exec error: {e!r}")
        return {}
    out = (p.stdout or "").strip()
    if p.stderr:
        log("probe stderr: " + p.stderr.strip()[:500])
    try:
        return json.loads(out.splitlines()[-1])
    except Exception:  # noqa: BLE001
        return {}


def _claim_open(now_ts: float, state: dict) -> bool:
    """领取窗口是否已开：只看「猫是不是真的到了」。

    到达时间来自接口的 arrive_at，派遣那一次运行就会落盘。**没有固定时刻兜底** ——
    到点去领是唯一的正当理由；时间兜底会凭空制造请求，而且「到点那一刻电脑恰好睡着」
    正是它最不可靠的时候。缺 arrive_at 的补救不是猜时间，而是去问接口（见 main）。

    为什么单独抽成一个函数：它是「什么时候去领」的唯一判据，也是这里最该被测试的一处。
    抽出来后自检可以直接喂时间戳断言，不必等到真的到点或真的派一只猫。
    """
    try:
        ts = float(state.get("arrive_at") or 0)
    except (TypeError, ValueError):   # 脏数据一律当「不知道到达时间」
        return False
    return ts > 0 and now_ts >= ts


def _due(state: dict, key: str, now_ts: float, gap_min: int) -> bool:
    """距上次真实尝试是否已超过最小间隔。首次尝试（无记录）恒为 True。"""
    try:
        last = float(state.get(key) or 0)
    except (TypeError, ValueError):
        return True
    return last <= 0 or (now_ts - last) >= gap_min * 60


_NET_KINDS = ("net",)
_LOCAL_KINDS = ("local",)
_LOCAL_FAILS_KEY = {"checkin": "checkin_local_fails", "travel": "claim_local_fails"}


def _is_net_fail(res: dict) -> bool:
    """判断一次失败是否属于「网络层」失败。

    网络层失败（DNS / 连接 / 超时 / SSL / 断开）与业务层失败（401 / 活动结束 /
    接口变更）性质完全不同：网络恢复后重试就能成功，**不该烧有限的 MAX_TRIES 配额**。
    底层 checkin.py / travel.py 已在失败结果上打 fail_kind 标记，这里只读它，
    不再靠关键词匹配 reason 字符串。
    """
    return bool(res) and res.get("status") == "failed" and res.get("fail_kind") in _NET_KINDS


def _is_local_fail(res: dict) -> bool:
    """判断一次失败是否属于「本地环境未就绪」（客户端没起来 / 没登录）。

    这类失败在**发出任何网络请求之前**就返回了（credentials 读不到 at-rest 密钥），
    因此不烧服务端配额，也不该占用 MAX_TRIES 的正式尝试预算。
    checkin.py / travel.py 把它标成 fail_kind="local"。
    """
    return bool(res) and res.get("status") == "failed" and res.get("fail_kind") in _LOCAL_KINDS


def _local_fails_key(res: dict) -> str:
    """本次失败对应哪个计数器（签到 / 旅行各一个）。"""
    return _LOCAL_FAILS_KEY.get(str(res.get("task") or ""), "checkin_local_fails")


def _local_quiet(res: dict, state: dict) -> bool:
    """这次 local 失败是否还在静默期（不告警，只静默重试）。

    节奏：第 1–2 次静默（绝大多数开机竞态在客户端起来后一两次内就恢复了），
          第 3 次告警一次（≈ 10 分钟还没好，多半不是竞态），
          之后每 LOCAL_REALERT_EVERY 次再提醒一次（约 4 小时）。
    为什么不能「越过阈值后每次都报」：local 失败不消耗 MAX_TRIES，会一直重试到
    当天结束；每 20 分钟一条会变成一天几十条，比原来的误报还糟。
    """
    if not _is_local_fail(res):
        return False
    n = int(state.get(_local_fails_key(res)) or 0)
    if n < LOCAL_ALERT_AFTER:
        return True
    return (n - LOCAL_ALERT_AFTER) % LOCAL_REALERT_EVERY != 0


def _gap_for(state: dict, kind_key: str, fails_key: str) -> int:
    """按上次失败类型决定重试间隔（分钟）。

    net   → 30 分钟（不烧配额，低频重试到当天结束）
    local → 未达阈值 5 分钟（抢客户端启动那几十秒）；已告警退回 20 分钟
            （避免一整天每 5 分钟扫一次进程内存）
    其它  → 20 分钟（业务失败，保护配额）
    """
    kind = state.get(kind_key)
    if kind == "net":
        return NET_RETRY_GAP_MIN
    if kind == "local":
        if int(state.get(fails_key) or 0) < LOCAL_ALERT_AFTER:
            return LOCAL_RETRY_GAP_MIN
        return MIN_RETRY_GAP_MIN
    return MIN_RETRY_GAP_MIN


# ═══════════════════════════════════════════════════════════════════════════
# 成长任务 / 成长空间 / 晚间活跃看门狗（2026-10-28 从 Mac 侧同步）
#
# 三个模块的共同点：**都不属于每日快照**，都单独成条，都必须在 `_day_finished`
# 早退之前调用（WorkBuddy 上午收工后，任务可能下午才达标、能量可能下午才攒够）。
# ═══════════════════════════════════════════════════════════════════════════
def _fmt_task_claim(c: dict) -> str:
    """一条任务领取结果：`体验优秀灵感：+100 积分 +5 能量`。"""
    bits = []
    if c.get("credit"):
        bits.append("+{} 积分".format(c["credit"]))
    if c.get("energy"):
        bits.append("+{} 能量".format(c["energy"]))
    return "{}：{}".format(c.get("title") or c.get("code"), " ".join(bits) or "已领取")


def _maybe_run_tasks(state: dict, now_ts: float, today: str) -> bool:
    """巡检成长任务（自动接受 + 领取已达标奖励）。返回 True = 改了 state，调用方要落盘。

    ── 为什么它不属于每日快照 ───────────────────────────────────────────────
    签到与旅行是「每天一次、结果确定」的事，适合并进那条编号快照。
    任务领取不一样：进度取决于**用户当天有没有真的用那些功能**，随时可能达标，
    而且一天可能领到好几次。塞进快照会让同一份内容反复出现，所以它单独成条 ——
    且**每天最多一条**（用户 2026-10-28 明确要求）：
      · 当天第一次领到 → 发一条，列出这次领到的全部项；
      · 之后同一天再领到 → 照常入账，只写日志不推送（否则一天几条，纯噪音）。

    ── 为什么它不看 _day_finished ───────────────────────────────────────────
    WorkBuddy 签到/旅行上午就收工，之后整轮会早退；但任务可能下午才达标。
    所以调用点有两处：done 分支里一次、正常路径一次（见 _run）。

    ── 节流 ────────────────────────────────────────────────────────────────
    触发频率是 5 分钟一次，不快照就不能每轮都打接口。60 分钟一次足够：
    任务达标不是时效性事件，晚一小时领到不影响一分钱。
    """
    if tasks is None:
        if TASKS_IMPORT_ERROR:
            log("tasks 模块不可用，已跳过：" + TASKS_IMPORT_ERROR)
        return False
    if not _due(state, "tasks_last_check", now_ts, TASKS_GAP_MIN):
        return False
    state["tasks_last_check"] = now_ts

    try:
        res = tasks.run_daily()
    except Exception as e:  # noqa: BLE001 — 任务模块坏了不该影响签到/旅行
        log("tasks error: {!r}".format(e))
        return True
    log("tasks: " + json.dumps(res, ensure_ascii=False)[:500])

    claimed = res.get("claimed") or []
    if not claimed:
        return True
    if state.get("tasks_notified_day") == today:
        log("tasks: 今日已推送过，本次 {} 项不再通知".format(len(claimed)))
        return True

    state["tasks_notified_day"] = today
    amt = []
    if res.get("credit"):
        amt.append("+{} 积分".format(res["credit"]))
    if res.get("energy"):
        amt.append("+{} 能量".format(res["energy"]))
    lines = ["{}、{}".format(i, _fmt_task_claim(c)) for i, c in enumerate(claimed, 1)]
    # 2026-10-28：**先攒着不发**。同一轮里 growth 往往紧接着也有收益（它们都在
    # 60 分钟节流后同批触发），两条消息连着推等于把「今天赚了什么」拆成两半。
    # 由 _maybe_run_growth 合并，或本轮的 _flush_growth_pending 单独发出。
    state["tasks_pending"] = {"lines": lines, "amt": amt, "count": len(claimed)}
    return True


def _sum_credit_prizes(wins: list[dict]) -> int | None:
    """全是积分奖时求合计；混了实物奖或名字解析不出来就返回 None（调用方回退成列举）。

    奖品名是服务端给的 "10 积分" 这种自然语言，没有数值字段（`prizes` 接口里的
    probability 与 credit_amount 是奖池配置，不是中奖结果）。所以只能从名字里读数字，
    读不出来就诚实地逐个列出，不猜。
    """
    total = 0
    for w in wins:
        name = str(w.get("name") or "")
        if w.get("type") not in (None, "credit") or "积分" not in name:
            return None
        head = name.split("积分")[0].strip().replace(",", "")
        if not head.isdigit():
            return None
        total += int(head)
    return total or None


TIER_NAME = {"7d": "入门档", "14d": "进阶档", "28d": "巅峰档"}   # 官方叫法


def _fmt_growth(res: dict) -> tuple[str, str]:
    """把一次成长空间兑现结果排成 (标题, 正文)。只在真有进项时调用。

    排版沿用每日快照的编号形式：一条一行，一行一件事。
    """
    lines = []
    n = 0
    redeemed = [r for r in (res.get("redeemed") or []) if not r.get("dry_run")]
    if redeemed:
        n += 1
        bits = []
        c = sum(r.get("credit", 0) for r in redeemed)
        e = sum(r.get("energy", 0) for r in redeemed)
        cards = sum(r.get("cards", 0) for r in redeemed)
        ch = sum(r.get("chances", 0) for r in redeemed)
        if c:
            bits.append("+{} 积分".format(c))
        if e:
            bits.append("+{} 能量".format(e))
        if cards:
            bits.append("+{} 张补登卡".format(cards))
        if ch:
            bits.append("+{} 次抽奖".format(ch))
        tiers = " / ".join(TIER_NAME.get(r["tier"], r["tier"]) for r in redeemed)
        lines.append("{}、连登兑换（{}）：{}".format(n, tiers, " ".join(bits)))

    wins = [w for w in (res.get("wins") or []) if not w.get("dry_run")]
    if wins:
        n += 1
        # 全是积分奖时合计成「共 N 积分」，读起来比逐个列举清爽。奖品名是
        # "10 积分" 这种自然语言，抠不出来就退回原样列举 —— 绝不因为解析失败而少报。
        total = _sum_credit_prizes(wins)
        if total is not None:
            lines.append("{}、抽奖：{} 次 → 共 {} 积分".format(n, len(wins), total))
        else:
            names = "、".join(str(w.get("name") or "?") for w in wins)
            lines.append("{}、抽奖：{} 次 → {}".format(n, len(wins), names))

    madeup = [m for m in (res.get("madeup") or []) if not m.get("dry_run")]
    if madeup:
        n += 1
        # 带上连登变化，一眼看出补登有没有真的把记录接上（没接上就是要人工看的信号）
        bits = []
        for m in madeup:
            b, a = m.get("streak_before"), m.get("streak_after")
            bits.append(m["date"][5:] + ("（连登 {} → {} 天）".format(b, a) if a else ""))
        lines.append("{}、补登：{}".format(n, "、".join(bits)))

    opened = res.get("opened") or {}
    if opened.get("count") and not opened.get("dry_run"):
        n += 1
        new = opened.get("new") or []
        got = "、".join("{}（{}）".format(b.get("name"), b.get("rarity")) for b in new)
        lines.append("{}、能量开盒：{} 次{}".format(
            n, opened["count"], " → " + got if got else ""))

    physical = res.get("physical") or []
    if physical:
        n += 1
        lines.append("{}、实物中奖：{} —— 需要你在客户端填收货地址".format(
            n, "、".join(p.get("name") or "?" for p in physical)))

    if res.get("credit"):
        title = "成长空间：+{} 积分".format(res["credit"])
    elif wins:
        title = "成长空间：抽奖 {} 次".format(len(wins))
    elif madeup:
        title = "成长空间：补登 {} 天".format(len(madeup))
    elif opened.get("count"):
        title = "成长空间：开盒 {} 次".format(opened["count"])
    else:
        title = "成长空间：有新的收益"

    lines.append("")
    lines.append("时间：" + datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
    return title, "\n".join(lines)


def _maybe_run_growth(state: dict, now_ts: float, today: str) -> bool:
    """兑现成长空间收益（兑换 / 补登 / 抽奖 / 开盒）。返回 True = 改了 state，要落盘。

    ── 为什么单独一条、每天最多一条 ─────────────────────────────────────────
    和 tasks 同源：这些收益都不确定什么时候到手（兑换要等连登够天数、抽奖要等兑换
    给次数、开盒要等能量攒够 10），塞进每日快照会反复出现。所以单列，且有真实进项
    才发、每天最多一条 —— 没有进项时一声不响（避免每天一条「今天没收益」的噪音）。

    ── 为什么不受 _day_finished 影响 ────────────────────────────────────────
    签到/旅行上午收工后整轮早退，但补登卡可能是下午才够天数触发、能量可能下午才攒够。
    所以和 tasks 一样，done 分支与正常路径各调一次。

    ── 与 tasks 的关系 ─────────────────────────────────────────────────────
    两者会互相喂饭：tasks 领到的奖励里有能量（够 10 点就能开盒），兑换来的卡又能
    保住连登。所以 growth 排在 tasks 之后跑，同一轮里就能用上刚领到的能量。
    """
    if growth is None:
        if GROWTH_IMPORT_ERROR:
            log("growth 模块不可用，已跳过：" + GROWTH_IMPORT_ERROR)
        return False
    if not _due(state, "growth_last_check", now_ts, GROWTH_GAP_MIN):
        return False
    state["growth_last_check"] = now_ts

    try:
        res = growth.run_daily()
    except Exception as e:  # noqa: BLE001 — 成长空间坏了不该影响签到/旅行
        log("growth error: {!r}".format(e))
        return True
    log("growth: " + json.dumps(res, ensure_ascii=False)[:600])

    if not res.get("changed"):
        return True
    if state.get("growth_notified_day") == today:
        log("growth: 今日已推送过，本次不再通知")
        return True

    state["growth_notified_day"] = today
    title, body = _fmt_growth(res)
    # 同轮里 tasks 已经攒下待发内容 → 合并成一条。两条消息连着推，用户要在两个
    # 气泡里拼「今天到底赚了多少」，合起来反而更短（只留一个时间行、一个标题）。
    pending = state.pop("tasks_pending", None)
    if pending:
        lines = (pending.get("lines") or []) + [""]
        body = "\n".join(lines) + body
        title = "成长空间：任务与连登收益"
        log("growth: 与 {} 项任务领取合并为一条".format(pending.get("count") or 0))
    try:
        _notify(title, body, "success")
    except Exception as e:  # noqa: BLE001
        log("growth notify error: {!r}".format(e))
    return True


def _flush_growth_pending(state: dict) -> None:
    """把 tasks 攒下的那条单独发出去（growth 本轮没有收益时才会走到这里）。

    必须在 tasks / growth 两个调用之后立刻调用：攒着不发又没有 growth 来合并，
    这条消息就永远丢了 —— 那是真的少报收益，比多发一条严重得多。
    """
    p = state.pop("tasks_pending", None)
    if not p:
        return
    title = "WorkBuddy 成长任务：{}（{} 项）".format(
        " ".join(p.get("amt") or []) or "已领取", p.get("count") or 0)
    body = "\n".join((p.get("lines") or [])
                     + ["", "时间：" + datetime.datetime.now().strftime("%Y-%m-%d %H:%M")])
    try:
        _notify(title, body, "success")
    except Exception as e:  # noqa: BLE001
        log("tasks notify error: {!r}".format(e))


def _run_activity_watch() -> dict:
    """晚间活跃看门狗：今天热力墙黑了就提醒一次（21:30 / 23:40 两档）。

    ── 为什么需要（这不是可选的锦上添花）──────────────────────────────────
    连登门槛只要当天活跃分 > 0，但实测**签到 / 旅行 / 兑换 / 补登全部计 0 分**，
    唯一常规来源是真发一次对话请求（2 分）。所以完全不打开 WorkBuddy 的那天，
    热力墙必然黑，连登当场断；断 1 天要花 1 张补登卡才可能接回，而整月 28 天档
    只有一次机会。补登是事后补救，这条是事前拦截。

    ── 为什么只读、绝不代发对话 ──────────────────────────────────────────
    模拟对话刷活跃既是伪造使用行为，也没有收益（分数高低不兑换任何奖励，
    只按 7/14/28 天档发）。所以这里只查分数、只发提醒，把「点一下」留给本人。

    ── 为什么放在 _day_finished 早退之前 ────────────────────────────────
    签到与旅行通常上午就收工，之后每轮都会早退；看门狗在晚上才触发，
    放到早退之后就永远轮不到它。

    ── 为什么调用方不需要节流 ────────────────────────────────────────────
    未到点 / 已点亮 / 本档已提醒 三种情况都在 activity_watch.check() 里直接返回，
    不产生网络请求；只有真正「到点且还没查过」的那一次才读接口。
    """
    if activity_watch is None:
        if ACTIVITY_IMPORT_ERROR:
            log("activity_watch 模块不可用，已跳过：" + ACTIVITY_IMPORT_ERROR)
        return {"action": "unavailable"}
    res = activity_watch.check()
    act = res.get("action")
    if act == "alert":
        # 级别用 "warning" 而不是 Mac 侧的 "warn"：Win 版 notify.py 的 `_LEVEL_SWITCH`
        # 只认 success/failure/warning/info，"warn" 会落进 notify_noop 那一档 ——
        # 消息仍会发（配置项缺省为 True），但拿不到 Toast 样式，日志里级别也成了非标准值。
        _notify(res["title"], res["body"], "warning")
        log("activity_watch: 已提醒（{} 档，score={}，连登={}）".format(
            res.get("slot"), res.get("score"), res.get("streak_days")))
    elif act == "lit":
        log("activity_watch: 今日已点亮（{} 分），今天不再查".format(res.get("score")))
    elif act == "error":
        log("activity_watch: " + str(res.get("reason")))
    return res


def _quiet_now(now: datetime.datetime) -> bool:
    """是否处于完全静默时段（00:00 – 07:00）。

    为什么要这一条：凌晨既不该签到（闸门未开），也没有任何事可做，但计划任务
    仍会随系统的维护唤醒被触发。不加判断的话，每次触发都要启动解释器、读 state、
    跑续期检查与端点探测、再写盘 —— 纯属空转。

    刻意与 WINDOW1 同值：闸门几点开门，就几点结束静默，不留下
    「醒了但不该干活」的中间态。
    """
    return now.hour * 100 + now.minute < QUIET_FROM


def _day_finished(state: dict, today: str) -> bool:
    """今天该做的都做完了吗 —— 是则后续触发全部静默。

    口径刻意只有一条：**签到成功 且 旅行奖励到手**。两件都完成之后，再触发
    也没有任何事可做，继续跑纯属空转。

    为什么不把「领取预算耗尽」也算进来：那是「今天没领到」，与「做完了」是
    两回事，由 probe_exhausted 告警负责（每天一条）。把失败路径混进静默判据
    会让口径分裂，还逼着 watchdog 侧再同步一个常量 —— 不值得。

    为什么必须比对 day：完成状态属于**某一天**。昨天的 checkin_done 不能拿来
    给今天免跑 —— 否则跨日后的第一个触发会被静默掉，当天直接不签到。
    """
    return (state.get("day") == today
            and bool(state.get("checkin_done"))
            and bool(state.get("claim_done")))


def _reshow_pending_notifications() -> None:
    """用户回到机器前后，把「投递时用户不在场」的通知补弹一次。

    ★ 2026-09-21（用户要求）：Windows 在熄屏 / 长时间无人操作时**不弹横幅**，
      通知只进通知中心 —— 光把通知设成常驻也没用，那条通知压根没在屏幕上出现过。
      `winenv` 侧负责「判定不在场 + 记下原文」，这里负责每次触发问一句
      「用户回来了吗」，回来了就把积压的补弹出来（依旧常驻，点叉才消失）。

    为什么放在静默判定**之前**：夜里回到机器前，也该看到积压的通知。
    代价极小：正常情况下队列文件不存在，只多做一次文件存在性检查。
    """
    if winenv.platform() != "win":
        return            # 非 Windows 上不做这层兜底（本项目只跑 Windows）
    try:
        res = winenv.reshow_pending(paths.LOG_DIR)
    except Exception as e:  # noqa: BLE001
        log("notify reshow error: {!r}".format(e))
        return
    if res.get("resent"):
        log("notify: 补弹 {} 条（投递时用户不在场）{}".format(
            res["resent"],
            "" if not res.get("left") else "，还剩 {} 条".format(res["left"])))


def main() -> None:
    """入口：先抢文件锁，再跑 _run()。

    为什么入口只做这一件事：触发频率提到 5 分钟后，「计划任务」与「手动 run_now」
    撞在同一分钟的概率不再可忽略，而两者都会读 → 改 → 写 state.json。
    锁放在最外层，保证任何退出路径（包括异常）都会释放。
    """
    # 先补弹积压的本机通知（用户"回来了"就该看到）。放在静默判定之前，理由见该函数。
    _reshow_pending_notifications()
    if _quiet_now(datetime.datetime.now()):
        # 静默期：不抢锁、不写日志、不读 state —— 只多一次上面那次「补弹队列是否为空」
        # 的文件存在性检查（通常是 0 次读盘），其余零落盘开销。
        print(json.dumps({"action": "quiet_hours"}, ensure_ascii=False))
        return
    now_ts = time.time()
    if not _acquire_lock(now_ts):
        log("另一个实例正在运行（{} 存在且有效），本次跳过".format(LOCK.name))
        print(json.dumps({"action": "skipped_busy"}, ensure_ascii=False))
        return
    try:
        _run()
    finally:
        _release_lock()


def _run() -> None:
    # 补发队列：之前未送达微信的关键通知（签到/旅行到账、失败告警）在此兜底重试。
    # 必须在 _day_finished 静默检查**之前** —— 当天任务完成后会直接 return，放后面就永远跑不到。
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    done = _day_finished(_load_state(), today)
    try:
        notify.flush_pending(day_done=done)
    except Exception as e:  # noqa: BLE001
        log(f"flush_pending error: {e!r}")

    # ── 晚间活跃看门狗（2026-10-28 加，见 scripts/activity_watch.py）──────────
    # 连登门槛只要 score>0，而签到/旅行/兑换全计 0 分 —— 唯一来源是真发一次对话，
    # 所以「今天点亮没有」只能靠人。21:30 还来得及，23:40 是最后一道。
    # 位置：必须在 _day_finished 早退之前（白天收工后不再有别的检查点）。
    # 未到点 / 已点亮 / 本档已提醒 都直接返回，空转零请求。
    activity_res: dict = {}
    try:
        activity_res = _run_activity_watch()
    except Exception as e:  # noqa: BLE001 — 看门狗坏了不该影响任何主流程
        log("activity_watch error: {!r}".format(e))

    # ── ima 每日签到（2026-10-28 加，独立账号体系）──────────────────────────
    # 为什么放在 day_done 早退**之前**：WorkBuddy 两项通常上午就跑完，之后每次触发
    # 都会在这里早退；ima 可能还在等重试间隔，放在早退之后就永远轮不到它。
    # 它有自己的状态文件（runtime/state/ima_state.json）与闸门，这里只做兜底：
    # ima 的任何异常都不该拖累 WorkBuddy 的补跑（反之亦然）。
    ima_summary: dict = {}
    ima_item: dict = {}
    ima_reported = False
    if ima is None:
        if IMA_IMPORT_ERROR:
            log(f"ima 模块不可用，已跳过：{IMA_IMPORT_ERROR}")
    elif ima.load_cred() is None:
        # ★ Win 侧差异（2026-10-28）：未扫码/未迁凭据时**整块跳过**，连摘要都不取。
        #   理由：ima.run_daily() 在无凭据分支会自己推一条 failure（「还没有登录凭证」
        #   + 一条 python3 命令）。Mac 上早就扫过码，所以那条只在首次出现一次；
        #   Win 上还没配，就会变成**每天一条**失败告警，纯噪音。
        #   「没配置」不是故障，不该占用通知栏 —— 配好凭据（扫码或从 Mac 拷
        #   ima_credentials.json）后，这里自然就开始工作。
        #   日志也每天只记一次：5 分钟一轮，每轮一条就是 288 行/天（见 _log_once_a_day）。
        _log_once_a_day(
            "ima_no_cred",
            "ima: 未配置凭据，跳过（不影响签到/旅行）。要启用：python scripts\\ima.py login 扫码一次")
    else:
        try:
            # embed=True：结果不自己推，交给下面 WorkBuddy 那条消息带走
            # （两者同在 07:00 开闸，并排推两条只会让人多看一条消息）。
            ima_summary = ima.run_daily(embed=True)
            ima_item = ima_summary.get("item") or {}
            if ima_summary.get("action") in ("ran", "blocked", "exhausted"):
                log("ima: " + json.dumps(ima_summary, ensure_ascii=False)[:400])
        except Exception as e:  # noqa: BLE001
            log(f"ima error: {e!r}")

    def _flush_ima_item(st: dict) -> None:
        """ima 结果没有被任何 WorkBuddy 消息带走时，自己补发一条（否则结果彻底丢失）。

        补发也用**同一套 `_compose`**：用户视野里只该有一种排版，
        不然「WorkBuddy 已收工的那天」会收到一条长得不一样的 ima 专属消息。
        """
        nonlocal ima_reported
        if not ima_item or ima_reported:
            return
        # 只补发「本轮真有结果」的情形（`ran` 且结果为完成/新到账）。
        # `waiting` 是常态（每 5 分钟一次），补发它等于把消息刷成心跳；
        # `blocked` / `exhausted` 已由 ima 自己推过，再补一条就是重复。
        if ima_summary.get("action") != "ran":
            return
        if not (ima_item.get("new") or ima_item.get("done")):
            return
        ima_reported = True
        try:
            _notify(
                _snapshot_title(st, ima_item, st.get("checkin_streak_days")),
                _compose(st, {}, {}, ima_item,
                         datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                         brief=bool(st.get("msg_first_sent"))),
                "success" if ima_item.get("new") else "info")
        except Exception as e:  # noqa: BLE001
            log(f"ima notify error: {e!r}")

    # 静默第二道：当天该做的都做完了 → 之后每次触发都不再做任何事。
    # 放在 ensure_config / renew / discovery 之前，因为那些都属于「每次触发都跑」
    # 的固定开销，正是静默要省掉的那部分。
    if done:
        # ima 可能刚在这一轮完成，而 WorkBuddy 早已收工 —— 用现读的 state 拼快照
        st = _load_state()
        _flush_ima_item(st)
        # 成长任务不看签到/旅行的闸门：WorkBuddy 上午收工之后，任务仍可能下午才达标。
        # 放在早退之前跑，否则当天后半段的达标永远等不到领取。
        tasks_ran = _maybe_run_tasks(st, time.time(), today)
        # 成长空间同理：兑换要等连登天数，开盒要等能量，都可能下午才成立。
        growth_ran = _maybe_run_growth(st, time.time(), today)
        # tasks 若攒下了内容而 growth 没合并（本轮无收益），在这里补发
        _flush_growth_pending(st)
        if tasks_ran or growth_ran or ima_reported:
            _save_state(st)
        print(json.dumps({"action": "day_done", "ima": ima_summary.get("action"),
                          "tasks_checked": tasks_ran,
                          "growth_checked": growth_ran,
                          "activity": activity_res.get("action") or "-"},
                         ensure_ascii=False))
        return

    notify.ensure_config()   # 首次运行自动生成 notify_config.json 模板
    renew.ensure_config()    # 首次运行自动生成 renew_config.json 模板

    # 续期守护：与积分闸门无关，每次触发都要跑
    # （监控 WorkBuddy 桌面端是否还在轮询 ClawBot —— 停摆是会话失效的前兆；
    #   纯本地读文件，不发网络请求，因此不会与桌面端抢消息。）
    renew_result: dict = {}
    try:
        renew_result = renew.check()
    except Exception as e:  # noqa: BLE001
        log(f"renew error: {e!r}")
        renew_result = {"action": "error", "error": str(e)[:160]}

    # 接口发现：每次触发都做（纯本地读 app.asar，按客户端指纹缓存 → 有缓存时开销近 0）。
    # 目的是**即使当天无事可做**，也能在客户端升级/端点变化的第一时间发现并告警，
    # 而不是等到下次签到失败才知道。
    disc: dict = {}
    try:
        disc = api_discovery.discover()
        if disc.get("changed"):
            log("ENDPOINT CHANGED: {} -> {}".format(
                (disc.get("changed_from") or {}).get("status_paths", [])[:2],
                disc.get("status_paths", [])[:2]))
    except Exception as e:  # noqa: BLE001
        log(f"discovery error: {e!r}")

    now = datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")
    hm = now.hour * 100 + now.minute
    now_ts = now.timestamp()

    state = _load_state()
    prev_seen = state.get("last_seen_ts")
    if state.get("day") != today:
        # 跨日重置每日进度，但**保留** no_activity_streak（它是跨日累计的失效指标）
        carry_streak = int(state.get("no_activity_streak") or 0)
        # 连续签到天数也是跨日累积的成就（标题要用），不能跟着当天进度清掉。
        # 万一今天签到接口拿不到这个数（already_checked 那轮不回），标题就该显示
        # 昨天的值，而不是干脆不显示——见 _report 里的 streak_days 回退。
        carry_days = state.get("checkin_streak_days")
        state = {"day": today, "checkin_done": False, "checkin_tries": 0,
                 "checkin_last_try": 0, "checkin_last_kind": "biz",
                 "checkin_local_fails": 0,
                 "claim_done": False, "claim_tries": 0,
                 "claim_last_try": 0, "claim_last_kind": "biz",
                 "claim_local_fails": 0,
                 "probe_last_try": 0,
                 "arrive_at": None,
                 "no_activity_streak": carry_streak,
                 "checkin_streak_days": carry_days}

    # 系统时间倒退检测：手动改表 / 时区改错 / 虚拟机恢复快照，以及 Windows 上
    # 「拔电源后 CMOS 时间回到出厂」都会让闸门判断错乱 —— 例如时间跳回凌晨后，
    # 「≥07:00」的闸门当天不会再开，签到与领取被**静默**跳过。
    # 只在真正倒退时报警（正常的前后两次运行一定是递增的），每天最多一条。
    if isinstance(prev_seen, (int, float)) and now_ts < float(prev_seen) - 60:
        log("CLOCK BACK: prev={} now={} (倒退 {:.1f}h)".format(
            prev_seen, now_ts, (float(prev_seen) - now_ts) / 3600.0))
        if state.get("clock_warn_day") != today:
            state["clock_warn_day"] = today
            _notify(
                "WorkBuddy 积分：系统时间异常（比上次运行倒退了）",
                "\n".join([
                    "本次运行的时间比上一次**早了 {:.1f} 小时**。".format(
                        (float(prev_seen) - now_ts) / 3600.0),
                    "上一次：{}".format(
                        datetime.datetime.fromtimestamp(float(prev_seen)).strftime("%F %T")),
                    "本次：  {}".format(now.strftime("%F %T")),
                    "",
                    "为什么要注意：签到闸门是「≥07:00」的判断，时间跳回凌晨后",
                    "当天不会再开闸，签到和领取都会被静默跳过。",
                    "若是手动改过系统时间/时区，调回后手动跑一次 run_now.cmd 即可。",
                ]),
                "failure")

    # 上次失败类型决定重试间隔：网络失败用更长间隔（不烧配额），本地环境未就绪
    # 用更短间隔（客户端一启动就能成功），业务失败用原间隔。
    checkin_gap = _gap_for(state, "checkin_last_kind", "checkin_local_fails")
    claim_gap = _gap_for(state, "claim_last_kind", "claim_local_fails")

    checkin_budget_exhausted = int(state.get("checkin_tries") or 0) >= MAX_TRIES
    claim_budget_exhausted = int(state.get("claim_tries") or 0) >= MAX_TRIES

    need_checkin = (hm >= WINDOW1 and not state["checkin_done"]
                    and not checkin_budget_exhausted
                    and _due(state, "checkin_last_try", now_ts, checkin_gap))

    claim_open = _claim_open(now_ts, state)
    # 该去处理旅行：① 猫到了（窗口开）；或 ② 还不知道猫何时到达，需要问一次接口补齐
    #    （state 被删过 / 猫是用户自己在 App 里派的 / 昨天派遣那一步失败了）。
    # ② 是唯一「明明没到点也要问一次」的例外，同样受 MAX_TRIES + 最小间隔约束，
    # 并且要求已过 07:00 —— 否则凌晨会顺带把当天的签到也提前做掉。
    need_arrive_info = (not state["claim_done"] and not state.get("arrive_at")
                        and hm >= WINDOW1)
    need_claim = ((claim_open and not state["claim_done"]) or need_arrive_info) \
        and not claim_budget_exhausted \
        and _due(state, "claim_last_try", now_ts, claim_gap)

    # 探测恢复触发条件：业务预算耗尽、当天该事项仍未完成时，进入低频只读探测。
    need_probe = (hm >= WINDOW1
                  and ((checkin_budget_exhausted and not state["checkin_done"])
                       or (claim_budget_exhausted and not state["claim_done"]))
                  and _due(state, "probe_last_try", now_ts, PROBE_GAP_MIN))

    c: dict = {}
    t: dict = {}
    pf: dict = {}
    if need_checkin or need_claim:
        parts = []
        if need_checkin:
            parts.append("checkin")
        if need_claim:
            parts.append("claim" if claim_open else "probe")
        tag = "+".join(parts)
        r = run_all(tag)
        c = r.get("checkin") or {}
        t = r.get("travel") or {}
        pf = r.get("preflight") or {}   # main.py 在真正执行前做的前置校验
        if pf:
            log("preflight: ok={} resolved={} changed={} tried={}".format(
                pf.get("ok"), pf.get("resolved_status"), pf.get("changed"),
                len(pf.get("tried") or [])))

        if need_checkin:
            state["checkin_last_try"] = now_ts
            # no_activity 也计入「完成」：活动空档期无论重试多少次都不会有积分，
            # 计入完成才能让后续轮询真正空转、不做无谓请求。
            # suspect 同样计入完成 —— 全零载荷说明链路有问题，重试不会变好，
            # 该做的是**立刻告警**（见 _report），而不是反复重试刷屏。
            if c.get("status") in ("success", "already_checked", "no_activity", "suspect"):
                state["checkin_done"] = True
                state["checkin_last_kind"] = "biz"
                state["checkin_local_fails"] = 0
            elif _is_net_fail(c):
                # 网络层失败：不烧配额（tries 不动），只记失败类型 → 下次用更长间隔重试
                state["checkin_last_kind"] = "net"
            elif _is_local_fail(c):
                # 本地环境未就绪（客户端没起来 / 没登录）：请求根本没发出去 → 不烧配额。
                # 记次数供「前两次静默、第三次告警」用；间隔走 LOCAL_RETRY_GAP_MIN。
                state["checkin_local_fails"] = int(state.get("checkin_local_fails") or 0) + 1
                state["checkin_last_kind"] = "local"
            else:
                # 业务层失败：烧配额
                state["checkin_tries"] += 1
                state["checkin_last_kind"] = "biz"
                state["checkin_local_fails"] = 0
            # 跨日累计「拿不到活动数据」的天数：连续多日即为接口/活动判定异常的信号
            if c.get("status") in ("no_activity", "suspect"):
                state["no_activity_streak"] = int(state.get("no_activity_streak") or 0) + 1
            elif c.get("status") in ("success", "already_checked"):
                state["no_activity_streak"] = 0
        if need_claim:
            state["claim_last_try"] = now_ts
            if t.get("status") in ("claimed", "daily_limit_reached"):
                state["claim_done"] = True
                state["claim_last_kind"] = "biz"
                state["claim_local_fails"] = 0
            elif _is_net_fail(t):
                state["claim_last_kind"] = "net"
            elif _is_local_fail(t):
                state["claim_local_fails"] = int(state.get("claim_local_fails") or 0) + 1
                state["claim_last_kind"] = "local"
            else:
                state["claim_tries"] += 1
                state["claim_last_kind"] = "biz"
                state["claim_local_fails"] = 0

        # 记录 / 清除猫的到达时间（「到达即领」的判据来源）：
        #   领到手 → 清掉，不留过期时间戳；
        #   已派出 / 在路上 → 记下来，下次触发不花一次网络请求就能判断该不该去领。
        # 接口本来就回了这个字段，落盘等于把它从「当次用完即弃」变成「可复用」。
        if t.get("status") == "claimed":
            state["arrive_at"] = None
        elif t.get("arrive_at"):
            state["arrive_at"] = t["arrive_at"]

        # 记下这次派出去的是哪只猫：派遣那一刻的结果里才带猫名，等猫回来领奖时
        # claim 接口不回猫信息，只能靠落盘补上 —— 与 arrive_at 同一思路。
        _duty_cur = ((t.get("duty") or {}).get("current") or {})
        if t.get("status") == "departed" and _duty_cur.get("name"):
            state["duty"] = {"name": _duty_cur.get("name"),
                             "rarity": _duty_cur.get("rarity")}
            # 技能一并落盘：领取那条消息要在没有接口数据的情况下也说出「哪位在岗」
            if _duty_cur.get("skill"):
                state["duty"]["skill"] = _duty_cur["skill"]

        # 网络失败 → 完全静默（2026-10-14 改，原为「每天推一条告警」）：
        # 本次失败若**全是网络层失败**（且没有成功到账），不推送任何通知，只记日志。
        # 理由：
        #   1. 网络失败不烧业务配额，系统按 NET_RETRY_GAP_MIN 自动重试到当天结束；
        #   2. 用户无法介入 —— 不是 token 过期也不是端点变更，通常几十分钟自愈
        #      （2026-10-14 实测：10:31 全部网络失败含 preflight，11:11 同端点
        #      一次通过、领到 +5，积分零损失）；
        #   3. 恢复后必有「到账」成功推送兜底，信息不丢；
        #   4. 推了反而制造矛盾观感：「补跑失败」（带 preflight 警告 + 自查命令）
        #      与 40 分钟后的「到账成功」并列，像出了大事，实际什么都没损失。
        # 端点真变更时业务请求会返回 404/401（业务失败），仍会照常推送，不受本静默影响。
        fails = [r for r in (c, t) if r and r.get("status") == "failed"]
        gains = [r for r in (c, t) if r and r.get("status") in ("success", "already_checked", "claimed")]
        net_only = bool(fails) and not gains and all(_is_net_fail(r) for r in fails)
        # 本地环境未就绪（开机时客户端还没起来）：请求都没发出去，属竞态不是故障。
        # 前两次只写日志，第 LOCAL_ALERT_AFTER 次才告警 —— 判据见 _local_quiet。
        local_quiet = bool(fails) and not gains and all(_local_quiet(r, state) for r in fails)
        if local_quiet:
            log("本地环境未就绪（客户端可能尚未启动），静默重试，不发通知")
        elif net_only:
            log("网络失败（自动重试中，不推送）: " + "; ".join(
                "{task}={status}/{kind}".format(task=r.get("task"), status=r.get("status"),
                                                kind=r.get("fail_kind")) for r in fails))
        else:
            ima_reported = _report(c, t, state, pf, ima_item)

    # 本轮没有 WorkBuddy 消息把 ima 结果带走（不需要派发 / 纯网络失败静默）→ 自己发一条
    _flush_ima_item(state)

    # 成长任务巡检：接受待接受的任务 + 领取已达标奖励（不进快照，单独成条，见函数说明）。
    # state 末尾统一 _save_state，所以这里不用立即落盘。
    # ★ 必须放在 _day_finished 早退之外的另一半：上面 done 分支已经return，这条路径
    #   是「今天还没办完」时跑的，同样要巡检 —— 两处都不能漏，否则半天不领。
    tasks_ran = _maybe_run_tasks(state, now_ts, today)
    # 成长空间兑现：兑换/补登/抽奖/开盒（同样不进快照，单独成条，见函数说明）。
    # 排在 tasks 之后：tasks 刚领到的能量可能正好够开一次盒。
    growth_ran = _maybe_run_growth(state, now_ts, today)
    # 同轮收尾：tasks 攒下的内容如果没被 growth 合并，就在这一轮单独发出
    _flush_growth_pending(state)

    if need_probe:
        # 探测恢复：业务预算耗尽、当天该事项仍未完成时，低频只读探测一次状态。
        # 外部条件恢复（登录态刷新 / 网络恢复 / 活动重开）→ 状态查询返回 ok →
        # 重置预算，下次触发恢复正式签到。探测失败则不重置，继续等下一个探测周期。
        state["probe_last_try"] = now_ts
        st = run_status()
        cs = (st.get("checkin_status") or {})
        ts = (st.get("travel_status") or {})
        if checkin_budget_exhausted and not state["checkin_done"] and cs.get("status") == "ok":
            state["checkin_tries"] = 0
            state["checkin_last_try"] = 0
            state["checkin_last_kind"] = "biz"
            state["checkin_local_fails"] = 0
            log("probe: 签到状态已恢复，重置重试预算")
        if claim_budget_exhausted and not state["claim_done"] and ts.get("status") == "ok":
            state["claim_tries"] = 0
            state["claim_last_try"] = 0
            state["claim_last_kind"] = "biz"
            state["claim_local_fails"] = 0
            log("probe: 旅行状态已恢复，重置重试预算")

    if (not need_checkin and not need_claim and not need_probe) and disc.get("changed"):
        # 今日无待办（空转中），但客户端升级导致端点变了 —— 立刻报，不等明天。
        # 端点变化很少见（每次客户端发版至多一次），这点推送量可忽略。
        try:
            old = ((disc.get("changed_from") or {}).get("status_paths") or [])[:2]
            _notify(
                "WorkBuddy 积分：接口已自动切换",
                "\n".join([
                    "检测到 WorkBuddy 客户端接口变化，脚本已自动跟随。",
                    "",
                    "客户端版本：{}".format((disc.get("fingerprint") or {}).get("version")),
                    "旧端点：{}".format("、".join(old) or "(无)"),
                    "新端点：{}".format(disc.get("preferred_status")),
                    "领取端点：{}".format(disc.get("preferred_claim")),
                    "",
                    "说明：端点每次运行都从本机客户端现读，无需你操作。",
                    "今日任务均已完成，本条仅为接口变更通知。",
                ]),
                "warning")
        except Exception as e:  # noqa: BLE001
            log(f"notify change error: {e!r}")

    # probe 预算耗尽告警（Plan 1.3）。
    # 为什么单列一条：这种失败**不会**走上面的 _report —— 因为 claim_tries 用满后
    # need_claim 恒为 False，之后每一轮都是空转。若不在这里告警，结果就是
    # 「今天的旅行奖励没领到，而你什么消息都没收到」这类静默失败。
    probe_exhausted = (not state.get("claim_done") and not state.get("arrive_at")
                       and int(state.get("claim_tries") or 0) >= MAX_TRIES)
    if probe_exhausted and state.get("probe_exhausted_day") != today:
        state["probe_exhausted_day"] = today
        _notify(
            "WorkBuddy 积分：今天没能拿到猫的到达时间",
            "\n".join([
                "旅行奖励**今天未领取**：连续 {} 次都没能从接口问到到达时间。".format(
                    state.get("claim_tries")),
                "签到不受影响。",
                "",
                "可能原因：旅行接口异常 / 今日未派猫 / 活动已结束。",
                "脚本会持续只读探测，一旦接口恢复当天即自动重试；若连续多天如此，{}".format(winenv.selfcheck_hint()),
            ]),
            "failure")

    state["last_seen_ts"] = now_ts     # 下一轮做时间倒退检测的基线
    _save_state(state)
    log(f"state={state}")

    summary = {
        "action": "ran" if (need_checkin or need_claim) else ("probe" if need_probe else "none"),
        "checkin_done": state["checkin_done"],
        "claim_done": state["claim_done"],
        # 领取闸门状态：排查「为什么还没领」时一眼就能看出是闸门没开还是领取失败了
        "claim_open": claim_open,
        "arrive_at": state.get("arrive_at"),
        # 排查「为什么一直没领到」：试了几次、预算是否已用尽
        "claim_tries": state.get("claim_tries"),
        "probe_exhausted": probe_exhausted,
        # 2026-10-28 新增三项的当轮结果，排查「怎么没领任务 / 怎么没提醒看点灯」时看这里
        "ima": ima_summary.get("action"),
        "tasks_checked": tasks_ran,
        "growth_checked": growth_ran,
        "activity": activity_res.get("action") or "-",
        "renew": renew_result,
        "endpoints": {
            "source": disc.get("source"),
            "client_version": (disc.get("fingerprint") or {}).get("version"),
            "preferred_status": disc.get("preferred_status"),
            "preferred_claim": disc.get("preferred_claim"),
            "changed": bool(disc.get("changed")),
        },
    }
    if need_checkin or need_claim:
        summary["result"] = {"checkin": c, "travel": t}
        if pf:
            summary["preflight"] = {
                "ok": pf.get("ok"),
                "resolved_status": pf.get("resolved_status"),
                "degraded_fallback": pf.get("degraded_fallback"),
            }
    print(json.dumps(summary, ensure_ascii=False))


def _entry() -> int:
    """★ Windows 差异 4：入口包装 —— 初始化编码 + 顶层异常兜底。

    为什么需要兜底：计划任务没有控制台，脚本崩掉时用户**什么都看不到**，
    只有翻日志才知道 —— 这正是本项目反复吃过的「静默失效」。所以任何未捕获异常
    都要写日志 + 推一条 failure 通知，绝不留白。
    """
    paths.rotate_log(paths.log_path("stdio.log"), max_bytes=512 * 1024, keep_lines=1000)
    redirected = winenv.setup_stdio(paths.LOG_DIR)
    try:
        if redirected:
            print("\n{} [catchup] stdio redirected to {}".format(
                datetime.datetime.now().strftime("%F %T"), redirected))
        main()
        return 0
    except Exception as e:  # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        try:
            log("FATAL: " + tb.replace("\n", " | ")[:1500])
        except Exception:  # noqa: BLE001
            pass
        try:
            notify.ensure_config()
            _notify(
                "WorkBuddy 积分补跑器异常退出",
                "脚本执行过程中抛出未捕获异常，本次补跑未完成。\n\n"
                "异常类型：{}\n".format(type(e).__name__) + str(e)[:300] + "\n\n"
                "完整堆栈已写入：\n  catchup.log\n\n"
                "常见原因：本机未安装/未登录 WorkBuddy 桌面端、Python 环境被改动、"
                "文件夹被移动。自查方式：{}".format(winenv.selfcheck_hint()),
                "failure")
        except Exception:  # noqa: BLE001
            pass
        return 1


if __name__ == "__main__":
    sys.exit(_entry())
