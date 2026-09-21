#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ClawBot 会话续期守护（renew.py）

为什么需要它
------------
ClawBot 的登录会话会失效（`errcode=-14 / session timeout`），失效后**必须人工重新扫码**，
失效期间所有自动推送都会被静默丢弃。本模块负责**提前预警**。

监测原理（v2，2026-09-16 重写）
------------------------------
v1 用 `getupdates` 主动轮询来探测"用户是否发过消息"。**该方案已废弃**，原因：

    脚本与 WorkBuddy 桌面端现在共用同一个 `bot_id`（同一个 iLink 账号）。
    iLink 的消息队列对同一 bot 只有一个消费位，两边都长轮询就会**互相抢消息**：
    脚本每轮询一次就推进自己的游标，发给该 bot 的消息可能被脚本半路消费掉。

v2 改为**只读桌面端的轮询游标文件**，零网络请求、零抢消息：

    ~/.workbuddy/claw-state/weixin/<bot_id>.cursor.json

该文件由 WorkBuddy 桌面端在**每次成功轮询后**写出。实测刷新间隔**固定 18.1 秒**
（两次独立观测：18.1 / 18.1 / 18.2 / 18.1；18.18 / 17.98 / 18.12 / 18.13 / 18.14）。

**这个 18 秒的出处已定位**（2026-09-17 读桌面端 `app.asar` → `weixin-api.ts` 核实）：
它不是客户端写死的常量，而是**腾讯 iLink 服务端每轮响应下发的 `longpolling_timeout_ms`**，
客户端在 `pollLoop()` 里跟随（`this.nextPollTimeoutMs = result.longpolling_timeout_ms`）。
客户端自己的兜底默认其实是 **35 秒**（`DEFAULT_POLL_TIMEOUT_MS = 35e3`）。

因此这是**长轮询超时**循环，不是每 18 秒新发一次请求 ——
请求挂在服务端等约 18 秒、没有新消息就返回、客户端**立刻重连**（正常路径无 sleep、
无退避；异常才走 `backoffDelay()` 的指数退避）。任一时刻只有 1 个挂起连接。
它的 `mtime` 就是「桌面端最后一次成功维持会话」的时间戳：

    · mtime 新鲜  → 有人在持续消费该 bot 的消息，会话被正常维护 → 健康
    · mtime 陈旧  → 桌面端已停止轮询（App 退出 / 崩溃 / 被更新中断）→ 会话有失效风险

这条判据有实测支撑：本机上一个已停用 bot 的游标 mtime 停在 2026-08-19 11:11，
而该会话在同一时期之后再未成功通信，最终于 2026-09-16 确认 -14 失效 —— 游标停滞
正是会话失活的前兆信号。

失效的兜底识别
--------------
游标判据覆盖"无人轮询"这一路径。若会话因其它原因失效，脚本的**日常推送本身**会立刻
返回 -14，由 `notify.py` 记录并在下次运行时报出「需重新扫码」。两条路径合起来不留盲区。

提醒行为
--------
游标陈旧超过 `cursor_stale_hours` → 推送一条提醒（**每天最多 1 条**，直到恢复），
文案同时建议两个动作（打开桌面端 / 给 bot 发条消息），任一都能刷新会话。

状态文件 `renew_state.json` 可安全删除（删后按"现在"重新起算）。

自测：
  python3 renew.py            # 执行一次检查（纯本地，无网络请求）
  python3 renew.py --force    # 强制执行（忽略节流）
  python3 renew.py status     # 只看状态
  python3 renew.py reset      # 把计时基线重置为现在
"""
from __future__ import annotations

import datetime
import json
import pathlib
import sys

DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(DIR))
import paths  # noqa: E402

import clawbot
import notify

CONF = paths.config_path("renew_config.json")
STATE = paths.state_path("renew_state.json")
LOG = paths.log_path("catchup.log")

# WorkBuddy 桌面端存放各 bot 轮询游标的目录
CLAW_STATE_DIR = pathlib.Path.home() / ".workbuddy" / "claw-state" / "weixin"

DEFAULTS: dict = {
    "enabled": True,
    "cursor_stale_hours": 24,
    "check_interval_minutes": 30,
    "_help_说明": "监控 WorkBuddy 桌面端是否在持续轮询 ClawBot。停放轮询是会话失效的前兆，本模块据此提前提醒。全程只读本地文件，不发任何网络请求，因此不会与桌面端抢消息。",
    "_help_字段": {
        "enabled": "总开关",
        "cursor_stale_hours": "桌面端游标超过这个小时数没有刷新就提醒（默认 24）。桌面端正常轮询间隔约 18 秒，故 24 小时只可能是真的停了；阈值需大于一次电脑睡眠时长，避免误报",
        "check_interval_minutes": "检查间隔（分钟，默认 30）。纯本地文件读取，开销可忽略；提醒本身每天最多 1 条",
    },
}


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    try:
        with LOG.open("a") as f:
            f.write("[{}] [renew] {}\n".format(datetime.datetime.now().strftime("%F %T"), msg))
    except OSError:
        pass


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        if CONF.exists():
            d = json.loads(CONF.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                cfg.update({k: v for k, v in d.items() if not str(k).startswith("_")})
    except Exception:  # noqa: BLE001
        pass
    return cfg


def ensure_config() -> None:
    if not CONF.exists():
        try:
            CONF.write_text(json.dumps(DEFAULTS, ensure_ascii=False, indent=2), encoding="utf-8")
            paths.secure_runtime_files()
        except OSError:
            pass


def load_state() -> dict:
    try:
        if STATE.exists():
            d = json.loads(STATE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return d
    except Exception:  # noqa: BLE001
        pass
    return {}


def save_state(d: dict) -> None:
    try:
        STATE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        paths.secure_runtime_files()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 游标新鲜度（核心判据，纯本地）
# ---------------------------------------------------------------------------
def _cursor_file() -> pathlib.Path | None:
    """按当前使用的 bot_id 推导桌面端游标文件路径。"""
    try:
        ch = clawbot.load_channel() or {}
        cid = ch.get("channel_id") or ""
        if not cid:
            return None
        # desktop 端把 "@" 写成 "_"：example-current-bot@im.bot → example-current-bot_im.bot
        name = str(cid).replace("@", "_") + ".cursor.json"
        return CLAW_STATE_DIR / name
    except Exception:  # noqa: BLE001
        return None


def cursor_status(now_ts: float | None = None) -> dict:
    """返回 {'found', 'path', 'age_hours', 'mtime_ts', 'bot_id'}。"""
    now_ts = now_ts or datetime.datetime.now().timestamp()
    out: dict = {"found": False, "path": None, "age_hours": None, "mtime_ts": None}
    p = _cursor_file()
    if not p:
        return out
    out["path"] = str(p)
    try:
        st = p.stat()
    except OSError:
        return out
    out.update({"found": True, "mtime_ts": st.st_mtime,
                "age_hours": round((now_ts - st.st_mtime) / 3600.0, 2)})
    return out


# ---------------------------------------------------------------------------
# 通知
# ---------------------------------------------------------------------------
def _notify_stale(now: datetime.datetime, age_hours: float) -> bool:
    cs = cursor_status(now.timestamp())
    body = "\n".join([
        "WorkBuddy 桌面端已经 {:.0f} 小时没有维护 ClawBot 连接了。".format(age_hours),
        "",
        "ClawBot 会话靠「持续轮询」保活。桌面端停止轮询后，",
        "会话可能随时失效，届时所有自动推送都会静默丢失（你收不到任何提示）。",
        "",
        "请做以下任一操作即可恢复：",
        "  1. 打开 / 重启 WorkBuddy 桌面端",
        "  2. 在微信里给 ClawBot 随便发一条消息（例如「1」）",
        "",
        "恢复后本提醒会自动停止并重新计时。",
        "",
        "游标文件：{}".format(cs.get("path") or "(未找到)"),
        "最后活动：{}".format(
            datetime.datetime.fromtimestamp(cs["mtime_ts"]).strftime("%Y-%m-%d %H:%M")
            if cs.get("mtime_ts") else "(无)"),
        "检查时间：{}".format(now.strftime("%Y-%m-%d %H:%M")),
    ])
    r = notify.send("WorkBuddy 积分推送 · ClawBot 连接已停摆，请恢复", body, "warning")
    return bool(r.get("sent"))


def _notify_expired(now: datetime.datetime) -> bool:
    body = "\n".join([
        "检测到 ClawBot 登录会话已失效（errcode=-14）。",
        "",
        "此时主动推送无法送达，而且「发消息」也救不回来 —— 必须重新扫码登录。",
        "",
        "恢复方法（在终端执行）：",
        'cd "{}"'.format(DIR),
        '"{}" clawbot.py login'.format(sys.executable or "python3"),
        "",
        "然后打开生成的 clawbot_login.html 用微信扫码。",
        "或者更简单：在 WorkBuddy 设置 → 远程通道里重新连接「微信助理」。",
        "",
        "检查时间：{}".format(now.strftime("%Y-%m-%d %H:%M")),
    ])
    r = notify.send("WorkBuddy 积分推送 · 通道已失效，需重新扫码", body, "failure")
    return bool(r.get("sent"))


# ---------------------------------------------------------------------------
# 主检查
# ---------------------------------------------------------------------------
def check(*, now: datetime.datetime | None = None, force: bool = False) -> dict:
    """执行一次续期守护检查，返回摘要 dict（不抛异常）。纯本地，无网络请求。"""
    cfg = load_config()
    now = now or datetime.datetime.now()
    now_ts = now.timestamp()
    today = now.strftime("%Y-%m-%d")

    st = load_state()

    if not cfg.get("enabled", True):
        return {"action": "disabled"}

    # 节流（纯本地检查，开销极低；保留是为了日志干净）
    interval = float(cfg.get("check_interval_minutes") or 30)
    last_check = st.get("last_check_ts")
    if not force and isinstance(last_check, (int, float)) and \
            (now_ts - float(last_check)) < interval * 60:
        return {"action": "throttled"}
    st["last_check_ts"] = now_ts

    cs = cursor_status(now_ts)
    out: dict = {"action": "healthy", "cursor_found": cs["found"],
                 "cursor_age_hours": cs["age_hours"],
                 "last_session_expired": last_expired_at()}

    # 游标文件不存在 —— 桌面端从未用过该 bot，或路径规则变了
    if not cs["found"]:
        out["action"] = "no_cursor"
        st["last_cursor_ok_ts"] = None
        save_state(st)
        log("cursor file not found: {}".format(cs.get("path")))
        return out

    stale_hours = float(cfg.get("cursor_stale_hours") or 24)
    stale = cs["age_hours"] is not None and cs["age_hours"] >= stale_hours

    if stale:
        out["action"] = "stale"
        out["stale_hours"] = stale_hours
        if st.get("last_remind_date") != today:
            out["remind_sent"] = _notify_stale(now, cs["age_hours"])
            st["last_remind_date"] = today
            st["remind_count"] = int(st.get("remind_count") or 0) + 1
            out["remind_count"] = st["remind_count"]
            log("stale reminder #{} sent={} age={:.1f}h".format(
                st["remind_count"], out["remind_sent"], cs["age_hours"]))
        else:
            out["remind_skipped"] = "今日已提醒"
    else:
        # 健康：游标在刷新，清空提醒计数，下次失活可立即提醒
        if st.get("last_remind_date"):
            log("recovered, cursor age={:.2f}h".format(cs["age_hours"] or 0))
        st["last_remind_date"] = None
        st["remind_count"] = 0
        st["last_cursor_ok_ts"] = now_ts

    save_state(st)
    return out


def last_expired_at() -> str | None:
    """读 `clawbot.py` 记下的最后一次 -14 失效时间（没有则 None）。"""
    try:
        ts = clawbot.internal_state().get("last_session_expired_ts")
    except Exception:  # noqa: BLE001
        return None
    if isinstance(ts, (int, float)):
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    ensure_config()
    action = sys.argv[1] if len(sys.argv) > 1 else "run"

    if action == "status":
        cfg = load_config()
        st = load_state()
        cs = cursor_status()
        print(json.dumps({
            "config": {k: v for k, v in cfg.items() if not str(k).startswith("_")},
            "enabled": cfg.get("enabled", True),
            "cursor": {
                "path": cs.get("path"),
                "found": cs.get("found"),
                "age_hours": cs.get("age_hours"),
                "last_active": (datetime.datetime.fromtimestamp(cs["mtime_ts"]).strftime("%Y-%m-%d %H:%M:%S")
                                if cs.get("mtime_ts") else "(无)"),
                "stale_threshold_hours": cfg.get("cursor_stale_hours"),
                "is_stale": (cs.get("age_hours") is not None
                             and cs["age_hours"] >= float(cfg.get("cursor_stale_hours") or 24)),
            },
            "last_check": (datetime.datetime.fromtimestamp(st["last_check_ts"]).strftime("%Y-%m-%d %H:%M")
                           if isinstance(st.get("last_check_ts"), (int, float)) else "(从未)"),
            "last_session_expired": last_expired_at(),
            "last_remind_date": st.get("last_remind_date"),
            "remind_count": st.get("remind_count") or 0,
        }, ensure_ascii=False, indent=2))
        sys.exit(0)

    if action == "reset":
        st = load_state()
        st.update({"last_check_ts": datetime.datetime.now().timestamp(),
                   "last_remind_date": None, "remind_count": 0})
        save_state(st)
        print(json.dumps({"reset": True}, ensure_ascii=False))
        sys.exit(0)

    print(json.dumps(check(force=("--force" in sys.argv)), ensure_ascii=False))
