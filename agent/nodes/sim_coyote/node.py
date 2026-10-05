"""``sim_coyote`` 节点（T1）—— 冒充 DG-LAB 郊狼主机，截获 App 的指令。

职责边界（T1 任务书 §5）：

* ✅ **忠实透出** App 的原始意图（A/B 强度变化 + 两通道波形 + 软上限），不做有损压缩
* ❌ **不做叠加合并** —— 那是 ``bridge`` / T9 的活
* ❌ 不碰网络、不碰房间、不碰惩罚逻辑

数据流::

    控制者手机 App ──BLE──> ESP32 假主机 ──串口/TCP──> 本节点
                                                          │ command/pulse_raw（本机）
                                                          ▼
                                                       coyote_v4 ──device.op──> 被控端

* **发布**：``command/pulse_raw``（本机专用，不跨机）—— B0 的强度意图 + 每通道 8 字节波形帧，
  由 ``coyote_v4`` 转成 V4 的 ``device.op``（映射见 :mod:`.v4_map`）
* **订阅**：无

⚠️ BLE 外设本身走 ESP32（Windows 做不了，见 T1 任务书 §8）；
本节点通过可插拔后端（:mod:`agent.nodes.sim_coyote.transport`）与它对接，
因此**换硬件不用改这里**。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any, Callable

from agent.core import Node, topics

from . import esp32_link
from . import protocol as P
from . import v4_map
from .transport import ClientInfo, PeripheralBackend, PeripheralError, create_backend

__all__ = ["SimCoyoteNode"]


def _backend_from_config(config: Any) -> tuple[str, dict[str, Any]]:
    """从 ``agent/config.toml`` 的 ``[sim_coyote]`` 读后端类型与它的参数。

    例::

        [sim_coyote]
        backend = "esp32"
        transport = "serial"
        port = "COM5"
    """
    raw = getattr(config, "raw", None) or {}
    section = raw.get("sim_coyote") or {}
    if not isinstance(section, dict):
        return "esp32", {}
    kind = str(section.get("backend") or "esp32").strip() or "esp32"
    kwargs: dict[str, Any] = {}
    for key in ("transport", "port", "baudrate"):
        if section.get(key) is not None:
            kwargs[key] = section[key]
    return kind, kwargs


class SimCoyoteNode(Node):
    """模拟郊狼主机节点。

    :param backend: 后端类型 —— ``None``（默认）→ 读 ``agent/config.toml`` 的
        ``[sim_coyote] backend``；再没有就是 ``esp32``。可选 ``"esp32"``（现行）/
        ``"winrt"``（受限）/ ``"loopback"``（自测）
    :param backend_kwargs: 透传给后端的参数（如 ``transport="serial"``, ``port="COM5"``）；
        **覆盖**配置文件里同名项
    :param publish_pulse_raw: 是否把截获的 B0 转发到 ``command/pulse_raw``（默认 **True**）。
        B0 里每个通道那 8 个字节**就是** V4 的波形帧格式，映射是 1:1 的（见 :mod:`.v4_map`），
        不需要波形名查表。BF（软上限/平衡参数）是**主机本地设置**，不往设备发。
    :param on_command: 每条指令的钩子 ``on_command(dict)`` —— 上层接缝 & 测试用。
    """

    name = "sim_coyote"

    def __init__(
        self,
        config: Any | None = None,
        *,
        backend: str | None = None,
        backend_kwargs: dict[str, Any] | None = None,
        publish_pulse_raw: bool = True,
        on_command: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        super().__init__(config)
        # 后端：显式参数 > `[sim_coyote]` 配置 > 默认 esp32
        cfg_kind, cfg_kwargs = _backend_from_config(self.config)
        self._backend_kind = backend or cfg_kind
        self._backend_kwargs = {**cfg_kwargs, **(backend_kwargs or {})}
        self._publish_pulse_raw = publish_pulse_raw
        self._on_command = on_command

        #: 模拟主机的状态：B0 的 ADD/SUB 是相对量，必须记住当前强度才能回 B1
        self.state = P.CoyoteState()
        self.backend: PeripheralBackend | None = None
        self.stats: dict[str, int] = {"b0": 0, "bf": 0, "b1": 0, "bad": 0}
        self.commands: list[dict[str, Any]] = []
        #: 启动期的问题（如后端没起来）—— 不致命，记下来供 /status 与排障
        self.errors: list[str] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pub_pulse: Any = None
        self._pub_link: Any = None      # event/link（面板上的 ESP32 / 手机指示灯）

    # ──────────────────────────────────────────────────────
    # 生命周期
    # ──────────────────────────────────────────────────────
    async def setup(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._pub_pulse = self.publish(topics.COMMAND_PULSE_RAW)
        self._pub_link = self.publish(topics.EVENT_LINK)      # ★ 面板上的 ESP32 / 手机指示灯
        # ★ 串口热切换入口（复用 M0 通用设备层）：{"kind":"sim_coyote","id":"COM18"}
        self.subscribe(topics.COMMAND_SELECT_DEVICE, self.on_select)
        await self._start_backend()
        name = self.backend.name if self.backend is not None else "(未接硬件)"
        self.log.info(
            "sim_coyote 就绪：后端=%s 串口=%s 广播名=%s pulse_raw 转发=%s",
            self._backend_kind,
            self.serial_port or "(未指定)",
            name,
            "开" if self._publish_pulse_raw else "关",
        )
        self.publish_link()
        self._link_task = asyncio.create_task(self._link_loop())

    async def teardown(self) -> None:
        task, self._link_task = getattr(self, "_link_task", None), None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        backend, self.backend = self.backend, None
        if backend is not None:
            try:
                await backend.stop()
            except Exception:  # noqa: BLE001
                self.log.exception("停止后端失败")

    # ──────────────────────────────────────────────────────
    # 链路状态（面板上的指示灯）
    # ──────────────────────────────────────────────────────
    async def _link_loop(self) -> None:
        """定期把链路状态推给面板 —— 插拔线、App 连断都靠它体现在界面上。"""
        while True:
            await asyncio.sleep(2.0)
            self.publish_link()

    def publish_link(self) -> None:
        """发一条 ``event/link``：ESP32 串口通不通、手机 App 连没连上。

        面板上的「ESP32 假主机」和「手机 DG-LAB App」两个灯读它。
        注意区分两件事：**串口开着**（backend 起来了）和 **App 连着**（假主机被 App 连接）。
        """
        pub = self._pub_link
        if pub is None:
            return
        serial_ok = self.backend is not None
        phone = self.is_connected
        if serial_ok:
            where = self.serial_port
            state = "ready"
            detail = f"串口 {where} 已打开" if where else "后端已就绪"
        elif self.errors:
            state, detail = "error", self.errors[-1]
        else:
            state, detail = "idle", "后端未启动"
        pub.put({
            "ts": int(time.time() * 1000),
            "source": self.name,
            "kind": "serial",
            "state": state,                     # ready / error / idle
            "port": self.serial_port or "",
            "backend": self._backend_kind,
            "detail": detail,
            "hint": "" if serial_ok else "没插 ESP32？插上 USB 后在设备下拉里选它的串口",
            "phone_connected": phone,           # 手机 App 到底连没连上假主机
            "phone": self.client_info,
            "stats": dict(self.stats),
        })

    async def _start_backend(self) -> None:
        """起后端；**硬件没到位时降级运行**。

        默认后端是 ``esp32``（需要真实硬件）。如果它起不来就抛异常退出，
        supervisor 会当"异常退出"反复重启 —— 那是重启风暴，不是故障隔离。
        所以这里**吞掉启动失败**、记进 :attr:`errors`，节点保持存活但功能为空
        （没有写入源 = 不会有任何指令进来，安全）。
        """
        backend = create_backend(
            self._backend_kind,
            on_write=self._on_write,
            on_connect=self._on_connect,
            **self._backend_kwargs,
        )
        try:
            await backend.start()
        except PeripheralError as exc:
            self.backend = None
            self.errors.append(str(exc))
            self.log.error(
                "模拟郊狼主机后端 %s 起不来 → 节点降级为「只解析不转发」（等硬件就位）：%s",
                self._backend_kind, exc,
            )
            self.publish_link()      # 让面板立刻显示"串口起不来"，别只在日志里
            return
        self.backend = backend

    @property
    def is_connected(self) -> bool:
        return bool(self.backend and self.backend.is_connected)

    @property
    def client_info(self) -> dict[str, Any] | None:
        """当前连着的 App 是谁 —— 需求 7「看到是谁访问了」的一环。"""
        if self.backend is None or self.backend.client_info is None:
            return None
        return self.backend.client_info.to_dict()

    # ──────────────────────────────────────────────────────
    # 后端回调
    # ──────────────────────────────────────────────────────
    def _on_connect(self, info: ClientInfo | None) -> None:
        if info is None:
            self.log.info("App 已断开")
        else:
            self.log.info(
                "★ App 已连接：name=%r mac=%s backend=%s",
                info.device_name or "?", info.peer_mac or "?", info.backend,
            )
        self.publish_link()      # ★ 面板上的「手机 DG-LAB App」灯立刻跟着变

    def _on_write(self, data: bytes) -> None:
        """后端写入回调 —— 一定在事件循环线程上（后端负责跨线程）。"""
        try:
            cmd = P.parse_command(data)
        except P.ProtocolError as exc:
            self.stats["bad"] += 1
            self.log.warning("无法解析的写入（%d 字节 %s）：%s", len(data), data.hex(" "), exc)
            return
        if isinstance(cmd, P.B0Command):
            self._handle_b0(cmd)
        else:
            self._handle_bf(cmd)

    def _handle_b0(self, cmd: P.B0Command) -> None:
        self.stats["b0"] += 1
        self.state.apply(cmd)
        delta_a, delta_b = cmd.strength_delta()
        self.log.debug(
            "B0 seq=%d mode=%s/%s delta=%s/%s waveA=%s waveB=%s → A=%d B=%d",
            cmd.seq, cmd.mode_a.name, cmd.mode_b.name, delta_a, delta_b,
            "valid" if cmd.wave_a.valid else "作废",
            "valid" if cmd.wave_b.valid else "作废",
            self.state.strength_a, self.state.strength_b,
        )
        # ★ 真机实测：seq≠0 就必回 B1（哪怕强度没变）
        b1 = self.state.b1_for(cmd)
        if b1 is not None:
            self._notify(b1)
        self._emit(cmd)

    def _handle_bf(self, cmd: P.BfCommand) -> None:
        self.stats["bf"] += 1
        self.state.apply(cmd)
        self.log.info(
            "BF：软上限 A=%d B=%d（无回包，真机会断电保存）",
            self.state.soft_cap_a, self.state.soft_cap_b,
        )
        self._emit(cmd)

    def _notify(self, b1: bytes) -> None:
        """通过后端把 B1 推回 App（异步，不能阻塞写入回调）。"""
        backend = self.backend
        if backend is None:
            return
        self.stats["b1"] += 1

        async def _do() -> None:
            n = await backend.notify(b1)
            if n == 0:
                self.log.debug("B1 无订阅者，丢弃：%s", b1.hex(" "))

        try:
            asyncio.get_running_loop().create_task(_do())
        except RuntimeError:  # 不在事件循环里 → 转交主循环
            loop = self._loop
            if loop is not None:
                asyncio.run_coroutine_threadsafe(_do(), loop)

    def _emit(self, cmd: P.Command) -> None:
        """把结构化指令交给上层（钩子 + 转发到 ``command/pulse_raw``）。"""
        data = cmd.to_dict()
        self.commands.append(data)
        if len(self.commands) > 512:  # 只留最近若干条，避免长时间跑爆内存
            del self.commands[: len(self.commands) - 512]

        if self._on_command is not None:
            try:
                self._on_command(data)
            except Exception:  # noqa: BLE001
                self.log.exception("on_command 钩子出错")

        if not self._publish_pulse_raw or self._pub_pulse is None:
            return
        if isinstance(cmd, P.B0Command):
            # B0 → V4 语义（强度 op + 每通道 8 字节波形帧）；BF 是本机设置，不发
            self._pub_pulse.put(v4_map.b0_to_payload(cmd))

    # ──────────────────────────────────────────────────────
    # 串口选择 / 热切换（复用 M0 通用设备层 command/select_device）
    # ──────────────────────────────────────────────────────
    @property
    def serial_port(self) -> str:
        """当前在用的串口（热切换会改它）。空 = 后端没配串口（如 loopback）。"""
        return str(self._backend_kwargs.get("port") or "")

    def on_select(self, msg: Any) -> None:
        """``command/select_device`` 回调 —— 按 ``kind`` 选串口并**热切换**。

        payload（沿用设备层的 ``{"kind":…, "id":…}`` 约定）::

            {"kind": "sim_coyote", "id": "COM18"}   → 热切换到该串口
            {"kind": "sim_coyote", "action": "list"} → 只把可用串口打进日志

        ``id`` 就是串口名（``COM18``），与 ``agent/config.toml`` 的 ``[sim_coyote] port`` 同格式；
        前端下拉的候选来自 :func:`agent.nodes.sim_coyote.esp32_link.list_serial_ports`。

        ⚠️ Zenoh 的订阅回调**不在事件循环线程**上，所以真正的切换动作经 :meth:`_spawn`
        送回事件循环执行（同线程时直接 create_task，便于单测）。
        """
        if not isinstance(msg, dict):
            return
        kind = msg.get("kind")
        # 也认 ``serial``：面板上那个下拉就叫「ESP32 串口」，发过来的 kind 是 serial
        if kind not in (None, self.name, "serial"):
            return  # 别人的设备指令（camera / microphone / …）
        if msg.get("action") == "list":
            self._log_available_ports()
            return
        port = str(msg.get("id") or msg.get("port") or "").strip()
        if not port:
            return
        if port == self.serial_port and self.backend is not None:
            self.log.info("串口未变（%s），忽略本次选择", port)
            return
        self._spawn(self._switch_backend(port))

    async def _switch_backend(self, port: str) -> None:
        """按新串口重启后端：停旧 → 用新串口起新的（起不来就降级存活，不崩）。"""
        old = self.serial_port
        self.log.info("串口热切换：%s → %s", old or "(未指定)", port)
        backend, self.backend = self.backend, None
        if backend is not None:
            try:
                await backend.stop()
            except Exception:  # noqa: BLE001
                self.log.exception("停止旧后端失败（继续切换）")
        self._backend_kwargs["port"] = port
        self.errors.clear()
        await self._start_backend()
        if self.backend is None:
            self.log.error(
                "热切换到 %s 失败：%s", port, self.errors[-1] if self.errors else "原因未知"
            )
        else:
            self.log.info("串口已热切换到 %s —— 模拟郊狼主机链路重建完成", port)

    def _spawn(self, coro: Any) -> None:
        """把协程送回事件循环执行（兼容"回调在别的线程"与"单测里同线程"）。"""
        loop = self._loop
        if loop is None:
            coro.close()
            self.log.warning("事件循环还没就绪，忽略本次选择")
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            loop.create_task(coro)
        else:
            asyncio.run_coroutine_threadsafe(coro, loop)

    def _log_available_ports(self) -> None:
        """把本机可用串口打进日志（排障用；前端下拉另有 ``esp32_link.list_serial_ports``）。"""
        ports = esp32_link.list_serial_ports()
        if not ports:
            self.log.info("本机没有枚举到可用串口（pyserial 装了吗？板子插了吗？）")
            return
        for port in ports:
            if port.likely_esp32:
                mark = "★ 像 ESP32 开发板"
            elif port.is_bluetooth:
                mark = "蓝牙虚拟口（能打开但收不到字节，别选）"
            else:
                mark = ""
            self.log.info("可用串口：%s %s", port.label, mark)
