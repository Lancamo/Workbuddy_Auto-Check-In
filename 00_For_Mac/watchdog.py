#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""watchdog.py —— 主脚本（catchup.py）的「存活监控」

为什么需要它：
  catchup.py 的所有告警都建立在「它自己跑起来了」这个前提上。如果 LaunchAgent
  没被加载、plist 被删掉、Python 被卸载、项目文件夹被移动，主脚本会**完全静默地
  死掉** —— 而且连告警机制一起死，你不会有任何感知。这是整套系统最大的盲区，
  也是唯一无法靠 catchup.py 自己解决的故障：必须由一个**独立、极简、零依赖**的
  第二个计划任务来盯它。

  所以本脚本刻意保持愚蠢：
    · 不 import notify / clawbot / 任何项目脚本 —— 免得同一个故障同时干掉两者；
    · 只做两件事：① 主脚本最近有没有动过  ② 计划任务还在不在；
    · 报警只走本机通知（osascript），不碰微信、不消耗任何推送配额。

它自己也会死 —— 这是原理性的（无限套娃）。所以本文件不是「万能兜底」，
而是「把最可能发生的静默死亡变成一条你能看见的本地通知」。

用法：
  python3 watchdog.py            # 检查并在异常时弹本机通知
  python3 watchdog.py status     # 只打印当前状态，不通知任何东西

计划任务：com.workbuddy.wb-reward-watchdog（每 30 分钟一次）
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

JOB_LABEL = "com.workbuddy.wb-reward-catchup"
STALE_MINUTES = 90       # 心跳超过这么久没更新就报警（> 一次电脑睡眠，避免误报）
REALERT_HOURS = 6        # 同一个问题最多每 6 小时提醒一次
MAX_LOG_BYTES = 256 * 1024


def log(msg: str) -> None:
    try:
        if SELF_LOG.exists() and SELF_LOG.stat().st_size > MAX_LOG_BYTES:
            kept = SELF_LOG.read_text(errors="replace").splitlines()[-400:]
            SELF_LOG.write_text("\n".join(kept) + "\n")
    except Exception:  # noqa: BLE001
        pass
    try:
        with SELF_LOG.open("a") as f:
            f.write(f"[{datetime.datetime.now():%F %T}] {msg}\n")
    except OSError:
        pass
    try:
        SELF_LOG.chmod(0o600)
        SELF_STATE.chmod(0o600)
    except OSError:
        pass


def _age_minutes(p: pathlib.Path):
    """文件距现在多少分钟没更新；文件不存在返回 None。"""
    try:
        return (time.time() - p.stat().st_mtime) / 60.0
    except OSError:
        return None


def _job_loaded() -> bool:
    """LaunchAgent 是否还挂在 launchd 上。

    为什么值得单独查：plist 被删 / `launchctl bootout` 之后，launchd 会安静地
    不再调度，而日志里什么都看不出来（因为没有进程去写日志了）。
    """
    try:
        r = subprocess.run(
            ["launchctl", "print", "gui/{}/{}".format(os.getuid(), JOB_LABEL)],
            capture_output=True, text=True, timeout=15)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _notify(title: str, body: str) -> bool:
    """只走本机通知。刻意不复用 notify.py —— 那正是可能已经坏掉的组件。"""
    try:
        safe = body.replace('"', "'").replace("\\", "/")[:220]
        subprocess.run(
            ["osascript", "-e",
             'display notification "{}" with title "{}"'.format(safe, title)],
            capture_output=True, timeout=10)
        return True
    except Exception:  # noqa: BLE001
        return False


def _load_self_state() -> dict:
    try:
        if SELF_STATE.exists():
            d = json.loads(SELF_STATE.read_text())
            if isinstance(d, dict):
                d.setdefault("last_alert", {})
                return d
    except Exception:  # noqa: BLE001
        pass
    return {"last_alert": {}}


def _save_self_state(st: dict) -> None:
    try:
        SELF_STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2))
    except OSError:
        pass


def check(now: float, job_loaded: bool | None = None) -> dict:
    """返回本次检查的结论（纯函数式：只读外部世界，不通知、不落盘）。

    job_loaded 传入时以它为准（自检脚本用它注入「任务已卸载」的情形）。
    """
    age = _age_minutes(STATE)
    log_age = _age_minutes(MAIN_LOG)
    loaded = _job_loaded() if job_loaded is None else bool(job_loaded)

    problems = []
    if age is None:
        problems.append(("missing", "找不到 state.json —— 主脚本可能从未成功运行过"))
    elif age > STALE_MINUTES:
        problems.append(("stale", "主脚本已 {:.0f} 分钟没有运行（心跳阈值 {} 分钟）".format(
            age, STALE_MINUTES)))
    if not loaded:
        problems.append(("unloaded", "计划任务 {} 未挂载到 launchd".format(JOB_LABEL)))

    return {
        "checked_at": datetime.datetime.fromtimestamp(now).strftime("%F %T"),
        "state_age_minutes": None if age is None else round(age, 1),
        "main_log_age_minutes": None if log_age is None else round(log_age, 1),
        "job_loaded": loaded,
        "problems": [k for k, _ in problems],
        "details": [d for _, d in problems],
        "healthy": not problems,
    }


def main() -> None:
    now = time.time()
    for name, limit, keep in (
        ("watchdog.out.log", MAX_LOG_BYTES, 500),
        ("watchdog.err.log", MAX_LOG_BYTES, 300),
    ):
        path = _LOG_DIR / name
        try:
            if path.exists() and path.stat().st_size > limit:
                kept = path.read_text(errors="replace").splitlines()[-keep:]
                path.write_text("\n".join(kept) + "\n")
                path.chmod(0o600)
        except OSError:
            pass
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

    if res["problems"]:
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
                "1) launchctl print gui/$(id -u)/{}".format(JOB_LABEL),
                "2) tail -20 {}".format(MAIN_LOG.name),
                "3) python3 {} status".format(pathlib.Path(__file__).name),
                "",
                "若电脑刚从长时间睡眠中醒来，可忽略本条。",
            ])
            if _notify("⚠️ WorkBuddy 积分脚本可能已停摆", body):
                for key, _ in announced:
                    st["last_alert"][key] = now
                _save_self_state(st)

    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        print(json.dumps(check(time.time()), ensure_ascii=False, indent=2))
    else:
        main()
