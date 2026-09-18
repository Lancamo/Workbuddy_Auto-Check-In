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
全不可用则降级**本机桌面通知**（Windows Toast / macOS 通知中心）；
凭据见 notify_config.json）；并向 stdout 输出一行 JSON 摘要。

★ 与 macOS 版的差异（**只在下列标记处**，其余代码逐字一致，便于两套同步）
  1. 解释器的取法（两侧都不再硬编码，2026-09-18 起一致）：
     mac 版用 `sys.executable`；Windows 版用 `winenv.default_python()`
     （同一语义，另做 python.exe ↔ pythonw.exe 的切换）。
     所以**整个文件夹拷到任何机器都能直接跑，不需要改任何常量**。
     （2026-09-18 之前 mac 版把一台机器专属的绝对路径写死在脚本顶部，换机即失效。）
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

WINDOW1 = 700       # 07:00 签到 + 派遣（「≥ 时间」语义，过了点补跑）
MAX_TRIES = 12      # 单事项最多真实尝试次数
MIN_RETRY_GAP_MIN = 20   # 两次真实尝试之间的最小间隔（分钟）
LOCK_STALE_SEC = 600     # 超过 10 分钟未释放的锁视为残留（正常一次运行只需几秒）

# 为什么需要 MIN_RETRY_GAP_MIN：触发频率从 30 分钟提到 5 分钟后，若不加间隔，
# MAX_TRIES=12 会在 1 小时内烧完，把「重试」变成「刷请求」。加 20 分钟间隔后，
# 12 次尝试 ≈ 4 小时，节奏与原来的 30 分钟触发基本一致。
#
# 为什么不做「固定时刻兜底」：领取的唯一正当理由是**猫真的到了**。
# 时间兜底会凭空制造一次请求，而且「到点电脑恰好睡着」反而是它最不可靠的时候。
# 到达时间由接口给出（travel 的 arrive_at）；缺失时下面的 need_arrive_info 会去问一次。


MAX_LOG_BYTES = 512 * 1024   # 日志超过这个大小就裁剪（触发频率提到 5 分钟后需要）


def log(msg: str) -> None:
    paths.rotate_log(LOG, max_bytes=MAX_LOG_BYTES, keep_lines=800)
    # ★ Windows 差异：显式 UTF-8（Windows 默认 GBK，中文日志会乱码/报错）
    with LOG.open("a", encoding="utf-8", errors="replace") as f:
        f.write(f"[{datetime.datetime.now():%F %T}] {msg}\n")


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
_NET_HINTS = ("timeout", "timed out", "超时", "网络", "connection", "ssl", "proxy", "代理")
_DRIFT_HINTS = ("404", "405", "不存在", "not found", "接口", "endpoint", "路由")


def _hint_for(reasons: list[str]) -> str:
    """按失败原因给出对应的处理动作（返回一句给用户看的话）。"""
    low = " ".join(str(r or "") for r in reasons).lower()
    if any(k in low for k in _DRIFT_HINTS):
        return ("像是接口变了，脚本已尝试自动跟随。请运行 py -3 scripts\\api_discovery.py "
                "核对端点；若仍失败，多半需要等客户端升级。")
    if any(k in low for k in _AUTH_HINTS):
        return "打开一次 WorkBuddy 桌面端即可刷新登录态，之后脚本会自动重试。"
    if any(k in low for k in _NET_HINTS):
        return "像是网络问题，脚本会在 20 分钟后自动重试；若持续失败，检查网络或代理。"
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


def _fmt_checkin(c: dict) -> str:
    st = c.get("status")
    if st == "success":
        return "签到：+{} 积分{}".format(
            c.get("credit") if c.get("credit") is not None else "?", _act(c))
    if st == "already_checked":
        return "签到：今日已领过{}".format(_act(c))
    if st == "no_activity":
        return "签到：当前没有进行中的活动（空档期不计积分）"
    if st == "suspect":
        return "签到：⚠️ 接口返回的数据全为零，无法确认活动状态"
    if st == "failed":
        return "签到：失败 —— {}".format(c.get("reason") or "未知原因")
    return "签到：未执行"


def _fmt_travel(t: dict) -> str:
    st = t.get("status")
    if st == "claimed":
        return "旅行：+{} 积分".format(
            t.get("reward_credit") if t.get("reward_credit") is not None else "?")
    if st == "departed":
        return "旅行：已派出（到达后可领奖励）"
    if st == "traveling":
        return "旅行：猫还在路上"
    if st == "daily_limit_reached":
        return "旅行：今日已达上限（已领过）"
    if st == "failed":
        return "旅行：失败 —— {}".format(t.get("reason") or "未知原因")
    return "旅行：未执行"


def _report(c: dict, t: dict, state: dict, pf: dict | None = None) -> None:
    """按结果推送通知。

    **到账类分成两条独立消息**：签到成功推「签到到账」，旅行领取成功推「旅行奖励到账」。
    它们本来是两件独立发生的事（实测常相隔 1–2 小时），合并成一条会让人分不清
    哪一笔到账了、也无法在时间线上对上账。同一次运行里两件都成，就推两条。

    失败类仍合并成一条 —— 一次故障不该刷两条，而且失败信息要放在一起才看得懂。
    """
    pf = pf or {}
    checkin_gain = c.get("status") == "success" and c.get("credit") is not None
    travel_gain = t.get("status") == "claimed" and t.get("reward_credit") is not None

    failed = [n for n, v in (("签到", c), ("旅行", t)) if v.get("status") == "failed"]
    suspect = c.get("status") == "suspect"
    no_activity = c.get("status") == "no_activity"
    # 前置校验异常：只剩兜底端点可用，或探活全失败
    pf_bad = bool(pf) and (pf.get("probed") and not pf.get("ok"))
    # 端点相对上次发生了变化（客户端升级导致接口换名/换前缀）—— 这是要主动报的
    ep_changed = bool(pf.get("changed"))
    # 交叉比对（查 vs 领）发现的漂移线索 —— checkin / travel 各自可能给出
    drift = c.get("drift_suspect") or t.get("drift_suspect")

    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    progress = "今日进度：签到 {} / 旅行 {}".format(
        "✓" if state.get("checkin_done") else "…",
        "✓" if state.get("claim_done") else "…")

    # ① 签到到账 —— 独立一条消息
    if checkin_gain:
        _notify(
            "WorkBuddy 签到到账：+{} 积分".format(c["credit"]),
            "\n".join([
                "签到成功，+{} 积分{}".format(c["credit"], _act(c)),
                "连续签到：{} 天".format(c.get("streak_days", "?")),
                "",
                "时间：{}".format(stamp),
                progress,
            ]),
            "success")

    # ② 旅行到账 —— 独立一条消息
    if travel_gain:
        _notify(
            "WorkBuddy 旅行奖励到账：+{} 积分".format(t["reward_credit"]),
            "\n".join([
                "猫已到达，旅行奖励领取成功：+{} 积分".format(t["reward_credit"]),
                "",
                "时间：{}".format(stamp),
                progress,
            ]),
            "success")

    content = "\n".join([
        _fmt_checkin(c),
        _fmt_travel(t),
        "",
        "时间：{}".format(stamp),
        progress,
    ])
    if ep_changed:
        old = ((pf.get("changed_from") or {}).get("status_paths") or [])[:2]
        content += "\n\n🔄 接口已切换（客户端 {}）：\n  旧：{}\n  新：{}".format(
            pf.get("client_version") or "?", "\n      ".join(old) or "(无)",
            pf.get("resolved_status") or pf.get("preferred_status") or "?")

    if failed or suspect or pf_bad:
        if drift and failed:
            # 漂移优先于「失败」：这类故障的处置是「核对端点」，
            # 不是「等重试」也不是「去刷登录态」，标题必须先把性质说清楚。
            title = "WorkBuddy 积分：接口疑似已变更（" + "、".join(failed) + "）"
        elif failed:
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
        _notify(title, content + extra, "failure")
    elif checkin_gain or travel_gain:
        pass   # 到账消息已在上面各推一条，这里不再补汇总
    elif no_activity and state.get("no_activity_streak", 0) >= 3:
        # 防「静默失效」保险：连续多日报无活动时，多半是接口/活动判定出了问题
        # （2026-09-17 就因旧接口恒返回 active=false，连续静默了整整一轮活动）。
        _notify(
            "WorkBuddy 积分：连续 {} 天报「无活动」，建议人工核对".format(
                state.get("no_activity_streak")),
            content + "\n\n⚠️ 请打开 WorkBuddy 桌面端确认积分页是否真有活动。\n"
            "若页面有活动而脚本报无活动，说明活动接口又变了，"
            "自查方式：{}。".format(winenv.selfcheck_hint()),
            "failure")
    elif no_activity:
        _notify(
            "WorkBuddy 积分：当前没有进行中的活动",
            content + "\n\n说明：活动空档期不产生积分，脚本会继续每日巡检，活动开启后自动恢复。",
            "warning")
    else:
        # 常规巡检：但若端点刚变过，提升为 warning 并改标题，确保不被当成噪音划过去
        _notify(
            "WorkBuddy 积分：接口已自动切换" if ep_changed else "WorkBuddy 积分巡检",
            content + ("\n\n说明：端点已自动改用客户端当前使用的新路径，不用你操作。"
                       if ep_changed else ""),
            "warning" if ep_changed else "info")


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


def main() -> None:
    """入口：先抢文件锁，再跑 _run()。

    为什么入口只做这一件事：触发频率提到 5 分钟后，「计划任务」与「手动 run_now」
    撞在同一分钟的概率不再可忽略，而两者都会读 → 改 → 写 state.json。
    锁放在最外层，保证任何退出路径（包括异常）都会释放。
    """
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
        state = {"day": today, "checkin_done": False, "checkin_tries": 0,
                 "checkin_last_try": 0,
                 "claim_done": False, "claim_tries": 0,
                 "claim_last_try": 0,
                 "arrive_at": None,
                 "no_activity_streak": carry_streak}

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

    need_checkin = (hm >= WINDOW1 and not state["checkin_done"]
                    and state["checkin_tries"] < MAX_TRIES
                    and _due(state, "checkin_last_try", now_ts, MIN_RETRY_GAP_MIN))

    claim_open = _claim_open(now_ts, state)
    # 该去处理旅行：① 猫到了（窗口开）；或 ② 还不知道猫何时到达，需要问一次接口补齐
    #    （state 被删过 / 猫是用户自己在 App 里派的 / 昨天派遣那一步失败了）。
    # ② 是唯一「明明没到点也要问一次」的例外，同样受 MAX_TRIES + 最小间隔约束，
    # 并且要求已过 07:00 —— 否则凌晨会顺带把当天的签到也提前做掉。
    need_arrive_info = (not state["claim_done"] and not state.get("arrive_at")
                        and hm >= WINDOW1)
    need_claim = ((claim_open and not state["claim_done"]) or need_arrive_info) \
        and state["claim_tries"] < MAX_TRIES \
        and _due(state, "claim_last_try", now_ts, MIN_RETRY_GAP_MIN)

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
            state["checkin_tries"] += 1
            state["checkin_last_try"] = now_ts
            # no_activity 也计入「完成」：活动空档期无论重试多少次都不会有积分，
            # 计入完成才能让后续轮询真正空转、不做无谓请求。
            # suspect 同样计入完成 —— 全零载荷说明链路有问题，重试不会变好，
            # 该做的是**立刻告警**（见 _report），而不是反复重试刷屏。
            if c.get("status") in ("success", "already_checked", "no_activity", "suspect"):
                state["checkin_done"] = True
            # 跨日累计「拿不到活动数据」的天数：连续多日即为接口/活动判定异常的信号
            if c.get("status") in ("no_activity", "suspect"):
                state["no_activity_streak"] = int(state.get("no_activity_streak") or 0) + 1
            elif c.get("status") in ("success", "already_checked"):
                state["no_activity_streak"] = 0
        if need_claim:
            state["claim_tries"] += 1
            state["claim_last_try"] = now_ts
            if t.get("status") in ("claimed", "daily_limit_reached"):
                state["claim_done"] = True

        # 记录 / 清除猫的到达时间（「到达即领」的判据来源）：
        #   领到手 → 清掉，不留过期时间戳；
        #   已派出 / 在路上 → 记下来，下次触发不花一次网络请求就能判断该不该去领。
        # 接口本来就回了这个字段，落盘等于把它从「当次用完即弃」变成「可复用」。
        if t.get("status") == "claimed":
            state["arrive_at"] = None
        elif t.get("arrive_at"):
            state["arrive_at"] = t["arrive_at"]

        _report(c, t, state, pf)
    elif disc.get("changed"):
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
                "明天会自动重试；若连续多天如此，{}".format(winenv.selfcheck_hint()),
            ]),
            "failure")

    state["last_seen_ts"] = now_ts     # 下一轮做时间倒退检测的基线
    _save_state(state)
    log(f"state={state}")

    summary = {
        "action": "ran" if (need_checkin or need_claim) else "none",
        "checkin_done": state["checkin_done"],
        "claim_done": state["claim_done"],
        # 领取闸门状态：排查「为什么还没领」时一眼就能看出是闸门没开还是领取失败了
        "claim_open": claim_open,
        "arrive_at": state.get("arrive_at"),
        # 排查「为什么一直没领到」：试了几次、预算是否已用尽
        "claim_tries": state.get("claim_tries"),
        "probe_exhausted": probe_exhausted,
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
