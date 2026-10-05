"""最小可行性验证脚本：Windows 能否用 Python 广播一个手机能搜到的自定义 GATT 服务？

对应任务 T1 §9「第一步」——只回答一个问题：
    这台 Windows 11 能不能用 PyWinRT 的 GattServiceProvider 广播出一个
    手机能搜到、能连接的 GATT 外设？

本脚本**不是**完整实现，只是一个探针：
  * 创建 0x180C 服务 + 0x150A(WRITE) / 0x150B(NOTIFY) 特征
  * 开始广播（IsDiscoverable=true, IsConnectable=true）
  * 订阅 WriteRequested，收到写入时打印原始字节（HEX + 长度）
  * 打印适配器能力、广播状态、连接进出的 client_info

用法（PowerShell）：
    python agent\\nodes\\sim_coyote\\probe_peripheral.py            # 默认广播 300 秒
    python agent\\nodes\\sim_coyote\\probe_peripheral.py --seconds 600
    python agent\\nodes\\sim_coyote\\probe_peripheral.py --name-hint

然后用手机 nRF Connect / DG-LAB App 搜索并连接，观察控制台输出。
Ctrl+C 可提前退出。

依赖：winrt-runtime + winrt-Windows.Devices.Bluetooth[.GenericAttributeProfile]
     + winrt-Windows.Foundation + winrt-Windows.Foundation.Collections
     + winrt-Windows.Storage.Streams  （不要用 bless）
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import sys
import threading
import time
import uuid

# ---- PyWinRT 模块 -----------------------------------------------------------
from winrt.windows.devices.bluetooth import BluetoothAdapter, BluetoothLEDevice
from winrt.windows.devices.bluetooth.genericattributeprofile import (
    GattCharacteristicProperties,
    GattLocalCharacteristic,
    GattLocalCharacteristicParameters,
    GattServiceProvider,
    GattServiceProviderAdvertisementStatus,
    GattServiceProviderAdvertisingParameters,
    GattSubscribedClient,
    GattWriteRequestedEventArgs,
)
from winrt.windows.storage.streams import DataReader

LOG = logging.getLogger("probe")

# ---- 任务要求的 UUID（0000xxxx-0000-1000-8000-00805f9b34fb）------------------
_BASE = "{:08x}-0000-1000-8000-00805f9b34fb"


def _uuid16(short: int) -> uuid.UUID:
    return uuid.UUID(_BASE.format(0x0000_0000 | short))


SERVICE_UUID = _uuid16(0x180C)  # 郊狼主服务
CHAR_WRITE_UUID = _uuid16(0x150A)  # App 的指令写入这里
CHAR_NOTIFY_UUID = _uuid16(0x150B)  # 我们的回应走这里


# ---- 小工具 -----------------------------------------------------------------
def ibuffer_to_bytes(buf) -> bytes:
    """把 WinRT IBuffer 转成 Python bytes。"""
    reader = DataReader.from_buffer(buf)
    return bytes(reader.read_bytes(buf.length))


def hexstr(data: bytes) -> str:
    return data.hex(" ")


# ---- 全局运行状态（供事件回调与主循环共享）----------------------------------
class State:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.write_count = 0
        self.notify_char: GattLocalCharacteristic | None = None
        self.last_client: dict | None = None
        self.conn_log: list[dict] = []


ST = State()


# ---- client_info 解析 -------------------------------------------------------
def parse_device_id(device_id: str) -> dict:
    """GattSession.device_id 形如:
        BluetoothLE#BluetoothLE8c:17:59:59:fb:36-00:11:22:33:44:55
    返回适配器 MAC / 对端 MAC。对端 MAC 用于后续查设备名。
    """
    info: dict = {"raw": device_id}
    try:
        after = device_id.rsplit("#", 1)[-1]  # BluetoothLE<adapter>-<peer>
        after = after[len("BluetoothLE"):]
        adapter_mac, _, peer_mac = after.partition("-")
        info["adapter_mac"] = adapter_mac.upper()
        info["peer_mac"] = peer_mac.upper()
        info["peer_addr_int"] = int(peer_mac.replace(":", ""), 16)
    except Exception as exc:  # noqa: BLE001 - 探测脚本，尽力而为
        info["parse_error"] = repr(exc)
    return info


async def resolve_client_info(session) -> dict:
    """从 GattSession 解析出 client_info（设备名 / MAC / 连接时刻）。"""
    info = parse_device_id(session.device_id)
    info["connected_at"] = time.time()
    info["session_status"] = str(session.session_status)
    info["max_pdu_size"] = session.max_pdu_size
    try:
        info["can_maintain_connection"] = session.can_maintain_connection
    except Exception:  # noqa: BLE001
        pass

    # 用对端 MAC 反查设备名（可能失败：设备名需系统缓存或成功连接后才可见）
    addr = info.get("peer_addr_int")
    if addr is not None:
        try:
            dev = await BluetoothLEDevice.from_bluetooth_address_async(addr)
            if dev is not None:
                info["device_name"] = dev.name or ""
                info["device_connection_status"] = str(dev.connection_status)
                try:
                    dev.close()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            info["name_lookup_error"] = repr(exc)
    return info


# ---- 事件回调 ---------------------------------------------------------------
def on_write_requested(sender: GattLocalCharacteristic, args: GattWriteRequestedEventArgs) -> None:
    """WinRT 在后台线程回调；转交给 asyncio 主循环处理以便 await。"""
    deferral = args.get_deferral()
    if ST.loop is not None:
        asyncio.run_coroutine_threadsafe(_handle_write(args, deferral), ST.loop)


async def _handle_write(args: GattWriteRequestedEventArgs, deferral) -> None:
    try:
        request = await args.get_request_async()
        data = ibuffer_to_bytes(request.value)
        ST.write_count += 1
        LOG.info(
            "WRITE #%d  len=%d  offset=%d  option=%s  data=%s",
            ST.write_count,
            len(data),
            request.offset,
            getattr(request.option, "name", request.option),
            hexstr(data),
        )
        # 若将来要回 B1，可在此通过 ST.notify_char.notify_value_async(...)
        request.respond()  # 确认写入成功（App 用 Write With Response 时必须回）
    except Exception:  # noqa: BLE001
        LOG.exception("处理写入请求失败")
    finally:
        deferral.complete()


def on_subscribed_clients_changed(sender: GattLocalCharacteristic, args) -> None:
    clients = list(sender.subscribed_clients)
    LOG.info("订阅态变化：当前订阅客户端数 = %d", len(clients))
    for c in clients:
        try:
            s = c.session
            LOG.info(
                "  + client device_id=%s  max_notify_size=%d  max_pdu=%d",
                s.device_id,
                c.max_notification_size,
                s.max_pdu_size,
            )
            if ST.loop is not None:
                asyncio.run_coroutine_threadsafe(_record_client(c), ST.loop)
        except Exception:  # noqa: BLE001
            LOG.exception("读取订阅客户端信息失败")


async def _record_client(client: GattSubscribedClient) -> None:
    info = await resolve_client_info(client.session)
    ST.last_client = info
    ST.conn_log.append(info)
    LOG.info("client_info = %s", info)


def on_status_changed(sender, args) -> None:
    LOG.info("广播状态变化 -> %s", sender.advertisement_status)


# ---- 主流程 -----------------------------------------------------------------
async def build_and_advertise() -> GattServiceProvider:
    ST.loop = asyncio.get_running_loop()

    # 1) 适配器能力（决定能否做 BLE 外设）
    adapter = await BluetoothAdapter.get_default_async()
    if adapter is None:
        raise RuntimeError("没有可用的蓝牙适配器（BluetoothAdapter.get_default_async 返回 null）")
    LOG.info("适配器: address=0x%x", adapter.bluetooth_address)
    LOG.info("  is_low_energy_supported      = %s", adapter.is_low_energy_supported)
    LOG.info("  is_peripheral_role_supported = %s   <-- 关键", adapter.is_peripheral_role_supported)
    LOG.info("  is_central_role_supported    = %s", adapter.is_central_role_supported)
    LOG.info("  max_advertisement_data_length= %s", adapter.max_advertisement_data_length)

    # 2) 创建主服务 0x180C
    LOG.info("创建服务 %s ...", SERVICE_UUID)
    result = await GattServiceProvider.create_async(SERVICE_UUID)
    LOG.info("  GattServiceProviderResult.error = %s (%s)",
             result.error, getattr(result.error, "name", result.error))
    if int(result.error) != 0:
        raise RuntimeError(
            f"GattServiceProvider.create_async 失败: error={result.error!r} "
            f"（BluetoothError，非 0 常见为 DisabledByPolicy）"
        )
    provider = result.service_provider

    # 3) 0x150A: WRITE（App 所有指令写这里）
    write_params = GattLocalCharacteristicParameters()
    write_params.characteristic_properties = (
        GattCharacteristicProperties.WRITE
        | GattCharacteristicProperties.WRITE_WITHOUT_RESPONSE
    )
    write_res = await provider.service.create_characteristic_async(CHAR_WRITE_UUID, write_params)
    if int(write_res.error) != 0:
        raise RuntimeError(f"创建 0x150A 失败: error={write_res.error!r}")
    write_char = write_res.characteristic
    write_char.add_write_requested(on_write_requested)
    LOG.info("  特征 0x150A (WRITE) OK  uuid=%s", write_char.uuid)

    # 4) 0x150B: NOTIFY（我们的回应走这里）
    notify_params = GattLocalCharacteristicParameters()
    notify_params.characteristic_properties = GattCharacteristicProperties.NOTIFY
    notify_res = await provider.service.create_characteristic_async(CHAR_NOTIFY_UUID, notify_params)
    if int(notify_res.error) != 0:
        raise RuntimeError(f"创建 0x150B 失败: error={notify_res.error!r}")
    notify_char = notify_res.characteristic
    notify_char.add_subscribed_clients_changed(on_subscribed_clients_changed)
    ST.notify_char = notify_char
    LOG.info("  特征 0x150B (NOTIFY) OK  uuid=%s", notify_char.uuid)

    # 5) 开始广播
    adv = GattServiceProviderAdvertisingParameters()
    adv.is_discoverable = True
    adv.is_connectable = True
    provider.add_advertisement_status_changed(on_status_changed)
    LOG.info("开始广播 (IsDiscoverable=True, IsConnectable=True) ...")
    provider.start_advertising_with_parameters(adv)
    await asyncio.sleep(0.5)
    LOG.info("  advertisement_status = %s",
             getattr(provider.advertisement_status, "name", provider.advertisement_status))
    return provider


async def run(seconds: int, stop_event: threading.Event) -> None:
    provider = await build_and_advertise()
    print("\n" + "=" * 64)
    print(" 广播已启动。本机蓝牙名（手机端可能显示的名字）:", socket.gethostname())
    print(" 服务 UUID:", SERVICE_UUID)
    print(" 用手机 nRF Connect / DG-LAB App 搜索并连接，观察下方日志。")
    print(f" {seconds} 秒后自动停止，或按 Ctrl+C 提前退出。")
    print("=" * 64 + "\n")
    try:
        await asyncio.sleep(seconds)
    finally:
        try:
            provider.stop_advertising()
            LOG.info("已停止广播。共收到 %d 次写入，%d 次连接。",
                     ST.write_count, len(ST.conn_log))
        except Exception:  # noqa: BLE001
            LOG.exception("停止广播失败")


def main() -> int:
    parser = argparse.ArgumentParser(description="BLE GATT 外设可行性探针（T1 §9）")
    parser.add_argument("--seconds", type=int, default=300, help="广播持续秒数（默认 300）")
    parser.add_argument("--verbose", action="store_true", help="输出 DEBUG 日志")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s.%(msecs)03d %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    try:
        asyncio.run(run(args.seconds, threading.Event()))
    except KeyboardInterrupt:
        LOG.info("用户中断，退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
