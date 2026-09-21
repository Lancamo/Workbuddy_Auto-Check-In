#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
http_client.py — WorkBuddy 积分助手 · 统一 HTTP 客户端

职责：
  - 统一封装带鉴权的 JSON 请求（method / headers / timeout / 错误处理）
  - 供 checkin.py（Buddy加油站签到）与后续 travel.py（Buddy旅行）共用，避免重复

设计：
  - 基于标准库 urllib，无第三方依赖
  - 返回 (http_status, parsed_json)；传输层失败（DNS/连接/超时）抛 HttpTransportError
  - HTTP 错误状态（4xx/5xx）不抛异常，仍尝试解析响应体后原样返回状态码与解析结果，
    由调用方按业务码（如 401 / code=10001）判断
  - 响应体非 JSON 时返回 {"__non_json__": true} 标记，不保留原始报文（避免夹带敏感信息）

安全：
  - token 仅作为请求头在内存中传递，绝不打印、不写日志、不落盘
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import winenv  # noqa: E402  跨平台适配层（客户端版本探测）

DEFAULT_TIMEOUT = 15


def _client_ua() -> str:
    """User-Agent 跟随**本机客户端真实版本**（跨平台，无副作用）。

    以前硬编码 "WorkBuddy/5.3.14"，客户端升到 5.5.6 后请求仍伪装成老版本 ——
    既不符合事实，也可能被网关按"过旧客户端"区别对待。

    ★ Windows 版：版本探测交给 `winenv`（macOS 读 Info.plist，Windows 解析 asar 头部
      的 package.json）。读不到就退回一个保守值，绝不影响请求。
    """
    try:
        v = winenv.client_cached().get("version")
        if v:
            return "WorkBuddy/" + str(v)
    except Exception:  # noqa: BLE001 — 读不到就用兜底值，不影响请求
        pass
    return "WorkBuddy/5.5.6"


DEFAULT_UA = _client_ua()

_OPENER = None


def _proxy_opener():
    """WorkBuddy 官方接口默认直连；系统代理只作为显式开关。

    Windows 系统代理也会跟随代理 App 的启停，代理不在时
    urllib 就会报 Connection refused；开启 TLS 拦截时还可能换成自签名证书链。
    """
    global _OPENER
    if _OPENER is None:
        use_system_proxy = os.environ.get(
            "WORKBUDDY_PROXY_MODE", "").strip().lower() == "system"
        proxies = (urllib.request.getproxies() if use_system_proxy
                   else urllib.request.getproxies_environment())
        _OPENER = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
    return _OPENER


class HttpTransportError(RuntimeError):
    """网络传输层失败（未收到 HTTP 响应：DNS/连接/超时/重置等）。"""


def _parse_json(raw: str) -> dict:
    """把响应体解析成 dict —— **保证返回 dict**，调用方一律可以直接 .get()。

    ★ 为什么必须保证类型，而不只是挡住「不是 JSON」：
      合法 JSON 的顶层**可以**是数组或标量（网关错误页、被包成 [] 的空响应、
      `"ok"` 这样的裸字符串都会解析成功）。旧实现直接 `return json.loads(raw)`，
      于是 checkin.py / travel.py 里的 `body.get("code")` 会抛 AttributeError——
      而它们只捕 HttpTransportError / CredentialError，异常会一路穿透到 main.py
      变成一段 traceback，当天签到直接空转，直到 MAX_TRIES 用完。
      这里多一层 isinstance 判断，把「形状不对」也归进同一个哨兵返回。
    """
    try:
        obj = json.loads(raw)
    except Exception:
        return {"__non_json__": True}
    if not isinstance(obj, dict):
        return {"__non_json__": True, "__json_type__": type(obj).__name__}
    return obj


def request_json(
    method: str,
    url: str,
    body: dict | None = None,
    token: str | None = None,
    extra_headers: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    user_agent: str = DEFAULT_UA,
) -> tuple[int, dict]:
    """
    发起一次 JSON 请求。
    返回 (http_status, parsed_json)。
    传输层失败抛 HttpTransportError。
    """
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": user_agent,
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    if extra_headers:
        headers.update(extra_headers)

    payload = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=payload, headers=headers, method=method)

    try:
        with _proxy_opener().open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, _parse_json(raw)
    except urllib.error.HTTPError as e:
        # 服务器返回了 HTTP 错误状态：仍尝试解析错误体（可能含业务码），不抛异常
        try:
            raw = e.read().decode("utf-8", "replace")
            return e.code, _parse_json(raw)
        except Exception:
            return e.code, {}
    except Exception as e:
        # DNS/连接/超时/SSL 等传输层失败
        raise HttpTransportError(str(e))


def post_json(url: str, body: dict | None = None, **kwargs) -> tuple[int, dict]:
    return request_json("POST", url, body=body, **kwargs)


def get_json(url: str, **kwargs) -> tuple[int, dict]:
    return request_json("GET", url, body=None, **kwargs)
