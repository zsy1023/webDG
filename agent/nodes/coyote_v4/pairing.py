"""配对：``targetId`` → App 能扫的 WebSocket URL + 二维码。

规则来自官方 SDK（`reference/dglab-kit/README.md` §"生成 APP 配对二维码"，实测过的原文）：

.. code-block:: ts

    const appSocketUrl = `wss://trex.dungeon-lab.cn/v4/?tid=${targetId}`;
    const qrcode = `https://dungeon-lab.cn/s/?v=1&action=socket&url=${encodeURIComponent(appSocketUrl)}`;

两个要点，很容易写错：

1. Query 名是 **``tid``**，不是 ``targetId``（README 明确点出这条）；
2. 二维码里装的是 **dungeon-lab.cn 的跳转链接**（``action=socket``），而不是裸的 ws URL ——
   App 扫这个链接才会被拉起来并连到我们的控制方。

自建服务端时把 :data:`DEFAULT_V4_WS_URL` 换成自己的地址即可（README 也这么写）。
"""

from __future__ import annotations

import logging
import socket
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

logger = logging.getLogger("coyotelink.coyote_v4.pairing")

__all__ = [
    "DEFAULT_V4_WS_URL",
    "DEFAULT_QR_BASE",
    "DEFAULT_QR_PATH",
    "DEFAULT_WS_PATH",
    "lan_ip",
    "reachable_url",
    "app_socket_url",
    "pairing_link",
    "qr_png",
    "qr_ascii",
]

#: V4 协议默认服务端（官方公共中继；自建时覆盖它）
DEFAULT_V4_WS_URL = "wss://trex.dungeon-lab.cn/v4/"

#: App 配对的跳转链接
DEFAULT_QR_BASE = "https://dungeon-lab.cn"
DEFAULT_QR_PATH = "/s/"

#: 服务端地址没给路径时的默认路径
DEFAULT_WS_PATH = "/v4/"


def lan_ip() -> str:
    """本机在局域网里的地址（给手机扫的地址要用它）。

    用 UDP "connect" 让内核按**默认路由**挑源地址 —— 不发任何包，也不需要联网。
    这样会自动避开 VMware / Radmin / Hyper-V 那些虚拟网卡（它们不在默认路由上）。
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))       # UDP connect 不发包，只查路由
            return str(sock.getsockname()[0])
        finally:
            sock.close()
    except OSError:
        return ""


#: 这些主机名手机连不上（`127.0.0.1` 会连到**手机自己**，`0.0.0.0` 不是可连地址）
_LOOPBACK_HOSTS = frozenset({"", "127.0.0.1", "localhost", "::1", "0.0.0.0", "[::1]"})


def reachable_url(url: str, *, ip: str = "") -> str:
    """把 URL 里的回环/通配主机换成**手机能连的局域网 IP**。

    ⚠️ 实测踩过（用户原话："你生成的 socket 二维码是什么玩意，回环地址，你搞笑呢？"）：
    配置里写的是 `ws://127.0.0.1:9997`（本机自己连自己用的），配对二维码**原样**带出去 ——
    手机 App 扫了就去连"手机自己"的 9997，永远连不上。

    只替换主机，端口 / 路径 / query 一律保持原样。
    """
    host = (ip or lan_ip()).strip()
    if not host:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if (parts.hostname or "") not in _LOOPBACK_HOSTS:
        return url                          # 本来就是可达地址，别动
    netloc = host
    if parts.port:
        netloc = f"{host}:{parts.port}"
    if parts.username:
        auth = parts.username + (f":{parts.password}" if parts.password else "")
        netloc = f"{auth}@{netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def app_socket_url(target_id: str, server_url: str = DEFAULT_V4_WS_URL) -> str:
    """``targetId`` → App 侧要连的 ws 地址：``wss://host/v4/?tid=<targetId>``。

    - ``server_url`` 可以带或不带路径与尾斜杠（自建服务端常见 ``ws://127.0.0.1:9998``）；
    - 已有的 query 会被保留（``tid`` 覆盖同名项）。
    """
    if not isinstance(target_id, str) or not target_id.strip():
        raise ValueError("targetId 必须是非空字符串")
    parts = urlsplit(server_url.strip())
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"服务端地址缺少 scheme/host：{server_url!r}（例：wss://trex.dungeon-lab.cn/v4/）")
    path = parts.path or DEFAULT_WS_PATH
    if not path.endswith("/"):
        path += "/"
    # 保留已有 query，但 tid 以我们给的为准
    existing = [(k, v) for k, v in _parse_query(parts.query) if k != "tid"]
    query = urlencode([("tid", target_id.strip()), *existing])
    return urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


def pairing_link(
    target_id: str,
    *,
    server_url: str = DEFAULT_V4_WS_URL,
    qr_base: str = DEFAULT_QR_BASE,
    qr_path: str = DEFAULT_QR_PATH,
) -> str:
    """``targetId`` → 给 App 扫的二维码内容（``https://dungeon-lab.cn/s/?v=1&action=socket&url=…``）。"""
    ws_url = app_socket_url(target_id, server_url)
    query = urlencode({"v": 1, "action": "socket", "url": ws_url}, quote_via=quote)
    return f"{qr_base.rstrip('/')}{qr_path}?{query}"


def _parse_query(query: str) -> list[tuple[str, str]]:
    if not query:
        return []
    out: list[tuple[str, str]] = []
    for chunk in query.split("&"):
        if not chunk:
            continue
        key, _, value = chunk.partition("=")
        out.append((key, value))
    return out


# ──────────────────────────────────────────────────────────────
# 二维码（`qrcode` 库已装；只有真要出图时才 import）
# ──────────────────────────────────────────────────────────────
def _make_qr(link: str):
    try:
        import qrcode  # 延迟导入：没装 qrcode 也不影响节点其余功能
    except ImportError as exc:  # pragma: no cover - 环境问题
        raise RuntimeError("生成二维码需要 qrcode：pip install qrcode（本机已装）") from exc
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(link)
    qr.make(fit=True)
    return qr


def qr_png(link: str, path: str | Path) -> Path:
    """把配对链接存成 PNG（返回落盘路径）。"""
    file = Path(path)
    if file.parent and not file.parent.exists():
        file.parent.mkdir(parents=True, exist_ok=True)
    image = _make_qr(link).make_image()
    image.save(file)
    logger.info("配对二维码已写入 %s", file)
    return file


def qr_ascii(link: str, *, margin: bool = True) -> str:
    """把配对链接渲染成**终端里可直接扫**的 ASCII/半块字符二维码。

    不依赖 PyPNG：直接读 ``qrcode`` 的布尔矩阵，用 ``▀▄█`` 把两行合成一行。
    真机联调时把这段打印到控制台，手机一扫就能连。
    """
    matrix = _make_qr(link).get_matrix()
    # qrcode 的 get_matrix() 已经带 border；margin=False 时裁掉它
    if not margin:
        matrix = [row[1:-1] for row in matrix[1:-1]]
    lines: list[str] = []
    for top in range(0, len(matrix), 2):
        upper = matrix[top]
        lower = matrix[top + 1] if top + 1 < len(matrix) else [False] * len(upper)
        row = "".join(
            "\u2588" if up and lo else "\u2580" if up else "\u2584" if lo else " "
            for up, lo in zip(upper, lower)
        )
        lines.append(row)
    return "\n".join(lines)
