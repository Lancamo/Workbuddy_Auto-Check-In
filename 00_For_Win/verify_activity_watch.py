#!/usr/bin/env python3
"""晚间活跃看门狗的离线桩测。

不出网、不碰真实账号：把「读活跃分 / 读连登 / 读写 state」四处替换成桩，
判定逻辑原样跑。

为什么值得单测这个模块：它是要在**晚上**触发发消息的。判错会有两种代价 ——
误报（分数>0 还提醒，用户被骚扰一次，还可能因为要「消掉提醒」去凑分数），
漏报（该提醒时没提醒，连登断掉，28 天档报废整月）。两种都比多写几行测试贵。
"""

from __future__ import annotations

import datetime
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "scripts"))
sys.path.insert(0, HERE)

import activity_watch as aw  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print("  ✅ " + name)
    else:
        print("  ❌ {}  {}".format(name, detail))
        FAILED.append(name)


class Clock:
    """把 state 与两个读接口都换成内存实现。"""

    def __init__(self, score: int | None = 0, days: int | None = 12,
                 score_raises: bool = False, state: dict | None = None):
        self.score = score
        self.days = days
        self.raises = score_raises
        self.state = state if state is not None else {}
        self.writes = 0
        self.fetches = 0

    # ---- 桩 ----
    def load_state(self) -> dict:
        return dict(self.state)

    def save_state(self, st: dict) -> None:
        self.state = dict(st)
        self.writes += 1

    def fetch_today_score(self, timeout: int = 20) -> int:
        self.fetches += 1
        if self.raises:
            raise RuntimeError("network down")
        return self.score

    def fetch_streak_days(self, timeout: int = 20) -> int | None:
        return self.days

    def install(self) -> None:
        aw._load_state = self.load_state
        aw._save_state = self.save_state
        aw.fetch_today_score = self.fetch_today_score
        aw.fetch_streak_days = self.fetch_streak_days


def at(clock: Clock, hh: int, mm: int) -> dict:
    return aw.check(now=datetime.datetime(2026, 11, 5, hh, mm))


def s1() -> None:
    print("S1 未到点：不查接口、不写盘")
    c = Clock(score=0)
    c.install()
    res = at(c, 21, 0)
    check("action=idle", res["action"] == "idle", str(res))
    check("零请求", c.fetches == 0, "fetches={}".format(c.fetches))
    check("零写盘", c.writes == 0, "writes={}".format(c.writes))

    res = at(c, 12, 30)
    check("中午也不查", res["action"] == "idle" and c.fetches == 0)


def s2() -> None:
    print("S2 21:31 且 0 分：提醒一次，并记住本档")
    c = Clock(score=0, days=12)
    c.install()
    res = at(c, 21, 31)
    check("action=alert", res["action"] == "alert", str(res))
    check("档位是 2130", res.get("slot") == "2130", str(res.get("slot")))
    check("带上连登天数", res.get("streak_days") == 12, str(res.get("streak_days")))
    check("文案含「发一句话」", "发一句话" in res.get("body", ""), res.get("body", ""))
    check("state 记了 alerted", c.state.get("alerted") == ["2130"],
          str(c.state.get("alerted")))

    # 同一档再跑：不再提醒，也不再请求
    before = c.fetches
    res2 = at(c, 21, 36)
    check("同日同档不重复提醒", res2["action"] == "idle", str(res2))
    check("不重复请求", c.fetches == before, "fetches {} -> {}".format(before, c.fetches))


def s3() -> None:
    print("S3 23:41 仍未点亮：第二档再提醒一次，文案与第一档不同")
    c = Clock(score=0, days=12)
    c.install()
    r2130 = at(c, 21, 31)
    r2340 = at(c, 23, 41)
    check("两档都发", r2130["action"] == "alert" and r2340["action"] == "alert",
          "{} / {}".format(r2130["action"], r2340["action"]))
    check("第二档 slot=2340", r2340.get("slot") == "2340", str(r2340.get("slot")))
    check("两档标题不同（否则会被通知去重吞掉）",
          r2130["title"] != r2340["title"],
          "{} == {}".format(r2130["title"], r2340["title"]))
    check("两档正文不同", r2130["body"] != r2340["body"])
    check("state 累计两档", c.state.get("alerted") == ["2130", "2340"],
          str(c.state.get("alerted")))
    # 23:50 再跑：都提醒过 → 静默
    check("两档都提醒过后不再打扰", at(c, 23, 50)["action"] == "idle")


def s4() -> None:
    print("S4 分数 > 0：记已点亮，之后两档都不再查")
    c = Clock(score=6, days=13)
    c.install()
    res = at(c, 21, 31)
    check("action=lit", res["action"] == "lit", str(res))
    check("state 标记 lit", c.state.get("lit") is True, str(c.state))
    before = c.fetches
    res2 = at(c, 23, 41)
    check("第二档不再检查", res2["action"] == "idle", str(res2))
    check("也不再请求（省一次接口调用）", c.fetches == before)


def s5() -> None:
    print("S5 23:45 才第一次运行：直接发 23:40 的文案，不补发过时的 21:30")
    c = Clock(score=0, days=3)
    c.install()
    res = at(c, 23, 45)
    check("只发一次", res["action"] == "alert", str(res))
    check("档位是 2340（不是 2130）", res.get("slot") == "2340", str(res.get("slot")))
    check("标题是最后提醒", "最后" in res.get("title", ""), res.get("title", ""))


def s6() -> None:
    print("S6 读活跃分失败：报 error、不写 state（下一轮可重试）")
    c = Clock(score=0, score_raises=True)
    c.install()
    res = at(c, 21, 31)
    check("action=error", res["action"] == "error", str(res))
    check("零写盘（不会被记成已提醒）", c.writes == 0, "writes={}".format(c.writes))

    # 恢复后能正常提醒
    c.raises = False
    check("恢复后照常提醒", at(c, 21, 36)["action"] == "alert")


def s7() -> None:
    print("S7 跨日：昨天的标记不影响今天")
    c = Clock(score=0, days=5, state={"day": "2026-11-04", "lit": True,
                                      "alerted": ["2130", "2340"]})
    c.install()
    res = at(c, 21, 31)
    check("新的一天重新开始", res["action"] == "alert", str(res))
    check("state.day 更新为今天", c.state.get("day") == "2026-11-05", str(c.state.get("day")))
    check("lit 被重置", c.state.get("lit") is False, str(c.state.get("lit")))


def s8() -> None:
    print("S8 拿不到连登天数：仍提醒，只是少一行")
    c = Clock(score=0, days=None)
    c.install()
    res = at(c, 21, 31)
    check("照样提醒", res["action"] == "alert", str(res))
    check("不含「当前连登」那行", "当前连登" not in res.get("body", ""), res.get("body", ""))
    check("仍含行动指引", "发一句话" in res.get("body", ""))


def s9() -> None:
    print("S9 脏 state（没有 alerted 字段 / 类型不对）不炸")
    c = Clock(score=0, days=9, state={"day": "2026-11-05"})
    c.install()
    res = at(c, 23, 41)
    check("缺字段也能判断", res["action"] == "alert", str(res))
    check("正常写回", c.state.get("alerted") == ["2340"], str(c.state.get("alerted")))


def s10() -> None:
    """接线契约：catchup 发提醒时传的 level 必须是 notify 认得的键。

    ── 为什么这条必须钉死 ────────────────────────────────────────────────
    `notify.send()` 里是 `switch = _LEVEL_SWITCH.get(level, "notify_noop")`，
    而 `DEFAULT_CONFIG["notify_noop"]` 是 **False**。也就是说：写错一个级别名，
    提醒不会报错、不会进日志告警，只会**静默地一条都发不出去**。

    2026-10-28 就发生过：Mac 侧传的是 `"warn"`（少一个 ing），落进兜底分支，
    整个晚间看门狗上线当天等于没接线；Win 侧当时写的 `"warning"` 是对的。
    这条断言是为了让「写对」以后不会退回去。

    这里不测「模块返回什么」，而是直接读 catchup 源码里的实参，再拿 notify
    的真实映射表校验 —— 桩测替换掉 send 的时候抓不到这类错，只有对真实常量
    断言才抓得到。
    """
    print("S10 接线契约：提醒用的 level 必须是 notify 认识的键")
    import re

    import notify  # 只读常量，不触发任何发送

    src_path = os.path.join(HERE, "catchup.py")
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    m = re.search(r'_notify\(res\["title"\],\s*res\["body"\],\s*"([^"]+)"\)', src)
    check("能在 catchup.py 里找到发提醒的那行", m is not None)
    if m is None:
        return
    level = m.group(1)
    check("level 在 notify._LEVEL_SWITCH 的四个键之内", level in notify._LEVEL_SWITCH,
          "当前写的是 {!r}，合法值：{}".format(
              level, "/".join(sorted(notify._LEVEL_SWITCH))))
    switch = notify._LEVEL_SWITCH.get(level, "notify_noop")
    check("该 level 对应的开关默认是开启的",
          notify.DEFAULT_CONFIG.get(switch) is True,
          "开关 {} 默认 {}".format(switch, notify.DEFAULT_CONFIG.get(switch)))


def main() -> int:
    print("晚间活跃看门狗桩测（模拟 2026-11-05）\n")
    for fn in (s1, s2, s3, s4, s5, s6, s7, s8, s9, s10):
        fn()
        print()
    if FAILED:
        print("失败 {} 项：{}".format(len(FAILED), "、".join(FAILED)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
