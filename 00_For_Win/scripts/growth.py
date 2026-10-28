#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
growth.py — WorkBuddy 成长空间：连登兑换 / 补登 / 抽奖 / 能量开盒

为什么需要这个模块（2026-10-28 接入）：
  成长空间有四种「不主动点就白丢」的收益，规则原文（活动页内嵌）逐条写着：

    · 连登兑换：「每档每月限兑 1 次，**连登天数每月清零并重新计算**」
      → 三档合计 200 积分 + 10 能量 + 3 张补登卡 + 3 次抽奖，**跨月就没了**。
    · 补登卡：「永久持有且上限 4 张，**仅可补救当月断登的天数**」
      → 卡本身不过期，但能补的范围只限当月；且余额满 4 后兑换给的卡会**溢出浪费**。
    · 抽奖：「次数通过连登兑换获得，**仅限活动有效期内使用，逾期作废**」
      → 兑换到次数后必须真的抽掉，否则等于没领。
    · 能量：「活动结束后，**未使用的能量值将作废**」；每 10 点开一只 Buddy。

  四者是一条链：兑换 → 拿到卡/能量/次数 → 用掉。链上任何一环不做，前面都是白干。
  本模块就是把这四个动作按正确的**顺序**和**门槛**自动走完。

执行顺序（顺序本身有讲究，不是随意排的）：
  ① 补登：先把手里已有的卡用掉，避免后续兑换给的卡顶到上限溢出
  ② 兑换：把 available 的档位兑掉（拿到卡/能量/抽奖次数）
  ③ 再补登：②刚给的卡如果还有断登可补，立刻用掉（回到 ① 的逻辑，最多两轮）
  ④ 抽奖：把刚兑到的次数抽掉（次数逾期作废）
  ⑤ 开盒：把能量换成 Buddy（能量活动结束作废）

补登策略（2026-10-28 按用户要求重写，取代旧的「凑档位」逻辑）：
  规则一句话：**当月漏了哪天、账上有卡，就补哪天，目的是保持连续记录。**
  不再要求「补完必须刚好跨过某个档位门槛」才动手。

  为什么旧的「gap 模型」是错的（旧实现已删，别再改回去）：
    旧逻辑把补登当成「把连登天数一天天往前推」，于是算出 gap = 档位门槛 − 当前天数，
    要求一次补齐 gap 天，gap 大于卡数就整轮放弃。但补登的真实语义是
    **「把断的那天补上，让前后两段连起来」**：月中 20 号断 1 天，补 20 号那一天，
    连登就从「21..今天」的 11 天接回「1..今天」的 31 天。按旧模型会算成 gap=17、
    卡只有 3 张 → 放弃 → 白丢整月 28 天档。所以必须**一次补一天、补完重读、再决定下一步**。

  两个护栏（都是"别把卡烧在错的地方"）：
    · 三档全兑完之后不再补：这时补登不产生任何奖励（奖励只认档位），而连登下月清零，
      补了纯亏——卡留着下月还可能救一次断登。想强行让格子连号，把
      `SPEND_CARD_AFTER_ALL_CLAIMED` 改成 True。
    · 补完连登天数没变 → 立刻暂停补登（`growth_state.json` 里的 `paused`）。
      这是「语义假设被证伪」的信号：服务端若根本不把补登算进连登，继续补只是每小时
      烧一张卡。宁可停在那儿等人看，也不能默默烧完 4 张。

接口（全部从客户端 bundle `growthSpace-*.js` 的 API 模块读出，不是猜的）：
  GET  /activity/growth/streak                  连登 + 兑换状态 + 补登卡余额
  GET  /activity/growth/heatmap                 365 天活跃分（断登判定依据）
  GET  /activity/growth/heatmap/detail?date=    某天的活跃分**构成明细**（2026-10-28 发现）
                                                → {"score":36,"actions":{"chat_request_send":
                                                  {"count":9,"score_per_count":2,"score":18}, …}}
                                                实测权重：对话 2 / 技能信息 3 / 真用专家 3 /
                                                完成任务 5 / 开盲盒 0。**签到、旅行、兑换、补登
                                                完全不产生活跃分**——所以「活跃地图」靠的是产品内
                                                使用行为，纯自动化点亮不了它。连登门槛只要 score>0。
  GET  /activity/growth/energy                  能量余额
  GET  /activity/growth/buddy/quota             开盒单价/可开次数/单次上限
  GET  /activity/growth/lottery/chances         抽奖次数余额
  GET  /activity/growth/lottery/summary         抽奖模块是否开启
  POST /activity/growth/redeem                  body {"tier","client_token"}
  POST /activity/growth/makeup-cards/use        body {"target_date"}
  POST /activity/growth/lottery/draw            body {"client_token"}
  POST /activity/growth/buddy/open              body {"count"}
  认证：Authorization: Bearer <token> + X-User-Id: <uid>

失败语义（2026-10-28 逐个实测，都不产生副作用，可以放心撞）：
  redeem     已兑过 → http=409 "该档位本月已兑换"       （幂等：视为完成，不算失败）
  draw       无次数 → http=400 "insufficient lottery chance balance"
             奖池停   → http=503（暂时性，本轮中止、下轮再来，**不要当失败告警**）
  open       能量不足 → http=400 "insufficient energy"
  makeup     该日不欠 → http=400 "date is not broken, no makeup needed"
             没卡     → http=403 "no makeup card"
             跨月     → http=400 "cannot makeup history month"
             ｜前端 bundle 里还有 "future date" / "target date before launch" 两种

成功判据一律**不看 code，看状态有没有真变**：每个写操作之后重读对应状态接口复核。
理由是 tasks.py 踩过的坑（claim 已领过的任务返回 200/code=0 但 credit=0），
这类「成功但没给东西」的响应在这里同样可能存在，靠 code 判断会把空手当丰收。
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import credentials  # noqa: E402
import http_client   # noqa: E402
import paths         # noqa: E402

STREAK_PATH = "/activity/growth/streak"
HEATMAP_PATH = "/activity/growth/heatmap"
ENERGY_PATH = "/activity/growth/energy"
QUOTA_PATH = "/activity/growth/buddy/quota"
CHANCES_PATH = "/activity/growth/lottery/chances"
LOTTERY_PATH = "/activity/growth/lottery/summary"
REDEEM_PATH = "/activity/growth/redeem"
MAKEUP_PATH = "/activity/growth/makeup-cards/use"
DRAW_PATH = "/activity/growth/lottery/draw"
OPEN_PATH = "/activity/growth/buddy/open"
BUDDY_LIST_PATH = "/activity/growth/buddy/list"

# 兑换档位。天数与奖励值来自官方规则原文与 streak 接口的 `redemption_status.tiers`，
# 两处一致；这里只把「档位名」和「门槛天数」固定下来（奖励值一律从接口读，不写死，
# 以免哪天官方调数值时本地还按旧账算）。
TIERS = ("7d", "14d", "28d")
TIER_DAYS = {"7d": 7, "14d": 14, "28d": 28}

# 兑换状态的取值。前端判断是 `status === "available"` 才允许点按钮
# （`GrowthCenterPage-*.js`: `C=b==="available"`），所以这里同样只认这一个值。
ST_AVAILABLE = "available"
ST_CLAIMED = "claimed"

# 补登策略开关（见文件头「补登策略」段）
SPEND_CARD_AFTER_ALL_CLAIMED = False   # 三档全兑后是否仍花卡保连续（默认 False＝省卡）
MAX_MAKEUP_PER_RUN = 4                 # 单次运行最多花几张卡（= 卡上限，防死循环）
STATE_NAME = "growth_state.json"       # 只存补登的异常记忆：
                                       #   paused / no_effect_dates（补了不生效）
                                       #   spent_dates（本月花过卡的日期，防重复补）

LAUNCH_DATE = "2026-06-17"   # 活动上线日；早于它的日期不计断登、也不可补登

UA = "WorkBuddy/5.3.14"      # 与 travel.py / tasks.py 保持一致


def _result(status: str, **fields) -> dict:
    out = {"task": "growth", "status": status}
    out.update(fields)
    return out


def _headers(uid: str) -> dict:
    return {"X-User-Id": uid}


# ---------------------------------------------------------------------------
# 只读接口
# ---------------------------------------------------------------------------
def _get(path: str, host: str, token: str, uid: str, timeout: int = 20) -> dict:
    code, body = http_client.get_json(
        host + path, token=token, extra_headers=_headers(uid),
        timeout=timeout, user_agent=UA)
    if code != 200 or body.get("code") != 0:
        raise RuntimeError("{} 失败：http={} code={} msg={}".format(
            path, code, body.get("code"), body.get("msg")))
    return body.get("data") or {}


def fetch_streak(host, token, uid, timeout=20) -> dict:
    return _get(STREAK_PATH, host, token, uid, timeout)


def fetch_heatmap(host, token, uid, timeout=20) -> dict:
    return _get(HEATMAP_PATH, host, token, uid, timeout)


def fetch_energy(host, token, uid, timeout=20) -> dict:
    return _get(ENERGY_PATH, host, token, uid, timeout)


def fetch_quota(host, token, uid, timeout=20) -> dict:
    return _get(QUOTA_PATH, host, token, uid, timeout)


def fetch_chances(host, token, uid, timeout=20) -> dict:
    return _get(CHANCES_PATH, host, token, uid, timeout)


def fetch_lottery(host, token, uid, timeout=20) -> dict:
    return _get(LOTTERY_PATH, host, token, uid, timeout)


def fetch_buddies(host, token, uid, timeout=20) -> list[dict]:
    return _get(BUDDY_LIST_PATH, host, token, uid, timeout).get("buddies") or []


def snapshot(host, token, uid, timeout=20) -> dict:
    """一次性读出当前全貌（只读）。任一子接口失败只让该字段为 None，不整体崩。"""
    def safe(fn):
        try:
            return fn(host, token, uid, timeout)
        except Exception as e:  # noqa: BLE001 — 缺一项不影响其余
            return {"__error__": str(e)[:160]}

    streak = safe(fetch_streak)
    rs = streak.get("redemption_status") or {}
    tiers = {}
    for t in TIERS:
        tiers[t] = {
            "status": rs.get("tier_{}_status".format(t)),
            "count": rs.get("tier_{}_count".format(t)),
            "days": TIER_DAYS[t],
        }
    return {
        "streak_days": (streak.get("streak") or {}).get("days"),
        "makeup_cards": (streak.get("makeup_cards") or {}).get("balance"),
        "makeup_cards_max": (streak.get("makeup_cards") or {}).get("max"),
        "makeup_dates": (streak.get("streak") or {}).get("makeup_dates") or [],
        "tiers": tiers,
        "tier_rewards": rs.get("tiers") or [],
        "energy": safe(fetch_energy),
        "quota": safe(fetch_quota),
        "chances": safe(fetch_chances),
        "lottery": safe(fetch_lottery),
        # 注意：活跃地图是**独立接口**，不在 streak 里。曾误把 streak.get("heatmap")
        # 当作地图数据，结果 cells 恒为空 → 整月都被判成断登。别改回去。
        "heatmap": safe(fetch_heatmap),
        "streak_error": streak.get("__error__"),
    }


# ---------------------------------------------------------------------------
# 补登：算清楚「该补哪几天」
# ---------------------------------------------------------------------------
def _month_breaks(cells: list[dict], today: datetime.date,
                  done_dates: list[str] | None = None) -> list[str]:
    """当月「今天之前、没有活跃分」的日期，从近到远。

    判定依据是前端自己的算法（`GrowthCenterPage-*.js` 的 `at()`）：
      `state = 今天 ? "today" : score > 0 ? "checked" : 更早 ? "broken" : "todo"`
    也就是说**断登 = 当月 + 日期早于今天 + score 为 0**，且不早于活动上线日。
    """
    score = {}
    for c in cells or []:
        d = c.get("date")
        if d:
            score[d] = int(c.get("score") or 0)
    done = set(done_dates or [])
    out = []
    d = today - datetime.timedelta(days=1)
    first = today.replace(day=1)
    while d >= first:
        ds = d.isoformat()
        if ds < LAUNCH_DATE:
            break
        if score.get(ds, 0) <= 0 and ds not in done:
            out.append(ds)
        d -= datetime.timedelta(days=1)
    return out


def _load_state() -> dict:
    """读补登异常记忆。文件不存在/损坏一律当空，不影响主流程。"""
    try:
        p = paths.state_path(STATE_NAME)
        if p.exists():
            data = json.loads(p.read_text() or "{}")
            if isinstance(data, dict):
                return data
    except Exception:  # noqa: BLE001
        pass
    return {}


def _save_state(st: dict) -> None:
    try:
        p = paths.state_path(STATE_NAME)
        p.write_text(json.dumps(st, ensure_ascii=False, indent=2))
        p.chmod(0o600)
    except Exception:  # noqa: BLE001 — 记不下异常不影响已经花掉的卡
        pass


def sim_streak(cells: list[dict], today: datetime.date,
               patched: set | frozenset = frozenset(),
               done: set | frozenset = frozenset()) -> int:
    """假想「补上 patched 这些天」之后，连登会是多少天。

    纯本地推演，不发任何请求 —— 这是决定「值不值得花卡」的唯一依据。

    两个前提都有实测支撑：
      · 连登**每月 1 日清零**：9 月全签 + 10 月全签，10-28 拿到的 `streak.days`
        是 28 而不是 58，`month_total_days` 也是 28。所以扫到月初就停。
      · 补过的天在活跃地图里 `score` 仍是 0（补登只写 `makeup_dates`，不回填分数），
        所以扫描时要把 `done` 也算作点亮，否则补过的天会把连登截断。
    """
    score = {}
    for c in cells or []:
        d = c.get("date")
        if d:
            score[d] = int(c.get("score") or 0)
    patched = set(patched or ())
    done = set(done or ())
    first = today.replace(day=1)
    n = 0
    d = today
    while d >= first:
        ds = d.isoformat()
        if score.get(ds, 0) > 0 or ds in patched or ds in done:
            n += 1
        else:
            break
        d -= datetime.timedelta(days=1)
    return n


def _next_unclaimed_tier(tiers: dict) -> int:
    """最近一个「状态明确且未兑」的档位天数；没有就返回 0。"""
    known = [t for t in TIERS if (tiers.get(t) or {}).get("status") is not None]
    pending = [t for t in known if tiers[t]["status"] != ST_CLAIMED]
    return min(TIER_DAYS[t] for t in pending) if pending else 0


def plan_makeup(cards: int, breaks: list[str], tiers: dict,
                cells: list[dict] | None = None,
                today: datetime.date | None = None,
                done: set | frozenset = frozenset(),
                ) -> tuple[list[str], str]:
    """决定这一轮要补哪几天。返回 (日期列表, 说明)。

    2026-10-28 第二版。第一版是「见断就补、一天一补」，方向对但漏了两种情况会白花卡：

      A. 卡不够补满当月所有断点 → 补一部分也接不上，连登并不会真正延长。
         例：卡 2 张、断 3 天连续 → 补 2 天，第 3 天还黑，连登只有 3 天，等于没补。
      B. 卡够但补完仍够不到任何未兑档位 → 奖励拿不到，卡留着下月更值。

    所以改成先算再花：

      卡够补满所有断点        → 全补（这是用户要的「保连续」：月初补 1 天，
                                后面一路点亮才不会被这个断点截断）
      卡不够                  → 推演「补完最近 cards 天后连登几天」，
                                能跨过最近未兑档位才补，否则一张不花

    为什么永远从最近的断点往前补：连登是「从今天往前连续点亮」的长度，补最近的
    那天才可能立刻延长它；补远处的孤立断点对当前连登毫无影响。
    ★ 这不只是为了好看——补远处的那天 `streak.days` 不会变，会被下面的
      「补完连登没增加 → paused」护栏误判成「补登不计入连登」，把功能整个停掉。
    """
    if cards <= 0:
        return [], "没有补登卡"
    pending = list(breaks or [])
    if not pending:
        return [], "当月没有可补的断登日"
    if not SPEND_CARD_AFTER_ALL_CLAIMED:
        # 三档全兑后补登不产生任何奖励（奖励只认档位，连登下月清零），此时花卡纯亏。
        # 只有「状态明确且全部 claimed」才敢下这个判断；状态未知时不拦（宁可花卡保连续）。
        known = [t for t in TIERS if (tiers.get(t) or {}).get("status") is not None]
        if known and all((tiers.get(t) or {}).get("status") == ST_CLAIMED for t in known):
            return [], "本月三档已兑换，补登不再产生奖励，卡留到下月"

    if cards >= len(pending):
        return pending[:cards], "补 {} 天补满本月断点，连登接上".format(len(pending))

    # 卡不够：只有「部分补完确实能拿到档位」才动手
    if cells is None or today is None:
        return [], "卡只能补 {} 天、断点有 {} 天，缺推演数据，不补".format(
            cards, len(pending))
    tier = _next_unclaimed_tier(tiers)
    after = sim_streak(cells, today, patched=set(pending[:cards]), done=done)
    if tier and after >= tier:
        return pending[:cards], "卡只够补 {} 天，但补完连登能到 {} 天（跨过 {} 天档）".format(
            cards, after, tier)
    return [], "卡只够补 {} 天、断点有 {} 天，补完连登才 {} 天（{} 档要 {} 天），不花".format(
        cards, len(pending), after, tier or "下一", tier or "更高")


# ---------------------------------------------------------------------------
# 写操作（每次写完都重读复核）
# ---------------------------------------------------------------------------
def redeem_tier(host, token, uid, tier: str, timeout=20) -> tuple[str, dict]:
    """兑换一个档位。返回 (判定, 明细)：

      "redeemed" → 兑换成功（复核状态已变 claimed）
      "already"  → 本月已兑（409，幂等，不算失败）
      "not_yet"  → 服务端认为还没达标（不该发生，本地是照 available 挑的）
      "error"    → 其他失败
    """
    body = {"tier": tier, "client_token": "redeem-{}-{}".format(
        tier, datetime.date.today().strftime("%Y-%m"))}
    try:
        code, resp = http_client.post_json(
            host + REDEEM_PATH, body=body, token=token,
            extra_headers=_headers(uid), timeout=timeout, user_agent=UA)
    except Exception as e:  # noqa: BLE001
        return "error", {"reason": "请求异常：{}".format(str(e)[:160])}

    msg = str(resp.get("msg") or "")
    if code == 409 or "已兑换" in msg:
        return "already", {"reason": msg}
    if code != 200 or resp.get("code") != 0:
        return "not_yet" if code == 400 else "error", {
            "reason": "http={} code={} msg={}".format(code, resp.get("code"), msg)}

    # 复核：状态真的变成 claimed 才算兑到（不看 code）
    try:
        rs = fetch_streak(host, token, uid, timeout).get("redemption_status") or {}
        now_status = rs.get("tier_{}_status".format(tier))
    except Exception as e:  # noqa: BLE001 — 复核失败按成功处理，下轮会自然纠正
        return "redeemed", {"verified": False, "reason": str(e)[:120]}
    if now_status == ST_CLAIMED:
        return "redeemed", {"verified": True}
    return "error", {"reason": "接口返回成功但状态仍是 {}（未兑到）".format(now_status)}


def use_makeup_card(host, token, uid, target_date: str, timeout=20) -> tuple[str, dict]:
    """补登某一天。返回 "made" / "not_broken" / "no_card" / "error"。"""
    try:
        code, resp = http_client.post_json(
            host + MAKEUP_PATH, body={"target_date": target_date}, token=token,
            extra_headers=_headers(uid), timeout=timeout, user_agent=UA)
    except Exception as e:  # noqa: BLE001
        return "error", {"reason": "请求异常：{}".format(str(e)[:160])}

    msg = str(resp.get("msg") or "")
    low = msg.lower()
    if code == 403 and "no makeup card" in low:
        return "no_card", {"reason": msg}
    if code == 400 and "not broken" in low:
        return "not_broken", {"reason": msg}
    if code != 200 or resp.get("code") != 0:
        return "error", {"reason": "http={} code={} msg={}".format(code, resp.get("code"), msg)}
    return "made", {}


def draw_once(host, token, uid, timeout=20) -> tuple[str, dict]:
    """抽一次奖。返回 "won" / "no_chance" / "disabled" / "unavailable" / "error"。

    "unavailable"（http 503）是奖池暂时不可用，属于**暂时性**故障：本轮应停止继续抽，
    但不该当成告警或失败上报（下次巡检自然重试）。
    """
    try:
        code, resp = http_client.post_json(
            host + DRAW_PATH, body={"client_token": str(uuid.uuid4())}, token=token,
            extra_headers=_headers(uid), timeout=timeout, user_agent=UA)
    except Exception as e:  # noqa: BLE001
        return "error", {"reason": "请求异常：{}".format(str(e)[:160])}

    msg = str(resp.get("msg") or "")
    low = msg.lower()
    if code == 503:
        return "unavailable", {"reason": msg or "奖池暂时不可用"}
    if code == 400 and "insufficient lottery chance" in low:
        return "no_chance", {"reason": msg}
    if code == 400 and "disabled" in low:
        return "disabled", {"reason": msg}
    if code != 200 or resp.get("code") != 0:
        return "error", {"reason": "http={} code={} msg={}".format(code, resp.get("code"), msg)}

    d = resp.get("data") or {}
    return "won", {"prize_code": d.get("prize_code"),
                   "prize_name": d.get("prize_name"),
                   "prize_type": d.get("prize_type"),
                   "reward_id": d.get("reward_id")}


def open_box(host, token, uid, count: int, timeout=20) -> tuple[str, dict]:
    """开 count 个盲盒。返回 "opened" / "no_energy" / "error"。"""
    try:
        code, resp = http_client.post_json(
            host + OPEN_PATH, body={"count": count}, token=token,
            extra_headers=_headers(uid), timeout=timeout, user_agent=UA)
    except Exception as e:  # noqa: BLE001
        return "error", {"reason": "请求异常：{}".format(str(e)[:160])}

    msg = str(resp.get("msg") or "")
    if code == 400 and "insufficient energy" in msg.lower():
        return "no_energy", {"reason": msg}
    if code != 200 or resp.get("code") != 0:
        return "error", {"reason": "http={} code={} msg={}".format(code, resp.get("code"), msg)}
    return "opened", resp.get("data") or {}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def current_streak_days(host: str | None = None, timeout: int = 20) -> int | None:
    """本月连登天数（与兑换 / 补登同源）。读不到返回 None，绝不抛异常。

    为什么不复用 `snapshot()`：通知标题只要这一个数字，而 snapshot 会顺带读
    能量 / 次数 / 奖池 / 在岗猫 —— 为了显示一个整数发四次请求不划算。

    注意它和签到接口的 `streak_days` **不是同一个数**：签到的是跨月累计连签，
    这里是本月连登（每月 1 日清零）。例：10-28 签到报 29 天，这里是 28。
    """
    try:
        cred = credentials.load_credentials()
        if host is None:
            import travel
            host = travel.resolve_host(cred)
        data = fetch_streak(host, cred.get("access_token", ""),
                            cred.get("uid", ""), timeout)
        days = (data.get("streak") or {}).get("days")
        return int(days) if days is not None else None
    except Exception:  # noqa: BLE001 — 只影响一句话里的一个数字，不值得让通知失败
        return None


def _today_from_heatmap(heat: dict, fallback: datetime.date) -> datetime.date:
    """用服务端 heatmap.today.date 当"今天"，比自己机器的钟可信（活动时区 Asia/Shanghai）。"""
    raw = ((heat or {}).get("today") or {}).get("date")
    if raw:
        try:
            return datetime.date.fromisoformat(raw)
        except ValueError:
            pass
    return fallback


def run_daily(host: str | None = None, dry_run: bool = False,
              max_lottery: int = 5, timeout: int = 20) -> dict:
    """一次完整兑现：补登 → 兑换 → 再补登 → 抽奖 → 开盒。

    幂等：已兑的不再兑（409 视作完成）、已补的不再补（不在断登列表里）、
    没次数/没能量直接跳过。可被高频调用，调用方负责节流（catchup 里 60 分钟一次）。
    """
    try:
        cred = credentials.load_credentials()
    except credentials.CredentialError as e:
        return _result("failed", reason="登录态读取失败：" + str(e))
    if host is None:
        import travel   # 惰性导入：host 解析复用 travel（它已有探测与缓存）
        host = travel.resolve_host(cred)
    token = cred.get("access_token", "")
    uid = cred.get("uid", "")

    snap = snapshot(host, token, uid, timeout)
    if snap.get("streak_error"):
        return _result("failed", reason="连登状态读取失败：" + str(snap["streak_error"]))

    today = _today_from_heatmap(snap.get("heatmap"), datetime.date.today())
    reward_of = {r.get("tier"): r for r in (snap.get("tier_rewards") or [])}
    result = {"redeemed": [], "madeup": [], "wins": [], "opened": None,
              "physical": [], "skipped": [], "errors": []}

    def _redeem_row(tier: str) -> dict:
        rw = reward_of.get(tier) or {}
        return {"tier": tier,
                "credit": int(rw.get("credit") or 0),
                "energy": int(rw.get("energy") or 0),
                "cards": int(rw.get("cards") or 0),
                "chances": int(rw.get("chances") or 0)}

    # ---- ①② 补登 → 兑换，最多两轮（兑换给的卡可能再补一天）----
    cards_used = 0
    gstate = _load_state()
    gstate_dirty = False

    for _round in range(2):
        streak = fetch_streak(host, token, uid, timeout)
        rs = streak.get("redemption_status") or {}
        tiers = {t: {"status": rs.get("tier_{}_status".format(t))} for t in TIERS}
        cards = (streak.get("makeup_cards") or {}).get("balance") or 0
        days = (streak.get("streak") or {}).get("days") or 0

        # ① 补登：先算这一轮该补哪几天（plan_makeup），再一天一补、补完重读
        made = 0
        # 三种日期一律不再碰：补过但服务端不认的、本月已经花过卡的、本轮刚试过的。
        # 第二种是兜底：万一服务端"收卡但格子还是黑的"，没有它就会每次巡检再补一遍。
        month = today.strftime("%Y-%m")
        spent = [d for d in (gstate.get("spent_dates") or []) if str(d)[:7] == month]
        tried = set(gstate.get("no_effect_dates") or []) | set(spent)
        if gstate.get("paused"):
            result["skipped"].append("补登：已暂停（此前补登未让连登增加，等人工确认）")
        elif cards <= 0:
            result["skipped"].append("补登：没有补登卡")
        else:
            while made < MAX_MAKEUP_PER_RUN:
                # 每补一天都重读：卡余额、连登天数、断点集合都会变，不能拿旧值推演
                try:
                    streak = fetch_streak(host, token, uid, timeout)
                    heat = fetch_heatmap(host, token, uid, timeout)
                except Exception as e:  # noqa: BLE001
                    result["errors"].append("读连登/活跃地图失败：" + str(e)[:120])
                    break
                rs = streak.get("redemption_status") or {}
                tiers = {t: {"status": rs.get("tier_{}_status".format(t))} for t in TIERS}
                cards = (streak.get("makeup_cards") or {}).get("balance") or 0
                days_before = (streak.get("streak") or {}).get("days") or 0
                today = _today_from_heatmap(heat, today)
                done = set((streak.get("streak") or {}).get("makeup_dates") or [])
                breaks = [d for d in _month_breaks(heat.get("cells"), today, done)
                          if d not in tried]
                plan, why = plan_makeup(cards, breaks, tiers,
                                        heat.get("cells"), today, done)
                if not plan:
                    if why:
                        result["skipped"].append("补登：" + why)
                    break
                if dry_run:
                    # 空跑：把整份计划列出来（预演是本地算的，不预测服务端行为）
                    for ds in plan:
                        result["madeup"].append({"date": ds, "dry_run": True})
                    result["skipped"].append("补登：" + why)
                    break
                ds = plan[0]

                verdict, detail = use_makeup_card(host, token, uid, ds, timeout)
                if verdict == "made":
                    made += 1
                    cards_used += 1
                    gstate["spent_dates"] = sorted(
                        {d for d in (set(spent) | set(gstate.get("spent_dates") or []) | {ds})
                         if str(d)[:7] == month})
                    tried.add(ds)
                    gstate_dirty = True
                    # 复核：补登必须让连登天数增加。不增加 = 「补登计入连登」这个前提被证伪，
                    # 继续补只会每小时烧一张卡，所以立刻暂停并上报（见文件头护栏说明）。
                    try:
                        after = (fetch_streak(host, token, uid, timeout).get("streak")
                                 or {}).get("days")
                    except Exception:  # noqa: BLE001
                        after = None
                    result["madeup"].append({"date": ds, "streak_before": days_before,
                                             "streak_after": after})
                    if after is not None and after <= days_before:
                        tried.add(ds)
                        gstate["no_effect_dates"] = sorted(
                            set(gstate.get("no_effect_dates") or []) | {ds})
                        gstate["paused"] = True
                        gstate_dirty = True
                        result["errors"].append(
                            "补登 {} 成功但连登没增加（{}→{}），已暂停补登，"
                            "需人工确认补登是否计入连登".format(ds, days_before, after))
                        break
                elif verdict == "no_card":
                    result["skipped"].append("补登：卡已用完")
                    break
                elif verdict == "not_broken":
                    tried.add(ds)     # 服务端认为不欠 → 换下一个日期，别原地重试
                    continue
                else:
                    result["errors"].append("补登 {}：{}".format(ds, detail.get("reason")))
                    break

        if made:
            # 补登直接改变连登天数 → 必须重读，否则用补登前的档位判断会**漏兑**
            # （踩过：补完够 7 天了，7d 档却因为旧 tiers 里还是 locked 而没兑）。
            try:
                streak = fetch_streak(host, token, uid, timeout)
                rs = streak.get("redemption_status") or {}
                tiers = {t: {"status": rs.get("tier_{}_status".format(t))} for t in TIERS}
                days = (streak.get("streak") or {}).get("days") or 0
            except Exception as e:  # noqa: BLE001
                result["errors"].append("补登后重读状态失败：" + str(e)[:120])
                break
            # 防「重复补同一天，把卡烧光」：本模块依赖服务端把补过的日期记进
            # `streak.makeup_dates`（前端也是拿它标记已补格子）。若服务端没记，
            # 要么 score 变了、要么两者都没变 —— 后者会让下次巡检再补一次。
            # 这里只做**可见性**校验，不改行为：真出问题时日志里能一眼看到。
            if not dry_run:
                done_dates = set((streak.get("streak") or {}).get("makeup_dates") or [])
                missing = [m["date"] for m in result["madeup"]
                           if not m.get("dry_run") and m["date"] not in done_dates]
                if missing:
                    result["errors"].append(
                        "补登成功但 makeup_dates 未包含 {}（可能重复补，需人工确认）".format(
                            "/".join(missing)))

        # ② 兑换
        wanted = [t for t in TIERS if tiers[t]["status"] == ST_AVAILABLE]
        if not wanted:
            if _round == 0:
                pend = [t for t in TIERS
                        if tiers[t]["status"] not in (ST_CLAIMED, None)]
                if pend:
                    result["skipped"].append(
                        "兑换：{} 尚未达标（连登 {} 天）".format("/".join(pend), days))
            break
        got = 0
        for tier in wanted:
            if dry_run:
                result["redeemed"].append(dict(_redeem_row(tier), dry_run=True))
                got += 1
                continue
            verdict, detail = redeem_tier(host, token, uid, tier, timeout)
            if verdict == "redeemed":
                result["redeemed"].append(_redeem_row(tier))
                got += 1
            elif verdict == "already":
                pass          # 幂等：别人/前端刚兑过，不是失败
            elif verdict == "not_yet":
                result["skipped"].append("兑换 {}：服务端认为未达标".format(tier))
            else:
                result["errors"].append("兑换 {}：{}".format(tier, detail.get("reason")))
        if not got:
            break             # 这一轮没兑到任何东西 → 再循环也没意义

    # ---- ④ 抽奖 ----
    try:
        enabled = ((snap.get("lottery") or {}).get("module") or {}).get("enabled")
        left = (fetch_chances(host, token, uid, timeout).get("balance") or 0)
        if left and enabled is not False:
            for _ in range(min(int(left), max_lottery)):
                if dry_run:
                    result["wins"].append({"dry_run": True})
                    continue
                verdict, detail = draw_once(host, token, uid, timeout)
                if verdict == "won":
                    win = {"name": detail.get("prize_name"),
                           "type": detail.get("prize_type")}
                    result["wins"].append(win)
                    if detail.get("prize_type") == "physical":
                        result["physical"].append({
                            "name": detail.get("prize_name"),
                            "reward_id": detail.get("reward_id")})
                elif verdict == "unavailable":
                    result["skipped"].append("抽奖：奖池暂时不可用，下轮再试")
                    break
                elif verdict in ("no_chance", "disabled"):
                    break
                else:
                    result["errors"].append("抽奖：" + str(detail.get("reason")))
                    break
    except Exception as e:  # noqa: BLE001 — 抽奖失败不影响开盒
        result["errors"].append("抽奖流程失败：" + str(e)[:120])

    # ---- ⑤ 开盒 ----
    try:
        quota = fetch_quota(host, token, uid, timeout)
        cost = int(quota.get("cost_per_open") or 10)
        balance = int(quota.get("balance") or 0)
        per_call = int(quota.get("max_open_count") or 5)
        can = balance // cost if cost > 0 else 0
        if can > 0:
            before = {b.get("instance_id") for b in fetch_buddies(host, token, uid, timeout)}
            done = 0
            while done < can:
                n = min(can - done, per_call)
                if dry_run:
                    done += n
                    break
                verdict, detail = open_box(host, token, uid, n, timeout)
                if verdict != "opened":
                    if verdict == "no_energy":
                        break
                    result["errors"].append("开盒：" + str(detail.get("reason")))
                    break
                done += n
            if dry_run:
                result["opened"] = {"count": done, "dry_run": True}
            else:
                new = [b for b in fetch_buddies(host, token, uid, timeout)
                       if b.get("instance_id") not in before]
                result["opened"] = {
                    "count": done,
                    "new": [{"name": b.get("name"), "rarity": b.get("rarity")} for b in new],
                }
        else:
            result["skipped"].append("开盒：能量 {} / 每次 {}，不足".format(balance, cost))
    except Exception as e:  # noqa: BLE001
        result["errors"].append("开盒流程失败：" + str(e)[:120])

    # ---- 汇总 ----
    if gstate_dirty:
        _save_state(gstate)
    # 只统计**确定**的进项（兑换档位）。抽奖的积分不做二次换算：奖品名是
    # "10 积分" 这种自然语言，从字符串里抠数字太脆，宁可在文案里原样列出。
    real = [r for r in result["redeemed"] if not r.get("dry_run")]
    credit = sum(r.get("credit", 0) for r in real)
    energy = sum(r.get("energy", 0) for r in real)
    got = bool(result["redeemed"] or result["madeup"] or result["wins"]
               or (result["opened"] and result["opened"].get("count")))
    return _result(
        "ok" if not result["errors"] else ("partial" if got else "failed"),
        redeemed=result["redeemed"],
        madeup=result["madeup"],
        cards_used=cards_used,
        wins=result["wins"],
        opened=result["opened"],
        physical=result["physical"],
        credit=credit,
        energy=energy,
        changed=got,
        skipped=result["skipped"],
        errors=result["errors"],
    )


# ---------------------------------------------------------------------------
# CLI（排查用）
# ---------------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    try:
        cred = credentials.load_credentials()
    except credentials.CredentialError as e:
        print(json.dumps(_result("failed", reason=str(e)), ensure_ascii=False))
        return 1
    import travel
    host = travel.resolve_host(cred)
    token, uid = cred.get("access_token", ""), cred.get("uid", "")

    if cmd == "status":
        try:
            snap = snapshot(host, token, uid)
        except Exception as e:  # noqa: BLE001
            print(json.dumps(_result("failed", reason=str(e)[:200]), ensure_ascii=False))
            return 1
        today = _today_from_heatmap(snap.get("heatmap"), datetime.date.today())
        try:
            breaks = _month_breaks((snap.get("heatmap") or {}).get("cells"), today,
                                   snap.get("makeup_dates"))
        except Exception:  # noqa: BLE001
            breaks = []
        tiers = snap.get("tiers") or {}
        done = set(snap.get("makeup_dates") or [])
        st = _load_state()
        banned = set(st.get("no_effect_dates") or [])
        breaks = [d for d in breaks if d not in banned]
        plan, why = plan_makeup(snap.get("makeup_cards") or 0, breaks, tiers,
                                (snap.get("heatmap") or {}).get("cells"),
                                today, done)
        print(json.dumps(_result(
            "ok",
            streak_days=snap.get("streak_days"),
            tiers=tiers,
            tier_rewards=snap.get("tier_rewards"),
            makeup_cards=snap.get("makeup_cards"),
            makeup_cards_max=snap.get("makeup_cards_max"),
            makeup_dates=snap.get("makeup_dates"),
            month_breaks=breaks,
            makeup_plan={"dates": plan, "why": why},
            streak_if_patched=sim_streak((snap.get("heatmap") or {}).get("cells"),
                                          today, set(plan), done),
            makeup_paused=bool(st.get("paused")),
            energy=(snap.get("energy") or {}).get("balance"),
            quota=snap.get("quota"),
            chances=(snap.get("chances") or {}).get("balance"),
            lottery=(snap.get("lottery") or {}).get("module"),
        ), ensure_ascii=False))
        return 0

    if cmd == "run":
        dry = "--dry-run" in argv
        r = run_daily(host, dry_run=dry)
        print(json.dumps(r, ensure_ascii=False))
        return 0 if r.get("status") in ("ok", "partial") else 1

    print(json.dumps(_result("failed", reason="未知子命令（status / run [--dry-run]）"),
                     ensure_ascii=False))
    return 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
