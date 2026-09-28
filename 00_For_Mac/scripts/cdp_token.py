#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cdp_token.py — WorkBuddy 积分助手 · 路线 B：CDP 远程调试取明文 token

用途
----
当「路线 A」（本地解密 at-rest 加密凭据，见 00_For_Win/scripts/atrest.py）因
客户端未来改版而失效时，改用 Chromium DevTools Protocol（CDP）从「运行中」的
WorkBuddy 客户端直接取明文 accessToken：

  1. 客户端以 --remote-debugging-port=9222 启动后，CDP 的 HTTP 端点
     http://127.0.0.1:<port>/json/list 会列出调试目标（含 webSocketDebuggerUrl）。
  2. 连上该 WebSocket 后发送 Runtime.evaluate，表达式调用客户端自己的 IPC：
         window.vscode.ipcRenderer.invoke('vscode:genie:auth:getSession')
     返回的会话对象里含 accessToken 与 uid。

硬约束（务必遵守）
------------------
  - 零第三方依赖：仅用 Python 标准库。WebSocket 客户端在本文件内自实现
    （HTTP Upgrade 握手 + RFC6455 帧编解码：客户端帧必须掩码、支持 126/127
    长度扩展、分片重组、ping/pong）。
  - 只连 127.0.0.1，绝不出网；只读：不改任何文件、不发任何业务请求。
  - access_token 等同账号密码：绝不出现在 stdout/日志，调试信息一律脱敏
    （只打长度）并走 stderr。
  - 所有公开接口失败一律返回 None / False，绝不抛异常。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request

DEFAULT_PORTS = (9222,)          # 默认探测端口
ENV_PORT = "WORKBUDDY_CDP_PORT"  # 环境变量可指定端口

SOURCE_CDP = "cdp-remote-debug"

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC6455 固定 GUID

# Runtime.evaluate 候选表达式，按优先级依次尝试：
#   1) 主路径：异步 IIFE 调客户端自身 IPC 取会话并 JSON 化，配合 awaitPromise。
#   2) 备用：直接 invoke（不 await），个别构建里该调用返回的是已物化值。
#   3) 兜底：扫描 localStorage/sessionStorage 里的 JWT 形态字符串。
#      仅在 IPC 通道改名/下线时才有意义；只能拿到 token 本体，拿不到 uid。
_EVAL_EXPRESSIONS = (
    (
        "(async()=>{const s=await window.vscode.ipcRenderer.invoke("
        "'vscode:genie:auth:getSession');return JSON.stringify(s)})()",
        True,
    ),
    (
        "window.vscode.ipcRenderer.invoke('vscode:genie:auth:getSession')",
        False,
    ),
    (
        "(()=>{try{const out=[];for(const st of [localStorage,sessionStorage]){"
        "for(let i=0;i<st.length;i++){const v=st.getItem(st.key(i));"
        "if(typeof v==='string'){const m=v.match(/eyJ[A-Za-z0-9_-]+\\."
        "[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+/);if(m)out.push(m[0]);}}}"
        "return JSON.stringify(out)}catch(e){return '[]'}})()",
        False,
    ),
)


def _dbg(msg: str) -> None:
    """调试信息：只走 stderr，且调用方保证内容已脱敏（绝不传 token 本体）。"""
    sys.stderr.write("[cdp_token] {}\n".format(msg))


# ---------------------------------------------------------------------------
# 端口解析与探测
# ---------------------------------------------------------------------------
def cdp_port() -> int | None:
    """解析 WORKBUDDY_CDP_PORT（非法值返回 None）。"""
    raw = os.environ.get(ENV_PORT, "").strip()
    if not raw:
        return None
    try:
        value = int(raw, 10)
    except ValueError:
        return None
    if 0 < value < 65536:
        return value
    return None


def _http_get_json(port: int, path: str, timeout: float):
    """GET http://127.0.0.1:<port><path> 并解析 JSON；失败返回 None。只连回环。"""
    url = "http://127.0.0.1:{}{}".format(port, path)
    try:
        req = urllib.request.Request(url, headers={"Connection": "close"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None


def probe(port: int | None = None, timeout: float = 0.5) -> int | None:
    """探测 CDP 是否可用：GET /json/version 或 /json/list。
    可用返回端口号，不可用返回 None。port=None 时遍历 DEFAULT_PORTS。"""
    ports = (port,) if port else DEFAULT_PORTS
    for p in ports:
        if not isinstance(p, int) or not (0 < p < 65536):
            continue
        for path in ("/json/version", "/json/list"):
            if _http_get_json(p, path, timeout) is not None:
                return p
    return None


# ---------------------------------------------------------------------------
# 最小 WebSocket 客户端（RFC6455，仅标准库）
# ---------------------------------------------------------------------------
def _build_frame(opcode: int, payload: bytes, masked: bool, fin: bool = True) -> bytes:
    """构造一个 RFC6455 帧。客户端发送必须 masked=True。"""
    header = bytearray()
    header.append((0x80 if fin else 0x00) | (opcode & 0x0F))
    mask_bit = 0x80 if masked else 0x00
    length = len(payload)
    if length < 126:
        header.append(mask_bit | length)
    elif length <= 0xFFFF:
        header.append(mask_bit | 126)
        header += struct.pack(">H", length)
    else:
        header.append(mask_bit | 127)
        header += struct.pack(">Q", length)
    if not masked:
        return bytes(header) + payload
    mask = os.urandom(4)
    header += mask
    masked_payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bytes(header) + masked_payload


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接被对端关闭")
        buf += chunk
    return bytes(buf)


def _recv_frame(sock: socket.socket):
    """解析一个帧，返回 (fin, opcode, payload, was_masked, used_extended_len)。"""
    b1, b2 = _recv_exact(sock, 2)
    fin = bool(b1 & 0x80)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    extended = False
    if length == 126:
        length = struct.unpack(">H", _recv_exact(sock, 2))[0]
        extended = True
    elif length == 127:
        length = struct.unpack(">Q", _recv_exact(sock, 8))[0]
        extended = True
    mask = _recv_exact(sock, 4) if masked else None
    payload = _recv_exact(sock, length) if length else b""
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload, masked, extended


def _ws_read_message(sock: socket.socket):
    """读一条完整消息（处理分片重组；自动回 pong；忽略 pong；遇 close 抛错）。
    返回 (opcode, payload_bytes)。"""
    buf = bytearray()
    first_opcode = None
    while True:
        fin, opcode, payload, _masked, _ext = _recv_frame(sock)
        if opcode == 0x8:  # close
            raise ConnectionError("对端发送 close 帧")
        if opcode == 0x9:  # ping -> pong（回显 payload）
            sock.sendall(_build_frame(0xA, payload, masked=True))
            continue
        if opcode == 0xA:  # pong
            continue
        if first_opcode is None:
            first_opcode = opcode
        buf += payload
        if fin:
            return first_opcode, bytes(buf)


def _ws_connect(host: str, port: int, path: str, timeout: float) -> socket.socket:
    """建立 TCP 并完成 HTTP Upgrade 握手，校验 Sec-WebSocket-Accept。"""
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        "GET {} HTTP/1.1\r\n"
        "Host: {}:{}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: {}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    ).format(path, host, port, key)
    sock.sendall(request.encode("ascii"))

    raw = bytearray()
    while b"\r\n\r\n" not in raw:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("握手期间连接被关闭")
        raw += chunk
        if len(raw) > 65536:
            raise ConnectionError("握手响应异常过大")
    head = bytes(raw).split(b"\r\n\r\n", 1)[0].decode("latin-1")
    lines = head.split("\r\n")
    if not lines or " 101" not in lines[0]:
        raise ConnectionError("握手被拒绝：{}".format(lines[0] if lines else "?"))
    accept = None
    for line in lines[1:]:
        if line.lower().startswith("sec-websocket-accept:"):
            accept = line.split(":", 1)[1].strip()
    expected = base64.b64encode(
        hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
    ).decode("ascii")
    if accept != expected:
        raise ConnectionError("Sec-WebSocket-Accept 校验失败")
    return sock


# ---------------------------------------------------------------------------
# CDP 交互
# ---------------------------------------------------------------------------
def _parse_ws_url(url: str):
    """解析 ws://host:port/path，返回 (host, port, path)；非法返回 None。"""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port or 80  # ★ parts.port 也要在 try 内：端口 >65535 时它抛 ValueError
    except ValueError:
        return None
    if parts.scheme not in ("ws", "wss"):
        return None
    host = parts.hostname
    if host not in ("127.0.0.1", "localhost", "::1"):  # 只连回环，绝不出网
        return None
    if host == "localhost":
        host = "127.0.0.1"
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return host, port, path


def _pick_target(targets) -> str | None:
    """从 /json/list 结果挑一个可调试目标，返回其 webSocketDebuggerUrl。"""
    if not isinstance(targets, list):
        return None
    fallback = None
    for t in targets:
        if not isinstance(t, dict):
            continue
        url = t.get("webSocketDebuggerUrl")
        if not isinstance(url, str) or not url:
            continue
        if t.get("type") == "page":
            return url
        if fallback is None:
            fallback = url
    return fallback


_msg_id = 0


def _cdp_evaluate(sock: socket.socket, expression: str, await_promise: bool,
                  timeout: float):
    """发送 Runtime.evaluate 并等待同 id 的响应；失败/超时返回 None。"""
    global _msg_id
    _msg_id += 1
    mid = _msg_id
    payload = json.dumps({
        "id": mid,
        "method": "Runtime.evaluate",
        "params": {
            "expression": expression,
            "awaitPromise": await_promise,
            "returnByValue": True,
        },
    }).encode("utf-8")
    try:
        sock.sendall(_build_frame(0x1, payload, masked=True))
    except OSError:
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            opcode, data = _ws_read_message(sock)
        except (OSError, ConnectionError, struct.error):
            return None
        if opcode != 0x1:  # 只关心文本帧
            continue
        try:
            msg = json.loads(data.decode("utf-8", "replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(msg, dict) and msg.get("id") == mid:
            return msg
    return None


def _find_string_value(obj):
    """在 CDP 响应里健壮地递归找出字符串值（优先 'value' 键）。
    响应可能是 {"id":1,"result":{"result":{"value":"<json字符串>"}}}，
    也可能是更深层的 result.result。"""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        value = obj.get("value")
        if isinstance(value, str):
            return value
        for v in obj.values():
            found = _find_string_value(v)
            if found is not None:
                return found
    if isinstance(obj, list):
        for v in obj:
            found = _find_string_value(v)
            if found is not None:
                return found
    return None


def _looks_like_token(value) -> bool:
    """只认 JWT 三段式（两个 '.'）或长度 > 40 的非空字符串，避免误取。"""
    if not isinstance(value, str):
        return False
    v = value.strip()
    if not v:
        return False
    return v.count(".") == 2 or len(v) > 40


def _normalize_domain(raw) -> str:
    if not isinstance(raw, str) or not raw.strip():
        return ""
    d = raw.strip()
    if not d.startswith(("http://", "https://")):
        d = "https://" + d
    return d.rstrip("/")


class _EncryptedEnvelope(Exception):
    """内部信号：会话里的 accessToken 仍是 {"$wbEncrypted": ...} 信封。"""


def _extract_from_session(session) -> dict:
    """从会话对象里提取 token/uid/domain；取不到 token 返回 None；
    遇到 $wbEncrypted 信封抛 _EncryptedEnvelope（由上层转为返回 None）。
    绝不记录/打印 token 本体。"""
    if isinstance(session, list):
        # 兜底表达式的返回：JWT 字符串数组
        for item in session:
            if _looks_like_token(item):
                return {"access_token": item, "uid": "", "domain": ""}
        return None
    if not isinstance(session, dict):
        return None

    auth = session.get("auth") if isinstance(session.get("auth"), dict) else {}
    token = None
    for container in (session, auth):
        if not isinstance(container, dict):
            continue
        for key in ("accessToken", "access_token"):
            v = container.get(key)
            if isinstance(v, dict) and "$wbEncrypted" in v:
                raise _EncryptedEnvelope()
            if _looks_like_token(v):
                token = v.strip()
                break
        if token:
            break
    if not token:
        return None

    account = session.get("account") if isinstance(session.get("account"), dict) else {}
    uid = ""
    for container in (account, session, auth):
        if not isinstance(container, dict):
            continue
        for key in ("uid", "userId"):
            v = container.get(key)
            if isinstance(v, str) and v.strip():
                uid = v.strip()
                break
            if isinstance(v, int):
                uid = str(v)
                break
        if uid:
            break

    domain = _normalize_domain(auth.get("domain") or session.get("domain"))
    return {"access_token": token, "uid": uid, "domain": domain}


# ---------------------------------------------------------------------------
# 公开入口
# ---------------------------------------------------------------------------
def load_via_cdp(port: int | None = None, timeout: float = 6.0) -> dict | None:
    """通过 CDP 从运行中的客户端取明文 token。
    返回 {"access_token": str, "uid": str, "domain": str,
           "source": "cdp-remote-debug"}；
    任何一步失败返回 None。绝不抛异常、绝不打印 token。"""
    try:
        p = port or cdp_port() or probe()
        if not p:
            _dbg("无可用 CDP 端口（未指定 / 环境变量非法 / 默认端口未监听）")
            return None

        targets = _http_get_json(p, "/json/list", timeout)
        ws_url = _pick_target(targets)
        if not ws_url:
            _dbg("/json/list 中没有可用调试目标")
            return None
        parsed = _parse_ws_url(ws_url)
        if not parsed:
            _dbg("调试目标 URL 非法或非回环地址")
            return None
        host, wport, path = parsed

        sock = _ws_connect(host, wport, path, timeout)
        try:
            token = ""
            uid = ""
            domain = ""
            for expression, await_promise in _EVAL_EXPRESSIONS:
                resp = _cdp_evaluate(sock, expression, await_promise, timeout)
                if resp is None:
                    continue
                raw = _find_string_value(resp)
                if not raw:
                    continue
                try:
                    session = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                try:
                    info = _extract_from_session(session)
                except _EncryptedEnvelope:
                    _dbg("会话仍为 $wbEncrypted 信封（该实例返回加密值），放弃")
                    return None
                if not info:
                    continue
                if not token and info.get("access_token"):
                    token = info["access_token"]
                if not uid and info.get("uid"):
                    uid = info["uid"]
                if not domain and info.get("domain"):
                    domain = info["domain"]
                if token and uid:
                    break
            if not token:
                _dbg("所有候选表达式均未取到 token")
                return None
            _dbg("已取到 token（不展示，长度 {}），uid 长度 {}".format(
                len(token), len(uid)))
            return {
                "access_token": token,
                "uid": uid,
                "domain": domain,
                "source": SOURCE_CDP,
            }
        finally:
            try:
                sock.sendall(_build_frame(0x8, b"", masked=True))
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
    except Exception as exc:  # load_via_cdp 必须永不抛出
        _dbg("load_via_cdp 失败：{}".format(type(exc).__name__))
        return None


# ---------------------------------------------------------------------------
# 自测：真实 socket 桩服务端 + 完整握手/帧/往返
# ---------------------------------------------------------------------------
_STUB_TOKEN = "eyJhbGciOiJIUzI1NiJ9.headerpart.payloadpart"
_STUB_UID = "u-123"


def _read_http_headers(conn: socket.socket) -> str | None:
    raw = bytearray()
    conn.settimeout(10)
    while b"\r\n\r\n" not in raw:
        try:
            chunk = conn.recv(4096)
        except OSError:
            return None
        if not chunk:
            return None
        raw += chunk
        if len(raw) > 65536:
            return None
    return bytes(raw).decode("latin-1")


class _StubCDPServer(threading.Thread):
    """最小 CDP 桩：同一端口先服务 /json/version 与 /json/list（HTTP），
    再接受一次 WebSocket Upgrade，校验客户端帧掩码与长度扩展，
    做一次 ping/pong，最后用「分片 + 126 长度扩展」回一条 CDP 响应。"""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.port = None
        self.ready = threading.Event()
        self.done = threading.Event()
        self.error = None
        self.saw_masked_client_frame = False
        self.saw_extended_length = False
        self.saw_pong_echo = False

    def run(self) -> None:
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("127.0.0.1", 0))
            srv.listen(4)
            srv.settimeout(20)
            self.port = srv.getsockname()[1]
            self.ready.set()
            while not self.done.is_set():
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    break
                try:
                    conn.settimeout(10)
                    headers = _read_http_headers(conn)
                    if headers is None:
                        continue
                    if "upgrade: websocket" in headers.lower():
                        self._serve_ws(conn, headers)
                        self.done.set()
                    else:
                        self._serve_http(conn, headers)
                finally:
                    try:
                        conn.close()
                    except OSError:
                        pass
            srv.close()
        except Exception as exc:  # noqa: BLE001 - 桩内部错误仅记录，不外抛
            self.error = repr(exc)
        finally:
            self.ready.set()
            self.done.set()

    def _serve_http(self, conn: socket.socket, headers: str) -> None:
        request_line = headers.split("\r\n", 1)[0]
        parts = request_line.split(" ")
        path = parts[1] if len(parts) > 1 else "/"
        if path.startswith("/json/list"):
            body = json.dumps([{
                "id": "stub-page-1",
                "type": "page",
                "title": "WorkBuddy (stub)",
                "url": "https://copilot.tencent.com/",
                "webSocketDebuggerUrl":
                    "ws://127.0.0.1:{}/devtools/page/stub-page-1".format(self.port),
            }]).encode("utf-8")
        else:  # /json/version 及其它
            body = json.dumps({
                "Browser": "cdp-token-stub/1.0",
                "webSocketDebuggerUrl":
                    "ws://127.0.0.1:{}/devtools/browser/stub".format(self.port),
            }).encode("utf-8")
        conn.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(body)).encode("ascii")
            + b"\r\nConnection: close\r\n\r\n" + body
        )

    def _serve_ws(self, conn: socket.socket, headers: str) -> None:
        key = None
        for line in headers.split("\r\n"):
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
        if not key:
            raise ValueError("缺少 Sec-WebSocket-Key")
        accept = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        conn.sendall((
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Accept: {}\r\n\r\n"
        ).format(accept).encode("ascii"))

        # 1) 读客户端的 Runtime.evaluate 请求帧：必须带掩码
        fin, opcode, payload, masked, extended = _recv_frame(conn)
        if not masked:
            raise ValueError("客户端帧未掩码，违反 RFC6455")
        self.saw_masked_client_frame = True
        if extended:
            self.saw_extended_length = True
        if opcode != 0x1:
            raise ValueError("期望文本帧，得到 opcode={}".format(opcode))
        request = json.loads(payload.decode("utf-8"))
        mid = request.get("id")

        # 2) ping/pong 往返：桩发 ping，客户端必须回 pong 且回显 payload
        conn.sendall(_build_frame(0x9, b"ping-stub", masked=False))
        while True:
            _fin2, op2, pl2, _m2, _e2 = _recv_frame(conn)
            if op2 == 0x8:
                raise ConnectionError("客户端提前关闭")
            if op2 == 0xA:
                self.saw_pong_echo = (pl2 == b"ping-stub")
                break

        # 3) 回 CDP 响应：内容 >125 字节以强制 126 两字节长度扩展；
        #    并故意切成「FIN=0 文本帧 + FIN=1 续帧」覆盖分片重组。
        session_json = json.dumps({
            "accessToken": _STUB_TOKEN,
            "account": {"uid": _STUB_UID},
            "auth": {"domain": ""},
        })
        response_json = json.dumps({
            "id": mid,
            "result": {"result": {"type": "string", "value": session_json}},
        })
        data = response_json.encode("utf-8")
        if len(data) <= 125:
            raise ValueError("桩响应长度未超过 125，无法覆盖 126 长度分支")
        cut = len(data) // 2
        conn.sendall(_build_frame(0x1, data[:cut], masked=False, fin=False))
        conn.sendall(_build_frame(0x0, data[cut:], masked=False, fin=True))


def _unused_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def self_test() -> bool:
    """内置自测（真实 socket，不 mock）：
      ① WebSocket HTTP Upgrade 握手（含 Sec-WebSocket-Accept 校验）
      ② 客户端帧掩码与 126 长度扩展（桩服务端校验）
      ③ 服务端帧解析（126 长度 + 分片文本帧重组）
      ④ Runtime.evaluate 请求/响应往返（含 ping/pong）
      ⑤ 从桩返回的会话 JSON 中解析出 access_token/uid
      ⑥ 边界：死端口时 probe()/load_via_cdp() 返回 None 且不抛异常
      ⑦ cdp_port() 环境变量解析
    全部通过返回 True。"""
    checks = []

    def check(name: str, ok: bool) -> None:
        checks.append(ok)
        sys.stderr.write("[self_test] {}: {}\n".format(
            name, "PASS" if ok else "FAIL"))

    stub = _StubCDPServer()
    stub.start()
    if not stub.ready.wait(5) or not stub.port:
        check("桩服务端启动", False)
        return False
    check("桩服务端启动", True)

    # ① HTTP 探测（/json/version）
    check("probe() 命中桩端口", probe(port=stub.port, timeout=2.0) == stub.port)

    # ①②③④⑤ 完整 CDP 流程
    cred = None
    try:
        cred = load_via_cdp(port=stub.port, timeout=5.0)
    except Exception:
        cred = None
    check("load_via_cdp() 返回结果", cred is not None)
    check("access_token 解析正确",
          isinstance(cred, dict) and cred.get("access_token") == _STUB_TOKEN)
    check("uid 解析正确",
          isinstance(cred, dict) and cred.get("uid") == _STUB_UID)
    check("source 标记正确",
          isinstance(cred, dict) and cred.get("source") == SOURCE_CDP)

    stub.done.wait(5)
    check("桩服务端无内部错误", stub.error is None)
    check("②客户端帧已掩码（桩校验）", stub.saw_masked_client_frame)
    check("②客户端帧使用 126 长度扩展（桩校验）", stub.saw_extended_length)
    check("④ping/pong 回显正确（桩校验）", stub.saw_pong_echo)

    # ⑥ 死端口边界：必须返回 None 且不抛异常
    dead = _unused_port()
    probe_ok = False
    try:
        probe_ok = probe(port=dead, timeout=0.5) is None
    except Exception:
        probe_ok = False
    check("⑥probe() 死端口返回 None", probe_ok)
    load_ok = False
    try:
        load_ok = load_via_cdp(port=dead, timeout=0.5) is None
    except Exception:
        load_ok = False
    check("⑥load_via_cdp() 死端口返回 None 且不抛异常", load_ok)

    # ⑦ 环境变量解析
    old = os.environ.get(ENV_PORT)
    try:
        os.environ[ENV_PORT] = "9222"
        r1 = cdp_port() == 9222
        os.environ[ENV_PORT] = "abc"
        r2 = cdp_port() is None
        os.environ[ENV_PORT] = "0"
        r3 = cdp_port() is None
        os.environ[ENV_PORT] = "99999"
        r4 = cdp_port() is None
    finally:
        if old is None:
            os.environ.pop(ENV_PORT, None)
        else:
            os.environ[ENV_PORT] = old
    check("⑦cdp_port() 解析合法/非法值", r1 and r2 and r3 and r4)

    ok = all(checks)
    sys.stderr.write("[self_test] 总计 {}/{} 通过\n".format(
        sum(1 for c in checks if c), len(checks)))
    return ok


if __name__ == "__main__":
    sys.exit(0 if self_test() else 1)
