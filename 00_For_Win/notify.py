#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
notify.py — 统一通知模块（微信直推 + 本机桌面通知兜底）—— Windows 版

通道优先级（channel = "auto" 时自动探测）：
  1. clawbot   腾讯 iLink 官方个人微信通道 —— 复用 WorkBuddy 已绑定的 ClawBot 凭据，
               零第三方中转、零额外配置、免实名。**推荐。**
  2. pushplus  PushPlus（第三方代发，需实名 + token）
  3. serverchan Server 酱（第三方代发，需 SendKey）

⚠️ iLink 配额：官方限制**每人 24 小时内最多 10 条主动推送**，超出返回 429。
   本模块用 max_pushes_per_day（默认 8）做本地封顶，留出余量；
   触顶后不再走微信，改为**本机桌面通知**，信息不会丢。

⚠️ iLink 消息格式限制：不支持表格 / 图片 / 代码块，Markdown 渲染不稳定
   → 一律推送纯文本（emoji 可用）。

配置项（notify_config.json）：
  channel                "auto" | "clawbot" | "pushplus" | "serverchan"
  pushplus_token         PushPlus token（走 pushplus 时必填）
  serverchan_key         Server 酱 SendKey（走 serverchan 时必填）
  notify_success         成功（有积分入账）是否推送，默认 true
  notify_failure         失败是否推送，默认 true
  notify_warning         警告（如当前无活动）是否推送，默认 true
  notify_noop            空转（当日已完成）是否推送，默认 false
  native_notification    微信不可用时是否补一条**本机桌面通知**，默认 true
  macos_notification     同上，旧字段名（两套兼容，任一设为 false 即关闭）
  dedupe_window_minutes  同一内容去重窗口（分钟），默认 360；0 关闭
  max_pushes_per_day     微信通道每日推送上限，默认 8（iLink 硬上限 10）
  clawbot_cooldown_minutes          登录会话失效（-14）后的熔断，默认 120
  clawbot_blocked_cooldown_minutes  投递被拒（prepare failed）后的熔断，默认 60
  clawbot_failure_cooldown_minutes  其它失败后的熔断，默认 20

去重与熔断（为什么需要）：
  计划任务每 5 分钟触发一次。若某类失败持续存在，同一内容会反复推送——
  既骚扰用户，又会撞上第三方「相同内容 1 小时限 3 条」，还会白耗 iLink 的
  每日 10 条配额（**请求失败同样计入配额**）。故本地先拦两道：
  · 去重：同一 (级别 + 标题 + 内容) 在 dedupe_window_minutes 内只推一次
  · 熔断：微信通道失败后进入冷却，冷却期内直接走本机通知，不做无谓请求
  · 限额：微信通道每日最多 max_pushes_per_day 条，触顶降级为本机通知
  记录写在 notify_state.json（可安全删除，删后最多重复推一次）。

去重的粒度是「通道」而不是「消息」（2026-09-18 修正）：
  notify_state.json 里有**两张**指纹表，语义完全不同 ——
  · sent[fp]       = 这条消息**真的送到过微信**，拦下所有后续尝试（含本机通知）；
  · local_sent[fp] = 这条消息只弹出过**本机通知**（当时微信通道不可用），
                     它只负责「本机通知不重复弹」，**不拦微信补发**。
  为什么必须分开：修复前只要本机通知弹成功，指纹就写进 sent 表 —— 于是等通道恢复
  （用户给机器人发了消息）之后，那条到账消息在去重窗口内**再也不会重试**，
  等于永久丢失。现在凡「没送到微信」的一律不记指纹，下一次运行立刻重试。

对外接口：
  send(title, content, level="info", force=False) -> dict
    level: "success" | "failure" | "warning" | "info"
    force: True 跳过级别开关与去重（仅测试用）
    返回 {"level","sent","channel","reason"}；**永不抛异常**，绝不阻断主流程。

安全：
  凭据只从本地配置读取（clawbot 凭据复用 WorkBuddy 的 settings.json）：
  不打印、不写日志、不进代码。

★ 与 macOS 版的差异：
  1. 本机通知：`_send_native()` 走 `winenv.native_notify()`（Windows Toast /
     macOS 通知中心），不再直接 exec osascript。**这是唯一的功能性差异。**
  2. 配置项新增 `native_notification`，与旧名 `macos_notification` 双向兼容。

自测（Windows 上把 `python3` 换成 `py -3`）：
  python3 notify.py            # 真实发送一条测试通知（退出码看"微信是否真送到"）
  python3 notify.py status     # 查看配置、配额（发起数 / 送达数分开）、本机通知自测
  python3 notify.py localtest  # 只测本机兜底通知，不碰微信、不消耗配额
"""

from __future__ import annotations

import datetime
import hashlib
import json
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(DIR))   # 同目录（winenv / clawbot）

import paths   # noqa: E402
import winenv  # noqa: E402  跨平台适配层（本机通知 / 路径）

try:
    import clawbot  # 同目录：腾讯 iLink 官方个人微信通道
except Exception:  # noqa: BLE001
    clawbot = None

CONFIG = paths.config_path("notify_config.json")
STATE = paths.state_path("notify_state.json")

DEFAULT_CONFIG = {
    "channel": "auto",
    "pushplus_token": "",
    "serverchan_key": "",
    "notify_success": True,
    "notify_failure": True,
    "notify_warning": True,
    "notify_noop": False,
    "native_notification": True,
    "macos_notification": True,
    "dedupe_window_minutes": 360,
    "max_pushes_per_day": 8,
    "clawbot_cooldown_minutes": 120,
    # 熔断时长（分钟），按失败类型分三档。为什么分档：三种故障的「重试有没有意义」
    # 完全不同 —— 登录失效和投递被拒都要等人工动作，重试纯属浪费配额；网络抖动则应尽快重试。
    "clawbot_blocked_cooldown_minutes": 60,    # 投递被拒（prepare failed）→ 等用户开窗
    "clawbot_failure_cooldown_minutes": 20,    # 网络/未知失败 → 下下轮就重试
}

PUSHPLUS_URL = "https://www.pushplus.plus/send"
SERVERCHAN_URL = "https://sctapi.ftqq.com/{key}.send"

_LEVEL_SWITCH = {
    "success": "notify_success",
    "failure": "notify_failure",
    "warning": "notify_warning",
    "info": "notify_noop",
}


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def load_config() -> dict:
    """读取配置；文件不存在或损坏时回落到默认值（绝不抛异常）。"""
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG.exists():
            user = json.loads(CONFIG.read_text(encoding="utf-8"))
            if isinstance(user, dict):
                cfg.update({k: v for k, v in user.items() if k in DEFAULT_CONFIG})
    except Exception:  # noqa: BLE001
        pass
    return cfg


def ensure_config() -> bool:
    """配置文件不存在时生成一份模板；返回 True 表示本次新建。"""
    if CONFIG.exists():
        return False
    try:
        CONFIG.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        paths.secure_runtime_files()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 本地状态：去重 + 每日配额
# ---------------------------------------------------------------------------
def _fingerprint(title: str, content: str, level: str) -> str:
    raw = "\x00".join((level, title, content)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _load_state() -> dict:
    try:
        if STATE.exists():
            d = json.loads(STATE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                # ★ 必须**强制**成 dict，而不是 setdefault。
                #   setdefault 只在「键不存在」时补齐；若文件被手工改坏或被别的版本
                #   写成 list/字符串，键是存在的、值却不是 dict，后面那几处
                #   `.<items()>` / `.get(fp)` 就会抛 AttributeError —— 而 send()
                #   对外承诺「永不抛异常」，异常会直接穿透到调用方。
                #   这里是唯一的入口，就地收口最省事。
                for _k in ("sent", "local_sent"):
                    if not isinstance(d.get(_k), dict):
                        d[_k] = {}
                return d
    except Exception:  # noqa: BLE001
        pass
    return {"sent": {}, "local_sent": {}}


def _save_state(st: dict) -> None:
    try:
        STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        paths.secure_runtime_files()
    except OSError:
        pass


def _today() -> str:
    return datetime.date.today().isoformat()


def _daily_counts(st: dict) -> tuple[int, int]:
    """返回 (今日向微信**发起**的请求数, 今日真正**送达**微信的条数)。

    为什么必须分开：只报「发起数」会让人误判通道健康 —— 2026-09-18 就是因为看到
    count=2 而断定「两条都送到了」，实际两条全被服务端拒了（假成功）。而失败的
    请求照样扣 iLink 配额，所以：**发起数 = 配额消耗，送达数 = 交付结果**。
    """
    d = st.get("daily") or {}
    if d.get("date") != _today():
        return 0, 0
    requested = d.get("requested", d.get("count", 0))   # 兼容旧字段名 count
    try:
        return int(requested or 0), int(d.get("delivered") or 0)
    except (TypeError, ValueError):
        return 0, 0


def _bump_daily(st: dict, delivered: bool = False) -> tuple[int, int]:
    req, ok = _daily_counts(st)
    req += 1
    if delivered:
        ok += 1
    st["daily"] = {"date": _today(), "requested": req, "delivered": ok}
    return req, ok


def _dedupe_lookup(cfg: dict, fp: str, now: float) -> tuple[bool, bool, int]:
    """去重判定。返回 (拦全部, 仅拦本机, 距上次推送的分钟数)。

    两张表的分工见模块顶部：`sent`（真的送到过微信）拦下所有后续尝试；
    `local_sent`（只弹出过本机通知）只负责别重复弹窗，**必须放行微信通道** ——
    否则通道恢复后就补发不出来了（2026-09-18 修的就是这个）。
    """
    try:
        window = int(cfg.get("dedupe_window_minutes") or 0)
    except (TypeError, ValueError):
        window = 0
    if window <= 0:
        return False, False, 0
    st = _load_state()
    for key in ("sent", "local_sent"):
        last = (st.get(key) or {}).get(fp)
        if isinstance(last, (int, float)):
            elapsed_min = (now - last) / 60.0
            if elapsed_min < window:
                minutes = max(1, int(elapsed_min))
                return key == "sent", key == "local_sent", minutes
    return False, False, 0


def _prune_sent(st: dict, now: float) -> None:
    """清掉 7 天前的指纹记录（两张表都清）。"""
    cutoff = now - 7 * 24 * 3600
    for key in ("sent", "local_sent"):
        st[key] = {k: v for k, v in (st.get(key) or {}).items()
                   if isinstance(v, (int, float)) and v >= cutoff}


# ---------------------------------------------------------------------------
# ClawBot 会话熔断
# ---------------------------------------------------------------------------
# 主动推送被服务端拒时返回 ret=-2 / errmsg="prepare failed"（**不是** errcode=-14，
# -14 是登录失效）。此时连续重试毫无意义 —— 计划任务每 5 分钟触发一次，会白试一整天，
# 而且**失败的请求同样计入 iLink 每日配额**，等于把配额烧在注定失败的请求上。
# 故失败后进入冷却期，期间直接走本机通知，冷却结束再探一次。
#
# ⚠️ 成因未确认：早先认为这是「距用户上次发消息超过会话窗口时长」，但 2026-09-18
# 的数据否掉了这个模型 —— 距上次发消息 38 小时后反而推送成功（当时桌面端在持续轮询，
# 见 renew.py 里「桌面端正常轮询间隔约 18 秒」）。现有证据更支持「与本机客户端是否在
# 持续轮询该 bot 有关」。所以熔断时长只是「别把配额烧光」的工程取舍，不代表对成因的判断。
#
# 2026-09-18 补：熔断从「只对 -14」扩大到「任何失败」。
# 原因就是上面那句 —— 触发频率提高后，未熔断的失败会在 1 小时内烧光配额，
# 等通道真恢复时反而没额度可用了。
def _clawbot_cooldown_remaining_min(st: dict, now: float) -> int:
    until = st.get("clawbot_cooldown_until")
    if isinstance(until, (int, float)) and until > now:
        return max(1, int((until - now) / 60.0))
    return 0


def _set_clawbot_cooldown(st: dict, minutes: int, now: float) -> None:
    if minutes > 0:
        st["clawbot_cooldown_until"] = now + minutes * 60


def _clear_clawbot_cooldown(st: dict) -> None:
    st.pop("clawbot_cooldown_until", None)


def _current_inbound_ts() -> float:
    """用户最后一次给机器人发消息的时间。

    取自 clawbot_state.json 的 `context_token_ts` —— 它是每次捕获到**入站消息**时
    更新的（`clawbot.remember_context_token`）。纯本地读文件，无网络、无配额消耗。
    """
    if clawbot is None:
        return 0.0
    try:
        return float((clawbot.internal_state() or {}).get("context_token_ts") or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def _inbound_since_cooldown(st: dict) -> bool:
    """熔断期内用户是否又给机器人发过消息（= 会话窗口已重新打开）。

    为什么需要它：`_alert_channel_expired` 给用户的承诺是「发一条消息，推送随即恢复」。
    但投递被拒的冷却有 60 分钟 —— 如果只看冷却，用户发完消息还得等最多一小时，
    承诺就变成了谎话。这里用「入站时间有没有前进」作为开窗证据，一前进就立刻放行。
    """
    prev = st.get("cooldown_context_ts")
    if not isinstance(prev, (int, float)):
        return False
    return _current_inbound_ts() > float(prev)


# ---------------------------------------------------------------------------
# 通道可用性
# ---------------------------------------------------------------------------
def _has_credential(cfg: dict) -> bool:
    """是否至少有一条微信通道可用。"""
    return bool(_channel_order(cfg))


def _clawbot_ready() -> bool:
    if clawbot is None:
        return False
    try:
        return clawbot.load_channel() is not None
    except Exception:  # noqa: BLE001
        return False


def _channel_order(cfg: dict) -> list[str]:
    """返回本次可尝试的微信通道顺序。"""
    wanted = str(cfg.get("channel") or "auto").lower()
    if wanted != "auto":
        avail = {
            "clawbot": _clawbot_ready(),
            "pushplus": bool(cfg.get("pushplus_token")),
            "serverchan": bool(cfg.get("serverchan_key")),
        }
        return [wanted] if avail.get(wanted) else []
    order = []
    if _clawbot_ready():
        order.append("clawbot")
    if cfg.get("pushplus_token"):
        order.append("pushplus")
    if cfg.get("serverchan_key"):
        order.append("serverchan")
    return order


# ---------------------------------------------------------------------------
# 各通道发送
# ---------------------------------------------------------------------------
def _post_json(url: str, payload: dict, timeout: int = 15) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _send_pushplus(cfg: dict, title: str, content: str) -> tuple[bool, str]:
    r = _post_json(PUSHPLUS_URL, {
        "token": cfg["pushplus_token"],
        "title": title,
        "content": content,
        "template": "txt",
    })
    if r.get("code") == 200:
        return True, "pushplus 已送达"
    hint = ""
    if r.get("code") == 905:
        hint = "（账号未实名，请在 verify.pushplus.plus 完成实名）"
    elif r.get("code") == 903:
        hint = "（token 无效，请重新复制）"
    elif r.get("code") == 900:
        hint = "（请求次数超限，当日已受限）"
    return False, "pushplus 返回 code={} msg={}{}".format(
        r.get("code"), r.get("msg"), hint)


def _send_serverchan(cfg: dict, title: str, content: str) -> tuple[bool, str]:
    r = _post_json(SERVERCHAN_URL.format(key=cfg["serverchan_key"]),
                   {"title": title, "desp": content})
    if r.get("code") == 0:
        return True, "serverchan 已送达"
    return False, "serverchan 返回 code={} msg={}".format(r.get("code"), r.get("msg"))


def _send_via(channel: str, cfg: dict, title: str, content: str) -> tuple[bool, str]:
    """经指定通道发送。返回 (成功, 说明)。"""
    if channel == "clawbot":
        if clawbot is None:
            return False, "clawbot 模块未加载"
        try:
            return clawbot.send_text(title + "\n" + content)
        except Exception as e:  # noqa: BLE001
            return False, "clawbot 异常：" + str(e)[:120]
    if channel == "pushplus":
        try:
            return _send_pushplus(cfg, title, content)
        except Exception as e:  # noqa: BLE001
            return False, "pushplus 异常：" + str(e)[:120]
    if channel == "serverchan":
        try:
            return _send_serverchan(cfg, title, content)
        except Exception as e:  # noqa: BLE001
            return False, "serverchan 异常：" + str(e)[:120]
    return False, "未知通道 " + channel


def _native_enabled(cfg: dict) -> bool:
    """是否允许降级到本机桌面通知。

    `native_notification` 为规范字段名，`macos_notification` 为旧字段名（两套兼容）。
    任一显式设为 false 即关闭 —— 这样老配置文件里的 `macos_notification: false`
    在 Windows 上依然生效，不会因为换了平台就悄悄开始弹通知。
    """
    return bool(cfg.get("native_notification", True)) and bool(cfg.get("macos_notification", True))


def _send_native(title: str, content: str) -> bool:
    """本机桌面通知兜底。

    ★ Windows 差异：平台差异（Windows Toast / macOS 通知中心 / Linux notify-send）
      全部封装在 `winenv.native_notify()`；投递失败时内容仍会落进
      `logs/notify_fallback.log`，保证信息不丢。
    """
    return winenv.native_notify(title, content, log_dir=paths.LOG_DIR)


def _reg_get(path: str, name: str):
    """读一个注册表值（仅 Windows）。读不到返回 None。

    用途只有一个：判断「系统级 Toast 开关」是不是被关掉了 —— 那是最容易被忽略、
    又会让所有本机兜底通知静默消失的一种配置。
    """
    if not winenv.IS_WIN:
        return None
    try:
        # ★ 取 bytes 自己解，不能用 `text=True`：它按 locale 解码，而 locale 会漂移
        #   （开了 PYTHONUTF8/LANG=C.UTF-8 的进程里是 utf-8，Windows 命令给的却是 GBK）。
        #   实测本机 `reg query` 查不到键时那句中文报错是 GBK，会在 reader 线程里抛
        #   UnicodeDecodeError —— 用户看到一段 traceback，值被静默当成「读不到」。
        #   详见 winenv.decode_console 的说明。
        r = subprocess.run(["reg", "query", path, "/v", name],
                           capture_output=True, timeout=8)
        if r.returncode != 0:
            return None
        for line in winenv.decode_console(r.stdout or b"").splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] == name:
                # ★ DWORD 在 `reg query` 里是 `0x1` / `0x0` 形式，必须归一成 1 / 0 再返回。
                #   2026-09-21 实测踩到：调用方拿返回值去查 {"1": "开", "0": "关"}，
                #   于是 `0x1`（开）显示成「读不到」，而 `0x0`（**已被关掉**）
                #   同样落进「读不到（按默认开处理）」—— 这条检查本来就是为了抓住
                #   「系统通知被关、本机兜底全部静默消失」这个场景，判错就等于它不存在。
                raw = parts[-1].strip().lower()
                try:
                    return str(int(raw, 0))
                except ValueError:
                    return raw
    except Exception:  # noqa: BLE001
        return None
    return None


def local_check() -> dict:
    """自测「本机通知到底弹不弹得出来」。供 `notify.py status` 使用。

    为什么值得单列：本机通知是**最后一道**兜底。它如果不弹，用户就彻底没感知了 ——
    而它失效的方式全都无声无息（配置关掉、系统 Toast 关掉、专注助手压掉）。
    """
    cfg = load_config()
    backend = ("windows-toast" if winenv.IS_WIN else
               "macos-notification-center" if winenv.IS_MAC else "notify-send")
    out: dict = {"enabled": _native_enabled(cfg), "backend": backend}

    toast = None
    if winenv.IS_WIN:
        toast = _reg_get(r"HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\PushNotifications",
                         "ToastEnabled")
        out["toast_enabled_registry"] = {"1": "开", "0": "关（本机通知会被系统丢弃）"}.get(
            str(toast), "读不到（按默认「开」处理）")
        # ★ 2026-09-21：本机通道默认已经不是 Toast 了 —— 是**必须点掉的弹窗**
        #   （Toast 实测"过一会儿就自己收走"）。这里如实报出实际通道，
        #   免得 status 显示的 backend 与真正弹出的东西对不上，把人带偏。
        style = winenv._alert_style()
        out["alert_style"] = style
        out["backend"] = "windows-dialog（必须点掉）" if style == "dialog" else "windows-toast"
        out["focus"] = "unknown"
        out["focus_note"] = ("Windows 的专注助手状态没有稳定的公开读取方式，这里不假装能判断；"
                             "若怀疑被吞，请看下面的兜底日志。")
    else:
        out["focus"] = "unknown"
        out["focus_note"] = "非 Windows 平台的本机通知自测（Windows 版只负责 Windows 语义）"

    # 兜底日志是「本机通知真的没弹出来」最直接的证据 —— winenv.native_notify
    # 投递失败时会把内容落到这里。最近还在写入 = 本机通知当前是坏的。
    fb = paths.LOG_DIR / "notify_fallback.log"
    try:
        age_h = (time.time() - fb.stat().st_mtime) / 3600.0 if fb.exists() else None
    except OSError:
        age_h = None
    out["fallback_log"] = ("（无）" if age_h is None else
                           "{:.1f} 小时前还在写入 —— 说明当时本机通知没弹出来".format(age_h))

    ok = bool(out["enabled"]) and str(toast or "1") != "0"
    out["verdict"] = "会正常弹出" if ok else "可能看不到（见上面各项）"
    return out


def _alert_channel_expired(st: dict, cfg: dict, now: float, kind: str = "session") -> bool:
    """微信推不出去时，额外弹一条**明确的**本机告警（每天最多 1 次）。

    为什么需要单独告警：推不出去时所有微信通知只会「降级成本机通知」，
    而通知内容是"签到成功 +100 积分"之类，用户容易误以为通道正常 —— 实际只是
    本地弹窗，手机微信什么都收不到。这条告警把真实原因和恢复动作说清楚。

    kind 区分两种完全不同的故障（恢复动作也不同）：
      · session —— 登录会话失效（-14），必须重新扫码；
      · blocked —— 投递被拒（ret=-2 prepare failed），发条消息就能开窗，不需要扫码。
    """
    if not _native_enabled(cfg):
        return False
    # 两类故障各留一次额度：否则当日先报过 session，blocked 就被吞掉了
    key = "channel_alert_date" if kind == "session" else "channel_alert_blocked_date"
    if st.get(key) == _today():
        return False
    if kind == "blocked":
        ok = _send_native(
            "⚠️ 微信拒收积分通知（通道未失效，但发不出去）",
            "服务端拒绝投递主动消息（prepare failed）。请在微信里给机器人"
            "随便发一条消息（例如「1」）重新开窗，推送随即自动恢复 —— 不需要重新扫码。",
        )
    else:
        ok = _send_native(
            "⚠️ 微信推送通道已失效，积分通知发不出去",
            "ClawBot 登录会话过期。请在 WorkBuddy 设置 → 远程通道里"
            "重新连接「微信助理」，扫码后即恢复。",
        )
    # ★ 只有**真的弹出来**才记「今天已告警」。
    #   旧写法是 `st[key] = _today()` 写在发送之前 —— 本机 Toast 一旦发不出去
    #   （系统通知被关掉、没有交互式桌面会话、pythonw 无窗口站），这条告警就被
    #   永久吞掉一整天：用户既收不到微信、也收不到本机通知，日志里还显示「已告警」。
    #   这正是本项目最忌讳的静默失效，而且和两张去重表同一条原则：
    #   **只有真的送达，才配记账。**
    if ok:
        st[key] = _today()
    return ok


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------
def send(title: str, content: str, level: str = "info", force: bool = False) -> dict:
    """发送通知。level: success | failure | warning | info。永不抛异常。

    force=True 跳过级别开关与去重（测试用），但**不跳过**每日配额。
    返回 {"level","sent","channel","reason",...}：
      sent    —— 是否送达（微信或本机任一条都算，调用方通常只关心"有人看到了"）
      wechat  —— **是否真的送到了微信**。判断"通道是否打通"只能看这个字段
      channel —— 实际送达的通道（clawbot / pushplus / serverchan / native）
    """
    cfg = load_config()
    now = time.time()
    out = {"level": level, "sent": False, "wechat": False, "channel": "", "reason": ""}

    switch = _LEVEL_SWITCH.get(level, "notify_noop")
    if not force and not cfg.get(switch, True):
        out["reason"] = "该级别未开启推送（{}）".format(switch)
        return out

    fp = _fingerprint(title, content, level)
    block_all, block_local, blocked_min = False, False, 0
    if not force:
        block_all, block_local, blocked_min = _dedupe_lookup(cfg, fp, now)
        if block_all:
            out["reason"] = "去重拦截：相同内容 {} 分钟前已经微信送达（窗口 {} 分钟）".format(
                blocked_min, cfg.get("dedupe_window_minutes"))
            return out
        # block_local 不在这里 return —— 本机通知发过了，但微信还没送到，必须继续试。
        # 这正是 2026-09-18 修掉的那个缺陷：以前这里一并拦掉，导致"补发"永远不发生。

    st = _load_state()

    def _local_fallback() -> bool:
        """本机通知兜底。block_local 时跳过（同一内容不重复弹窗）。"""
        if not _native_enabled(cfg):
            return False
        if block_local:
            out["reason"] += "；本机通知此前已弹过，不重复弹（微信通道恢复后会自动补发）"
            return False
        if _send_native(title, content):
            out["channel"] = out["channel"] or "native"
            out["reason"] += "；已降级为本机通知"
            return True
        out["reason"] += "；本机通知也失败了"
        return False

    order = _channel_order(cfg)

    # ClawBot 冷却期内先跳过（会话窗口过期的重试无意义），必要时再降级本机通知
    cooldown_min = 0 if force else _clawbot_cooldown_remaining_min(st, now)
    if cooldown_min and _inbound_since_cooldown(st):
        # 用户在冷却期内又发过消息 → 窗口已重开，冷却立刻失效
        _clear_clawbot_cooldown(st)
        st.pop("cooldown_context_ts", None)
        st.pop("last_send_error", None)
        cooldown_min = 0
    if cooldown_min and "clawbot" in order:
        order = [c for c in order if c != "clawbot"]
        out["reason"] = "ClawBot 会话冷却中（还剩约 {} 分钟）".format(cooldown_min)

    if not order:
        if not out["reason"]:
            out["reason"] = "无可用微信通道（请在 WorkBuddy 绑定 ClawBot，或配置 pushplus/serverchan 凭据）"
        if _local_fallback():
            # ★ 本机确实弹出来了 → 对调用方而言就是「有人看到了」，必须置 sent。
            #   旧写法只写了 local_sent 指纹却不置 sent，于是 catchup.log 里显示
            #   `sent=False ... reason=已降级为本机通知` —— 日志自相矛盾，
            #   排障时按 README 第 6 节去看 `sent=…` 会得出「根本没送到」的错误结论。
            out["sent"] = True
            st.setdefault("local_sent", {})[fp] = now
        _prune_sent(st, now)
        _save_state(st)
        return out

    # 每日配额封顶（保护 iLink 24h/10 条硬限制）。配额按**发起数**计（失败也扣）。
    try:
        cap = int(cfg.get("max_pushes_per_day") or 0)
    except (TypeError, ValueError):
        cap = 0
    used, _ok_today = _daily_counts(st)
    if cap > 0 and used >= cap:
        out["reason"] = "已达今日微信请求上限 {}/{}，降级为本机通知".format(used, cap)
        if _local_fallback():
            out["sent"] = True          # 理由同上：本机送到了就该算送达
            st.setdefault("local_sent", {})[fp] = now
        _prune_sent(st, now)
        _save_state(st)
        return out

    reasons = []
    for ch in order:
        ok, reason = _send_via(ch, cfg, title, content)

        # ClawBot 熔断：成功则解除冷却；**任何失败**都进入冷却，避免无谓重试（见上方长注释）
        if ch == "clawbot" and not force:
            if ok:
                _clear_clawbot_cooldown(st)
                st.pop("last_send_error", None)
            else:
                r_str = str(reason)
                # 记下最后一次失败原因：否则「微信没收到」时只能靠猜（2026-09-18 的教训）
                st["last_send_error"] = r_str[:300]
                st["last_send_error_ts"] = now
                if clawbot is not None and clawbot.SESSION_EXPIRED_MARK in r_str:
                    kind = "session"          # 登录失效：必须重新扫码
                elif clawbot is not None and clawbot.DELIVER_BLOCKED_MARK in r_str:
                    kind = "blocked"          # 投递被拒：用户发条消息即可恢复
                elif (clawbot is not None
                      and getattr(clawbot, "TOKEN_MISSING_MARK",
                                  "缺 context_token") in r_str):
                    # 缺 context_token：服务端已受理并计入配额，但消息永远到不了微信；
                    # 只能等用户给 bot 发消息刷新令牌。把它当「网络抖动」按 20 分钟短冷却
                    # 反复重试毫无意义 —— 每次都在白烧 iLink 每日配额（默认 8 条），
                    # 结果是把配额耗尽、连本该能送达的通知也只能降级成本机弹窗。
                    # 归进「要等人工动作」那一档（与 blocked 同为 60 分钟）。
                    kind = "token_missing"
                else:
                    kind = "other"            # 网络/未知：短冷却，允许较早重试
                cd_key = {
                    "session": "clawbot_cooldown_minutes",
                    "blocked": "clawbot_blocked_cooldown_minutes",
                    "token_missing": "clawbot_blocked_cooldown_minutes",
                    "other": "clawbot_failure_cooldown_minutes",
                }[kind]
                try:
                    cd = int(cfg.get(cd_key) or 0)
                except (TypeError, ValueError):
                    cd = 0
                _set_clawbot_cooldown(st, cd, now)
                if kind in ("blocked", "token_missing"):
                    # 记下失败时刻的用户活动基线：之后只要用户再发过消息，
                    # 冷却就该立刻解除（这两种故障的恢复动作都是「发一条消息」）
                    st["cooldown_context_ts"] = _current_inbound_ts()
                if kind in ("session", "blocked"):
                    # 额外弹一条明确告警，避免用户把"降级后的本机通知"误当成通道正常。
                    # 刻意不含 token_missing：缺令牌的失败文案本身已经说清了原因与动作，
                    # 而复用 blocked 的告警会把原因错写成「prepare failed」。
                    out["channel_expired_alert"] = _alert_channel_expired(st, cfg, now, kind)
                out["cooldown_min"] = cd

        # 只要请求真的发出去过，就计入当日配额（失败也占用 iLink 配额）
        _bump_daily(st, delivered=ok)
        _save_state(st)
        if ok:
            out["sent"] = True
            out["wechat"] = True
            out["channel"] = ch
            out["reason"] = reason
            break
        reasons.append("{}: {}".format(ch, reason))
    else:
        out["reason"] = "；".join(reasons)

    wechat_delivered = out["wechat"]
    local_delivered = False
    if not wechat_delivered:
        local_delivered = _local_fallback()
        out["sent"] = local_delivered

    # 指纹按**通道**分别记（2026-09-18 修正，详见模块顶部说明）：
    #   送到微信 → sent（拦全部，并清掉本机记录，因为它已被更可靠的方式送达）
    #   只到本机 → local_sent（只拦本机重复弹窗，微信下次仍会重试）
    #   都没送到 → 什么都不记，留给下一次运行补发
    if wechat_delivered:
        st.setdefault("sent", {})[fp] = now
        (st.get("local_sent") or {}).pop(fp, None)
    elif local_delivered:
        st.setdefault("local_sent", {})[fp] = now
    _prune_sent(st, now)
    _save_state(st)
    return out


if __name__ == "__main__":
    import sys

    action = sys.argv[1] if len(sys.argv) > 1 else "test"
    cfg = load_config()

    if action == "status":
        st = _load_state()
        ch_info = None
        if clawbot is not None:
            try:
                c = clawbot.load_channel()
                ch_info = {
                    "ready": c is not None,
                    "base_url": c["base_url"] if c else None,
                    "bot_token": clawbot.mask(c["bot_token"]) if c else None,
                }
            except Exception as e:  # noqa: BLE001
                ch_info = {"ready": False, "error": str(e)[:80]}
        print(json.dumps({
            "config_file": str(CONFIG),
            "state_file": str(STATE),
            "channel_setting": cfg.get("channel"),
            "channel_order": _channel_order(cfg),
            "clawbot": ch_info,
            "pushplus_configured": bool(cfg.get("pushplus_token")),
            "serverchan_configured": bool(cfg.get("serverchan_key")),
            "notify_success": cfg.get("notify_success"),
            "notify_failure": cfg.get("notify_failure"),
            "notify_warning": cfg.get("notify_warning"),
            "notify_noop": cfg.get("notify_noop"),
            "native_notification": _native_enabled(cfg),
            "macos_notification": cfg.get("macos_notification"),
            "native_notify_backend": "windows-toast" if winenv.IS_WIN else (
                "macos-notification-center" if winenv.IS_MAC else "notify-send"),
            "local_notify_check": local_check(),
            "dedupe_window_minutes": cfg.get("dedupe_window_minutes"),
            "max_pushes_per_day": cfg.get("max_pushes_per_day"),
            # 必须分开看：「发起数」才是配额消耗，「送达数」才是用户真收到了几条。
            # 2026-09-18 就是因为只看到旧字段 count=2 而误判"两条都送到了"。
            "pushes_today": {
                "requested_微信请求数": _daily_counts(st)[0],
                "delivered_微信送达数": _daily_counts(st)[1],
                "note": "失败请求同样消耗 iLink 配额；requested > delivered 说明通道在失败",
            },
            "dedupe_records": {
                "sent_已送微信": len(st.get("sent") or {}),
                "local_sent_仅本机": len(st.get("local_sent") or {}),
            },
            "clawbot_cooldown_minutes": cfg.get("clawbot_cooldown_minutes"),
            "clawbot_blocked_cooldown_minutes": cfg.get("clawbot_blocked_cooldown_minutes"),
            "clawbot_failure_cooldown_minutes": cfg.get("clawbot_failure_cooldown_minutes"),
            "clawbot_cooldown_remaining_min": _clawbot_cooldown_remaining_min(st, time.time()),
            "last_send_error": st.get("last_send_error") or "(无)",
        }, ensure_ascii=False, indent=2))
        sys.exit(0)

    if action == "channels":
        print(json.dumps(_channel_order(cfg), ensure_ascii=False))
        sys.exit(0)

    if action == "localtest":
        # 只测本机通知这一级，不碰微信、不消耗任何配额
        r = local_check()
        r["sent"] = _send_native("WorkBuddy 积分助手 · 本机通知自测",
                                 "看到这条就说明本机兜底通知是通的。")
        print(json.dumps(r, ensure_ascii=False, indent=2))
        sys.exit(0 if r["sent"] else 1)

    res = send(
        "WorkBuddy 积分助手 · 测试通知",
        "这是一条测试消息。收到即表示微信推送通道已打通。",
        "success", force=True,
    )
    print(json.dumps(res, ensure_ascii=False))
    # 退出码看的是「微信有没有真送到」——降级成本机通知不算打通（2026-09-18 的教训：
    # 以前 sent=True 就算成功，于是"本机弹了一下"被当成"通道正常"）。
    sys.exit(0 if res.get("wechat") else 1)
