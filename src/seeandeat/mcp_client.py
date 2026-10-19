"""麦当劳 MCP Server 的最小 Streamable HTTP 客户端。

只依赖标准库，避免为跑一个 skill 拉一整个 MCP SDK。
Token 只从环境变量读取，绝不落盘、绝不写进日志。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

DEFAULT_URL = "https://mcp.mcd.cn"
DEFAULT_TIMEOUT = 45


class McpError(RuntimeError):
    """MCP 调用失败。"""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class McdMcpClient:
    """麦当劳 MCP Server (Streamable HTTP) 客户端。"""

    def __init__(
        self,
        token: str | None = None,
        url: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.token = token or os.environ.get("MCD_MCP_TOKEN", "").strip()
        if not self.token:
            raise McpError(
                "缺少 MCP Token。请先 export MCD_MCP_TOKEN=<你的麦当劳 MCP Token>，"
                "申请地址：https://open.mcd.cn/mcp"
            )
        self.url = url or os.environ.get("MCD_MCP_URL", DEFAULT_URL)
        self.timeout = timeout
        self._id = 0
        self._session_id: str | None = None

    # ------------------------------------------------------------------ 传输层
    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        req = urllib.request.Request(self.url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                # 服务端可能回 SSE，也可能直接回 JSON，两种都要能解
                new_sid = resp.headers.get("Mcp-Session-Id")
                if new_sid:
                    self._session_id = new_sid
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:  # 401 / 429 等
            detail = exc.read().decode("utf-8", "ignore")[:300]
            hint = {
                401: "MCP Token 无效或已过期（401），请到 https://open.mcd.cn/mcp 重新获取",
                429: "触发限流（429），麦当劳 MCP 限制 600 次/分钟，请稍后重试",
            }.get(exc.code, f"MCP 请求失败 HTTP {exc.code}")
            raise McpError(f"{hint} | {detail}", code=exc.code) from exc
        except urllib.error.URLError as exc:
            raise McpError(f"无法连接 MCP Server：{exc.reason}") from exc

        return self._decode(raw)

    @staticmethod
    def _decode(raw: str) -> dict[str, Any]:
        raw = raw.strip()
        if not raw:
            raise McpError("MCP Server 返回空响应")
        # SSE: 若干 `data:` 行，取最后一个可解析的 JSON-RPC 报文
        if raw.startswith("event:") or "\ndata:" in raw or raw.startswith("data:"):
            parsed: dict[str, Any] | None = None
            for line in raw.splitlines():
                if line.startswith("data:"):
                    chunk = line[5:].strip()
                    if not chunk or chunk == "[DONE]":
                        continue
                    try:
                        parsed = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
            if parsed is None:
                raise McpError(f"无法解析 SSE 响应：{raw[:200]}")
            return parsed
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise McpError(f"无法解析响应：{raw[:200]}") from exc

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        result = self._post(
            {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        )
        if "error" in result:
            err = result["error"]
            raise McpError(f"MCP 调用 {method} 失败：{err.get('message', err)}")
        return result

    # ------------------------------------------------------------------ 工具层
    def tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """调用 MCP tool，返回结构化数据（优先 structuredContent，其次文本）。"""
        result = self.call("tools/call", {"name": name, "arguments": arguments or {}})
        return self.unwrap(result)

    @staticmethod
    def unwrap(result: dict[str, Any]) -> Any:
        res = result.get("result", {})
        if res.get("isError"):
            text = "".join(
                c.get("text", "") for c in res.get("content", []) if isinstance(c, dict)
            )
            raise McpError(f"MCP tool 返回错误：{text[:300]}")
        if res.get("structuredContent"):
            return res["structuredContent"]
        parts = [
            c.get("text", "")
            for c in res.get("content", [])
            if isinstance(c, dict) and c.get("type") == "text"
        ]
        text = "\n".join(parts)
        parsed = McdMcpClient._extract_json(text)
        return parsed if parsed is not None else text

    @staticmethod
    def _extract_json(text: str) -> Any | None:
        """从 `## Original Response` 之后的正文里抠出真实业务 JSON。"""
        marker = "## Original Response"
        if marker in text:
            text = text.split(marker, 1)[1]
        text = text.strip()
        # 去掉尾部的展示提示块
        for cut in ("\n## ", "\n当 reservation"):
            if cut in text:
                text = text.split(cut, 1)[0]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
