#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
clawbot.py — 腾讯 iLink / 微信 ClawBot 直推模块（零第三方依赖，仅标准库）

背景：
  腾讯 2026-03 通过 OpenClaw 框架开放了官方个人微信 Bot API（协议名 iLink，
  域名 ilinkai.weixin.qq.com）。WorkBuddy 桌面端的「微信 ClawBot」通道就是基于它。
  本机在 WorkBuddy 里绑定过 ClawBot 时，凭据落在
  ~/.workbuddy/settings.json → claw.users.<uid>.channels.weixinClawBot。

请求格式（严格对齐 WorkBuddy 自身实现
  packages/workbuddy-server/src/claw/plugins/weixin/weixin-api.ts）：
  发消息  POST {base_url}/ilink/bot/sendmessage
  收消息  POST {base_url}/ilink/bot/getupdates   （长轮询 35s）
  二维码  GET  {base_url}/ilink/bot/get_bot_qrcode?bot_type=3
  扫码态  GET  {base_url}/ilink/bot/get_qrcode_status?qrcode=…   （头 iLink-App-ClientVersion: 1）

  请求头（消息/轮询接口）：
    Content-Type: application/json
    AuthorizationType: ilink_bot_token
    Authorization: Bearer {bot_token}
    X-WECHAT-UIN: base64(随机 uint32 的十进制字符串)   ← 防重放
  Body：
    {"msg": {"to_user_id", "client_id", "message_type": 2, "message_state": 2,
             "context_token", "item_list": [{"type": 1, "text_item": {"text": …}}]},
     "base_info": {"channel_version": "workbuddy-desktop-1.0.0"}}

  注意：WorkBuddy 原生实现**不带** iLink-App-Id / iLink-App-ClientVersion 到消息接口；
  这两个只在二维码状态轮询时用（值为 "1"）。channel_version 用 "workbuddy-desktop-1.0.0"。

错误语义（务必分清，两者都表现为 HTTP 200 + 响应体）：
  · errcode=-14 / "session timeout"  → **微信登录会话已过期**。
      不是"用户没发消息"，发消息也救不回来。必须**重新扫码登录**
      （本模块 `login` 命令，或 WorkBuddy 设置里的「微信 ClawBot」重新绑定）。
      处理：冻结该账号所有调用、清空本地游标与凭证、重走二维码登录。
  · ret=-2                           → 频率限制（每 bot 约 7 条 / 5 分钟），等 60–120s 重试。
  · context_token 缺失               → 主动推送无法建立会话，需先用 `wait` 捕获一次。

context_token：
  主动推送必须携带「从入站消息捕获」的 context_token。
  获取方式：让用户在微信里给 ClawBot 发一条消息，本模块 `wait` 长轮询捕获并持久化。
  ⚠️ context_token 是临时的，不保证长期有效；失效后需重新捕获。

历史教训：errcode 才是业务错误码（不是 ret）。早期版本只查 ret，导致会话过期被误判为
"发送成功"，日志恒显示正常而微信实际收不到。已修。

自测：
  python3 clawbot.py status        # 只读：凭据 / context_token / 游标
  python3 clawbot.py login         # 扫码重新登录（会话过期时用）
  python3 clawbot.py wait [秒数]   # 长轮询捕获 context_token（默认 60s）
  python3 clawbot.py test          # 真实推一条测试消息到微信
"""

from __future__ import annotations

import base64
import json
import pathlib
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(DIR))
import paths  # noqa: E402

SETTINGS = pathlib.Path.home() / ".workbuddy" / "settings.json"
STATE = paths.state_path("clawbot_state.json")

# 对齐 WorkBuddy 原生实现（weixin-api.ts 的 buildBaseInfo / buildHeaders）
CHANNEL_VERSION = "workbuddy-desktop-1.0.0"
QR_CLIENT_VERSION = "1"
DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"

SEND_PATH = "/ilink/bot/sendmessage"
GETUPDATES_PATH = "/ilink/bot/getupdates"
QRCODE_PATH = "/ilink/bot/get_bot_qrcode"
QRSTATUS_PATH = "/ilink/bot/get_qrcode_status"

# notify.py 依赖这个标记做熔断判断（勿随意改字面量）
SESSION_EXPIRED_MARK = "会话已过期"
RATE_LIMIT_MARK = "频率限制"
# 「微信侧拒绝投递」：实测 ret=-2 + errmsg="prepare failed"（2026-09-18）。
# 它与频率限制共用 ret=-2，含义完全不同；notify.py 据此决定「重试有没有意义」。
DELIVER_BLOCKED_MARK = "投递被拒"

# 消息枚举
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2
ITEM_TYPE_TEXT = 1


# ---------------------------------------------------------------------------
# 凭据：优先用本机 login 得到的，其次复用 WorkBuddy 的
# ---------------------------------------------------------------------------
def _read_local_credentials() -> dict | None:
    d = _load_state()
    c = d.get("credentials")
    if isinstance(c, dict) and c.get("bot_token"):
        return c
    return None


def _apply_user_override(ch: dict | None) -> dict | None:
    """用本地记录的 user_id 覆盖凭据里的值（登录接口未必回传，靠入站消息补全）。"""
    if ch:
        ov = (_load_state().get("user_id_override") or "").strip()
        if ov:
            ch["user_id"] = ov
    return ch


def load_channel() -> dict | None:
    """返回 {bot_token, base_url, user_id, channel_id, source}；读不到返回 None。"""
    local = _read_local_credentials()
    if local:
        return _apply_user_override(local)

    try:
        cfg = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None

    claw = cfg.get("claw") or {}
    users = claw.get("users") or {}
    owner = claw.get("legacyOwnerUid")

    candidates = []
    if isinstance(owner, str) and owner in users:
        candidates.append(users[owner])
    candidates.extend(u for k, u in users.items() if k != owner)

    for u in candidates:
        ch = ((u or {}).get("channels") or {}).get("weixinClawBot") or {}
        token = ch.get("botToken")
        user_id = ch.get("userId")
        if ch.get("enabled") and token and user_id:
            return _apply_user_override({
                "bot_token": token,
                "base_url": (ch.get("baseUrl") or DEFAULT_BASE_URL).rstrip("/"),
                "user_id": user_id,
                "channel_id": ch.get("channelId"),
                "source": "workbuddy-settings",
            })
    return None


def _uid_is_incomplete(ch: dict | None) -> bool:
    if not ch:
        return True
    uid = (ch.get("user_id") or "").strip()
    return (not uid) or uid.startswith("@") or uid == "@im.wechat"


def _fill_user_id(uid: str) -> None:
    """把入站消息里的发送者 ID 记下来，补全凭据中缺失的 user_id。"""
    if not uid:
        return
    st = _load_state()
    st["user_id_override"] = uid.strip()
    _save_state(st)


def save_credentials(bot_token: str, base_url: str, user_id: str, bot_id: str = "") -> None:
    st = _load_state()
    st["credentials"] = {
        "bot_token": bot_token,
        "base_url": (base_url or DEFAULT_BASE_URL).rstrip("/"),
        "user_id": user_id,
        "channel_id": bot_id,
        "source": "local-login",
    }
    _save_state(st)


def clear_credentials() -> None:
    st = _load_state()
    st.pop("credentials", None)
    st.pop("context_token", None)
    st.pop("get_updates_buf", None)
    _save_state(st)


def mask(token: str | None) -> str:
    if not token:
        return "(空)"
    return "{}{}（共 {} 位）".format(token[:4], "·" * 6, len(token))


def _to_user_id(user_id: str) -> str:
    """iLink 用户 ID 形如 xxx@im.wechat。裸 openid 时补后缀。"""
    return user_id if "@" in user_id else user_id + "@im.wechat"


# ---------------------------------------------------------------------------
# 本地状态（context_token / 游标 / 本地登录凭据）
# ---------------------------------------------------------------------------
def _load_state() -> dict:
    try:
        if STATE.exists():
            d = json.loads(STATE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return d
    except Exception:  # noqa: BLE001
        pass
    return {}


def _save_state(d: dict) -> None:
    try:
        STATE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        paths.secure_runtime_files()
    except OSError:
        pass


def internal_state() -> dict:
    """供同项目其他模块（如 renew.py）读取本地状态：
    credentials / context_token / context_token_ts / last_send_ok_ts / get_updates_buf。
    """
    return _load_state()


def remember_context_token(token: str) -> None:
    if not token:
        return
    st = _load_state()
    st["context_token"] = token
    # 记录捕获时间：这近似等于「用户最后一次给机器人发消息」的时间，
    # renew.py 用它作为会话续期的计时基线。
    st["context_token_ts"] = time.time()
    _save_state(st)


def clear_cursor() -> None:
    """会话过期时清空同步游标（协议要求：-14 后不得沿用旧游标）。"""
    st = _load_state()
    st.pop("get_updates_buf", None)
    _save_state(st)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _headers(bot_token: str) -> dict:
    """消息 / 轮询接口的请求头（不含 iLink-App-Id，对齐 WorkBuddy 原生实现）。"""
    uin = base64.b64encode(str(random.getrandbits(32)).encode()).decode()
    return {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": uin,
        "Authorization": "Bearer " + bot_token,
    }


def _post(base_url: str, path: str, payload: dict, token: str, timeout: int = 20):
    """POST 并解析。返回 (http_ok, data_or_none, raw, err)。

    iLink 的失败常常是 HTTP 200 + 响应体里的 errcode，因此**必须解析响应体**。
    连接超时统一返回 err="TIMEOUT"，由调用方决定语义（长轮询里超时是正常的）。
    """
    body = json.dumps(payload)
    req = urllib.request.Request(base_url.rstrip("/") + path,
                                 data=body.encode("utf-8"), method="POST",
                                 headers=_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace").strip()
            data = json.loads(raw) if raw else {}
            return True, data, raw, ""
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace").strip()
        try:
            return False, json.loads(raw), raw, "HTTP {}".format(e.code)
        except json.JSONDecodeError:
            return False, None, raw, "HTTP {} {}".format(e.code, raw[:120])
    except TimeoutError:
        return False, None, "", "TIMEOUT"
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), TimeoutError):
            return False, None, "", "TIMEOUT"
        return False, None, "", "请求异常：" + str(e)[:140]
    except Exception as e:  # noqa: BLE001
        return False, None, "", "请求异常：" + str(e)[:140]


def _explain_error(data: dict) -> str:
    """把 iLink 的错误响应翻译成可执行的中文说明。

    ⚠️ 每个分支都必须把服务端的 errmsg 原样带上（哪怕不认识它）。
    2026-09-18 的教训：`ret=-2` 一律被翻译成「频率限制」，而这次服务端给的
    errmsg 是 `prepare failed` —— 解释文案把真实原因盖掉了，排查时只能看到
    我们**自己编的说法**，方向完全被带偏。
    """
    errcode = data.get("errcode")
    ret = data.get("ret")
    errmsg = str(data.get("errmsg") or "")
    low = errmsg.lower()
    detail = "（errcode={} ret={} errmsg={}）".format(errcode, ret, errmsg or "-")

    if errcode == -14 or ret == -14 or "session timeout" in low:
        return (SESSION_EXPIRED_MARK + "（errcode=-14）：微信登录会话已失效，"
                "发送消息无法恢复。请重新扫码登录："
                "运行 `python3 clawbot.py login`，"
                "或在 WorkBuddy 设置里重新绑定「微信 ClawBot」。")
    # 实测（2026-09-18）：ret=-2 配 errmsg="prepare failed" —— 服务端拒绝为这条
    # 主动消息做准备。它与「频率限制」共用 ret=-2，含义却完全不同，所以必须先判 errmsg。
    if "prepare" in low:
        return (DELIVER_BLOCKED_MARK + "：服务端拒绝为这条主动消息建立会话" + detail + "。"
                "**具体判据服务端没有公开，不要相信任何「N 小时不过期」的说法** ——"
                "实测有两个相反的数据点：距上次给机器人发消息 9 小时能发、32 小时发不出"
                "（那次刚过夜休眠醒来），但 38 小时后又能发出（当时桌面端在持续轮询）。"
                "现有证据更支持「与本机客户端是否在持续轮询这个 bot 有关」，样本太少，仍属推测。"
                "恢复动作：① 在微信里给机器人随便发一条消息（如「1」）；"
                "② 保持 WorkBuddy 桌面端运行。两者都可恢复，都不需要重新扫码。")
    if ret == -2 or errcode == -2:
        return (RATE_LIMIT_MARK + detail +
                "：同一 bot 约 7 条 / 5 分钟，请等 60–120 秒重试。")
    if errcode in (401, 403):
        return "凭据被拒" + detail + "，需重新扫码登录。"
    return "未识别的错误" + detail


def _is_session_expired(data: dict) -> bool:
    return (data.get("errcode") == -14
            or data.get("ret") == -14
            or "session timeout" in str(data.get("errmsg") or "").lower())


def probe_context(timeout: int = 15) -> tuple[bool, str]:
    """只读探针：用本地 context_token 调 `getconfig`，判断令牌是否仍被服务端认可。

    这是**唯一可以在本地自证「会话是否还活着」的手段**（发消息接口只回 message_id，
    没有投递状态字段）。桌面端用同一接口取 typing_ticket，所以它的返回可作判据：
      ret=0 且带 typing_ticket  → 令牌有效、会话活着
      ret!=0 / 无 typing_ticket  → 令牌已失效，需重新 `wait` 捕获
    不发送任何消息、不占用推送配额。
    """
    ch = load_channel()
    if not ch:
        return False, "未找到 ClawBot 凭据"
    ctx = (_load_state().get("context_token") or "")
    if not ctx:
        return False, "本地没有 context_token（先让用户在微信发一条消息并运行 wait）"

    ok, data, raw, err = _post(ch["base_url"], "/ilink/bot/getconfig",
                               {"ilink_user_id": _to_user_id(ch["user_id"]),
                                "context_token": ctx,
                                "base_info": {"channel_version": CHANNEL_VERSION}},
                               ch["bot_token"], timeout)
    if not ok:
        return False, ("请求超时" if err == "TIMEOUT" else err)
    if not data:
        return False, "空响应体"
    if _is_session_expired(data):
        return False, SESSION_EXPIRED_MARK + "：getconfig 返回 -14"
    ret = data.get("ret")
    if ret not in (None, 0):
        return False, "getconfig ret={} errmsg={}".format(ret, data.get("errmsg") or "")
    if data.get("typing_ticket"):
        return True, "context_token 有效（getconfig ret=0，拿到 typing_ticket）"
    return False, "getconfig ret=0 但未返回 typing_ticket，令牌可能已过期"


# ---------------------------------------------------------------------------
# 发送
# ---------------------------------------------------------------------------
def send_text(text: str, *, timeout: int = 20, use_context: bool = True) -> tuple[bool, str]:
    """推送一条纯文本到用户微信。返回 (成功, 说明)。永不抛异常。

    协议限制：不支持表格 / 图片 / 代码块，Markdown 渲染不稳定 → 传纯文本。
    """
    ch = load_channel()
    if not ch:
        return False, "未找到 ClawBot 凭据（请先在 WorkBuddy 绑定，或运行 login 扫码）"

    # 报文严格对齐桌面端 WeixinClawBotClient.sendTextReply：
    #   - from_user_id 必须给空串（不能省字段）
    #   - client_id 必须**每条唯一**（桌面端用 `workbuddy-{ts}-{rand}`）。
    #     此前用固定串 "wb-reward-catchup" 是错的：client_id 是客户端消息标识，
    #     重复值可能被服务端按幂等/去重处理而不再投递。
    msg = {
        "from_user_id": "",
        "to_user_id": _to_user_id(ch["user_id"]),
        "client_id": "wb-reward-{}-{}".format(int(time.time() * 1000),
                                              random.randrange(16 ** 6)),
        "message_type": MSG_TYPE_BOT,
        "message_state": MSG_STATE_FINISH,
        "item_list": [{"type": ITEM_TYPE_TEXT, "text_item": {"text": text}}],
    }
    ctx = _load_state().get("context_token") if use_context else None
    if ctx:
        msg["context_token"] = ctx

    ok, data, raw, err = _post(ch["base_url"], SEND_PATH,
                               {"msg": msg, "base_info": {"channel_version": CHANNEL_VERSION}},
                               ch["bot_token"], timeout)

    if not ok:
        return False, ("请求超时（{} 秒无响应）".format(timeout) if err == "TIMEOUT" else err)
    if data:
        if _is_session_expired(data):
            clear_cursor()
            # 记录失效时刻：notify.py 据此发一条明确的「通道已失效」本地告警
            # （否则 -14 只会表现为"积分通知降级成 macOS 通知"，用户察觉不到通道挂了）。
            stx = _load_state()
            stx["last_session_expired_ts"] = time.time()
            _save_state(stx)
        if data.get("errcode") not in (None, 0) or data.get("ret") not in (None, 0):
            return False, _explain_error(data)

    # 送达判据（2026-09-17 修正，务必不要再改回去）：
    #
    # 历史坑：曾以为「服务端返回 message_id」= 已送达，据此把没有 context_token
    # 的推送也记为成功。实测证明这个假设是错的 —— 缺令牌时服务端照样返回
    # message_id、照样占用当日配额，但消息**不会出现在微信里**。
    # 这就是「一直显示发送成功、用户一直收不到」的根因。
    #
    # 现在把两件事分开：
    #   「受理」= 无错误码（服务端收下了请求）
    #   「送达」= 受理 + 持有 context_token
    # 缺令牌时返回 False，让 notify.py 降级到 macOS 通知并提示用户做恢复动作，
    # 而不是静默地假装成功。
    mid = data.get("message_id") if data else None
    st = _load_state()
    st["last_send_ok_ts"] = time.time()
    if ctx:
        st.pop("token_missing_since", None)
    else:
        st.setdefault("token_missing_since", time.time())
    _save_state(st)

    mid_txt = "（message_id={}）".format(mid) if mid else ""
    if not ctx:
        return False, (
            "缺 context_token：服务端已受理" + mid_txt +
            "，但消息不会到达微信。请在微信给机器人发任意一条消息"
            "（如「1」）刷新令牌，推送即自动恢复。"
        )
    return True, "clawbot 已送达" + mid_txt


# ---------------------------------------------------------------------------
# 长轮询：捕获 context_token
# ---------------------------------------------------------------------------
def poll_updates(*, timeout: int = 40) -> tuple[dict, str]:
    """长轮询一次。返回 (响应 dict, 错误说明)。服务端最多挂起 35s。"""
    ch = load_channel()
    if not ch:
        return {}, "未找到 ClawBot 凭据"

    payload = {
        "get_updates_buf": _load_state().get("get_updates_buf") or "",
        "base_info": {"channel_version": CHANNEL_VERSION},
    }
    ok, data, raw, err = _post(ch["base_url"], GETUPDATES_PATH, payload,
                               ch["bot_token"], timeout)
    if not ok:
        if err == "TIMEOUT":
            # 长轮询正常超时 = 这段时间没有新消息，不算错误
            return {}, ""
        return data or {}, err
    if not data:
        return {}, ""

    if _is_session_expired(data):
        clear_cursor()  # 协议要求：-14 后清空游标
        return data, SESSION_EXPIRED_MARK
    if data.get("errcode") not in (None, 0) or data.get("ret") not in (None, 0):
        return data, _explain_error(data)

    buf = data.get("get_updates_buf")
    if buf:
        st = _load_state()
        st["get_updates_buf"] = buf
        _save_state(st)
    return data, ""


def capture_context_token(*, wait_seconds: int = 60) -> tuple[bool, str]:
    """长轮询等待用户发消息，抓到 context_token 即持久化。

    用法：先在微信里给 ClawBot 机器人发一条消息（如「1」），再运行本函数。
    注意：WorkBuddy 桌面端若也在轮询同一 bot，会与本脚本抢消息；排障用即可，勿长期挂后台。
    """
    deadline = time.monotonic() + max(wait_seconds, 10)
    last_err = ""
    while time.monotonic() < deadline:
        data, err = poll_updates()
        if err:
            last_err = err
            if err == SESSION_EXPIRED_MARK:
                return False, (
                    SESSION_EXPIRED_MARK + "：登录已失效，捕获 context_token 必须先重新登录。"
                    "请运行 `python3 clawbot.py login`。"
                )
            time.sleep(3)
            continue
        for m in (data.get("msgs") or []):
            ctx = m.get("context_token")
            if ctx:
                remember_context_token(ctx)
                # 登录接口未必回传 ilink_user_id；用入站消息的发送者补全
                src = m.get("from_user_id") or ""
                if src and _uid_is_incomplete(load_channel()):
                    _fill_user_id(src)
                return True, "已获取并保存 context_token（来自 {}{}）".format(
                    src or "未知",
                    "，内容：" + str((m.get("item_list") or [{}])[0]
                                     .get("text_item", {}).get("text", ""))[:20]
                    if m.get("item_list") else "")
    suffix = ("；最后错误：" + last_err) if last_err else ""
    return False, ("等待超时：未收到消息。请在微信里打开 ClawBot 对话，"
                   "随便发一条消息（如「1」），然后重跑本命令" + suffix)


# ---------------------------------------------------------------------------
# 扫码登录（会话过期时使用）
# ---------------------------------------------------------------------------
def fetch_qrcode(base_url: str = DEFAULT_BASE_URL) -> tuple[dict | None, str]:
    url = base_url.rstrip("/") + QRCODE_PATH + "?bot_type=" + urllib.parse.quote("3")
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8", "replace")), ""
    except urllib.error.HTTPError as e:
        return None, "HTTP {} {}".format(e.code, e.read().decode("utf-8", "replace")[:150])
    except Exception as e:  # noqa: BLE001
        return None, "请求异常：" + str(e)[:140]


def poll_qr_status(base_url: str, qrcode: str, timeout: int = 40) -> tuple[dict | None, str]:
    url = (base_url.rstrip("/") + QRSTATUS_PATH
           + "?qrcode=" + urllib.parse.quote(qrcode))
    req = urllib.request.Request(url, method="GET",
                                 headers={"iLink-App-ClientVersion": QR_CLIENT_VERSION})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace")), ""
    except urllib.error.HTTPError as e:
        return None, "HTTP {} {}".format(e.code, e.read().decode("utf-8", "replace")[:150])
    except Exception as e:  # noqa: BLE001
        return None, "请求异常：" + str(e)[:140]


DIR = pathlib.Path(__file__).resolve().parent


def render_qr_html(url: str, out_html: pathlib.Path, *, stamp: str = "",
                   refresh_note: str = "") -> bool:
    """把授权链接渲染成可扫码的 HTML 页面（二维码由浏览器端 JS 生成）。"""
    tpl = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>微信 ClawBot 扫码登录</title>
<script src="https://cdn.jsdelivr.net/npm/qrcodejs@1.0.0/qrcode.min.js"></script>
<style>
  body { font-family: -apple-system, system-ui, "PingFang SC", sans-serif;
         background: #F5F5F3; color: #2C2C2A; min-height: 100vh; margin: 0;
         display: flex; flex-direction: column; align-items: center;
         justify-content: center; gap: 18px; }
  h1 { font-size: 19px; font-weight: 500; margin: 0; }
  #qrcode { background: #fff; padding: 22px; border-radius: 14px;
            box-shadow: 0 1px 3px rgba(0,0,0,.06); }
  p { font-size: 14px; color: #5F5E5A; margin: 0; }
  .stamp { font-size: 12px; color: #888780; }
  .note { font-size: 12px; color: #B4553F; }
  code { font-size: 12px; color: #888780; word-break: break-all;
         max-width: 420px; text-align: center; }
</style>
</head>
<body>
<h1>用微信扫码登录 ClawBot</h1>
<div id="qrcode"></div>
<p>二维码有效期约 5 分钟 · 扫码后在手机上确认</p>
<p class="stamp">本页生成于 __STAMP__</p>
<p class="note">__REFRESH_NOTE__</p>
<code>__QR_URL__</code>
<script>
new QRCode(document.getElementById("qrcode"),
           { text: "__QR_URL__", width: 264, height: 264,
             correctLevel: QRCode.CorrectLevel.M });
</script>
</body>
</html>
"""
    try:
        out_html.write_text(
            tpl.replace("__QR_URL__", url)
               .replace("__STAMP__", stamp or "—")
               .replace("__REFRESH_NOTE__", refresh_note or "&nbsp;"),
            encoding="utf-8")
        return True
    except OSError:
        return False


def qr_login(*, out_html: pathlib.Path | None = None,
             wait_seconds: int = 1500,
             auto_refresh: bool = True,
             on_event=None) -> tuple[bool, str]:
    """走扫码登录流程。

    二维码以 HTML 形式输出（浏览器打开后用手机扫）。返回 (成功, 说明)。

    auto_refresh=True 时，二维码过期（约 5 分钟）会自动重新获取并**重写同一个 HTML 文件**，
    因此用户无需赶时间 —— 只要在浏览器里刷新页面就能看到新码。
    """
    base = DEFAULT_BASE_URL
    if out_html is None:
        out_html = DIR / "clawbot_login.html"

    def emit(obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=False)
        print(line, flush=True)
        if on_event:
            try:
                on_event(obj)
            except Exception:  # noqa: BLE001
                pass

    def fetch_and_render(note: str = "") -> tuple[str, str]:
        """取新码并重写页面。返回 (qrcode, url)；失败返回 ("", 原因)。"""
        nonlocal base
        qr, err = fetch_qrcode(base)
        if err or not qr:
            return "", "获取二维码失败：" + (err or "无响应")
        code = qr.get("qrcode") or ""
        url = qr.get("qrcode_img_content") or ""
        if not code or not url:
            return "", "响应缺少 qrcode / qrcode_img_content：" + json.dumps(qr, ensure_ascii=False)[:200]
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        if not render_qr_html(url, out_html, stamp=stamp, refresh_note=note):
            return "", "二维码页面写入失败：" + str(out_html)
        return code, url

    qrcode, url = fetch_and_render()
    if not qrcode:
        return False, url  # url 此时是错误说明

    emit({"step": "qrcode_ready", "qrcode_html": str(out_html),
          "hint": "请打开该 HTML 页面并用微信扫码；过期会自动换码，刷新页面即可。"})

    deadline = time.monotonic() + max(wait_seconds, 60)
    refreshes = 0

    while time.monotonic() < deadline:
        st, err = poll_qr_status(base, qrcode)
        if err or not st:
            time.sleep(2)
            continue

        status = st.get("status")
        if status == "confirmed":
            token = st.get("bot_token") or ""
            if token.lower().startswith("bearer "):
                token = token[7:]
            if not token:
                return False, "登录已确认但未返回 bot_token：" + json.dumps(st, ensure_ascii=False)[:200]
            save_credentials(
                bot_token=token,
                base_url=st.get("baseurl") or base,
                user_id=st.get("ilink_user_id") or "",
                bot_id=st.get("ilink_bot_id") or "",
            )
            emit({"step": "confirmed", "refreshes": refreshes,
                  "bot_id": st.get("ilink_bot_id"), "user_id": st.get("ilink_user_id")})
            return True, "登录成功 · bot_id={} · user_id={}".format(
                st.get("ilink_bot_id"), st.get("ilink_user_id"))
        if status == "scaned_but_redirect":
            host = st.get("redirect_host")
            if host:
                base = "https://" + host
                emit({"step": "redirect", "host": host})
        if status == "expired":
            if auto_refresh and time.monotonic() < deadline:
                refreshes += 1
                note = "二维码已自动刷新第 {} 次（{}）。若页面仍是旧码，请刷新浏览器。".format(
                    refreshes, time.strftime("%H:%M:%S"))
                code2, url2 = fetch_and_render(note)
                if code2:
                    qrcode, url = code2, url2
                    emit({"step": "qrcode_refreshed", "count": refreshes})
                    continue
                return False, "二维码过期后换码失败：" + url2
            return False, "二维码已过期且未开启自动换码，请重跑 login。"
        time.sleep(2)

    return False, "等待扫码超时（{} 秒，已自动换码 {} 次），请重跑 login。".format(
        wait_seconds, refreshes)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    ch = load_channel()

    if action == "status":
        st = _load_state()
        out = {"settings_file": str(SETTINGS), "state_file": str(STATE)}
        if ch:
            ctx = st.get("context_token")
            ts = st.get("context_token_ts")
            out.update({
                "found": True,
                "source": ch.get("source", "?"),
                "base_url": ch["base_url"],
                "bot_token": mask(ch["bot_token"]),
                "user_id": _to_user_id(ch["user_id"]),
                "channel_id": ch.get("channel_id"),
                "has_context_token": bool(ctx),
                "context_token": mask(ctx) if ctx else "(无 → 推送不会到达微信，需 wait 捕获)",
                "context_token_age_hours": (
                    round((time.time() - ts) / 3600, 1) if isinstance(ts, (int, float)) else None),
                "token_missing_since": st.get("token_missing_since"),
                "cursor": "有" if st.get("get_updates_buf") else "(空)",
            })
        else:
            out["found"] = False
            out["reason"] = "没有可用凭据：WorkBuddy 未绑定 ClawBot，且本机未 login"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        sys.exit(0 if ch else 1)

    if action == "login":
        # 用法：login [等待扫码秒数] [等待消息秒数]
        qr_wait = int(sys.argv[2]) if len(sys.argv) > 2 else 1500
        msg_wait = int(sys.argv[3]) if len(sys.argv) > 3 else 600
        html = DIR / "clawbot_login.html"
        ok, reason = qr_login(out_html=html, wait_seconds=qr_wait)
        print(json.dumps({"logged_in": ok, "reason": reason, "qrcode_html": str(html)},
                         ensure_ascii=False), flush=True)
        if not ok:
            sys.exit(1)

        # ⚠️ 2026-09-17 更正：以下"不带 context_token 也能送达"的假设**是错的**。
        # 服务端返回 message_id 只代表**受理**（并照样计配额），不带令牌的推送
        # 不会进入微信 —— 已由实测确认（桌面端带令牌发送 delivered 可见；
        # 本模块不带令牌发送返回 message_id 但微信收不到）。
        # 因此 send_text 的 True 只能说明"会话仍有效"，**不能**据此宣告"直推已恢复"。
        # 判定投递成功必须以"持有 context_token"为前提，否则应先走下方 wait 分支。
        sent, sreason = send_text(
            "【WorkBuddy 积分助手 · 通道已恢复】\n"
            "扫码登录成功。收到本条即表示微信直推已恢复，无需再做其他操作。")
        print(json.dumps({"test_sent": sent, "reason": sreason,
                          "caveat": "sent=true 仅表示服务端受理；实际投递需 context_token"},
                         ensure_ascii=False), flush=True)
        if sent:
            # 顺手短探一次抓 token（抓到才真正具备投递能力）
            ok2, reason2 = capture_context_token(wait_seconds=15)
            print(json.dumps({"context_token_optional": ok2, "reason": reason2},
                             ensure_ascii=False), flush=True)
            sys.exit(0)

        # 推不出去 → 服务端需要一条入站消息建立会话
        print(json.dumps({
            "hint": "直接推送未成功，需要一条入站消息来建立会话。"
                    "请在微信里给 ClawBot 发一条消息（如「1」），"
                    "正在等待（最长 {} 秒）…".format(msg_wait)
        }, ensure_ascii=False), flush=True)
        ok2, reason2 = capture_context_token(wait_seconds=msg_wait)
        print(json.dumps({"context_token_captured": ok2, "reason": reason2},
                         ensure_ascii=False), flush=True)
        if ok2:
            sent2, s2 = send_text(
                "【WorkBuddy 积分助手 · 通道已恢复】\n"
                "收到本条即表示微信直推已恢复。")
            print(json.dumps({"test_sent": sent2, "reason": s2},
                             ensure_ascii=False), flush=True)
        sys.exit(0 if ok2 else 1)

    if action == "wait":
        secs = int(sys.argv[2]) if len(sys.argv) > 2 else 60
        print(json.dumps({
            "hint": "请在微信里给 ClawBot 机器人发一条消息（如「1」），"
                    "本命令将在 {} 秒内自动捕获 context_token".format(secs)
        }, ensure_ascii=False))
        ok, reason = capture_context_token(wait_seconds=secs)
        print(json.dumps({"captured": ok, "reason": reason}, ensure_ascii=False))
        sys.exit(0 if ok else 1)

    if action == "probe":
        ok, reason = probe_context()
        print(json.dumps({"token_valid": ok, "reason": reason}, ensure_ascii=False))
        sys.exit(0 if ok else 1)

    if action == "race":
        # 用法：race [文本] [等待秒数]
        # 语义：盯着入站消息，一旦用户发来消息就**立刻**抢发推送。
        # 验证「每个入站上下文只允许一条出站、桌面端自动回复会抢先消耗掉」这一假设。
        text = sys.argv[2] if len(sys.argv) > 2 else "X｜竞速测试"
        secs = int(sys.argv[3]) if len(sys.argv) > 3 else 180
        print(json.dumps({"hint": "请在微信给机器人发一条消息（如「1」）；"
                                  "本命令会在收到的那一刻立刻抢发推送，"
                                  "窗口 {} 秒".format(secs)}, ensure_ascii=False), flush=True)
        deadline = time.monotonic() + secs
        while time.monotonic() < deadline:
            data, err = poll_updates()
            if err:
                if err == SESSION_EXPIRED_MARK:
                    print(json.dumps({"error": err}, ensure_ascii=False), flush=True)
                    sys.exit(1)
                time.sleep(2)
                continue
            for m in (data.get("msgs") or []):
                ctx = m.get("context_token")
                if not ctx:
                    continue
                t_recv = time.time()
                remember_context_token(ctx)
                ok, reason = send_text(text)
                print(json.dumps({
                    "inbound_msg_id": m.get("msg_id") or m.get("msgId"),
                    "sent_immediately": ok,
                    "reason": reason,
                    "delta_ms": round((time.time() - t_recv) * 1000),
                }, ensure_ascii=False), flush=True)
                sys.exit(0 if ok else 1)
        print(json.dumps({"timeout": True}, ensure_ascii=False), flush=True)
        sys.exit(1)

    if action == "test":
        ok, reason = send_text(
            "【WorkBuddy 积分助手 · 通道自测】\n"
            "收到这条即表示微信直推已打通。\n"
            "来源：腾讯 iLink ClawBot 官方通道，无第三方中转。"
        )
        print(json.dumps({"sent": ok, "reason": reason}, ensure_ascii=False))
        sys.exit(0 if ok else 1)

    print("用法：python3 clawbot.py [status|login|wait [秒数]|test]")
    sys.exit(2)
