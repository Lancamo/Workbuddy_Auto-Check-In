"""WorkBuddy 积分余额（只读，供每日快照展示）。

数据来源 = 客户端「套餐与积分」页里那一支 summary 接口：

    POST {host}/billing/meter/get-user-resource-summary   body={}

返回 `data.TotalCredits` 就是当前可用积分（带小数 —— AI 消耗按 token 精算，
所以余额天然不是整数），本轮只取整数部分展示。

⚠️ **这条路径不带 `/v2` 前缀**，签到接口才带。同一个网关挂载方式不同，
   2026-10-30 实测：`/v2/billing/meter/get-user-resource-summary` → `404 Route Not Found`，
   去掉 `/v2` 才通。两条候选都留着，谁先通用谁（服务端改挂载时不用改代码）。

为什么不用签到接口的 `total_credits`：那是**本次活动累计发放**（31 天 × 100 = 3100），
与「手上还有多少」无关。上一轮就是拿它当余额才闹了误会。

设计约束（与 notify 一致）：**fail-closed 但不阻断**。
余额查不到时返回 `{"__error__": ...}`，由调用方决定不显示这一行 ——
绝不能因为查余额失败而让整条签到快照发不出去。早上 DarkWake 窗口只有 2~8 秒，
所以这里**短超时 + 不重试**，宁可少一行字，也不占用投递时间。
"""

from __future__ import annotations

import json

import http_client

# 候选主机：与 checkin 一致（同一套账号体系，两个域名互为镜像）
BALANCE_HOSTS = ("https://www.workbuddy.cn", "https://copilot.tencent.com")

# 候选路径：不带 /v2 优先（当前有效），带 /v2 作抗变更兜底
BALANCE_PATHS = (
    "/billing/meter/get-user-resource-summary",
    "/v2/billing/meter/get-user-resource-summary",
)

TIMEOUT = 6          # 秒；比常规请求短，见模块顶部说明


def query_balance(cred: dict | None = None, timeout: int = TIMEOUT) -> dict:
    """查当前可用积分。

    返回 `{"total": int, "raw": "2599.02"}`；失败返回 `{"__error__": "..."}`。
    只读：不改任何状态、不发任何消耗积分的请求。

    cred 省略时自己读登录态（调用方只管要数字，不必关心凭据怎么来）。
    """
    if cred is None:
        try:
            import credentials
            cred = credentials.load_credentials()
        except Exception as e:  # noqa: BLE001
            return {"__error__": "读取登录态失败：{}".format(e)}

    token = (cred or {}).get("access_token")
    if not token:
        return {"__error__": "没有可用登录态"}

    last_err = "未知错误"
    for host in BALANCE_HOSTS:
        for path in BALANCE_PATHS:
            try:
                code, body = http_client.post_json(
                    host + path, body={}, token=token,
                    extra_headers={"Accept-Language": "zh-CN"},
                    timeout=timeout)
            except http_client.HttpTransportError as e:
                last_err = "网络异常：{}".format(e)
                continue
            if code == 401:
                return {"__error__": "令牌已过期（401）"}
            if code == 404:
                last_err = "端点不存在（404）"
                continue
            data = (body or {}).get("data") or {}
            raw = data.get("TotalCredits")
            try:
                total = int(round(float(raw)))
            except (TypeError, ValueError):
                last_err = "返回里没有 TotalCredits：{}".format(
                    json.dumps(body, ensure_ascii=False)[:120])
                continue
            return {"total": total, "raw": str(raw),
                    "plan": data.get("SubscriptionPackageName") or ""}
    return {"__error__": last_err}
