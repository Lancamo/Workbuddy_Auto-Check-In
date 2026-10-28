#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_growth_makeup.py —— 补登策略桩测（离线，不碰真实账号、不发请求）

覆盖 2026-10-28 重写的「断一天补一天、保连续」策略：

  S1 月中断 1 天 + 3 张卡      → 补 1 天，连登 11 → 21，随后巅峰档可兑
  S2 补完连登不增加（服务端不认）→ 只花 1 张卡，paused 落盘
  S3 连断 3 天但只有 2 张卡     → 补最近 2 天，卡尽即停，不多花
  S4 三档已全兑 + 有断点        → 一张都不补（省卡）
  S5 dry-run                  → 只报下一天，零写操作
  S6 服务端收卡但格子还是黑的   → 同一次运行不原地重复，跨运行也不再碰（spent_dates）
  S7 plan_makeup / sim_streak  → 连登推演 / 卡不够时够档才补 / 三档全兑 / 状态未知

用法：python3 verify_growth_makeup.py
退出码：0 = 全部通过；1 = 有用例失败
"""

from __future__ import annotations

import datetime
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))

import growth  # noqa: E402

TODAY = datetime.date(2026, 11, 21)
MONTH = "2026-11"

FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print("  ✅ " + name)
    else:
        print("  ❌ " + name + ("：" + detail if detail else ""))
        FAILED.append(name)


# ---------------------------------------------------------------------------
# 假服务端：只实现补登链路用到的接口，其余一律返回"没有可在做的事"
# ---------------------------------------------------------------------------
class Fake:
    def __init__(self, zero_days, cards, tiers, streak_before, streak_after=None,
                 flat_streak=False):
        self.zero = set(zero_days)          # 本月的断登日期（服务端的账）
        self.cards = cards
        self.tiers = tiers                  # {"7d": "claimed"/"available"/...}
        self.days = streak_before
        # 补一天之后连登变成多少：默认先跳到 streak_after（模拟"接回整段"），
        # 之后每天 +1（模拟继续往前接）。flat_streak=True 模拟"服务端根本不认补登"。
        self.days_after = streak_before if streak_after is None else streak_after
        self.flat_streak = flat_streak
        self.makeup_dates = set()
        self.writes = []                    # [(动作, 日期)]
        self.forget_makeup = False          # True = 收卡但不改格子（模拟服务端不记账）

    # ---- 只读 ----
    def cells(self):
        out, d = [], TODAY.replace(day=1)
        while d <= TODAY:
            ds = d.isoformat()
            lit = ds not in self.zero or ds in self.makeup_dates
            out.append({"date": ds, "score": 2 if lit else 0})
            d += datetime.timedelta(days=1)
        return out

    def fetch_streak(self, *a, **k):
        return {
            "redemption_status": {"tier_{}_status".format(t): self.tiers.get(t)
                                  for t in growth.TIERS},
            "makeup_cards": {"balance": self.cards, "max": 4},
            "streak": {"days": self.days, "makeup_dates": sorted(self.makeup_dates)},
        }

    def fetch_heatmap(self, *a, **k):
        return {"today": {"date": TODAY.isoformat(), "score": 2}, "cells": self.cells()}

    def fetch_chances(self, *a, **k):
        return {"balance": 0}

    def fetch_quota(self, *a, **k):
        return {"balance": 0, "cost_per_open": 10, "max_open_count": 5}

    def fetch_buddies(self, *a, **k):
        return []

    def snapshot(self, *a, **k):
        return {"streak_days": self.days,
                "makeup_cards": self.cards,
                "makeup_cards_max": 4,
                "makeup_dates": sorted(self.makeup_dates),
                "tiers": {t: {"status": self.tiers.get(t)} for t in growth.TIERS},
                "tier_rewards": [{"tier": t, "credit": 0, "energy": 0, "cards": 0,
                                  "chances": 0} for t in growth.TIERS],
                "energy": {"balance": 0}, "quota": {"balance": 0},
                "chances": {"balance": 0}, "lottery": {"module": {"enabled": False}},
                "heatmap": self.fetch_heatmap(), "streak_error": None}

    # ---- 写 ----
    def use_makeup_card(self, host, token, uid, ds, timeout=20):
        self.writes.append(("makeup", ds))
        if self.cards <= 0:
            return "no_card", {"reason": "no makeup card"}
        if ds not in self.zero:
            return "not_broken", {"reason": "date is not broken, no makeup needed"}
        self.cards -= 1
        if not self.forget_makeup:
            self.zero.discard(ds)
            self.makeup_dates.add(ds)
        if not self.flat_streak:
            self.days = self.days_after if self.days < self.days_after else self.days + 1
        return "made", {}

    def redeem_tier(self, host, token, uid, tier, timeout=20):
        self.writes.append(("redeem", tier))
        self.tiers[tier] = "claimed"
        return "redeemed", {"verified": True}

    def draw_once(self, *a, **k):
        return "no_chance", {}

    def open_box(self, *a, **k):
        return "no_energy", {}


# ---------------------------------------------------------------------------
# 装桩：只替换 growth 模块里"会出网"的函数，策略代码原样跑
# ---------------------------------------------------------------------------
def install(fake: Fake, state: dict) -> None:
    growth.credentials.load_credentials = lambda: {"access_token": "t", "uid": "u"}
    growth._today_from_heatmap = lambda heat, fallback: TODAY
    for name in ("fetch_streak", "fetch_heatmap", "fetch_chances", "fetch_quota",
                 "fetch_buddies", "snapshot"):
        setattr(growth, name, getattr(fake, name))
    growth.use_makeup_card = fake.use_makeup_card
    growth.redeem_tier = fake.redeem_tier
    growth.draw_once = fake.draw_once
    growth.open_box = fake.open_box
    growth._load_state = lambda: state
    growth._save_state = lambda st: None      # 就地改同一个 dict，断言直接看它


def run(fake: Fake, state: dict, **kw) -> dict:
    install(fake, state)
    return growth.run_daily(host="https://example.invalid", **kw)


ALL_CLAIMED = {"7d": "claimed", "14d": "claimed", "28d": "claimed"}
# 28d 用 "locked"（还没达标），不要用 None —— None 在本模块里的语义是"接口没给这个字段"，
# 而"全部 known 都已 claimed"会被判成省卡（这张嘴在 S4 才有意义）。
MID_MONTH = {"7d": "claimed", "14d": "claimed", "28d": "locked"}


def wrap(tiers: dict) -> dict:
    """把 {档位: 状态} 包成接口/CLI 里的形状 {档位: {"status": 状态}}。"""
    return {t: {"status": v} for t, v in tiers.items()}


# ---------------------------------------------------------------------------
# S1 月中断 1 天 → 1 张卡接回整段
# ---------------------------------------------------------------------------
def s1() -> None:
    print("S1 月中断 1 天（11-10）+ 3 张卡：补 1 天，连登 11 → 21")
    fake = Fake(zero_days=["2026-11-10"], cards=3, tiers=dict(MID_MONTH),
                streak_before=11, streak_after=21)
    state = {}
    res = run(fake, state)

    check("只花 1 张卡", res["cards_used"] == 1, "cards_used={}".format(res["cards_used"]))
    check("补的正是最近断点 11-10",
          [m["date"] for m in res["madeup"]] == ["2026-11-10"],
          json.dumps(res["madeup"], ensure_ascii=False))
    check("记录里带连登 11 → 21",
          res["madeup"] and res["madeup"][0].get("streak_before") == 11
          and res["madeup"][0].get("streak_after") == 21)
    check("卡余额剩 2", fake.cards == 2, "cards={}".format(fake.cards))
    check("没有报错", not res["errors"], json.dumps(res["errors"], ensure_ascii=False))
    check("paused 未被置位", not state.get("paused"))
    # 服务端 28d 档此时应已达标；本用例只验补登，档位状态变化不在断言内


# ---------------------------------------------------------------------------
# S2 补了但连登不增加 → 花 1 张就停 + paused
# ---------------------------------------------------------------------------
def s2() -> None:
    print("S2 连断 3 天 + 4 张卡，但补登不改变连登（前提被证伪）")
    fake = Fake(zero_days=["2026-11-10", "2026-11-11", "2026-11-12"], cards=4,
                tiers=dict(MID_MONTH), streak_before=8, flat_streak=True)
    state = {}
    res = run(fake, state)

    check("只花 1 张卡就停", res["cards_used"] == 1, "cards_used={}".format(res["cards_used"]))
    check("paused 已置位", state.get("paused") is True)
    check("失败日期进 no_effect_dates",
          "2026-11-12" in (state.get("no_effect_dates") or []),
          json.dumps(state, ensure_ascii=False))
    check("有明确报错说明", any("连登没增加" in e for e in res["errors"]),
          json.dumps(res["errors"], ensure_ascii=False))

    # 第二次运行：已暂停 → 一张都不补
    res2 = run(fake, state)
    check("再次运行不再花卡", res2["cards_used"] == 0, "cards_used={}".format(res2["cards_used"]))
    check("提示已暂停", any("已暂停" in s for s in res2["skipped"]),
          json.dumps(res2["skipped"], ensure_ascii=False))


# ---------------------------------------------------------------------------
# S3 卡不够补满时：够档才补，不够档一张不花
# ---------------------------------------------------------------------------
def s3() -> None:
    print("S3 连断 3 天，卡只够补 2 天")
    # 3a：最近未兑是 28d 档，补 2 天只能到 11 天 → 补了也拿不到，一张不花
    fake = Fake(zero_days=["2026-11-10", "2026-11-11", "2026-11-12"], cards=2,
                tiers=dict(MID_MONTH), streak_before=8, streak_after=9)
    state = {}
    res = run(fake, state)
    check("a 够不到档位 → 零写", res["cards_used"] == 0,
          "cards_used={}".format(res["cards_used"]))
    check("a 给出省卡理由",
          any("断点有 3 天" in s for s in res["skipped"]),
          json.dumps(res["skipped"], ensure_ascii=False))
    check("a 卡没动", fake.cards == 2, "cards={}".format(fake.cards))

    # 3b：7d 档还没兑，补 2 天后连登能到 11 天 ≥ 7 → 值得补，卡尽即停
    fake2 = Fake(zero_days=["2026-11-10", "2026-11-11", "2026-11-12"], cards=2,
                 tiers={"7d": "available", "14d": "claimed", "28d": "claimed"},
                 streak_before=8, streak_after=9)
    res2 = run(fake2, {})
    got = [m["date"] for m in res2["madeup"]]
    check("b 够得到 7d 档 → 补 2 天", len(got) == 2, json.dumps(got))
    check("b 从最近的开始（11-12 先补，再 11-11）",
          got == ["2026-11-12", "2026-11-11"], json.dumps(got))
    check("b 卡已用尽", fake2.cards == 0, "cards={}".format(fake2.cards))
    check("b 没有报错", not res2["errors"], json.dumps(res2["errors"], ensure_ascii=False))


# ---------------------------------------------------------------------------
# S4 三档全兑 → 省卡
# ---------------------------------------------------------------------------
def s4() -> None:
    print("S4 三档已全兑 + 有断点 + 有卡：一张都不补")
    fake = Fake(zero_days=["2026-11-10"], cards=3, tiers=dict(ALL_CLAIMED),
                streak_before=11, streak_after=21)
    state = {}
    res = run(fake, state)

    check("零写操作", not fake.writes, json.dumps(fake.writes))
    check("卡没动", fake.cards == 3, "cards={}".format(fake.cards))
    check("给出了省卡的理由", any("三档已兑换" in s for s in res["skipped"]),
          json.dumps(res["skipped"], ensure_ascii=False))


# ---------------------------------------------------------------------------
# S5 dry-run
# ---------------------------------------------------------------------------
def s5() -> None:
    print("S5 dry-run：只报下一天，不写")
    fake = Fake(zero_days=["2026-11-10"], cards=3, tiers=dict(MID_MONTH),
                streak_before=11, streak_after=21)
    state = {}
    res = run(fake, state, dry_run=True)

    check("零写操作", not fake.writes, json.dumps(fake.writes))
    check("报了 1 天且标了 dry_run",
          len(res["madeup"]) == 1 and res["madeup"][0].get("dry_run") is True,
          json.dumps(res["madeup"], ensure_ascii=False))
    check("卡没动", fake.cards == 3, "cards={}".format(fake.cards))


# ---------------------------------------------------------------------------
# S6 服务端收卡但格子还是黑的 → 不重复补
# ---------------------------------------------------------------------------
def s6() -> None:
    print("S6 收卡但格子仍黑、连登却增加了：同轮不原地重复，跨轮也不再碰")
    fake = Fake(zero_days=["2026-11-10"], cards=3, tiers=dict(MID_MONTH),
                streak_before=11, streak_after=21)
    fake.forget_makeup = True          # 收卡不改格子
    state = {}
    res = run(fake, state)

    check("只花 1 张卡", res["cards_used"] == 1, "cards_used={}".format(res["cards_used"]))
    check("日期记进 spent_dates", "2026-11-10" in (state.get("spent_dates") or []),
          json.dumps(state, ensure_ascii=False))

    before = len(fake.writes)
    res2 = run(fake, state)
    new_makeup = [w for w in fake.writes[before:] if w[0] == "makeup"]
    check("第二次运行不再补同一天", not new_makeup, json.dumps(new_makeup))
    check("第二次也没花卡", res2["cards_used"] == 0, "cards_used={}".format(res2["cards_used"]))


# ---------------------------------------------------------------------------
# S7 plan_makeup / sim_streak 纯函数边界（2026-10-28 第二版：先算再花）
# ---------------------------------------------------------------------------
def _cells(zero=(), upto="2026-11-21") -> list:
    """造 2026-11-01..upto 的活跃地图，zero 里的日期 score=0。"""
    out, d = [], datetime.date(2026, 11, 1)
    end = datetime.date.fromisoformat(upto)
    while d <= end:
        ds = d.isoformat()
        out.append({"date": ds, "score": 0 if ds in zero else 2})
        d += datetime.timedelta(days=1)
    return out


def s7() -> None:
    print("S7 plan_makeup / sim_streak 边界")
    today = datetime.date(2026, 11, 21)
    mid = wrap(MID_MONTH)
    # 14d 档还没兑的局面：用来验证「部分补登是否够得到下一档」
    t14 = wrap({"7d": "claimed", "14d": "locked", "28d": "locked"})

    # ---- sim_streak：连登是「从今天往前连续点亮」，且每月 1 日清零 ----
    check("全亮 → 21 天", growth.sim_streak(_cells(), today) == 21)
    check("11-10 断 → 11 天", growth.sim_streak(_cells({"2026-11-10"}), today) == 11)
    check("补上 11-10 → 21 天",
          growth.sim_streak(_cells({"2026-11-10"}), today, {"2026-11-10"}) == 21)
    check("已补过的天算点亮（done）",
          growth.sim_streak(_cells({"2026-11-10"}), today, done={"2026-11-10"}) == 21)
    # 上月全亮也不计入：连登每月 1 日清零，扫描不得越月
    oct_cells = [{"date": "2026-10-{:02d}".format(i), "score": 2} for i in range(1, 32)]
    check("不跨月累计（9/10 月全亮不算进 11 月）",
          growth.sim_streak(oct_cells + _cells(), today) == 21)

    # ---- plan_makeup：卡够补满 ----
    got, why = growth.plan_makeup(3, ["2026-11-10", "2026-11-09"], mid,
                                  _cells({"2026-11-10", "2026-11-09"}), today)
    check("卡够 → 全补", got == ["2026-11-10", "2026-11-09"], str(got) + " | " + why)

    # ---- plan_makeup：卡不够，部分补能否跨档 ----
    zero4 = {"2026-11-10", "2026-11-09", "2026-11-08", "2026-11-07"}
    b4 = ["2026-11-10", "2026-11-09", "2026-11-08", "2026-11-07"]
    got, why = growth.plan_makeup(3, b4, t14, _cells(zero4), today)
    check("卡 3 张、断 4 天，补完到 14 天正好够 14d 档 → 补 3 天",
          got == ["2026-11-10", "2026-11-09", "2026-11-08"], str(got) + " | " + why)
    got, why = growth.plan_makeup(2, b4, t14, _cells(zero4), today)
    check("卡 2 张、断 4 天，补完只到 13 天不够 14d 档 → 一张不花",
          got == [], str(got) + " | " + why)
    got, why = growth.plan_makeup(3, b4, mid, _cells(zero4), today)
    check("同一局面但最近未兑是 28d 档 → 补完 14 天也不够，不花",
          got == [], str(got) + " | " + why)

    # ---- 其余边界 ----
    check("没卡 → 空", growth.plan_makeup(0, ["2026-11-10"], mid, _cells(), today)[0] == [])
    check("没断点 → 空", growth.plan_makeup(3, [], mid, _cells(), today)[0] == [])
    check("三档全兑 → 空（卡留到下月）",
          growth.plan_makeup(3, ["2026-11-10"], wrap(ALL_CLAIMED), _cells(), today)[0] == [])
    check("状态未知但卡够 → 照补（保连续优先）",
          growth.plan_makeup(3, ["2026-11-10"],
                             wrap({"7d": None, "14d": None, "28d": None}),
                             _cells({"2026-11-10"}), today)[0] == ["2026-11-10"])
    check("卡不够又缺推演数据 → 不补（不猜）",
          growth.plan_makeup(1, ["2026-11-10", "2026-11-09"], mid, None, None)[0] == [])


def main() -> int:
    print("补登策略桩测（模拟 2026-11-21 这一天）\n")
    for fn in (s1, s2, s3, s4, s5, s6, s7):
        fn()
        print()
    if FAILED:
        print("失败 {} 项：{}".format(len(FAILED), "、".join(FAILED)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
