#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ima「每日登录福利」自动签到（WorkBuddy 积分补跑器的并行任务）

为什么单独一个模块而不并进 scripts/checkin.py：
  ima 与 WorkBuddy 是**两套完全独立的账号体系**（ima 走 ima.qq.com 的 cgi 接口，
  凭据是 refresh token；WorkBuddy 读本机客户端登录态）。混在一个状态机里，
  任一方的登录态问题都会污染另一方。所以：同一份 state 目录、各自的 state 文件、
  各自的凭据文件，互不引用。

机制（2026-10-21 调研，来源：ima 客户端「设置 → ima → 用量统计」页面实际调用的接口
+ 公开实现 LeapYa/ima-qiandao，接口已在本机实测可用）：
  · 认证：refresh_token（长期）→ POST /cgi-bin/auth_login/refresh 换 access token（约 2 小时）
          请求头 x-ima-cookie 带 IMA-UID / IMA-TOKEN / IMA-REFRESH-TOKEN / UID-TYPE / IMA-GUID，
          外加 x-ima-bkn（token 的 djb2 签名，服务端校验用）
  · 查状态：POST /cgi-bin/daily_login_activity/get_activity_info  → 本周 7 格 + 今日状态
  · 签到：  POST /cgi-bin/daily_login_activity/check_in  {"type": 1}   每日 +50 算力
  · 满签：  同一接口 {"type": 2}，周期周一~周日，周日结算；未满签返回 code=1001，已签过 1002
  · 状态枚举：1 已过期 / 2 已领取 / 3 可领取 / 4 未解锁

凭据从哪来（平台现实约束）：
  ima 没有账号密码通道，唯一登录方式是微信扫码。本机 ima 客户端虽然存了 refresh_token，
  但**不含 userId**，而 refresh 接口强制要求 userId —— 所以客户端那条 token 无法直接复用
  （Win 上客户端还把 cookie 库独占锁着，运行中根本读不出来）。首次（以及将来失效时）用
  `python scripts/ima.py login` 扫一次码即可，之后 refresh_token 自动续，无需再扫。

安全：
  - 凭据只落 runtime/config/ima_credentials.json（0600，runtime/ 已被 .gitignore 排除）
  - token 只在内存流转：不打印、不写日志、不进 git
"""

from __future__ import annotations

import datetime
import json
import os
import re
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid as uuidlib

# 支持两种调用方式：被 catchup.py 导入（父目录已在 sys.path）、或直接
# `python scripts/ima.py status`（此时只有 scripts/ 在路径里，需补上父目录才能 import paths）。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paths

BASE = "https://ima.qq.com"
REFRESH_URL = BASE + "/cgi-bin/auth_login/refresh"
LOGIN_URL = BASE + "/cgi-bin/auth_login/login"
ACTIVITY_INFO_URL = BASE + "/cgi-bin/daily_login_activity/get_activity_info"
CHECK_IN_URL = BASE + "/cgi-bin/daily_login_activity/check_in"

# 与 ima 内置浏览器一致的固定请求头（服务端不严格校验具体值，但缺 from_browser_ima 会被拒）
FROM_BROWSER_IMA = "1"
EXTENSION_VERSION = "5.17.2"

CHECKIN_TYPE_DAILY = 1
CHECKIN_TYPE_WEEKLY_BONUS = 2

STATUS_CLAIMED = 2
STATUS_AVAILABLE = 3
STATUS_LOCKED = 4
STATUS_TEXT = {1: "已过期", 2: "已领取", 3: "可领取", 4: "未解锁"}

CODE_OK = 0
CODE_NOT_FULL_WEEK = 1001
CODE_ALREADY_CHECKED_IN = 1002

BONUS_TITLE = "满签奖励"
BONUS_WEEKDAY = 6            # 周一=0 … 周日=6；满签奖励周日结算

WINDOW_FROM = 700            # 07:00 之后才签到（与 WorkBuddy 闸门同值，避免半夜打扰）
MAX_TRIES = 6                # 业务失败最多真实尝试次数（网络失败不烧配额）
MIN_RETRY_GAP_MIN = 20
NET_RETRY_GAP_MIN = 30

CRED_NAME = "ima_credentials.json"
STATE_NAME = "ima_state.json"

APPID = "wx0d63f5de059f1d52"
CHECKIN_PAGE = BASE + "/copilot-token-daily-checkin/pc"
WX_AUTH = BASE + "/wx-auth/"
QRCONNECT_URL = "https://open.weixin.qq.com/connect/qrconnect"
QRCODE_URL = "https://open.weixin.qq.com/connect/qrcode/"
POLL_URL = "https://lp.open.weixin.qq.com/connect/l/qrconnect?uuid="
ACCOUNT_TYPE_WX = 2
QR_LIFETIME_SECONDS = 300
QR_MAX_ATTEMPTS = 3

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")


# ── 基础工具 ────────────────────────────────────────────────────────────────

def _to_int32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value >= 0x80000000 else value


def bkn(token: str) -> int:
    """复刻页面 JS 的 djb2 签名算法，输出 x-ima-bkn 头。"""
    result = 5381
    for ch in token:
        result += _to_int32(_to_int32(result) << 5) + ord(ch)
    return _to_int32(result) & 0x7FFFFFFF


def _post(url: str, body: dict, headers: dict, timeout: int = 20) -> dict:
    """POST JSON；空响应 / 非 JSON 一律转成可接住的结构，绝不抛异常给调用方。"""
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    except Exception as exc:  # noqa: BLE001  DNS / 超时 / SSL
        return {"code": -1, "msg": "网络异常：%s" % exc, "fail_kind": "net"}

    if not raw.strip():
        return {"code": -1, "msg": "HTTP %s 空响应（可能被限流）" % status,
                "http_status": status, "fail_kind": "net" if status in (403, 429, 502, 503) else "biz"}
    try:
        data = json.loads(raw)
    except ValueError:
        return {"code": -1, "msg": "HTTP %s 非 JSON 响应：%s" % (status, raw[:160]),
                "http_status": status, "fail_kind": "biz"}
    if isinstance(data, dict):
        data.setdefault("http_status", status)
    return data


# ── 凭据 ────────────────────────────────────────────────────────────────────

def cred_path():
    return paths.config_path(CRED_NAME)


def state_path():
    return paths.state_path(STATE_NAME)


def load_cred() -> dict | None:
    p = cred_path()
    if not p.exists():
        return None
    try:
        cred = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    for key in ("ima_uid", "uid_type", "ima_guid", "refresh_token"):
        if not cred.get(key):
            return None
    return cred


def save_cred(cred: dict) -> None:
    p = cred_path()
    p.write_text(json.dumps(cred, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass


def _cookie_header(cred: dict, token: str) -> str:
    return "; ".join([
        "IMA-UID=%s" % cred["ima_uid"],
        "IMA-TOKEN=%s" % token,
        "IMA-REFRESH-TOKEN=%s" % cred["refresh_token"],
        "UID-TYPE=%s" % cred["uid_type"],
        "IMA-GUID=%s" % cred["ima_guid"],
        "PLATFORM=H5",
    ])


def _headers(cookie: str, token: str | None = None) -> dict:
    h = {
        "Content-Type": "application/json",
        "User-Agent": UA,
        "from_browser_ima": FROM_BROWSER_IMA,
        "extension_version": EXTENSION_VERSION,
        "x-ima-cookie": cookie,
    }
    if token:
        h["x-ima-bkn"] = str(bkn(token))
    return h


def refresh_access_token(cred: dict) -> dict:
    """长期 refresh_token → 2 小时有效的 access token。"""
    device_cookie = "IMA-GUID=%s; PLATFORM=H5" % cred["ima_guid"]
    data = _post(REFRESH_URL, {
        "userId": cred["ima_uid"],
        "refreshToken": cred["refresh_token"],
        "tokenType": -1,
    }, _headers(device_cookie))
    token = data.get("token")
    if data.get("code") == 0 and token:
        return {"ok": True, "token": token}
    return {
        "ok": False,
        "reason": "刷新登录失败：code=%s msg=%s" % (data.get("code"), data.get("msg")),
        "fail_kind": data.get("fail_kind") or "auth",   # auth = refresh_token 失效，需重新扫码
    }


# ── 业务接口 ────────────────────────────────────────────────────────────────

def get_activity_info(cookie: str, token: str) -> dict:
    return _post(ACTIVITY_INFO_URL, {}, _headers(cookie, token))


def check_in(cookie: str, token: str, checkin_type: int) -> dict:
    return _post(CHECK_IN_URL, {"type": checkin_type}, _headers(cookie, token))


def _find(info: dict, top: str) -> dict | None:
    for item in info.get("infos") or []:
        if item.get("top") == top:
            return item
    return None


def _reward(resp: dict):
    return resp.get("reward_received", resp.get("rewardReceived"))


def week_summary(info: dict) -> str:
    cells = []
    for item in info.get("infos") or []:
        top = item.get("top") or "?"
        mark = STATUS_TEXT.get(item.get("status"), "?")
        cells.append("%s:%s" % (top, mark))
    return "  ".join(cells)


# ── 状态 ────────────────────────────────────────────────────────────────────

def _today() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d")


def load_state() -> dict:
    p = state_path()
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def save_state(state: dict) -> None:
    p = state_path()
    p.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass


def _due(state: dict, key: str, now_ts: float, gap_min: int) -> bool:
    try:
        last = float(state.get(key) or 0)
    except (TypeError, ValueError):
        return True
    return last <= 0 or (now_ts - last) >= gap_min * 60


# ── 扫码登录（首次 / refresh_token 失效后）─────────────────────────────────

def _http_get_bytes(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _gen_qr() -> tuple[str, bytes]:
    """向微信开放平台申请扫码登录二维码，返回 (uuid, png_bytes)。"""
    state = uuidlib.uuid4().hex
    redirect_uri = WX_AUTH + "?v=2&r=" + urllib.parse.quote(CHECKIN_PAGE, safe="")
    url = (QRCONNECT_URL
           + "?appid=" + APPID
           + "&redirect_uri=" + urllib.parse.quote(redirect_uri, safe="")
           + "&response_type=code&scope=snsapi_login&state=" + state
           + "&login_type=jssdk&self_redirect=default#wechat_redirect")
    html = _http_get_bytes(url).decode("utf-8", "replace")
    m = (re.search(r'uuid\s*=\s*["\']([0-9a-zA-Z=_\-]+)["\']', html)
         or re.search(r'"uuid"\s*:\s*"([0-9a-zA-Z=_\-]+)"', html)
         or re.search(r"/qrcode/([0-9a-zA-Z=_\-]+)", html))
    if not m:
        raise RuntimeError("授权页里没找到 uuid，页面可能已改版（len=%d）" % len(html))
    qr_uuid = m.group(1)
    return qr_uuid, _http_get_bytes(QRCODE_URL + qr_uuid)


def _poll_code(qr_uuid: str, deadline: float) -> str | None:
    while time.time() < deadline:
        try:
            body = _http_get_bytes(POLL_URL + qr_uuid, timeout=40).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            time.sleep(2)
            continue
        m = re.search(r"wx_errcode=(\d+)", body)
        err = int(m.group(1)) if m else -1
        if err == 405:                        # 408 等待扫码 / 404 已扫待确认 → 继续轮询
            m2 = re.search(r"wx_code='([^']*)'", body)
            return (m2.group(1) if m2 else None) or None
        if err in (402, 403):                 # 二维码过期 / 被取消
            return None
        time.sleep(2)
    return None


def _login_exchange(code: str, guid: str) -> dict:
    data = _post(LOGIN_URL, {
        "accountType": ACCOUNT_TYPE_WX,
        "code": code,
        "authAppid": APPID,
        "clientInfo": {"guid": guid, "platform": 4, "qimei36": ""},
    }, _headers("IMA-GUID=%s; PLATFORM=H5" % guid))
    if data.get("code") != 0:
        raise RuntimeError("登录失败：code=%s msg=%s" % (data.get("code"), data.get("msg")))

    def pick(*names):
        for n in names:
            if data.get(n):
                return data[n]
        return None

    return {
        "token": pick("token"),
        "refresh_token": pick("refreshToken", "refresh_token"),
        "ima_uid": pick("userId", "user_id"),
        "id_type": pick("idType", "id_type"),
    }


def login(deadline_seconds: int = 300, qr_out: str | None = None,
          poll: bool = True) -> dict:
    """生成二维码 → 等扫码 → 换凭证并落盘。

    poll=False 时只生成二维码就返回（供调用方把图片拿给用户，再单独轮询）。
    """
    old = load_cred() or {}
    guid = old.get("ima_guid") or str(random.randint(10 ** 15, 10 ** 16 - 1))
    qr_path = qr_out or str(paths.cache_path("ima_login_qr.png"))

    qr_uuid, png = _gen_qr()
    with open(qr_path, "wb") as fp:
        fp.write(png)
    if not poll:
        return {"ok": False, "pending": True, "qr_path": qr_path,
                "uuid": qr_uuid, "guid": guid,
                "msg": "二维码已生成，等待扫码"}

    deadline = time.time() + deadline_seconds
    code = _poll_code(qr_uuid, deadline)
    if not code:
        return {"ok": False, "reason": "二维码过期或未扫码", "qr_path": qr_path}

    new = _login_exchange(code, guid)
    if not new.get("refresh_token") or not new.get("ima_uid"):
        return {"ok": False, "reason": "登录响应缺少必要字段"}

    cred = {
        "ima_uid": str(new["ima_uid"]),
        "uid_type": str(new.get("id_type") or old.get("uid_type") or "2"),
        "ima_guid": guid,
        "refresh_token": new["refresh_token"],
        "saved_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_cred(cred)
    return {"ok": True, "msg": "凭证已保存", "qr_path": qr_path,
            "ima_uid_tail": cred["ima_uid"][-4:]}


# ── 签到主流程 ──────────────────────────────────────────────────────────────

def _notify(title: str, content: str, level: str) -> dict:
    """通知失败绝不能影响签到结果，故整体兜住。"""
    try:
        import notify  # 延迟导入：本模块的 status 子命令不该拉起整个通知栈
        r = notify.send(title, content, level)
        return {"sent": r.get("sent"), "wechat": r.get("wechat"),
                "channel": r.get("channel"), "reason": r.get("reason")}
    except Exception as e:  # noqa: BLE001
        return {"sent": False, "reason": "notify 异常：%r" % e}


def probe(cred: dict) -> dict:
    """只读：刷新 token + 查活动状态，不做任何领取动作。"""
    r = refresh_access_token(cred)
    if not r.get("ok"):
        return {"status": "refresh_failed", "reason": r.get("reason"),
                "fail_kind": r.get("fail_kind")}
    cookie = _cookie_header(cred, r["token"])
    info = get_activity_info(cookie, r["token"])
    if info.get("code") != 0:
        return {"status": "info_failed",
                "reason": "查询活动失败：code=%s msg=%s" % (info.get("code"), info.get("msg")),
                "fail_kind": info.get("fail_kind") or "biz"}
    today = _find(info, "今日") or {}
    return {
        "status": "ok",
        "today_status": today.get("status"),
        "today_text": STATUS_TEXT.get(today.get("status"), "未知"),
        "checkin_days": info.get("checkin_days"),
        "total_reward_points": info.get("total_reward_points"),
        "week": week_summary(info),
        "_cookie": cookie,
        "_token": r["token"],
        "_info": info,
    }


FULL_DAYS = 7          # 满签所需的连续天数（7 格，每格 50 算力）


def _mk_item(body: str, done: bool, new: bool = False) -> dict:
    """给 catchup 的一条「ima 算力」条目：正文行 + 是否已完成 + 本次是否真领到。

    正文行只写结果本身（「成功，+50 算力，本周已签 7 天」），
    标题「3、ima 算力：」由 catchup 的 `_compose()` 统一加 —— 排版只有一处。
    """
    return {"body": body, "done": bool(done), "new": bool(new)}


def run_daily(force: bool = False, embed: bool = False) -> dict:
    """每次触发调用：内部自带闸门（未知凭据 / 今日已完成 / 重试间隔 / 预算）。

    embed=True 时签到结果**不自己推送**，而是在返回值的 `item` 里交给调用方，
    由 catchup 用 `_compose()` 拼成统一格式的编号消息（同一次运行只推一条）。

    `item` 在任何分支都返回：`{"body": 正文行, "done": 今日是否完成, "new": 本次是否真领到}`。
    待领取 / 失败也走同一个 item —— 三种状态用同一种排版，才一眼看得出今天差哪件。
    失败类通知（凭据缺失 / 失效 / 预算耗尽）不受 embed 影响，仍由本模块直接推送 ——
    它们是独立故障，不该藏在别人的消息里。

    返回 {"action": ..., "detail": ...}，action ∈
      done       今天已经完成（签到过）
      blocked    凭据缺失或失效，已通知用户，今天不再尝试
      waiting    闸门未到点 / 未到重试间隔
      ran        本次真实执行了签到
      exhausted  业务预算用尽，今天不再尝试
    """
    now = datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")
    now_ts = time.time()
    state = load_state()

    if state.get("day") != today:
        state = {"day": today, "done": False, "tries": 0, "last_try": 0,
                 "last_kind": "biz", "bonus_done": False, "blocked_day": None,
                 "exhausted_day": None}

    if state.get("done"):
        # 当天已完成：正文从落盘的结果回填，好让「补发快照」也能写出真实数字
        r = state.get("result") or {}
        days = r.get("checkin_days")
        body = "今日已领过"
        if days is not None:
            body += "，本周已签 {} 天".format(days)
            if int(days) >= FULL_DAYS:
                body += "（满签✓）"
        return {"action": "done", "item": _mk_item(body, done=True)}
    if state.get("blocked_day") == today:
        return {"action": "blocked", "detail": "凭据问题，已通知，今日不再尝试",
                "item": _mk_item("⚠️ 登录或授权有问题，今日不再尝试", done=False)}
    if not force and now.hour * 100 + now.minute < WINDOW_FROM:
        return {"action": "waiting", "detail": "07:00 前不签到",
                "item": _mk_item("待领取（07:00 后自动领取）", done=False)}

    cred = load_cred()
    if not cred:
        state["blocked_day"] = today
        save_state(state)
        _notify("ima 算力：还没有登录凭证",
                "\n".join([
                    "ima 每日签到需要先授权一次（ima 没有账号密码通道，只能微信扫码）。",
                    "",
                    "在这台电脑上运行：",
                    "  cd \"{}\"".format(str(paths.ROOT)),
                    "  python scripts\\ima.py login",
                    "",
                    "扫一次码即可，之后 refresh_token 自动续期，不用再扫。",
                ]), "failure")
        return {"action": "blocked", "detail": "无凭据",
                "item": _mk_item("⚠️ 未授权，需要扫码一次", done=False)}

    if int(state.get("tries") or 0) >= MAX_TRIES:
        if state.get("exhausted_day") != today:
            state["exhausted_day"] = today
            save_state(state)
            _notify("ima 算力：今天签到失败（已停止重试）",
                    "\n".join([
                        "连续 {} 次业务失败，今天不再尝试。".format(MAX_TRIES),
                        "最后一次原因：{}".format(state.get("last_reason") or "(未记录)"),
                        "",
                        "常见原因：接口改版 / ima 客户端版本过旧 / 活动规则变化。",
                        "凭据本身没问题的话，明天会自动重试。",
                    ]), "failure")
        return {"action": "exhausted",
                "item": _mk_item("今天签到失败（已停止重试）", done=False)}

    gap = NET_RETRY_GAP_MIN if state.get("last_kind") == "net" else MIN_RETRY_GAP_MIN
    if not force and not _due(state, "last_try", now_ts, gap):
        return {"action": "waiting", "detail": "未到重试间隔",
                "item": _mk_item("待领取（自动重试中）", done=False)}

    state["last_try"] = now_ts
    p = probe(cred)

    # 凭据失效：只能等用户重新扫码，通知一次即可，今天不再刷
    if p.get("status") == "refresh_failed" and p.get("fail_kind") == "auth":
        state["blocked_day"] = today
        state["last_reason"] = p.get("reason")
        save_state(state)
        _notify("ima 算力：登录已失效，需要重新扫码",
                "\n".join([
                    "ima 的长期凭据（refresh_token）失效了，签到暂时做不了。",
                    "原因：{}".format(p.get("reason")),
                    "",
                    "在这台电脑上运行下面这条命令，扫一次码即可恢复：",
                    "  cd \"{}\"".format(str(paths.ROOT)),
                    "  python scripts\\ima.py login",
                ]), "failure")
        return {"action": "blocked", "detail": "refresh_token 失效",
                "item": _mk_item("⚠️ 登录已失效，需要重新扫码", done=False)}

    if p.get("status") != "ok":
        is_net = p.get("fail_kind") == "net"
        state["last_kind"] = "net" if is_net else "biz"
        state["last_reason"] = p.get("reason")
        if not is_net:
            state["tries"] = int(state.get("tries") or 0) + 1
        save_state(state)
        return {"action": "ran", "detail": {"ok": False, "net": is_net,
                                            "reason": p.get("reason")},
                "item": _mk_item("待领取（网络异常，自动重试中）" if is_net
                                 else "失败（自动重试中）", done=False)}

    cookie, token, info = p["_cookie"], p["_token"], p["_info"]
    today_item = _find(info, "今日") or {}
    status = today_item.get("status")
    messages = []
    checked_now = False      # 今天「已签到」成立（含在别处签的）→ 满签奖励才有意义
    claimed_now = False      # 本次真的领到了 → 决定通知标题，两者不能混用
    claimed_credit = None    # 本次领到的算力数（供统一格式的正文行使用）

    if status == STATUS_AVAILABLE:
        resp = check_in(cookie, token, CHECKIN_TYPE_DAILY)
        code = resp.get("code")
        if code == CODE_OK:
            got = _reward(resp)
            claimed_credit = got
            checked_now = claimed_now = True
            messages.append("签到成功，+{} 算力".format(got) if got else "签到成功")
        elif code == CODE_ALREADY_CHECKED_IN:
            checked_now = True
            messages.append("今天已签到")
        else:
            is_net = resp.get("fail_kind") == "net"
            state["last_kind"] = "net" if is_net else "biz"
            state["last_reason"] = "签到失败：code=%s msg=%s" % (code, resp.get("msg"))
            if not is_net:
                state["tries"] = int(state.get("tries") or 0) + 1
            save_state(state)
            return {"action": "ran", "detail": {"ok": False, "net": is_net,
                                                "reason": state["last_reason"]},
                    "item": _mk_item("待领取（网络异常，自动重试中）" if is_net
                                     else "失败（自动重试中）", done=False)}
        info = get_activity_info(cookie, token)     # 重取：满签奖励状态会变
    elif status == STATUS_CLAIMED:
        checked_now = True
        messages.append("今天已签到")
    elif status == STATUS_LOCKED:
        state["last_reason"] = "今日尚未解锁（服务端状态 4）"
        state["tries"] = int(state.get("tries") or 0) + 1
        save_state(state)
        return {"action": "ran", "detail": {"ok": False, "reason": state["last_reason"]},
                "item": _mk_item("失败（今日尚未解锁，自动重试中）", done=False)}
    else:
        state["last_reason"] = "未知今日状态：%s" % status
        state["tries"] = int(state.get("tries") or 0) + 1
        save_state(state)
        return {"action": "ran", "detail": {"ok": False, "reason": state["last_reason"]},
                "item": _mk_item("失败（今日状态未知，自动重试中）", done=False)}

    # 满签奖励：只在周日试一次，其余日期静默跳过（不发请求、不打扰）
    bonus_msg = ""
    bonus = _find(info, BONUS_TITLE) or {}
    if now.weekday() == BONUS_WEEKDAY and checked_now and not state.get("bonus_done"):
        if bonus.get("status") == STATUS_AVAILABLE:
            rb = check_in(cookie, token, CHECKIN_TYPE_WEEKLY_BONUS)
            if rb.get("code") == CODE_OK:
                got = _reward(rb)
                state["bonus_done"] = True
                bonus_msg = "满签奖励 +{} 算力".format(got) if got else "满签奖励已领取"
                info = get_activity_info(cookie, token)
            elif rb.get("code") == CODE_ALREADY_CHECKED_IN:
                state["bonus_done"] = True
        elif bonus.get("status") == STATUS_CLAIMED:
            state["bonus_done"] = True

    state["done"] = True
    state["tries"] = 0
    state["last_kind"] = "biz"
    state["last_reason"] = None
    state["result"] = {
        "checkin_days": info.get("checkin_days"),
        "total_reward_points": info.get("total_reward_points"),
        "week": week_summary(info),
        "message": " | ".join(messages + ([bonus_msg] if bonus_msg else [])),
    }
    save_state(state)

    days = state["result"]["checkin_days"]
    total = state["result"]["total_reward_points"]
    # 正文行（由 catchup 的 `_compose` 加上「3、ima 算力：」标题）：
    #   成功，+50 算力，本周已签 7 天（满签✓）
    # 注意：checkin_days / total_reward_points 都是**本周**口径（7 格各 50 算力，
    # 今天领了 1 天就是 1/50），不是历史累计 —— 别写成「累计」。
    if claimed_now:
        body = "成功，+{} 算力".format(claimed_credit) if claimed_credit else "成功"
    else:
        body = "今日已领过"
    if days is not None:
        body += "，本周已签 {} 天".format(days)
        if int(days) >= FULL_DAYS:
            body += "（满签✓）"
    if bonus_msg:
        body += "，" + bonus_msg
    item = _mk_item(body, done=True, new=claimed_now)

    if embed:
        # 不自己发：把条目交回去，由 catchup 拼进当天那条统一格式的消息。
        # 兜底：若那一轮 WorkBuddy 没有任何消息要发（两项早已完成 / 网络失败静默），
        # catchup 会用同一套 `_compose` 补发一条快照，结果不会石沉大海。
        return {"action": "ran", "item": item,
                "detail": {"ok": True, "notified": False, "result": state["result"]}}

    # 手动跑（ima.py checkin）时独立成条：多给两行明细，排障用
    title = "ima 算力：签到到账" if claimed_now else "ima 算力：今日已签到"
    detail = _notify(title, "\n".join([
        body,
        "",
        "本周进度：{}".format(state["result"]["week"] or "(未返回)"),
        "本周已签 {} 天 / {} 算力".format(days, total),
    ]), "success")
    return {"action": "ran", "item": item,
            "detail": {"ok": True, "notify": detail, "result": state["result"]}}


# ── CLI ─────────────────────────────────────────────────────────────────────

def _cli() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args else "status"

    if cmd == "status":
        cred = load_cred()
        if not cred:
            print(json.dumps({"ok": False, "reason": "没有凭据，先运行 python scripts\\ima.py login"},
                             ensure_ascii=False))
            return 1
        p = probe(cred)
        if p.get("status") == "ok":
            out = {"ok": True, "today": p.get("today_text"),
                   "checkin_days": p.get("checkin_days"),
                   "total_reward_points": p.get("total_reward_points"),
                   "week": p.get("week")}
        else:
            out = {"ok": False, "reason": p.get("reason")}
        print(json.dumps(out, ensure_ascii=False))
        return 0 if out.get("ok") else 1

    if cmd == "checkin":
        force = "--force" in args
        print(json.dumps(run_daily(force=force), ensure_ascii=False, default=str))
        return 0

    if cmd == "login":
        poll = "--no-poll" not in args
        r = login(poll=poll)
        print(json.dumps(r, ensure_ascii=False))
        return 0 if r.get("ok") else 1

    print("用法: python scripts\\ima.py [status|checkin|login]  (checkin --force, login --no-poll)")
    return 2


if __name__ == "__main__":
    sys.exit(_cli())
