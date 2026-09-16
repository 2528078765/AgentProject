"""零依赖的 Edge TTS 客户端（自己实现 WebSocket 传输）。

为什么要有这个文件：

Edge 官方的在线 TTS 免费、无需 API Key、中文自然度很好，但官方 Python 客户端
`edge-tts` 依赖 `aiohttp`（含 C 扩展）。在装不了包的机器上（pip 被沙箱/权限挡住、
或没有网络装依赖），这条路就断了。

这个模块**只用标准库**（socket + ssl + hashlib + uuid）实现了：
    * 最小可用的 WebSocket 客户端（握手 / 掩码帧 / 分片 / ping-pong）
    * Edge TTS 的命令帧与 SSML 帧格式
    * Sec-MS-GEC 鉴权令牌（时间片 + SHA256）
    * 二进制音频帧的解析，以及 word boundary 元数据的收集

协议细节取自 edge-tts 7.2.8 的源码（constants.py / drm.py / communicate.py），
其中两点容易踩坑、这里都保留了原样：
    1. SSML 帧的 X-Timestamp 结尾会多一个 "Z" —— 看着像 bug，但不加会失败。
    2. speech.config 帧的 X-Timestamp **不带** "Z"。两条消息格式是不一样的。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import ssl
import struct
import time
import uuid
import xml.sax.saxutils as saxutils
from typing import Any

from .errors import AutoVidError

# --------------------------------------------------------------------------- #
# 协议常量（与 edge-tts 7.2.8 对齐）
# --------------------------------------------------------------------------- #
BASE_URL = "speech.platform.bing.com/consumer/speech/synthesize/readaloud"
TRUSTED_CLIENT_TOKEN = "6A5AA1D4EAFF4E9FB37E23D68491D6F4"
WSS_HOST = "speech.platform.bing.com"
WSS_PATH = f"/consumer/speech/synthesize/readaloud/edge/v1"

CHROMIUM_FULL_VERSION = "143.0.3650.75"
CHROMIUM_MAJOR_VERSION = CHROMIUM_FULL_VERSION.split(".", maxsplit=1)[0]
SEC_MS_GEC_VERSION = f"1-{CHROMIUM_FULL_VERSION}"

OUTPUT_FORMAT = "audio-24khz-48kbitrate-mono-mp3"
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

WIN_EPOCH = 11644473600
_HEADERS = {
    "Pragma": "no-cache",
    "Cache-Control": "no-cache",
    "Origin": "chrome-extension://jdiccldimpdaibmpdkjnbmckianbfold",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        f" (KHTML, like Gecko) Chrome/{CHROMIUM_MAJOR_VERSION}.0.0.0 Safari/537.36"
        f" Edg/{CHROMIUM_MAJOR_VERSION}.0.0.0"
    ),
}


# --------------------------------------------------------------------------- #
# 鉴权令牌
# --------------------------------------------------------------------------- #
_clock_skew_seconds = 0.0


def _unix_now() -> float:
    return time.time() + _clock_skew_seconds


def generate_sec_ms_gec() -> str:
    """Sec-MS-GEC = SHA256(Windows 文件时间(按 5 分钟取整, 100ns 单位) + 令牌)。"""
    ticks = _unix_now()
    ticks += WIN_EPOCH                 # 换到 1601-01-01 起点
    ticks -= ticks % 300               # 向下取整到 5 分钟
    ticks *= 1e9 / 100                 # 转成 100 纳秒整数
    return hashlib.sha256(f"{ticks:.0f}{TRUSTED_CLIENT_TOKEN}".encode("ascii")).hexdigest().upper()


def _adjust_clock_skew(server_date: str) -> None:
    """服务端返回 403 通常意味着本机时钟偏移，用响应头里的 Date 校正一次。"""
    global _clock_skew_seconds
    try:
        from email.utils import parsedate_to_datetime
        server_ts = parsedate_to_datetime(server_date).timestamp()
    except Exception:  # noqa: BLE001
        return
    _clock_skew_seconds += server_ts - time.time()


def _date_to_string() -> str:
    return time.strftime(
        "%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", time.gmtime()
    )


# --------------------------------------------------------------------------- #
# 最小 WebSocket 客户端
# --------------------------------------------------------------------------- #
class WebSocketError(AutoVidError):
    pass


class _WebSocket:
    """只实现这个场景需要的那部分 RFC 6455。"""

    def __init__(self, host: str, path: str, headers: dict[str, str], timeout: float = 30.0):
        self.host = host
        self.timeout = timeout
        self._buffer = b""

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        handshake = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            *[f"{name}: {value}" for name, value in headers.items()],
            "", "",
        ]
        raw = "\r\n".join(handshake).encode("ascii")

        context = ssl.create_default_context()
        self._sock = socket.create_connection((host, 443), timeout=timeout)
        self._sock = context.wrap_socket(self._sock, server_hostname=host)
        self._sock.settimeout(timeout)
        self._sock.sendall(raw)

        status, resp_headers = self._read_http_response()
        if status != 101:
            body_hint = resp_headers.get("x-msedge-ref", "")
            self.close()
            raise WebSocketError(f"WebSocket 握手失败：HTTP {status} {body_hint}")

        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if resp_headers.get("sec-websocket-accept", "") != expected:
            self.close()
            raise WebSocketError("WebSocket 握手校验失败：Sec-WebSocket-Accept 不匹配")

    # ---------------------------------------------------------- HTTP 升级响应
    def _read_http_response(self) -> tuple[int, dict[str, str]]:
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WebSocketError("握手阶段连接被关闭")
            head += chunk
        head, _, rest = head.partition(b"\r\n\r\n")
        self._buffer = rest
        lines = head.decode("latin-1").split("\r\n")
        status = int(lines[0].split(" ")[1]) if len(lines[0].split(" ")) > 1 else 0
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        return status, headers

    # ---------------------------------------------------------- 收发
    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        mask = os.urandom(4)                                  # 客户端帧必须掩码
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def _recv_exact(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self._sock.recv(max(4096, count - len(self._buffer)))
            if not chunk:
                raise WebSocketError("连接被服务端关闭")
            self._buffer += chunk
        data, self._buffer = self._buffer[:count], self._buffer[count:]
        return data

    def recv_message(self) -> tuple[int, bytes]:
        """返回 (opcode, payload)。自动处理 ping/pong 与分片续帧。"""
        fragments = bytearray()
        first_opcode = 0
        while True:
            b0, b1 = self._recv_exact(2)
            fin = bool(b0 & 0x80)
            opcode = b0 & 0x0F
            masked = bool(b1 & 0x80)
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length) if length else b""
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

            if opcode == 0x9:                                  # ping -> pong
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:                                  # pong
                continue
            if opcode == 0x8:                                  # close
                raise WebSocketError("服务端主动关闭连接")

            if opcode != 0x0:
                first_opcode = opcode
            fragments += payload
            if fin:
                return first_opcode, bytes(fragments)

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except Exception:  # noqa: BLE001
            pass
        try:
            self._sock.close()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# 文本处理
# --------------------------------------------------------------------------- #
_INCOMPATIBLE = {c for c in range(0x20) if c not in (0x9, 0xA, 0xD)}


def sanitize_text(text: str) -> str:
    """去掉控制字符并做 XML 转义 —— 文本要嵌进 SSML。"""
    cleaned = "".join(ch for ch in text if ord(ch) not in _INCOMPATIBLE)
    return saxutils.escape(cleaned)


def split_text(text: str, max_bytes: int = 4096) -> list[str]:
    """按 UTF-8 字节数切分，避免单条 SSML 超限。"""
    chunks: list[str] = []
    current = ""
    for char in text:
        if len((current + char).encode("utf-8")) > max_bytes:
            chunks.append(current)
            current = char
        else:
            current += char
    if current:
        chunks.append(current)
    return chunks or [""]


# --------------------------------------------------------------------------- #
# 合成
# --------------------------------------------------------------------------- #
def _mkssml(voice: str, rate: str, volume: str, pitch: str, escaped_text: str) -> str:
    return (
        "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='en-US'>"
        f"<voice name='{voice}'>"
        f"<prosody pitch='{pitch}' rate='{rate}' volume='{volume}'>"
        f"{escaped_text}"
        "</prosody>"
        "</voice>"
        "</speak>"
    )


def _parse_audio_frame(payload: bytes) -> bytes:
    """二进制帧 = 2 字节大端头长度 + 头文本 + 音频数据。"""
    if len(payload) < 2:
        return b""
    header_len = struct.unpack(">H", payload[:2])[0]
    return payload[2 + header_len:]


def _parse_metadata(payload: bytes) -> list[dict[str, Any]]:
    """audio.metadata 文本帧：头 + JSON，取里面的 WordBoundary 时间轴。"""
    _, _, body = payload.partition(b"\r\n\r\n")
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []
    out: list[dict[str, Any]] = []
    for item in data.get("Metadata") or []:
        if item.get("Type") == "WordBoundary":
            out.append({
                "text": item.get("Data", {}).get("text", ""),
                # 100 纳秒 -> 秒
                "start": round(int(item.get("Offset", 0)) / 1e7, 4),
                "duration": round(int(item.get("Duration", 0)) / 1e7, 4),
            })
    return out


def synthesize(
    text: str,
    voice: str = DEFAULT_VOICE,
    rate: str = "+0%",
    volume: str = "+0%",
    pitch: str = "+0Hz",
    connect_timeout: float = 15.0,
    receive_timeout: float = 90.0,
    retries: int = 2,
) -> dict[str, Any]:
    """合成一段文本，返回 {'audio': mp3 bytes, 'boundaries': [...], 'voice': ...}。

    失败会重试（含时钟偏移校正）—— Edge 的接口对时间敏感，本机时钟偏一点就会 403。
    """
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return _synthesize_once(text, voice, rate, volume, pitch,
                                    connect_timeout, receive_timeout)
        except WebSocketError as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(1.0 + attempt * 1.5)
        except (OSError, ssl.SSLError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(1.0 + attempt * 1.5)
    raise AutoVidError(f"Edge TTS 合成失败（重试 {retries} 次后）：{last_error}")


def _synthesize_once(
    text: str,
    voice: str,
    rate: str,
    volume: str,
    pitch: str,
    connect_timeout: float,
    receive_timeout: float,
) -> dict[str, Any]:
    url = (
        f"{WSS_PATH}?TrustedClientToken={TRUSTED_CLIENT_TOKEN}"
        f"&Sec-MS-GEC={generate_sec_ms_gec()}&Sec-MS-GEC-Version={SEC_MS_GEC_VERSION}"
    )
    ws = _WebSocket(WSS_HOST, url, _HEADERS, timeout=connect_timeout)
    try:
        # 1) 命令帧：告诉服务端要什么格式、要不要 word boundary
        ws.send_text(
            f"X-Timestamp:{_date_to_string()}\r\n"
            "Content-Type:application/json; charset=utf-8\r\n"
            "Path:speech.config\r\n\r\n"
            '{"context":{"synthesis":{"audio":{"metadataoptions":{'
            '"sentenceBoundaryEnabled":"false","wordBoundaryEnabled":"true"'
            '},"outputFormat":"' + OUTPUT_FORMAT + '"}}}}'
            "\r\n"
        )

        # 2) SSML 帧。注意 X-Timestamp 结尾的 "Z" 是微软的既定行为，不能省。
        request_id = uuid.uuid4().hex
        ssml = _mkssml(voice, rate, volume, pitch, sanitize_text(text))
        ws.send_text(
            f"X-RequestId:{request_id}\r\n"
            "Content-Type:application/ssml+xml\r\n"
            f"X-Timestamp:{_date_to_string()}Z\r\n"
            "Path:ssml\r\n\r\n"
            f"{ssml}"
        )

        # 3) 收流，直到 turn.end
        ws._sock.settimeout(receive_timeout)   # noqa: SLF001 - 内部细节，这里就该这么用
        audio = bytearray()
        boundaries: list[dict[str, Any]] = []
        while True:
            opcode, payload = ws.recv_message()
            if opcode == 0x2:                                  # 二进制 = 音频
                audio += _parse_audio_frame(payload)
                continue
            if opcode != 0x1:                                  # 其他类型忽略
                continue
            head, _, _ = payload.partition(b"\r\n\r\n")
            path = ""
            for line in head.decode("utf-8", errors="replace").split("\r\n"):
                if line.lower().startswith("path:"):
                    path = line.split(":", 1)[1].strip().lower()
            if path == "turn.end":
                break
            if path == "audio.metadata":
                boundaries.extend(_parse_metadata(payload))

        if not audio:
            raise WebSocketError("服务端没有返回任何音频数据")
        return {"audio": bytes(audio), "boundaries": boundaries, "voice": voice}
    finally:
        ws.close()


def list_voices(timeout: float = 20.0) -> list[dict[str, Any]]:
    """拉取可用音色列表（含中文音色）。"""
    import urllib.request

    url = f"https://{BASE_URL}/voices/list?trustedclienttoken={TRUSTED_CLIENT_TOKEN}"
    request = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise AutoVidError(f"拉取音色列表失败：{exc}") from exc
    return data if isinstance(data, list) else []
