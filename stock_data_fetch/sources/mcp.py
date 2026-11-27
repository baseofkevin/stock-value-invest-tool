"""MCP 的最小 JSON-RPC 客户端（传输层，只做"怎么拿"）。

搬自 `stock-judge-top-tool/judge/common/mcp.py`，改了三处：

1. **把 URL / 令牌变成构造参数**，不再读死 `judge.config`——同一个客户端要能给
   westock（令牌在 `~/.config/westock/tokens.json`）和 tdx（OAuth，目前拿不到）用；
2. **支持 `initialize` 握手**。westock 不握手也能直接 `tools/call`，tdx 走的是标准
   streamable-http，先用 `initialize` 拿 `Mcp-Session-Id` 更稳妥；
3. **把"没授权"单独识别成 `SourceUnavailable`**，让 router 立刻降级，而不是当成
   "这次没取到"去重试三次——401 是确定性错误，重试只是浪费时间。

`call()` 出错返回 `None` 而不是抛异常，原因写在 `last_error`：调用方才知道
"这段失败"能不能忍（重取一次 / 记 partial / 整段放弃）。
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ..errors import SourceUnavailable

log = logging.getLogger("stock_data_fetch.mcp")

UA = "stock_data_fetch/0.1 (+mcp)"
PROTOCOL = "2025-03-26"

#: 上游"没算完就回一段中文异常文本"的几种说法。**都是瞬态，不是源坏了。**
#:
#: 为什么必须分开：westock 的 `tool_filter` 有约 2 秒服务端硬超时（全市场 5030 行
#: 正好贴在红线上），超了就 HTTP 200 + `高级选股异常：… RoundTrip timeout …`。
#: 2026-11-27 实测同参数连打 8 次有 4 次超时——**重发就成功**。把它当确定性故障，
#: 一次抖动就会让一整天的横截面失败，进而整条 `./run.py all` 停在第 ① 步；
#: 而它本来只是一次重试的事。
_TRANSIENT_MARKERS = ("roundtrip timeout", "service error", "timeout",
                      "限频", "rate limit", "too many requests")


def is_transient(text: str) -> bool:
    """这段错误文本是不是"重发一次大概率就好"。判据只有关键词，**宁可认错方向**：
    多认成瞬态只是多重试两次；少认则会把一次抖动升级成整步失败。"""
    low = (text or "").lower()
    return any(m in low for m in _TRANSIENT_MARKERS)


def parse_body(body: str) -> dict:
    """响应可能是 SSE（`event:` / `data:` 行）或裸 JSON。两种都要认。"""
    text = body.strip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except ValueError:
            pass
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("data:"):
            s = s[5:].strip()
        if s.startswith("{"):
            try:
                return json.loads(s)
            except ValueError:
                continue
    raise SourceUnavailable("服务端返回无法解析：" + body[:200])


def pick_data(result: dict) -> dict:
    """从 `tools/call` 的 result 里取出业务数据。

    三种形态都见过：
      - `structuredContent.data`   （多数官方连接器）
      - `content[0].text` = `{"data": ...}`（westock）
      - `content[0].text` = 人读摘要 + 一行 JSON（tdx）→ 取最后一个能解析的 `{`
    """
    sc = result.get("structuredContent")
    if isinstance(sc, dict):
        return sc.get("data", sc)

    texts = [item.get("text") or "" for item in (result.get("content") or [])
             if item.get("type") == "text"]
    for txt in texts:
        txt = txt.strip()
        if not txt:
            continue
        for candidate in _json_candidates(txt):
            try:
                payload = json.loads(candidate)
            except ValueError:
                continue
            if isinstance(payload, dict):
                if payload.get("ok") is False:
                    raise SourceUnavailable(f"上游返回失败：{payload.get('message')}")
                return payload.get("data", payload)
    tail = texts[0][:120] if texts else "空"
    # 超时文本走的就是这条路：上游 200 + 一段中文异常，一个 `{` 都没有。
    # 判成瞬态，`McpClient.call` 才会重试、router 才会按瞬断处理。
    raise SourceUnavailable("响应里没有可用的数据字段：" + tail,
                            transient=is_transient(tail))


def _json_candidates(text: str):
    """从一段文本里依次切出可能的 JSON 对象（从每个 `{` 开始，配对到末尾）。"""
    start = text.find("{")
    while start >= 0:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield text[start:i + 1]
                    break
        start = text.find("{", start + 1)


class McpClient:
    """一个 MCP 端点。`token` 为空时按无鉴权调用（westock 之外的大多数端点要鉴权）。"""

    def __init__(self, url: str, *, token: str | None = None, timeout: float = 60.0,
                 handshake: bool = False, headers: dict | None = None) -> None:
        self.url = url
        self.token = token
        self.timeout = timeout
        self.handshake = handshake
        self.headers = dict(headers or {})
        self.session_id: str | None = None
        self.last_error: str = ""
        self.last_transient: bool = False
        """上一次失败是不是瞬态（`is_transient` 的判定）。调用方 `try/except` 不了
        `None` 的返回值，靠这个标记决定"要不要把这次失败升级成整天失败"——
        `westock._scan` 就把它原样传给 `SourceUnavailable(transient=…)`。"""
        # 连接复用：一次批量要打几十次 tools/call，每次重新 TLS 握手实测
        # 0.3–2.8s；复用后 0.15–0.35s。每个线程一条连接，Hub 不跨线程。
        parts = urllib.parse.urlsplit(self.url)
        self._scheme = parts.scheme or "https"
        self._host = parts.hostname or ""
        self._port = parts.port
        self._path = parts.path or "/"
        if parts.query:
            self._path += "?" + parts.query
        self._local = threading.local()
        # 有代理时退回 urllib：http.client 不会自动走 HTTPS_PROXY。
        self._use_proxy = bool(os.environ.get("HTTPS_PROXY")
                               or os.environ.get("https_proxy")
                               or os.environ.get("ALL_PROXY")
                               or os.environ.get("all_proxy"))

    # ---- 底层 ----

    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            if self._scheme == "http":
                conn = http.client.HTTPConnection(self._host, self._port,
                                                  timeout=self.timeout)
            else:
                conn = http.client.HTTPSConnection(
                    self._host, self._port, timeout=self.timeout,
                    context=ssl.create_default_context())
            self._local.conn = conn
        return conn

    def _drop_conn(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            finally:
                self._local.conn = None

    def _post_urllib(self, body: bytes, h: dict) -> tuple[int, dict, str]:
        req = urllib.request.Request(self.url, data=body, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers or {}), e.read().decode("utf-8", "replace")
        except urllib.error.URLError as e:
            raise SourceUnavailable(f"连不上 {self.url}：{e.reason}") from e

    def _post(self, payload: dict) -> tuple[int, dict, str]:
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream",
             "User-Agent": UA}
        h.update(self.headers)
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        body = json.dumps(payload).encode()
        if self._use_proxy or self._scheme not in ("http", "https"):
            return self._post_urllib(body, h)

        last: Exception | None = None
        for attempt in range(2):
            conn = self._conn()
            try:
                conn.request("POST", self._path, body=body, headers=h)
                resp = conn.getresponse()
                data = resp.read()
                status, headers = resp.status, dict(resp.getheaders())
                if resp.will_close:
                    self._drop_conn()
                return status, headers, data.decode("utf-8", "replace")
            except (http.client.HTTPException, OSError) as e:
                # 复用到的连接可能已被服务端关掉：丢掉重连一次，不让整只票失败。
                last = e
                self._drop_conn()
        raise SourceUnavailable(f"连不上 {self.url}：{last}") from last

    def _rpc(self, method: str, params: dict) -> dict:
        status, headers, body = self._post(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})

        if status == 401:
            raise SourceUnavailable(
                f"{self.url} 需要 OAuth 授权（HTTP 401）。"
                + ("当前没有配置令牌。" if not self.token else "当前令牌已失效。"))
        if status >= 400:
            raise SourceUnavailable(f"HTTP {status}：{body[:200]}")

        sid = _header(headers, "mcp-session-id")
        if sid:
            self.session_id = sid

        res = parse_body(body)
        if res.get("error"):
            raise SourceUnavailable(f"JSON-RPC 错误：{res['error']}")
        if "result" not in res:
            raise SourceUnavailable(f"响应缺少 result：{str(res)[:200]}")
        return res["result"]

    def open(self) -> None:
        """`initialize` + `notifications/initialized`。重复调用是幂等的。"""
        if self.session_id or not self.handshake:
            return
        self._rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                 "clientInfo": {"name": "stock_data_fetch", "version": "0.1"}})
        try:                              # 通知没有返回体，失败不影响后续调用
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        except SourceUnavailable:         # pragma: no cover - 有的服务端不认这条
            pass

    # ---- 对外 ----

    def call(self, tool: str, args: dict, *, tries: int = 1,
             delay: float = 0.0) -> dict | None:
        """调一个工具，返回业务数据；失败返回 `None`（原因在 `last_error`）。

        `SourceUnavailable`（鉴权/不可达/接口变更）**不吞**，直接往上抛——
        这类错误重试无意义，而且 router 要靠它决定降级。

        `delay`：两次尝试之间的间隔（秒）。默认 0 保持老行为。**只给"一次请求
        成本很高"的域用**（`tool_filter` 一次要服务端算全市场，连发容易继续超时），
        逐只取数的域不要设——那会把 4000 只的总时长拉爆。
        """
        self.last_error = ""
        self.last_transient = False
        last: Exception | None = None
        for i in range(max(1, tries)):
            if i and delay > 0:
                time.sleep(delay)
            try:
                self.open()
                data = pick_data(self._rpc("tools/call",
                                           {"name": tool, "arguments": args}))
                # 成功就把上一次的失败痕迹擦掉：`last_transient` 的含义是
                # "**这次**调用的结果是瞬态失败"，上一次抖动不能粘到这一次上
                # （否则 `_scan` 会把已经成功的那天又标成瞬态）。
                self.last_error = ""
                self.last_transient = False
                return data
            except SourceUnavailable as e:
                self.last_error = str(e)
                if "401" in str(e) or "OAuth" in str(e):
                    raise                 # 确定性错误，交给 router 降级
                self.last_transient = bool(e.ctx.get("transient")) or is_transient(str(e))
                last = e
            except ValueError as e:       # JSON 解析失败：偶发，可以重试
                self.last_error = str(e)
                self.last_transient = True
                last = e
        log.debug("%s 调用失败（%d 次，瞬态=%s）：%s", tool, tries,
                  self.last_transient, self.last_error)
        del last
        return None


def _header(headers: dict, name: str) -> str | None:
    for k, v in (headers or {}).items():
        if k.lower() == name:
            return v
    return None


# ---- 令牌 ----

def read_token_file(path: Path | str, *keys: str) -> str | None:
    """从 JSON 令牌文件里取第一个非空的键。`keys` 默认 `accessToken` / `access_token`。"""
    p = Path(path).expanduser()
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for k in keys or ("accessToken", "access_token"):
        v = raw.get(k)
        if isinstance(v, str) and v:
            return v
    return None
