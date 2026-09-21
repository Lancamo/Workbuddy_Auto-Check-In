#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""watchdog.py —— 主脚本（catchup.py）的「存活监控」（Windows 版）

为什么需要它：
  catchup.py 的所有告警都建立在「它自己跑起来了」这个前提上。如果计划任务
  没被注册、任务被停用、Python 被卸载、项目文件夹被移动，主脚本会**完全静默地
  死掉** —— 而且连告警机制一起死，你不会有任何感知。这是整套系统最大的盲区，
  也是唯一无法靠 catchup.py 自己解决的故障：必须由一个**独立、极简、零依赖**的
  第二个计划任务来盯它。

  所以本脚本刻意保持愚蠢：
    · 不 import notify / clawbot / winenv / 任何项目脚本 —— 免得同一个故障同时
      干掉两者（连「发通知」这件事也自己内联实现，不借用项目的通知封装）；
    · 只做两件事：① 主脚本最近有没有动过  ② 计划任务还在不在；
    · 报警只走本机通知，不碰微信、不消耗任何推送配额。

它自己也会死 —— 这是原理性的（无限套娃）。所以本文件不是「万能兜底」，
而是「把最可能发生的静默死亡变成一条你能看见的本地通知」。

用法：
  python watchdog.py            # 检查并在异常时弹本机通知
  python watchdog.py status     # 只打印当前状态，不通知任何东西
  python watchdog.py once       # 同名别名，便于放进计划任务

计划任务：WorkBuddyRewardWatchdog（每 30 分钟一次，由 install.py 一并注册）
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import subprocess
import sys
import time

DIR = pathlib.Path(__file__).resolve().parent
_RUNTIME = DIR / "runtime"
_STATE_DIR = _RUNTIME / "state"

# ★ 本脚本由计划任务用 pythonw 拉起（本身没有控制台），但它调用的 powershell.exe /
#   schtasks 都是**控制台程序**：不给 CREATE_NO_WINDOW，它们会各自新建一个控制台
#   窗口 —— 表现为「每 30 分钟桌面闪一个 PowerShell 黑框」（2026-09-21 用户实测报告，
#   根因就是这里）。本文件刻意不 import winenv（独立性约束，README 5.2 / 自检 8d），
#   所以这个常量只能就地定义，值必须与 winenv.subprocess_flags() 里的一致 ——
#   自检第 8 节有 AST 断言保证「每个子进程调用都带上了它」。
_CREATE_NO_WINDOW = 0x08000000
_LOG_DIR = _RUNTIME / "logs"
_STATE_DIR.mkdir(parents=True, exist_ok=True)
_LOG_DIR.mkdir(parents=True, exist_ok=True)
STATE = _STATE_DIR / "state.json"          # 主脚本每轮都会写 → 它的 mtime 就是心跳
MAIN_LOG = _LOG_DIR / "catchup.log"
SELF_LOG = _LOG_DIR / "watchdog.log"
SELF_STATE = _STATE_DIR / "watchdog_state.json"

JOB_NAME = "WorkBuddyRewardCatchup"        # 被监控的那个计划任务
STALE_MINUTES = 90       # 心跳超过这么久没更新就报警（静默期与收工后除外，见 check）
QUIET_FROM = 700         # 07:00 —— 主脚本的静默期起点。两边各自定义一份常量、
                         # 刻意不互相 import：本文件的价值就在于独立，宁可重复两行。
REALERT_HOURS = 6        # 同一个问题最多每 6 小时提醒一次
MAX_LOG_BYTES = 256 * 1024
# 只在 Windows 上才去探测计划任务（本文件要能在 mac 上跑自检/审阅）。
# 刻意用 sys.platform 而不是 import winenv —— 本文件不许 import 任何项目模块。
_IS_WIN = sys.platform.startswith("win")

# 静默期刚结束的宽限窗口（分钟）：见 check() 里的 first_run_grace。
_WAKE_GRACE_MIN = 10
# 主任务「刚开跑」的判定窗口（分钟）：这段时间内不对 LastTaskResult 下结论。
_JUDGE_RESULT_AFTER_MIN = 2


def log(msg: str) -> None:
    try:
        if SELF_LOG.exists() and SELF_LOG.stat().st_size > MAX_LOG_BYTES:
            kept = SELF_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
            SELF_LOG.write_text("\n".join(kept) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    try:
        with SELF_LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now():%F %T}] {msg}\n")
    except OSError:
        pass


def _age_minutes(p: pathlib.Path):
    """文件距现在多少分钟没更新；文件不存在返回 None。"""
    try:
        return (time.time() - p.stat().st_mtime) / 60.0
    except OSError:
        return None


def _state_info() -> dict:
    """读 state.json 的**内容**（而不是只看 mtime）。

    为什么必须读内容：主脚本有两种情况会**故意不写心跳** —— 凌晨静默期，以及
    当天任务全部完成之后。此时 mtime 陈旧是**设计预期**，不是故障。只看 mtime
    就分不清「正常静默」与「真的停摆」，于是长时间合盖/休眠后一恢复就误报。
    """
    try:
        d = json.loads(STATE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001 — 读不到/损坏就当作「不知道」，按原规则保守判定
        return {}


def _quiet_now(now: float) -> bool:
    """是否处于主脚本的静默时段（00:00–07:00）。"""
    dt = datetime.datetime.fromtimestamp(now)
    return dt.hour * 100 + dt.minute < QUIET_FROM


def _day_finished(st: dict, now: float) -> bool:
    """主脚本今天是否已收工（签到 + 旅行奖励都到手）。

    口径必须与 catchup.py 的 `_day_finished` **完全一致** —— 它是主脚本决定
    「今天不再写心跳」的唯一判据，判错就会把正常静默当成故障。
    """
    today = datetime.datetime.fromtimestamp(now).strftime("%F")
    return (st.get("day") == today
            and bool(st.get("checkin_done"))
            and bool(st.get("claim_done")))


def _decode_console(raw: bytes) -> str:
    """按 Windows 控制台代码页解码命令输出 —— **绝不能写死 utf-8**。

    为什么：`schtasks` 的输出**编码跟随控制台代码页**（中文系统 = GBK/936），
    而**字段名跟随系统显示语言**（中文系统给「已禁用」）。旧代码用
    `text=True, encoding="utf-8", errors="replace"` 去读：GBK 的中文字节在
    UTF-8 下全部变成 U+FFFD，于是中文字面量永远匹配不上。
    实测同一份输出：utf-8 解出来找不到「已禁用」，gb18030 解出来有 6 处。
    """
    if not raw:
        return ""
    for bom, enc in ((b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16"),
                     (b"\xef\xbb\xbf", "utf-8-sig")):
        if raw.startswith(bom):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                break
    # 不带 BOM 的 UTF-16：ASCII 内容每隔一个字节就是 NUL，是可靠信号
    sample = raw[:4096]
    even, odd = sample[::2].count(b"\x00"), sample[1::2].count(b"\x00")
    if even or odd:
        try:
            return raw.decode("utf-16-be" if even > odd else "utf-16-le")
        except UnicodeDecodeError:
            pass
    # 严格 UTF-8 优先（纯 ASCII 与真 UTF-8 都走这条），失败再按中文代码页
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# 「任务状态」这一行的字段名在不同系统语言下不同。**只认这些键** ——
# 因为整串里还散落着若干**无关字段**的值也叫「已禁用」：
#   空闲时间 / 删除没有计划的任务 / 重复: 截止: 持续时间 / 重复: 如果还在运行，停止
# 实测中文系统上整串有 6 处「已禁用」，全是这些无关字段 ——
# 用整串子串匹配的话，健康任务会被判成「未加载」，每 30 分钟误报一次。
_TASK_STATE_KEYS = ("计划任务状态", "scheduled task state", "task state", "status")
_DISABLED_WORDS = ("disabled", "已禁用", "deaktiviert", "desactivada", "desactivado",
                   "désactivé", "disabilitato", "無効", "사용 안 함", "отключено")


def _task_state_via_powershell() -> str | None:
    """用 PowerShell 的 `State` 枚举读任务状态。读不到返回 None。

    返回 "ENABLED" / "DISABLED" / "MISSING"。

    ★ 刻意让脚本**只输出这三个纯 ASCII 字面量**：这样输出编码
      （控制台代码页 / UTF-16 / UTF-8）怎样都影响不到判定，
      和「系统显示语言」也完全解耦 —— 语言与编码两个坑一起绕开。
    """
    safe = str(JOB_NAME).replace("'", "''")
    script = (
        "$ErrorActionPreference='Stop';"
        "try{{"
        "  $s=(Get-ScheduledTask -TaskName '{name}').State;"
        "  if($s -eq 'Disabled'){{'DISABLED'}}else{{'ENABLED'}}"
        "}}catch{{'MISSING'}}"
    ).format(name=safe)
    try:
        p = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
             "-Command", script],
            capture_output=True, timeout=25, creationflags=_CREATE_NO_WINDOW)
    except Exception:  # noqa: BLE001
        return None
    if p.returncode != 0:
        return None
    out = (p.stdout or b"").decode("ascii", "replace").strip().upper()
    for token in ("DISABLED", "MISSING", "ENABLED"):
        if token in out:
            return token
    return None


def _parse_task_state_text(blob: str) -> bool | None:
    """从 `schtasks /FO LIST /V` 的文本里判断任务是否启用。判断不了返回 None。

    ★ **只认「任务状态」那一行**，绝不能拿整串去子串匹配：
      /V LIST 里还散着若干**无关字段**的值也叫「已禁用」——
        空闲时间 / 删除没有计划的任务 / 重复: 截止: 持续时间 / 重复: 如果还在运行，停止
      实测中文系统整串有 6 处「已禁用」，全是这些无关字段。
      整串匹配的话，健康的启用任务会被判成「未加载」，每 30 分钟误报一次。

    抽成独立函数是为了能在自检里直接喂样本断言（不必真去改本机任务状态）。
    """
    for line in blob.splitlines():
        line = line.strip()
        if not line:
            continue
        key, val = None, ""
        for sep in (":", "："):
            if sep in line:
                key, _, val = line.partition(sep)
                break
        if key is None:
            continue
        if key.strip().lower() not in _TASK_STATE_KEYS:
            continue
        low = val.strip().lower()
        return not any(w in low for w in _DISABLED_WORDS)
    return None


def _task_state_via_text() -> bool | None:
    """兜底路径：解析 `schtasks /FO LIST /V` 的「任务状态」行。判断不了返回 None。"""
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", JOB_NAME, "/FO", "LIST", "/V"],
                           capture_output=True, timeout=15,
                           creationflags=_CREATE_NO_WINDOW)
    except Exception:  # noqa: BLE001
        return None
    return _parse_task_state_text(_decode_console(r.stdout or b""))


def _job_loaded() -> bool:
    """计划任务是否**存在且处于启用状态**。

    为什么值得单独查：任务被 `schtasks /Delete` 或 `/Change /DISABLE` 之后，
    调度就安静地停了，而日志里什么都看不出来（因为没有进程去写日志了）。
    这是 watchdog 唯一的存在理由 —— 判错就等于它不存在。

    ★ 2026-09-20 修的两个**互相掩盖**的缺陷（只修任一个都会出问题）：
      · 旧实现用 `encoding="utf-8", errors="replace"` 读 schtasks 输出，
        而该输出编码跟随**控制台代码页**（中文系统 = GBK）→ 中文字面量
        永远读不出来 → **任务真被停用也报健康**（监控彻底失灵）；
      · 旧实现还把「已禁用」当**整串子串**匹配，而 /V LIST 里
        「空闲时间: 已禁用」等无关字段恒含该词（实测中文系统 6 处）→
        若只把编码修对，健康任务会**每 30 分钟误报一次**。
      所以现在：主路径用 PowerShell 的 `State` 枚举（语言无关 + 纯 ASCII 输出），
      兜底路径按控制台代码页解码并**只认「任务状态」那一行**。
    """
    if sys.platform != "win32":
        # 非 Windows（比如在 mac 上跑自检）无法判定 → 视为「无此问题」，避免误报
        return True

    # ① 主路径：与系统显示语言、控制台编码都无关
    state = _task_state_via_powershell()
    if state == "DISABLED" or state == "MISSING":
        return False
    if state == "ENABLED":
        return True

    # ② 兜底：PowerShell 不可用（被策略禁用 / 组件缺失）时退回文本解析
    by_text = _task_state_via_text()
    if by_text is not None:
        return by_text

    # ③ 两条路都判断不了 —— 保守放行，但**留痕**，别让它变成又一处静默失效
    log("无法判定计划任务状态（PowerShell 与 schtasks 都读不到），本次按「正常」处理")
    return True


# ---------------------------------------------------------------------------
# 本机通知（内联，刻意不复用项目的通知封装）
# ---------------------------------------------------------------------------
_PS_APPID = ("{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe")

_PS_TOAST = r"""
$ErrorActionPreference = 'Stop'
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime]
$tpl = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
    [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$nodes = $tpl.GetElementsByTagName('text')
$nodes.Item(0).AppendChild($tpl.CreateTextNode($env:WB_NOTIFY_TITLE)) | Out-Null
$nodes.Item(1).AppendChild($tpl.CreateTextNode($env:WB_NOTIFY_BODY)) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($tpl)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:WB_NOTIFY_APPID).Show($toast)
"""


def _notify(title: str, body: str) -> bool:
    """只走 Windows 本机通知；失败则把原文落到 watchdog.log，绝不静默。

    ★ 刻意不复用 notify.py / winenv.py —— 那正是可能已经坏掉的组件。
      也刻意只写 Windows 分支：本文件是 Windows 版，平台差异必须收口在 winenv.py
      （这条约束由 selftest 第 8 节强制检查）。mac 版的通知由 mac 版 watchdog.py 负责。
    """
    ok = False
    if sys.platform == "win32":
        try:
            import base64
            enc = base64.b64encode(_PS_TOAST.encode("utf-16-le")).decode("ascii")
            env = dict(os.environ)
            env.update({
                "WB_NOTIFY_TITLE": title[:120],
                "WB_NOTIFY_BODY": body[:600],
                "WB_NOTIFY_APPID": _PS_APPID,
            })
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc],
                capture_output=True, timeout=25, env=env,
                creationflags=_CREATE_NO_WINDOW)
            ok = proc.returncode == 0
        except Exception:  # noqa: BLE001
            ok = False

    if not ok:
        try:
            with SELF_LOG.open("a", encoding="utf-8") as f:
                f.write("[{}] 本机通知投递失败，原文如下：\n{}\n{}\n".format(
                    datetime.datetime.now().strftime("%F %T"), title, body))
        except OSError:
            pass
    return ok


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------
def _load_self_state() -> dict:
    try:
        if SELF_STATE.exists():
            d = json.loads(SELF_STATE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                d.setdefault("last_alert", {})
                return d
    except Exception:  # noqa: BLE001
        pass
    return {"last_alert": {}}


def _save_self_state(st: dict) -> None:
    try:
        SELF_STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except OSError:
        pass


def _task_action_script() -> str | None:
    """计划任务里实际注册的**脚本绝对路径**。取不到返回 None。

    ★ 为什么需要它（2026-09-20）：只查「任务在不在」是不够的。本项目把计划任务装成
      **绝对路径**，文件夹一挪位置（或改名），任务仍旧指向旧位置：它照样「已注册、
      已启用」，每次触发却以 0x8007010B（目录名无效）失败 —— 不做任何事，也不报错。
      实测：搬迁后主任务与 watchdog **双双**指向旧路径、连续失败，
      而当时的 watchdog 全程报 healthy，一个能报警的都没有。
      ⇒ 判据必须包含「任务到底在跑哪个文件」。
    """
    safe = str(JOB_NAME).replace("'", "''")
    script = ("$ErrorActionPreference='Stop';"
              "(Get-ScheduledTask -TaskName '{name}').Actions | "
              "Select-Object -First 1 | ForEach-Object {{ $_.Arguments }}").format(name=safe)
    try:
        p = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
             "-Command", script],
            capture_output=True, timeout=25, creationflags=_CREATE_NO_WINDOW)
    except Exception:  # noqa: BLE001
        return None
    if p.returncode != 0:
        return None
    out = _decode_console(p.stdout or b"").strip()
    if not out:
        return None
    return out.splitlines()[0].strip().strip('"')


def _task_run_info() -> tuple[int | None, float | None]:
    """主任务的 `(LastTaskResult, LastRunTime 的 epoch 秒)`。取不到给 None。

    取值成功时是 0；失败时是 Win32 错误码（>= 0x80000000）；
    还有一批 0x000413xx 的**信息码**（267011 = 从未运行过、267009 = 正在运行…）。
    是不是失败由 `_result_is_error()` 判，这里只负责把数字取回来。

    ★ 为什么要连 `LastRunTime` 一起取（2026-09-21 实测教训）：结果字段在**刚启动的
      那一刻可能是过渡值**。07:00:05 采到过 `0x800710E0`（「操作员或管理员拒绝了
      请求」——同一时刻唤醒任务与主任务都在拉 `catchup.py`），而当天签到其实成功了。
      有了 `LastRunTime` 才能判「这一次是不是刚开跑、还不该下结论」。
      PowerShell 侧直接算成 epoch 秒，免得把本地化时间串读回来再解析。
    """
    safe = str(JOB_NAME).replace("'", "''")
    script = ("$ErrorActionPreference='Stop';"
              "$i=Get-ScheduledTaskInfo -TaskName '{name}';"
              "'{{0}};{{1}}' -f $i.LastTaskResult,"
              " ([DateTimeOffset]$i.LastRunTime).ToUnixTimeSeconds()").format(name=safe)
    try:
        p = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
             "-Command", script],
            capture_output=True, timeout=25, creationflags=_CREATE_NO_WINDOW)
    except Exception:  # noqa: BLE001
        return None, None
    if p.returncode != 0:
        return None, None
    parts = _decode_console(p.stdout or b"").strip().split(";")
    result: int | None = None
    last_run: float | None = None
    if parts and parts[0].strip():
        try:
            result = int(parts[0].strip())
        except ValueError:
            result = None
    if len(parts) > 1 and parts[1].strip():
        try:
            ts = float(parts[1].strip())
            # 从未运行过时 LastRunTime 是 1999-11-30 的哨兵值（约 9.4e8 秒）；
            # 比 2001 年还早的时间戳一律当作「没有运行过」，别拿去算时间差。
            last_run = ts if ts > 1.0e9 else None
        except ValueError:
            last_run = None
    return result, last_run


def _result_is_error(result: int) -> bool:
    """LastTaskResult 是否代表**失败**。

    0 = 成功；0x000413xx 是任务计划程序的**信息码**（未运行过 / 正在运行 / 已排队），
    不是失败 —— 把它们当失败的话，「任务刚注册、一次都还没跑」就会误报。
    真正的失败码都 >= 0x80000000（如 0x8007010B = ERROR_DIRECTORY）。
    """
    return (int(result) & 0xFFFFFFFF) >= 0x80000000


def _same_file(a, b) -> bool:
    """两个路径是否指向同一个文件（Windows 大小写不敏感；resolve 失败则退化比较）。"""
    try:
        return pathlib.Path(a).resolve() == pathlib.Path(b).resolve()
    except Exception:  # noqa: BLE001
        return str(a).strip().lower() == str(b).strip().lower()


def check(now: float, job_loaded: bool | None = None,
          task_action: str | None = None, task_result: int | None = None,
          task_last_run: float | None = None) -> dict:
    """返回本次检查的结论（纯函数式：只读外部世界，不通知、不落盘）。

    这几个参数传入时以它为准（自检脚本用它注入「任务被卸载」「任务指向旧路径」
    「任务上次运行失败 / 刚开跑」等情形，不必真去改本机任务）。

    ★ 探测只在真机路径上做（即 `job_loaded is None` 时）：自检一律显式注入，
      于是它只测逻辑，不会因为本机任务恰好出问题而假失败。
    """
    age = _age_minutes(STATE)
    log_age = _age_minutes(MAIN_LOG)
    loaded = _job_loaded() if job_loaded is None else bool(job_loaded)

    if job_loaded is None and _IS_WIN:
        if task_action is None:
            task_action = _task_action_script()
        if task_result is None or task_last_run is None:
            _res, _run = _task_run_info()
            if task_result is None:
                task_result = _res
            if task_last_run is None:
                task_last_run = _run

    # 主脚本在「凌晨静默期」与「当日收工后」是**故意不写心跳**的（见 catchup.py
    # 的 _quiet_now / _day_finished）。这两种情况下心跳陈旧是预期行为，不算故障。
    dt = datetime.datetime.fromtimestamp(now)
    st_info = _state_info()
    quiet = _quiet_now(now)
    finished = _day_finished(st_info, now)

    # ★ 2026-09-21 实测新增的第三个「预期静默」：静默期刚结束的那几分钟。
    #   静默期内主脚本**故意不写心跳**，机器又整夜睡着/空转，于是 07:00 的第一次采样
    #   一定超阈值。若正好撞上 07:00 那次运行还没写完 state，就会报出一条
    #   「主脚本已 432 分钟没有运行」的假告警（本机实测就是这么发生的）。
    #   只在「盘上的 state 还是前一天的」时生效 —— 所以既不会掩盖 07:10 之后
    #   真正的停摆，也不会掩盖「今天已经跑过又停了」。
    hm = dt.hour * 100 + dt.minute
    first_run_grace = (QUIET_FROM <= hm < QUIET_FROM + _WAKE_GRACE_MIN
                       and st_info.get("day") != dt.strftime("%F"))
    expected_silence = quiet or finished or first_run_grace

    problems = []
    if age is None:
        problems.append(("missing", "找不到 state.json —— 主脚本可能从未成功运行过"))
    elif age > STALE_MINUTES and not expected_silence:
        problems.append(("stale", "主脚本已 {:.0f} 分钟没有运行（心跳阈值 {} 分钟）".format(
            age, STALE_MINUTES)))
    if not loaded:
        problems.append(("unloaded", "计划任务 {} 未注册或已被停用".format(JOB_NAME)))

    # ★ 2026-09-20 新增：任务「指向哪个文件」与「上次跑成没成」。
    #   搬迁后任务仍指向旧绝对路径时，状态是「已注册、已启用」，但每次触发都失败 ——
    #   只查 loaded 的话会一直报 healthy（本机实测踩过，主任务与监控任务同时哑掉）。
    if task_action and not _same_file(task_action, DIR / "catchup.py"):
        problems.append(("wrong_path",
                         "计划任务指向的是旧路径（{}），当前目录是 {}"
                         " —— 签到不会执行".format(task_action, DIR / "catchup.py")))
    # ★ 刚开跑的那一次不下结论（2026-09-21 实测教训）：主任务每 5 分钟一次，
    #   采样很可能正好落在它启动的几秒内，此时 LastTaskResult 可能还是过渡值
    #   （实测 07:00:05 读到 0x800710E0「操作员或管理员拒绝了请求」——
    #    同一时刻唤醒任务与主任务都在拉 catchup.py，而当天签到其实是成功的）。
    #   只在「这次运行已开始 ≥ _JUDGE_RESULT_AFTER_MIN 分钟」时才判失败，
    #   所以既躲开过渡值，也不会让常驻失败被漏掉（下一轮采样照报）。
    result_just_started = (isinstance(task_last_run, (int, float))
                           and (now - float(task_last_run))
                           < _JUDGE_RESULT_AFTER_MIN * 60)
    if isinstance(task_result, int) and _result_is_error(task_result) \
            and not result_just_started:
        problems.append(("task_failed",
                         "主任务最近一次运行以错误码 0x{:08X} 结束（不是「已注册」"
                         "就等于在跑）".format(task_result & 0xFFFFFFFF)))

    return {
        "checked_at": dt.strftime("%F %T"),
        "state_age_minutes": None if age is None else round(age, 1),
        "main_log_age_minutes": None if log_age is None else round(log_age, 1),
        "job_loaded": loaded,
        # 把新判据的依据也显式写出来：事后核对「为什么这次报警/没报」时不用猜
        "task_script": task_action,
        "task_last_result": (None if task_result is None
                             else "0x{:08X}".format(task_result & 0xFFFFFFFF)),
        "task_last_run": (None if not isinstance(task_last_run, (int, float))
                          else datetime.datetime.fromtimestamp(
                              float(task_last_run)).strftime("%F %T")),
        # 把判定依据显式写出来：事后核对「为什么这次没报警」时不用猜。
        "quiet_hours": quiet,
        "day_finished": finished,
        "first_run_grace": first_run_grace,
        "problems": [k for k, _ in problems],
        "details": [d for _, d in problems],
        "healthy": not problems,
    }


def main() -> None:
    now = time.time()
    st = _load_self_state()
    res = check(now)

    if res["healthy"]:
        # 恢复正常时清掉告警记录，让下次故障能立刻再报（而不是被冷却吃掉）
        if st.get("last_alert"):
            log("恢复正常，清空告警冷却记录")
        st["last_alert"] = {}
        _save_self_state(st)
        log("ok state_age={} log_age={} job_loaded={}".format(
            res["state_age_minutes"], res["main_log_age_minutes"], res["job_loaded"]))
        print(json.dumps(res, ensure_ascii=False))
        return

    log("PROBLEM {} | {}".format(",".join(res["problems"]), " | ".join(res["details"])))

    announced = []
    for key, detail in zip(res["problems"], res["details"]):
        last = st["last_alert"].get(key)
        if isinstance(last, (int, float)) and (now - last) < REALERT_HOURS * 3600:
            continue
        announced.append((key, detail))

    if announced:
        body = "\n".join([
            "Watchdog 发现积分脚本可能已经停摆：",
            "",
            *["· " + d for _, d in announced],
            "",
            "自查：",
            "1) schtasks /Query /TN {} /V /FO LIST".format(JOB_NAME),
            "2) 看 {} 的最后几行".format(MAIN_LOG.name),
            "3) python {} status".format(pathlib.Path(__file__).name),
            "",
            "若电脑刚从长时间睡眠中醒来，可忽略本条。",
        ])
        if _notify("⚠️ WorkBuddy 积分脚本可能已停摆", body):
            for key, _ in announced:
                st["last_alert"][key] = now
            _save_self_state(st)

    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "status":
        print(json.dumps(check(time.time()), ensure_ascii=False, indent=2))
    else:
        main()
