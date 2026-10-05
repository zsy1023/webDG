# -*- coding: utf-8 -*-
"""郊狼控制链路 —— **真机 BLE 段**延迟实测。

测的是「主机收到一条 B0 → 回出 B1」的往返（真机内部处理周期 + BLE 链路）。
全程 `mode=00`（不改强度）+ 波形强度 0 → **真机零输出**，安全。

用法：python coyote_ble_latency.py [--rounds 20]
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bleak import BleakClient, BleakScanner  # noqa: E402

TARGET = "FB:C5:9A:4F:34:58"
NAME = "47L121000"
CH_150A = "0000150a-0000-1000-8000-00805f9b34fb"
CH_150B = "0000150b-0000-1000-8000-00805f9b34fb"


def b0(seq: int) -> bytes:
    """mode=00/00（不改强度）+ A 波形幅度 0 + B 通道作废 → 绝对无输出。"""
    return bytes([0xB0, (seq & 0xF) << 4, 0, 0,
                  10, 10, 10, 10,      # A freq
                  0, 0, 0, 0,          # A amp（全 0 = 无脉冲）
                  0, 0, 0, 0,          # B freq
                  0, 0, 0, 101])       # B amp（101 → 整组作废）


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100.0 * (len(xs) - 1))))]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=20)
    args = ap.parse_args()

    print("扫描 6s 找真机 …")
    found = await BleakScanner.discover(timeout=6.0, return_adv=True)
    addr = None
    for a, (d, adv) in found.items():
        nm = d.name or adv.local_name or ""
        if nm == NAME or a.upper().replace(":", "") == TARGET.replace(":", ""):
            addr = a
            print(f"  找到 {nm!r} {a} rssi={adv.rssi}")
    if addr is None:
        print("✗ 没扫到真机（不在范围 / 被手机占着）—— BLE 段无法实测")
        return 1

    lat: list[float] = []
    client = BleakClient(addr, timeout=25)
    await asyncio.wait_for(client.connect(), timeout=30)
    try:
        print(f"已连接（mtu={getattr(client, 'mtu_size', '?')}）\n")
        pending: dict[str, float] = {}

        def on_b1(_s, data: bytes) -> None:
            b = bytes(data)
            if b and b[0] == 0xB1:
                t0 = pending.pop("t0", None)
                if t0 is not None:
                    lat.append((time.perf_counter_ns() - t0) / 1e6)
                    print(f"  B0(seq={b[1]}) → B1 回到：{(time.perf_counter_ns() - t0) / 1e6:7.1f} ms")

        await client.start_notify(CH_150B, on_b1)
        await asyncio.sleep(0.5)

        for i in range(1, args.rounds + 1):
            pending["t0"] = time.perf_counter_ns()
            await client.write_gatt_char(CH_150A, b0(i), response=False)
            for _ in range(60):  # 最多等 3s
                if "t0" not in pending:
                    break
                await asyncio.sleep(0.05)
            if "t0" in pending:
                pending.pop("t0", None)
                print(f"  轮 {i:2d}: 超时（没等到 B1）")
            await asyncio.sleep(0.15)

        print()
        if lat:
            print(f"BLE 段（主机收到 B0 → 回出 B1）")
            print(f"    n={len(lat)}  min={min(lat):.1f}  p50={pct(lat, 50):.1f}  "
                  f"p90={pct(lat, 90):.1f}  max={max(lat):.1f}  mean={statistics.mean(lat):.1f}  (ms)")
        else:
            print("没收到任何 B1")
        await asyncio.sleep(0.3)
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
