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


def _job_loaded() -> bool:
    """计划任务是否还注册着。

    为什么值得单独查：任务被 `schtasks /Delete` 或 `/Change /DISABLE` 之后，
    调度就安静地停了，而日志里什么都看不出来（因为没有进程去写日志了）。
    注意：`/Query` 对「已停用」的任务**依然返回 0** —— 所以这里额外用 `/FO LIST`
    读一下状态串，尽量把「已停用」也识别出来。
    """
    if sys.platform != "win32":
        # 非 Windows（比如在 mac 上跑自检）无法判定 → 视为「无此问题」，避免误报
        return True
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", JOB_NAME],
                           capture_output=True, text=True, timeout=15,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0:
            return False
    except Exception:  # noqa: BLE001
        return False
    # 再读一次状态串，识别「已禁用」
    try:
        r2 = subprocess.run(["schtasks", "/Query", "/TN", JOB_NAME, "/FO", "LIST", "/V"],
                            capture_output=True, text=True, timeout=15,
                            encoding="utf-8", errors="replace")
        blob = (r2.stdout or "")
        low = blob.lower()
        # 英文系统 "Disabled" / 中文系统 "已禁用"；命中任一即视为未生效
        if "disabled" in low or "已禁用" in blob:
            return False
    except Exception:  # noqa: BLE001
        pass
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
                capture_output=True, timeout=25, env=env)
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


def check(now: float, job_loaded: bool | None = None) -> dict:
    """返回本次检查的结论（纯函数式：只读外部世界，不通知、不落盘）。

    job_loaded 传入时以它为准（自检脚本用它注入「任务已卸载」的情形）。
    """
    age = _age_minutes(STATE)
    log_age = _age_minutes(MAIN_LOG)
    loaded = _job_loaded() if job_loaded is None else bool(job_loaded)

    # 主脚本在「凌晨静默期」与「当日收工后」是**故意不写心跳**的（见 catchup.py
    # 的 _quiet_now / _day_finished）。这两种情况下心跳陈旧是预期行为，不算故障。
    quiet = _quiet_now(now)
    finished = _day_finished(_state_info(), now)
    expected_silence = quiet or finished

    problems = []
    if age is None:
        problems.append(("missing", "找不到 state.json —— 主脚本可能从未成功运行过"))
    elif age > STALE_MINUTES and not expected_silence:
        problems.append(("stale", "主脚本已 {:.0f} 分钟没有运行（心跳阈值 {} 分钟）".format(
            age, STALE_MINUTES)))
    if not loaded:
        problems.append(("unloaded", "计划任务 {} 未注册或已被停用".format(JOB_NAME)))

    return {
        "checked_at": datetime.datetime.fromtimestamp(now).strftime("%F %T"),
        "state_age_minutes": None if age is None else round(age, 1),
        "main_log_age_minutes": None if log_age is None else round(log_age, 1),
        "job_loaded": loaded,
        # 把判定依据显式写出来：事后核对「为什么这次没报警」时不用猜。
        "quiet_hours": quiet,
        "day_finished": finished,
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
