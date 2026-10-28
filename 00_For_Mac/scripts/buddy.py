#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
buddy.py — WorkBuddy 桌宠（Buddy）仓库与「常驻猫」锁定

为什么需要这个模块（2026-10-28）：
  旅行接口 `travel/depart` 只接受 `{"location_id": ...}`，**没有任何指定桌宠的参数** ——
  服务端永远派「当前在岗的那只猫」（buddy/info 返回的 current_buddy）。
  也就是说：想让某只猫去旅行，唯一手段是先把在岗猫换成它。

  用户在客户端抽到新猫后点「立即使用」、或换设备登录，在岗猫都可能被换掉，
  而旅行是第二天才发生的事 —— 今天被换，明天出门的就是另一只。

  本模块负责：读仓库 → 选目标猫 → 与在岗猫比对 → 不一致就换回来 → 复核。

关于「品级」的实测结论（别被传言带偏，2026-10-28 三方核对）：
  1. 官方活动规则只把 R/SR/SSR/UR 定义为**抽取概率**档位，全文未提任何收益加成；
  2. 卡池模板接口（buddy/templates）里没有任何 reward 字段；
  3. 旅行奖励区间来自地点配置（4 个地点完全一致：5–10），与桌宠无关；
  4. 本账号 42 天真实记录（全部是 R 级猫出行）已出现 5/6/7/8/9/10 全部数值，
     10 分出现过 5 次，均值 7.07。
  → **品级不影响旅行积分**。选高品级猫是为了「技能」和「观感一致」，
    不是为了单次旅行多拿分。

关于「技能」的现状（2026-10-28 核实，结论比传闻冷）：
  · 接口里**没有技能字段**：buddy/list 与 buddy/templates 的字段全是外观/文案/
    稀有度/获得时间，没有任何 skill / passive / effect 字段。
  · **官方物料里也找不到技能说明**：活动规则原文第三条第（四）项「奖项设置」只列了
    「编号 — 名称 — 稀有度 — 概率」9 只猫，全文没有一个字讲技能；客户端成长空间
    bundle（GrowthCenterPage-*.js）全文检索「火眼金睛 / 大师技能 / 被动 / 加成」= 0 命中。
  · 网上流传的「大圣喵·火眼金睛：每日任务 15% 双倍 Credits」只出现在第三方攻略里，
    且这些文章互相引用、且在别的事实上已被证伪（说「花艺喵是 SSR」，接口里是 R）。
    所以下表里的条目一律标注 `verified: False` —— **它是待证传闻，不是已核实事实**。
  · 那为什么还留着这一维：同品级之间本来就没有别的判据（旅行收益与品级无关），
    按传闻选一只**机会成本为 0**；若传闻为真则白赚，为假也无损失。但绝不能把它
    当成「官方确认」写进结论，更不能据此推断旅行收益。
  · 未验证的第二个假设：技能是否只对「当前在岗」的 Buddy 生效，官方同样没说过。
    本模块按「在岗生效」处理；若日后证明无关，删掉 SKILLS 排序即可，其余逻辑不受影响。
  · 若哪天核实了（例如客户端图鉴开放技能展示），把对应条目的 verified 改成 True 并补来源。

用法：
  python3 buddy.py status                 # 只读：仓库 + 在岗猫 + 目标猫（含技能标注）
  python3 buddy.py ensure [--dry-run]     # 让在岗猫变成目标猫
  python3 buddy.py set 暴富喵              # 指定常驻猫（写 runtime/config/buddy.json）
  python3 buddy.py set auto               # 取消指定，回到自动选

安全：token 仅内存使用，绝不打印 / 写日志 / 落盘。
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import credentials  # noqa: E402
import http_client   # noqa: E402
import paths         # noqa: E402

LIST_PATH = "/activity/growth/buddy/list"
INFO_PATH = "/activity/growth/buddy/info"
SWITCH_PATH = "/activity/growth/buddy/switch"

# 稀有度从低到高。**只用于挑「用户可能最想留着的那只」**，与旅行收益无关（见模块说明）。
RANK = {"R": 1, "SR": 2, "SSR": 3, "UR": 4}

# 技能表（手工维护）。**每一条都必须写清来源与置信度**，不带来源的条目不要加。
#
# 为什么是手写表而不是从接口读：buddy/list 与 buddy/templates 都没有技能字段，
# 从数据里推断出来的任何「技能」都是编的。宁可少一个维度，也不要假数据。
#
# 字段说明：
#   name     技能名
#   scope    作用域：task = 每日任务奖励；travel = 旅行带回的积分（目前无人证实存在）
#   rank     同作用域内的强弱序，越大越优先
#   how      效果描述，用于 status/消息展示
#   verified 是否已核实。False = 只有第三方说法，官方物料里查不到 → 仅作同级 tiebreak
#   source   来源。写不到「官方某页原文」就老实写「第三方」
#
# 当前状况：唯一一条来自第三方攻略，客户端 bundle 全文检索「火眼金睛/大师技能/被动/
# 加成」= 0 命中，活动规则「奖项设置」也只列名称/稀有度/概率，完全不提技能。
SKILLS = {
    "大圣喵": {"name": "火眼金睛", "scope": "task", "rank": 1,
               "how": "（传闻）每日任务 15% 概率双倍 Credits",
               "verified": False,
               "source": "第三方攻略；官方规则与客户端 bundle 均无佐证"},
}


def skill_of(buddy: dict | None) -> dict | None:
    """这只猫的已知技能；没有就返回 None（不臆造、不回退到「可能有效果」）。"""
    if not buddy:
        return None
    return SKILLS.get((buddy.get("name") or "").strip())

CONFIG_NAME = "buddy.json"
DEFAULT_CONFIG = {"prefer_name": ""}


def _result(status: str, **fields) -> dict:
    out = {"task": "buddy", "status": status}
    out.update(fields)
    return out


# ---------------------------------------------------------------------------
# 配置：常驻猫名字（可选）
# ---------------------------------------------------------------------------
def load_config() -> dict:
    """读 runtime/config/buddy.json；缺失/损坏一律退回默认值（自动选）。

    刻意不在读取时创建文件：这个配置是「可选覆盖」，不是必需品。
    只有 `buddy.py set` 才落盘，避免运行时往用户目录里塞空文件。
    """
    cfg = dict(DEFAULT_CONFIG)
    try:
        p = paths.config_path(CONFIG_NAME)
        if p.exists():
            raw = json.loads(p.read_text())
            if isinstance(raw, dict):
                cfg.update({k: v for k, v in raw.items() if k in DEFAULT_CONFIG})
    except Exception:  # noqa: BLE001 — 配置坏了不该影响旅行
        pass
    return cfg


def save_config(cfg: dict) -> None:
    p = paths.config_path(CONFIG_NAME)
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    try:
        p.chmod(0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------
def _headers(uid: str) -> dict:
    return {"X-User-Id": uid}


def list_buddies(host: str, token: str, uid: str, timeout: int = 15) -> list[dict]:
    """读桌宠仓库（含已拥有但不在岗的猫）。"""
    code, body = http_client.get_json(
        host + LIST_PATH, token=token, extra_headers=_headers(uid), timeout=timeout)
    if code != 200 or body.get("code") != 0:
        raise RuntimeError("仓库接口失败：http={} code={} msg={}".format(
            code, body.get("code"), body.get("msg")))
    data = body.get("data") or {}
    return data.get("buddies") or []


def current_buddy(host: str, token: str, uid: str, timeout: int = 15) -> dict | None:
    """读当前在岗猫（旅行派的就是它）。未领取 Buddy 时返回 None。"""
    code, body = http_client.get_json(
        host + INFO_PATH, token=token, extra_headers=_headers(uid), timeout=timeout)
    if code != 200 or body.get("code") != 0:
        raise RuntimeError("在岗猫接口失败：http={} code={} msg={}".format(
            code, body.get("code"), body.get("msg")))
    data = body.get("data") or {}
    b = data.get("buddy")
    return b or None


def switch_buddy(host: str, token: str, uid: str, instance_id: int,
                 timeout: int = 15) -> None:
    code, body = http_client.post_json(
        host + SWITCH_PATH, body={"instance_id": instance_id}, token=token,
        extra_headers=_headers(uid), timeout=timeout)
    if code != 200 or body.get("code") != 0:
        raise RuntimeError("切换接口失败：http={} code={} msg={}".format(
            code, body.get("code"), body.get("msg")))


# ---------------------------------------------------------------------------
# 选猫
# ---------------------------------------------------------------------------
def _acquired_ts(b: dict) -> float:
    """获得时间转时间戳；解析不了就给一个很大的数（排后面）。

    注意：`acquired_at` 是带时区的 ISO 串（+08:00），不能用 fromisoformat 以外的
    粗暴切片解析；解析失败时宁可不参与「最早获得」的比较，也不要抛异常。
    """
    import datetime as _dt
    raw = b.get("acquired_at") or ""
    try:
        return _dt.datetime.fromisoformat(raw).timestamp()
    except Exception:  # noqa: BLE001
        return float("inf")


def _skill_rank(b: dict) -> int:
    """有已知技能就返回技能等级，没有返回 0。"""
    s = skill_of(b)
    return int(s.get("rank") or 0) if s else 0


def choose_target(buddies: list[dict], prefer_name: str = "") -> tuple[dict | None, str]:
    """挑常驻猫。返回 (目标猫, 说明)。

    规则（按优先级）：
      ① 配置里点名了 → 就用它（前提是仓库里真有这只）；
      ② 否则取品级最高的那只；
      ③ 同品级取**有已知技能**的那只（技能表见 SKILLS；接口没有技能字段是硬限制）；
      ④ 再同则取**最早获得**的那只 —— 保证每天选出来的都是同一只，
         不会因为仓库里有多只同级猫而今天换一只、明天换一只。

    为什么 ③ 排在 ④ 前面：同品级之间的旅行收益完全相同，本来就没有别的判据；
    按「传闻有技能的那只」优先，机会成本为 0（传闻真则白赚，假则与随机选等价）。
    而「获得早晚」只是稳定性的兜底，所以排最后。稳定性由 ④ 保证 ——
    只要仓库不变，同品级同技能的猫永远选出同一只。
    """
    owned = [b for b in buddies if b.get("instance_id")]
    if not owned:
        return None, "仓库是空的"
    if prefer_name:
        for b in owned:
            if (b.get("name") or "").strip() == prefer_name.strip():
                return b, "指定常驻猫"
        return None, "指定的「{}」不在仓库里（现有：{}）".format(
            prefer_name, "、".join(b.get("name") or "?" for b in owned))
    best = min(owned, key=lambda b: (-RANK.get((b.get("rarity") or "").upper(), 0),
                                     -_skill_rank(b),
                                     _acquired_ts(b)))
    why = "自动选最高品级（同级优先有技能者，再取最早获得）"
    sk = skill_of(best)
    if sk:
        why += "；{} 带「{}」{}".format(
            best.get("name"), sk.get("name"),
            "" if sk.get("verified") else "（未核实）")
    return best, why


def _brief(b: dict | None) -> dict | None:
    if not b:
        return None
    out = {"instance_id": b.get("instance_id"),
           "name": b.get("name"),
           "rarity": b.get("rarity")}
    sk = skill_of(b)
    if sk:
        out["skill"] = sk.get("name")
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def ensure_duty(host: str, token: str, uid: str, prefer_name: str = "",
                dry_run: bool = False, timeout: int = 15) -> dict:
    """确保在岗猫 == 目标猫。幂等：已经一致时一个写请求都不发。

    切换后会**复核一次**在岗猫：本项目已有先例（旧接口返回 code=0 但数据没变），
    只信「再读一次」的结果，不信写接口自己的成功码。
    """
    try:
        buddies = list_buddies(host, token, uid, timeout)
        current = current_buddy(host, token, uid, timeout)
    except Exception as e:  # noqa: BLE001 — 读失败不阻断旅行
        return _result("failed", reason=str(e)[:200], switched=False)

    if not current:
        # 没有在岗猫 = 还没领过 Buddy。此时派猫会失败，交给上层决定怎么办。
        return _result("failed", reason="当前没有在岗猫（可能还没领取过 Buddy）",
                       switched=False)

    target, why = choose_target(buddies, prefer_name)
    if not target:
        return _result("failed", reason=why, switched=False, current=_brief(current))

    cur_b, tgt_b = _brief(current), _brief(target)
    if cur_b["instance_id"] == tgt_b["instance_id"]:
        return _result("ok", switched=False, current=cur_b, target=tgt_b, picked_by=why)
    if dry_run:
        return _result("would_switch", switched=False, current=cur_b, target=tgt_b,
                       picked_by=why)

    try:
        switch_buddy(host, token, uid, int(tgt_b["instance_id"]), timeout)
    except Exception as e:  # noqa: BLE001
        return _result("failed", reason=str(e)[:200], switched=False,
                       current=cur_b, target=tgt_b, picked_by=why)

    # 复核：接口说成功不算成功，读回来是它才算。
    try:
        after = _brief(current_buddy(host, token, uid, timeout))
    except Exception as e:  # noqa: BLE001
        return _result("failed", reason="切换后复核失败：" + str(e)[:160],
                       switched=True, current=cur_b, target=tgt_b, picked_by=why)
    if not after or after.get("instance_id") != tgt_b["instance_id"]:
        return _result("failed", reason="切换接口返回成功，但在岗猫未变（{}）".format(
            (after or {}).get("name")), switched=False, current=after,
            target=tgt_b, picked_by=why)

    return _result("switched", switched=True, current=after, target=tgt_b,
                   previous=cur_b, picked_by=why)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False))


def _main(argv: list[str]) -> int:
    import travel   # 惰性导入：host 解析复用 travel，避免模块级循环依赖

    cmd = argv[1] if len(argv) > 1 else "status"
    try:
        cred = credentials.load_credentials()
    except credentials.CredentialError as e:
        _print(_result("failed", reason="登录态读取失败：" + str(e)))
        return 1
    host = travel.resolve_host(cred)
    token = cred.get("access_token", "")
    uid = cred.get("uid", "")

    if cmd == "status":
        try:
            buddies = list_buddies(host, token, uid)
            current = current_buddy(host, token, uid)
        except Exception as e:  # noqa: BLE001
            _print(_result("failed", reason=str(e)[:200]))
            return 1
        cfg = load_config()
        target, why = choose_target(buddies, cfg.get("prefer_name") or "")
        _print(_result("ok", current=_brief(current), target=_brief(target),
                       picked_by=why, prefer_name=cfg.get("prefer_name") or None,
                       in_sync=bool(current and target
                                    and current.get("instance_id") == target.get("instance_id")),
                       owned=[{**_brief(b), "acquired_at": b.get("acquired_at")}
                              for b in buddies]))
        return 0

    if cmd == "ensure":
        r = ensure_duty(host, token, uid, (load_config().get("prefer_name") or ""),
                        dry_run="--dry-run" in argv)
        _print(r)
        return 0 if r.get("status") in ("ok", "would_switch", "switched") else 1

    if cmd == "set":
        name = argv[2] if len(argv) > 2 else ""
        cfg = load_config()
        cfg["prefer_name"] = "" if name in ("", "auto") else name
        save_config(cfg)
        _print(_result("ok", prefer_name=cfg["prefer_name"] or None,
                       message="已取消指定，回到自动选最高品级" if not cfg["prefer_name"]
                       else "已指定常驻猫：" + cfg["prefer_name"]))
        return 0

    _print(_result("failed", reason="未知子命令（status / ensure / set）"))
    return 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
