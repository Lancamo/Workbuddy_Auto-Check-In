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
    # 熔断时长（分钟），按失败类型分四档。为什么分档：四种故障的「重试有没有意义」
    # 完全不同 —— 登录失效和投递被拒都要等人工动作，重试纯属浪费配额；网络抖动则应尽快重试。
    "clawbot_blocked_cooldown_minutes": 60,    # 投递被拒（prepare failed）→ 等用户开窗
    "clawbot_failure_cooldown_minutes": 20,    # 网络/未知失败 → 下下轮就重试
    # 频率限制（ret=-2，非 prepare failed）：服务端自己说了「等 60–120 秒重试」。
    # 2026-09-28 新增独立档位 —— 它原先落进 failure 档被冻 20 分钟，
    # 等于把一次限流放大成 20 分钟的整条通道停摆（默认配额才 8 条/天，
    # 20 分钟里本该送达的通知会被挤到本机弹窗，甚至因超配额彻底发不出去）。
    "clawbot_ratelimit_cooldown_minutes": 3,
    # 冷却期内「再捕获一次 context_token」的窗口与最小间隔（见 _recover_while_cooling）。
    # 窗口要够用户看到本机告警、掏出手机、打一个字（25 秒起步）；
    # 最小间隔明显大于熔断检查频率（每 5 分钟一次），避免把长连接一直挂在后台。
    "clawbot_recover_window_seconds": 25,
    "clawbot_recover_min_interval_minutes": 15,
    # 「投递被拒」状态下两次**补发**之间的最小间隔（分钟）。2026-09-29 新增，与 macOS 侧同步。
    # 原因见 _blocked_state_skip_send：失败请求同样烧 iLink 配额，补发若无节制地每 5 分钟
    # 重试一次，几小时就能把当日额度烧光，等用户真去开窗时反而没配额可用。
    # 240 分钟按配额反推：每天最多 6 次补发 + 2 次正常到账通知 = 8，
    # 恰好等于 max_pushes_per_day，不会出现「补发把正常通知的额度吃光」。
    # 注意：刚捕获到用户新的入站消息时不受此间隔限制（立即重发）。
    "clawbot_blocked_retry_minutes": 240,
}

# 补发前「先收一次信箱」的短轮询上限（秒）。有消息时立即返回，这里的值只决定
# 「没有新消息时最多等多久」。取 5 秒是为了不拖慢每 5 分钟一次的 catchup。
_INBOUND_PEEK_TIMEOUT = 5

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
# ⚠️ 成因（2026-09-29 已定位）：真正的闸门是服务端那个「用户最近是否给机器人发过消息」
# 的**会话窗口** —— 窗口关了就 prepare failed，与令牌新旧无关（同一枚令牌跨 4 天两次发送
# 成功），也与桌面端是否在轮询无关（09-27 起连续两天推送全失败，而桌面端全程按约 18 秒
# 轮询，见 renew.py）。早先两个模型都已作废：①「距上次发消息超过 N 小时」（09-18 数据否掉：
# 38 小时后反而成功）；②「取决于本机客户端是否持续轮询」（09-29 否掉）。
# 熔断时长仍只是「别把配额烧光」的工程取舍 —— 窗口何时重开不由本地决定，但可靠检测入站
# 消息（见 _inbound_since_cooldown）提前解除冷却。
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


def _cfg_int(cfg: dict, key: str, default: int) -> int:
    """从配置里取一个整数，坏了就回落默认值（配置是用户手改的，不能信）。"""
    try:
        return int(cfg.get(key) if cfg.get(key) is not None else default)
    except (TypeError, ValueError):
        return default


def _clawbot_recoverable(st: dict) -> bool:
    """当前冷却是否属于「用户发一条消息就能恢复」那一档。

    判据就是 `cooldown_context_ts` 是否存在 —— 只有 blocked / token_missing
    两类失败会写它（见 send() 里的 cd_key 分支）；session 档不写，因为
    会话过期只能重新扫码，捕获再多消息也没用。
    """
    return isinstance(st.get("cooldown_context_ts"), (int, float))


def _recover_while_cooling(st: dict, cfg: dict, now: float) -> tuple[bool, str]:
    """冷却期内开一个**有界**捕获窗口：抓住用户新发的消息就解除冷却。

    ★ 为什么必须有这一步（2026-09-28 修，实测发现）：
      旧实现在冷却期内把 clawbot 整个摘出 `order` → **没有任何人再轮询** →
      `context_token_ts` 永不前进 → `_inbound_since_cooldown()` 恒为 False →
      用户即使照着本机告警的提示给机器人发了消息，也解锁不了，
      只能硬等满 `clawbot_blocked_cooldown_minutes`（默认 60 分钟）。
      而告警文案对用户的承诺恰恰是「发一条消息，推送随即恢复」——
      这个函数就是让那句承诺成真的唯一手段。链路里没有别的轮询者
      （桌面端只在内存里持有自己的 context_token，且它自己轮询的那份游标
      与我们无关，见 README「桌面端不持久化 context_token」那节）。

    ★ 为什么只「捕获」不「重发」：
      冷却的本意是别拿注定失败的请求去烧 iLink 每日配额（默认 8 条）。
      捕获走 getupdates，**不消耗推送配额**，所以照做无妨；
      捕获成功后冷却即被清除，紧接着的正常发送路径自然就把这条通知发出去了。

    ★ 为什么要有最小间隔：
      捕获本身不抢桌面端的消息（各方游标独立，2026-09-17 实测同一条消息两边各收一份），
      但它毕竟是一条长连接，长期高频地挂在后台没有意义 ——
      所以最小间隔（默认 15 分钟）要明显大于熔断检查的频率（每 5 分钟一次）。

    返回 (冷却是否已解除, 说明)。
    """
    if clawbot is None:
        return False, "clawbot 模块未加载"

    gap_min = _cfg_int(cfg, "clawbot_recover_min_interval_minutes", 15)
    last = st.get("last_recover_ts")
    if gap_min > 0 and isinstance(last, (int, float)) and now - last < gap_min * 60:
        return False, "距上次恢复尝试不足 {} 分钟，先不抢轮询".format(gap_min)

    st["last_recover_ts"] = now
    _save_state(st)
    window = max(_cfg_int(cfg, "clawbot_recover_window_seconds", 25), 10)
    try:
        ok, why = clawbot.capture_context_token(wait_seconds=window)
    except Exception as e:  # noqa: BLE001
        return False, "捕获异常：" + str(e)[:120]

    # capture 内部会读写盘上的 state（新 token / 游标）。无条件刷新内存快照，
    # 否则稍后 _save_state(st) 会用进入本函数前的旧值覆盖掉盘上最新状态。
    fresh = _load_state()
    st.clear()
    st.update(fresh)

    if ok:
        _clear_clawbot_cooldown(st)
        st.pop("cooldown_context_ts", None)
        st.pop("last_send_error", None)
        _save_state(st)
        return True, why
    return False, why


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


def _recapture_and_resend(cfg: dict, title: str, content: str,
                          st: dict, now: float) -> tuple[bool, str]:
    """「投递被拒 / 缺 context_token」的自动自愈（2026-09-25 新增，与 macOS 版同构）。

    背景：主动推送依赖本地捕获的 context_token，而捕获此前是**纯手动**操作
    （py -3 clawbot.py wait）。主动推送的闸门是**服务端会话窗口**：窗口一关，
    推送就进入 `prepare failed` 死状态且永不自愈 —— 2026-09-24/25 的漏推即此因：
    用户其实一直在微信里发消息（桌面端正常回复「知道了」），但脚本没在开窗的
    那一刻捕获到 context_token，于是在窗口关闭期间一条也发不出去。

    ⚠️ 2026-09-29 更正：当时把原因写成「旧 token 失效」——那是错的。
    令牌不会过期（同枚跨 4 天两次发送成功）。关掉通道的是**会话窗口**，
    窗口何时重开由用户是否给机器人发消息决定，与令牌新旧无关。
    所以本函数的实质是「开窗 + 捕获」，**不是**「换一枚令牌」。

    自愈流程：
      1. 弹本机告警，引导用户「给机器人发条消息」（与告警承诺的恢复动作一致）；
      2. 开 90 秒捕获窗口（长轮询），窗口内用户发消息即被捕获；
      3. 抓到新 token 立即重发一次。

    代价与边界：
      · 捕获走 getupdates，**不消耗推送配额**。它**不会**抢走桌面端的消息：
        2026-09-17 实测同一条消息两边各收一次（各自独立游标），
        旧注释里「会与桌面端抢消息」的说法已作废（见项目记忆）。
      · 最坏阻塞约 90 秒；计划任务每 5 分钟触发一次，可接受。
      · 重发这次请求单独计入每日配额（请求即消耗，与 iLink 规则一致）；
        原始失败那次由主循环照常计入。
    """
    alerted = _alert_channel_expired(st, cfg, now, "blocked")
    _save_state(st)
    cap_ok, cap_reason = clawbot.capture_context_token(wait_seconds=90)
    # capture 内部会读写盘上的 state（新 token / 游标）。无条件刷新内存快照，
    # 避免稍后 _save_state(st) 用进入本函数前的旧值覆盖掉盘上最新状态。
    fresh = _load_state()
    st.clear()
    st.update(fresh)
    ok, reason = _send_via("clawbot", cfg, title, content)
    _bump_daily(st, delivered=ok)
    _save_state(st)
    tag = "（已弹本机引导告警）" if alerted else ""
    if not cap_ok:
        return False, "未捕获到新 token" + tag + "：" + cap_reason
    if ok:
        return True, reason + "（自动重捕获 token 后重发成功）"
    return False, "已捕获新 token 但重发仍失败：" + reason


def _enqueue_pending(st: dict, level: str, title: str, content: str, now: float) -> None:
    """把「本应送达微信、却只降级了本机（或完全没送出）」的关键通知入队。

    为什么需要：签到到账 / 旅行到账 / 失败告警这类 success/failure 通知是**一次性**的
    —— 当天任务完成后 catchup 不会再触发。若发送时刻恰好会话窗口关着、当场自愈又没接住
    （用户不在场），这条通知就会永久漏掉微信。入队后由 catchup 每次触发时 flush_pending
    补发，窗口一重开就能补上，兑现「每天的消息一定微信通知到」。
    """
    fp = _fingerprint(title, content, level)
    pending = st.setdefault("pending", [])
    if not isinstance(pending, list):
        pending = st["pending"] = []
    for item in pending:
        if isinstance(item, dict) and item.get("fp") == fp:
            return
    pending.append({"fp": fp, "level": level, "title": title,
                    "content": content, "ts": now})
    # 只保留最近 48 小时、最多 12 条，防止窗口长期关闭时无限堆积
    cutoff = now - 48 * 3600
    st["pending"] = [it for it in pending
                     if isinstance(it, dict) and it.get("ts", 0) >= cutoff][-12:]


def _blocked_state_skip_send(cfg: dict, st: dict, now: float, refreshed: bool) -> int:
    """「投递被拒」状态下是否应当**跳过**本次补发发送；返回还要等几分钟（0 = 照发）。

    ★ 2026-09-29 新增，与 macOS 侧同步（macOS 先踩到同一个坑，Windows 结构同构，故一并修）。

    真实故障：`prepare failed` = 服务端拒绝为这条**主动消息**建立会话（会话窗口已关），
    而失败请求**同样计入 iLink 配额**。macOS 侧当天 08:05 冷却一结束，补发就每 5 分钟
    重试一次，3 次把当日 8 条配额烧光 —— 等用户 09:31 真按提示发了消息，反而没配额可用了。

    规则（顺序即优先级）：
      1. 本次刚捕获到入站消息（= 窗口已重开）→ 状态变了，立刻照发（真正能成功的路径）；
      2. 上一次不是失败收场（last_send_error 已被清）→ 照发；
      3. 上一次是网络 / 频率限制 / 未知类失败 → 照发（各自那档短冷却已经管住了，
         这里的间隔只针对「等人工动作」那一类）；
      4. 上一次是「投递被拒 / 缺 context_token」→ 距上次失败不足
         `clawbot_blocked_retry_minutes` 就跳过。

    为什么用「间隔」而不是「必须捕获到入站消息才发」：实测存在**自发恢复** ——
    令牌本身是耐用的会话句柄（macOS 侧同一枚 09-25 09:45 的令牌，在 09-27 与 09-29
    两次都发送成功），能否发出去只取决于服务端的会话窗口何时重开，而那一刻本地不一定
    捕捉得到。硬性要求"捕获到入站"会把自发恢复这条路堵死；
    按间隔限流则既省配额、又保留撞上窗口重开的机会。
    """
    if refreshed:
        return 0
    if not st.get("last_send_error"):
        return 0
    if clawbot is None:
        return 0
    txt = str(st.get("last_send_error") or "")
    # 与 send() 里的分档保持一致：blocked 与 token_missing 同属「等服务端窗口重开」，
    # 重试同样白烧配额；ratelimit / other 不在此列（它们靠各自那档短冷却）。
    tok_missing = getattr(clawbot, "TOKEN_MISSING_MARK", "缺 context_token")
    if clawbot.DELIVER_BLOCKED_MARK not in txt and tok_missing not in txt:
        return 0
    gap_min = _cfg_int(cfg, "clawbot_blocked_retry_minutes", 240)
    if gap_min <= 0:
        return 0
    last = st.get("last_send_error_ts")
    if not isinstance(last, (int, float)):
        return 0
    left = gap_min - (now - last) / 60.0
    return int(left + 0.999) if left > 0 else 0


def flush_pending(day_done: bool = False) -> dict:
    """补发 pending 队列里未送达微信的关键通知。由 catchup 每次运行时调用。

    返回 {"flushed": 已补发, "dropped": 已作废, "remaining": 剩余}；永不抛异常。
    补发走 send(..., allow_recapture=False, cooldown_on_fail=False)：不在批量补发里
    触发 90 秒自愈窗口，也不设熔断冷却 —— 自愈与冷却只该由正常通知路径触发一次，
    否则 flush 里多条失败会把单次运行拖到几分钟、或冻结通道拖累后续自愈。

    day_done=True（当天任务已全部完成）时，队列里的 failure 一律**作废**：它说的是
    「当时没办成」，而当天既然已经办成了，这条告警就永远不该再发出去。
    2026-09-26 的教训 —— 10:23 的网络故障告警排进队列，当天 19:02 其实已领取成功，
    21:35 才被补发到微信，用户先在 19:02 看到「到账」、又在 21:35 看到「失败」，彻底错乱。
    success（到账）不受影响：领到多少分是既成事实，晚到也比漏掉好。
    （warning / info 根本不会入队，见 send() 末尾的入队条件，故这里无需处理。）

    ★ 2026-09-29 新增「补发前先收一次信箱」（与 macOS 侧同步）：入站消息是本地判断
    **会话窗口是否重开**的唯一信号 —— 窗口才是能否主动推送的闸门，不是 context_token
    本身。此前只有 send() 失败后的自愈窗口会捕获（见 _recover_while_cooling /
    _recapture_and_resend），而**补发路径从不轮询** → 用户按告警提示发了消息也没有任何人
    接住，补发只能按固定间隔硬撞 `prepare failed`。现在每次有 pending 待补发时先短轮询
    一次（无消息则最多等 `_INBOUND_PEEK_TIMEOUT` 秒），一旦确认窗口重开就绕开
    `_blocked_state_skip_send` 的间隔限制、立即补发。没有 pending 时不轮询，常规 tick 零额外开销。
    """
    cfg = load_config()
    st = _load_state()
    pending = st.get("pending") or []
    if not pending:
        return {"flushed": 0, "dropped": 0, "already": 0, "remaining": 0}
    now = time.time()

    # ① 先收信箱：短轮询一次，有入站消息就说明窗口已重开（顺带刷新 token）
    refreshed = False
    if clawbot is not None and hasattr(clawbot, "capture_inbound_once"):
        try:
            refreshed = clawbot.capture_inbound_once(timeout=_INBOUND_PEEK_TIMEOUT)
        except Exception:  # noqa: BLE001
            refreshed = False
    if refreshed:
        st = _load_state()   # capture 内部写盘了，重载后再读 last_send_error 等字段

    # ② 「投递被拒」状态下不给无谓重试烧配额（见 _blocked_state_skip_send 的长注释）
    wait_min = _blocked_state_skip_send(cfg, st, now, refreshed)
    if wait_min > 0:
        return {"flushed": 0, "dropped": 0, "already": 0, "remaining": len(pending),
                "skipped": "投递被拒，{} 分钟后再试（避免烧配额）".format(wait_min)}

    cutoff = now - 48 * 3600
    # 已经送到过微信的指纹。这些条目若继续留在队列里会**永久卡住**：
    # 再发一次会被 `_dedupe_lookup` 拦下（返回 wechat=False），于是既排不空队列、
    # 又让每次 catchup 都白跑一遍补发分支。2026-09-29（macOS 侧）实测踩到 ——
    # 手工 force 补发两条后，pending 仍是 2 条、sent 表里已有这两条指纹，
    # 队列只能靠人工清；下一次 flush 甚至会被误记成"补发失败"。
    # 送达过的就是送达过了，无论由哪条路径送出去的。
    delivered = set((st.get("sent") or {}).keys())
    ok_n = 0
    dropped = 0
    already = 0
    rest = []
    for item in pending:
        if not isinstance(item, dict):
            continue
        if item.get("ts", 0) < cutoff:
            dropped += 1
            continue                      # 过期丢弃
        if day_done and (item.get("level") or "") == "failure":
            dropped += 1
            continue                      # 当天已办成 → 这条失败告警已过时，作废
        if item.get("fp") and item.get("fp") in delivered:
            already += 1
            continue                      # 已送达 → 不能再发一次（否则重复推给用户）
        r = send(item.get("title", ""), item.get("content", ""),
                 item.get("level") or "info", allow_recapture=False,
                 cooldown_on_fail=False)
        if r.get("wechat"):
            ok_n += 1
        else:
            rest.append(item)
    # 重新加载最新状态再只改 pending —— send() 内部会写盘（daily / 指纹），
    # 直接用进入本函数的旧 st 写回会把那些更新覆盖掉。
    st = _load_state()
    st["pending"] = rest
    _save_state(st)
    return {"flushed": ok_n, "dropped": dropped, "already": already,
            "remaining": len(rest)}


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------
def send(title: str, content: str, level: str = "info", force: bool = False,
         allow_recapture: bool = True, cooldown_on_fail: bool = True) -> dict:
    """发送通知。level: success | failure | warning | info。永不抛异常。

    force=True 跳过级别开关与去重（测试用），但**不跳过**每日配额。
    allow_recapture=False 时跳过「投递被拒 → 自动重捕获」的自愈（批量补发 flush 时用，
    避免每条失败各阻塞 90 秒；自愈只该由正常通知路径触发一次）。
    cooldown_on_fail=False 时失败**不设熔断冷却**（flush 补发时用 —— 补发是低优先级，
    失败就等下次，不该冻结 clawbot 通道、拖累后续正常通知的自愈）。
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
    if cooldown_min and "clawbot" in order and _clawbot_recoverable(st):
        # ★ 2026-09-28：冷却期内也要留一次「捕获」机会。
        #   旧实现到这里就把 clawbot 摘掉 → 没人轮询 → 用户照告警提示发来的那条
        #   消息永远接不住 → 冷却必然走满 60 分钟（详见 _recover_while_cooling）。
        recovered, rec_note = _recover_while_cooling(st, cfg, now)
        if recovered:
            cooldown_min = 0
            out["reason"] = "冷却期内捕获到新消息，已解除冷却（{}）".format(rec_note)
        else:
            out["reason"] = ("ClawBot 会话冷却中（还剩约 {} 分钟）；"
                             "本轮已尝试捕获新消息：{}".format(cooldown_min, rec_note))
    if cooldown_min and "clawbot" in order:
        order = [c for c in order if c != "clawbot"]
        if not out["reason"]:
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
    # 保住循环之前写下的前置说明（目前只可能是「冷却中 / 冷却已解除」那一段）。
    # 为什么要保住：失败时下面会用 reasons 整体覆盖 out["reason"]，于是
    # 「冷却期内捕获到你的消息、已解除冷却」这条**对用户最有用的证据**会被抹掉 ——
    # 而它正是告诉用户「你刚发的那条消息我们收到了」的唯一信号。
    if out["reason"]:
        reasons.append(out["reason"])
    for ch in order:
        ok, reason = _send_via(ch, cfg, title, content)
        # 原始尝试的送达结果（配额按它记；自愈重发在辅助函数里单独记）
        first_attempt_ok = ok

        # ClawBot 熔断：成功则解除冷却；**任何失败**都进入冷却，避免无谓重试（见上方长注释）
        #
        # 判据是 `not force or ok` 而不是 `not force`（2026-09-29 修，与 macOS 侧同步）：
        # force=True 只该跳过"失败后的冷却"，不该在**成功**时把过期的失败标记留在状态里。
        # 踩过的坑：手工 force 补发成功后，`last_send_error` 仍停在几个小时前的
        # 「投递被拒」，于是 `_blocked_state_skip_send` 认定通道仍被拒，
        # 接下来 240 分钟内每一次 flush 都直接跳过 —— 成功反倒把队列锁死了。
        if ch == "clawbot" and (not force or ok):
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
                elif (clawbot is not None
                      and getattr(clawbot, "RATE_LIMIT_MARK", "频率限制") in r_str):
                    # ★ 2026-09-28：频率限制单独成档。
                    #   它原先落进 other 档 → 被 clawbot_failure_cooldown_minutes 冻 20 分钟。
                    #   可服务端自己给的指引是「约 7 条/5 分钟，等 60–120 秒重试」——
                    #   把一次限流放大成 20 分钟通道停摆，会让这期间本该送达的通知
                    #   全被挤到本机弹窗，甚至（默认配额 8 条/天）把后面的通知预算吃光。
                    #   capture 类的自愈对它也无意义：限流不是「窗口没开」，等一会儿就好。
                    kind = "ratelimit"
                else:
                    kind = "other"            # 网络/未知：短冷却，允许较早重试

                # 2026-09-25：投递被拒 / 缺令牌不再只能人工恢复 —— 弹告警引导用户发消息
                # 的同时自动开 90 秒捕获窗口接新 token，抓到立即重发（详见辅助函数 docstring）。
                if kind in ("blocked", "token_missing") and allow_recapture:
                    ok2, reason2 = _recapture_and_resend(cfg, title, content, st, now)
                    reason = reason + "；自动重捕获：" + reason2
                    if ok2:
                        ok = True
                        _clear_clawbot_cooldown(st)
                        st.pop("last_send_error", None)

                if not ok:
                    cd_key = {
                        "session": "clawbot_cooldown_minutes",
                        "blocked": "clawbot_blocked_cooldown_minutes",
                        "token_missing": "clawbot_blocked_cooldown_minutes",
                        "ratelimit": "clawbot_ratelimit_cooldown_minutes",
                        "other": "clawbot_failure_cooldown_minutes",
                    }[kind]
                    try:
                        cd = int(cfg.get(cd_key) or 0)
                    except (TypeError, ValueError):
                        cd = 0
                    if cooldown_on_fail:
                        _set_clawbot_cooldown(st, cd, now)
                        if kind in ("blocked", "token_missing"):
                            # 记下失败时刻的用户活动基线：之后只要用户再发过消息，
                            # 冷却就该立刻解除（这两种故障的恢复动作都是「发一条消息」）
                            st["cooldown_context_ts"] = _current_inbound_ts()
                        out["cooldown_min"] = cd
                    if kind in ("session", "blocked"):
                        # 额外弹一条明确告警，避免用户把"降级后的本机通知"误当成通道正常。
                        # 刻意不含 token_missing：缺令牌的失败文案本身已经说清了原因与动作，
                        # 而复用 blocked 的告警会把原因错写成「prepare failed」。
                        out["channel_expired_alert"] = _alert_channel_expired(st, cfg, now, kind)

        # 只要请求真的发出去过，就计入当日配额（失败也占用 iLink 配额）
        _bump_daily(st, delivered=first_attempt_ok)
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
    # 关键通知（到账/失败）微信未送达时入队，供 flush_pending 稍后补发 ——
    # 保证「每天的消息一定微信通知到」（2026-09-25）。
    if not wechat_delivered and not force and level in ("success", "failure"):
        _enqueue_pending(st, level, title, content, now)
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
