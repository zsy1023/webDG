"""M0 设备管理 —— 摄像头 / 麦克风 / BLE 的枚举、稳定 ID、热切换、持久化。

**接口契约**：`docs/tasks/T2-姿态感知与骨骼可视化.md` §6.5（对外接口 + 四个实测坑）。
**归属**：`docs/tasks/README.md` §五明确 —— 本文件由 T2 独占实现（TF 只留了桩，现已整体替换）。

本机的真实情况（2026-04 实测，见模块末尾 `if __name__ == "__main__"` 的自检）：

| 类型 | 原始条目 | 去重后 |
|---|---|---|
| 摄像头（pygrabber/DirectShow） | 3 | 3 |
| 麦克风（sounddevice） | 22 | 2 个物理输入 + 3 个回环项 |

## 四个真机坑（§6.5，都是实机踩过的，不是理论）

1. **同一物理麦克风在 4 个 hostapi 下重复出现**（MME / DirectSound / WASAPI / WDM-KS）
   → 按设备名**归一化**归组（`normalize_device_name`），组内按 hostapi 优先级取代表
   （WASAPI 最优）；映射器（`Microsoft 声音映射器` / `主声音捕获驱动程序`）整组丢弃。
2. **`max_input_channels > 0` 不够** —— `立体声混音` / `电脑扬声器` 也满足
   → 标 `detail["loopback"] = True`，默认**不出现在** `list("microphone")` 里
   （要显示就传 `include_loopback=True`），而不是假装它是麦克风。
3. **设备名含 ® / 中文会让 GBK 控制台崩**
   → 入口 UTF-8 由 `agent.core.config.force_utf8()` 强制（`Node` 基类已调用）；
   本文件所有落盘一律 UTF-8 + `ensure_ascii=False`。
4. **摄像头 index 会随插拔重排**
   → 稳定 ID 一律用**设备名**（`cam:<name>`），打开前**重新枚举定位 index**
   （`DeviceRegistry.camera_index()`），**绝不持久化 index**。

## 稳定 ID 方案（B12 的落地）

| 设备 | id 形态 | 例子 |
|---|---|---|
| 摄像头 | `cam:<设备名>` | `cam:ACER HD User Facing` |
| 摄像头（视频文件降级） | `cam:file:<绝对路径>` | `cam:file:C:\\tmp\\a.mp4` |
| 麦克风 | `mic:<归一化名>@<hostapi>` | `mic:麦克风阵列@Windows WASAPI` |

`detail["index"]` 只是**本次枚举**的下标（打开时用），**不是**身份 —— 这就是坑 4 的边界。
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

__all__ = [
    "CAMERA",
    "MICROPHONE",
    "BLE",
    "REMOTE",
    "DeviceError",
    "DeviceInfo",
    "DeviceRegistry",
    "CameraSource",
    "Resolution",
    "MicRow",
    "HOSTAPI_PRIORITY",
    "LOOPBACK_DETAIL_KEY",
    "normalize_device_name",
    "is_mapper_name",
    "is_loopback_name",
    "materialize_microphones",
    "materialize_cameras",
    "default_persist_path",
    "microphone_stream_kwargs",
]

CAMERA = "camera"
MICROPHONE = "microphone"
BLE = "ble"
REMOTE = "remote"

#: 回环项在 `detail` 里的标记键
LOOPBACK_DETAIL_KEY = "loopback"

#: hostapi 优先级：数字越小越好（§6.5 坑 1 的排序规则）
#: WASAPI 延迟最低、时钟最准；WDM-KS 次之但采样率常被钉死；
#: MME 最老、延迟最高，只在没有更好选择时用。
HOSTAPI_PRIORITY: dict[str, int] = {
    "Windows WASAPI": 0,
    "Windows WDM-KS": 1,
    "Windows DirectSound": 2,
    "MME": 3,
}
_UNKNOWN_API_PRIORITY = 9

#: 映射器 / 汇整设备：不是物理麦克风，整组丢弃（§6.5 坑 1）
_MAPPER_MARKERS: tuple[str, ...] = (
    "声音映射器",
    "sound mapper",
    "主声音捕获驱动程序",
    "primary sound capture",
    "主声音驱动程序",
    "primary sound driver",
)

#: 回环 / 输出设备：`max_input_channels > 0` 也满足，必须另行识别（§6.5 坑 2）
_LOOPBACK_MARKERS: tuple[str, ...] = (
    "立体声混音",
    "stereo mix",
    "电脑扬声器",
    "扬声器",
    "speakers",
    "耳机",
    "headphones",
)

#: 虚拟摄像头（不是真实画面来源）：排在列表最后，前端可标注
_VIRTUAL_CAMERA_MARKERS: tuple[str, ...] = ("virtual", "虚拟", "obs")

#: 内置摄像头的**名字启发式**（本机 = `ACER HD User Facing`）。
#: ⚠️ 只用于"排序靠后 + 前端提示"，绝不用于排除设备 —— 名字不可靠，
#: 用户在只有内置摄像头时仍然要能用它。
_INTERNAL_CAMERA_MARKERS: tuple[str, ...] = (
    "user facing",
    "integrated",
    "built-in",
    "front camera",
    "内置",
    "内建",
)

_BRACKETS_RE = re.compile(r"[\(\（\[\【][^\(\（\[\【\)\）\]\】]*[\)\）\]\】]")
_LEFTOVER_BRACKETS_RE = re.compile(r"[\(\）\)\[\]\{\}（）【】]")
_MARKS_RE = re.compile(r"[®™©℠]")
_TRAILING_INDEX_RE = re.compile(r"\s*\d+\s*$")
_WHITESPACE_RE = re.compile(r"[\s_\-]+")

_DEFAULT_CAMERA_WIDTH = 640
_DEFAULT_CAMERA_HEIGHT = 480

#: 采集后端名 → `cv2.CAP_*` 常量名（延迟解析，本模块不 import cv2）。
#:
#: - `dshow`（默认）：DirectShow。**pygrabber 的 index 顺序与它一致**，所以是权威后端；
#: - `msmf`：Media Foundation。某些驱动在 DSHOW 下掉帧时更快，但
#:   ⚠️ **它的 index 顺序不保证与 DirectShow 相同**，只建议用来排查；
#: - `any`：让 OpenCV 自己挑。
_CV2_BACKENDS: dict[str, str] = {
    "dshow": "CAP_DSHOW",
    "msmf": "CAP_MSMF",
    "any": "CAP_ANY",
}


class DeviceError(RuntimeError):
    """设备操作失败（设备忙 / 不存在 / 打开失败 / 热切换失败）。"""


# ──────────────────────────────────────────────────────────────
# 名字归一化与分类（纯函数，便于单测）
# ──────────────────────────────────────────────────────────────
def _strip_brackets(text: str, rounds: int = 4) -> str:
    """反复剥掉括号内容，直到不再变化 —— 处理 `扬声器 (Realtek(R) Audio)` 这种嵌套括号。"""
    for _ in range(rounds):
        stripped = _BRACKETS_RE.sub(" ", text)
        if stripped == text:
            break
        text = stripped
    return text


def normalize_device_name(name: str) -> str:
    """归一化设备名，用于把同一物理设备的多个 hostapi 变体归到一组。

    处理：去掉 `®`/`™`/`©` → 去掉括号内容（含嵌套括号与 `()` 空括号）→
    去掉尾部序号（`麦克风阵列 1` → `麦克风阵列`）→ 压缩空白 → casefold。

    >>> normalize_device_name("麦克风阵列 (适用于数字麦克风的英特尔® 智音技术)")
    '麦克风阵列'
    >>> normalize_device_name("麦克风阵列 1 ()")
    '麦克风阵列'
    >>> normalize_device_name("麦克风 (Realtek HD Audio Mic input)")
    '麦克风'
    """
    text = _MARKS_RE.sub("", str(name or ""))
    text = _strip_brackets(text)  # "麦克风阵列 1 ()" → "麦克风阵列 1 "
    text = _LEFTOVER_BRACKETS_RE.sub(" ", text)  # 孤立括号残渣
    text = _TRAILING_INDEX_RE.sub("", text)  # 再尾部序号（"麦克风阵列 1 " → "麦克风阵列"）
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip().casefold()


def _contains_any(text: str, markers: Sequence[str]) -> bool:
    low = str(text or "").casefold()
    return any(marker.casefold() in low for marker in markers)


def is_mapper_name(name: str) -> bool:
    """是否是映射器 / 汇整设备（`Microsoft 声音映射器` 之类）。"""
    return _contains_any(name, _MAPPER_MARKERS)


def is_loopback_name(name: str) -> bool:
    """是否是回环 / 输出设备（`立体声混音` / `电脑扬声器` 之类）。"""
    return _contains_any(name, _LOOPBACK_MARKERS)


def _hostapi_priority(hostapi_name: str) -> int:
    return HOSTAPI_PRIORITY.get(str(hostapi_name or ""), _UNKNOWN_API_PRIORITY)


# ──────────────────────────────────────────────────────────────
# DeviceInfo
# ──────────────────────────────────────────────────────────────
@dataclass
class DeviceInfo:
    """一个可用设备。★ `id` 必须是**稳定 ID**（设备名 / 名字+hostapi），绝不是 index。

    index 会随插拔重排（§6.5 坑 4），所以持久化只存 `id`。
    `detail` 里的 `index` 只是本次枚举的打开用下标。
    """

    id: str
    kind: str  # "camera" | "microphone" | "ble" | "remote"
    name: str  # 展示名（可能含中文 / ®）
    detail: dict[str, Any] = field(default_factory=dict)
    available: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "detail": dict(self.detail),
            "available": self.available,
        }

    @property
    def is_loopback(self) -> bool:
        return bool(self.detail.get(LOOPBACK_DETAIL_KEY))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeviceInfo":
        return cls(
            id=str(data["id"]),
            kind=str(data.get("kind", "")),
            name=str(data.get("name", data["id"])),
            detail=dict(data.get("detail") or {}),
            available=bool(data.get("available", True)),
        )


@dataclass
class MicRow:
    """`sounddevice.query_devices()` 的一行（归一化后的形状）。测试可直接造。"""

    index: int
    name: str
    hostapi_index: int
    hostapi_name: str
    max_input_channels: int
    max_output_channels: int = 0
    default_samplerate: float = 0.0
    low_latency: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "hostapi_index": self.hostapi_index,
            "hostapi_name": self.hostapi_name,
            "max_input_channels": self.max_input_channels,
            "max_output_channels": self.max_output_channels,
            "default_samplerate": self.default_samplerate,
            "low_latency": self.low_latency,
        }


@dataclass
class Resolution:
    """`resolve()` 的结果：选中了谁、有没有回退、为什么。"""

    device: DeviceInfo | None
    requested: str | None = None
    reason: str = "ok"  # ok | fallback | empty

    @property
    def fell_back(self) -> bool:
        return self.reason != "ok"


# ──────────────────────────────────────────────────────────────
# 枚举（默认实现；测试可注入自己的枚举函数）
# ──────────────────────────────────────────────────────────────
def materialize_cameras(names: Sequence[str], *, available: bool = True) -> list[DeviceInfo]:
    """设备名列表 → `DeviceInfo` 列表（index 只写进 detail，**顺序 != index 顺序**）。

    摄像头 id 用 `cam:<设备名>`：名字是唯一稳定的东西，index 不是（坑 4）。

    排在最前的是**外置实体摄像头**，其次内置，最后虚拟摄像头（`OBS Virtual Camera`）：
    这样 `resolve()` 默认挑到的是外置设备 —— 本机实测内置的 `ACER HD User Facing`
    在 DirectShow 下只有 1 FPS（设备/驱动问题，见 §8 风险表），默认不该落到它身上。
    `detail["internal"]` 只是**按名字的启发式**，仅用于排序与前端提示，不参与打开逻辑。
    """
    out: list[DeviceInfo] = []
    for index, raw_name in enumerate(names):
        name = str(raw_name).strip()
        if not name:
            continue
        detail: dict[str, Any] = {"index": index, "backend": "dshow"}
        if _contains_any(name, _VIRTUAL_CAMERA_MARKERS):
            detail["virtual"] = True
        if _contains_any(name, _INTERNAL_CAMERA_MARKERS):
            detail["internal"] = True
        out.append(
            DeviceInfo(id=f"cam:{name}", kind=CAMERA, name=name, detail=detail, available=available)
        )
    out.sort(
        key=lambda d: (
            bool(d.detail.get("virtual")),  # 虚拟摄像头排最后
            bool(d.detail.get("internal")),  # 内置排中（本机内置那台只有 1 FPS）
            int(d.detail.get("index", 0)),  # 其余保持 DirectShow 枚举顺序
        )
    )
    return out


def materialize_microphones(
    rows: Sequence[MicRow],
    *,
    include_loopback: bool = False,
    default_index: int | None = None,
) -> list[DeviceInfo]:
    """`MicRow` 列表 → 去重后的 `DeviceInfo` 列表（坑 1 + 坑 2 的落地）。

    规则：
    1. `max_input_channels <= 0` 的行直接丢（纯输出设备）；
    2. 映射器整组丢（`Microsoft 声音映射器` 等）；
    3. 按 `normalize_device_name` 归组，组内按 (hostapi 优先级, index) 取代表；
    4. 组内名字含回环标记 → `detail["loopback"]=True`；`include_loopback=False` 时不出现在结果里；
    5. 代表项带上同组其它 hostapi 变体（`detail["variants"]`），前端要区分时可用；
    6. `default_index` 是系统默认输入设备的下标 → 那一组排最前并标 `detail["is_default"]`，
       这样 `resolve()` 默认挑的就是用户平时在用的那支麦克风。
    """

    default_norm: str | None = None
    if default_index is not None:
        for row in rows:
            if row.index == default_index:
                default_norm = normalize_device_name(row.name)
                break

    groups: dict[str, list[MicRow]] = {}
    for row in rows:
        if row.max_input_channels <= 0:
            continue
        if is_mapper_name(row.name):
            continue
        groups.setdefault(normalize_device_name(row.name), []).append(row)

    devices: list[DeviceInfo] = []
    for norm, members in groups.items():
        ordered = sorted(
            members,
            key=lambda r: (_hostapi_priority(r.hostapi_name), r.index),
        )
        best = ordered[0]
        if not norm:
            # 名字全被过滤掉（极端情况）→ 用原始名兜底，避免所有设备撞成同一个 id
            norm = str(best.name).casefold()
        loopback = is_loopback_name(best.name)
        detail: dict[str, Any] = {
            "index": best.index,
            "hostapi": best.hostapi_name,
            "hostapi_index": best.hostapi_index,
            "max_input_channels": best.max_input_channels,
            "default_samplerate": best.default_samplerate,
            "low_latency": best.low_latency,
            "normalized_name": norm,
            "is_default": default_norm is not None and norm == default_norm,
            "variants": [m.to_dict() for m in ordered],
            LOOPBACK_DETAIL_KEY: loopback,
        }
        if loopback and not include_loopback:
            continue
        devices.append(
            DeviceInfo(
                id=f"mic:{norm}@{best.hostapi_name}",
                kind=MICROPHONE,
                name=best.name,
                detail=detail,
                available=True,
            )
        )
    devices.sort(
        key=lambda d: (
            d.is_loopback,  # 回环排最后（前端置灰展示时顺序稳定）
            not d.detail.get("is_default"),  # 系统默认输入最前
            _hostapi_priority(d.detail.get("hostapi", "")),  # 其次看 hostapi 质量
            d.name.casefold(),
        )
    )
    return devices


def _com_initialize() -> bool:
    """Windows：在**当前线程**初始化 COM，成功返回 True。

    ⚠️ 实测踩过：`pygrabber`（DirectShow）底层是 COM，而枚举是跑在**线程池线程**里的，
    没初始化就抛 `OSError: [WinError -2147221008] 尚未调用 CoInitialize` →
    **面板上"摄像头"下拉整个是空的**（用户看到的就是"选了摄像头没反应"）。
    `CoInitialize` 是**每线程**的，所以只能在使用点就地调用。
    """
    if sys.platform != "win32":  # pragma: no cover - 非 Windows 不需要
        return False
    try:
        import ctypes  # noqa: PLC0415

        return ctypes.windll.ole32.CoInitialize(None) in (0, 1)  # S_OK / S_FALSE
    except Exception:  # noqa: BLE001 - 失败就当没初始化（上层会记 last_error）
        return False


def _com_uninitialize() -> None:
    if sys.platform != "win32":  # pragma: no cover
        return
    try:
        import ctypes  # noqa: PLC0415

        ctypes.windll.ole32.CoUninitialize()
    except Exception:  # noqa: BLE001
        pass


def enumerate_cameras() -> list[DeviceInfo]:
    """用 pygrabber 枚举 DirectShow 摄像头（顺序 == `cv2.VideoCapture(index, CAP_DSHOW)`）。

    任何失败都**不抛**（返回空列表让上层降级到视频文件），原因记在调用方的 `last_error`。
    """
    from pygrabber.dshow_graph import FilterGraph  # 延迟 import：没装也不影响其它功能

    initialized = _com_initialize()
    try:
        names = FilterGraph().get_input_devices()
    finally:
        if initialized:
            _com_uninitialize()
    return materialize_cameras(names)


def enumerate_microphones(*, include_loopback: bool = False) -> list[DeviceInfo]:
    """用 sounddevice 枚举输入设备（含 hostapi 信息），按坑 1 / 坑 2 去重。"""
    import sounddevice as sd  # 延迟 import

    hostapis = list(sd.query_hostapis())
    rows: list[MicRow] = []
    for index, dev in enumerate(sd.query_devices()):
        api_index = int(dev.get("hostapi", -1))
        api_name = ""
        if 0 <= api_index < len(hostapis):
            api_name = str(hostapis[api_index].get("name", ""))
        rows.append(
            MicRow(
                index=index,
                name=str(dev.get("name", "")),
                hostapi_index=api_index,
                hostapi_name=api_name,
                max_input_channels=int(dev.get("max_input_channels", 0)),
                max_output_channels=int(dev.get("max_output_channels", 0)),
                default_samplerate=float(dev.get("default_samplerate", 0.0) or 0.0),
                low_latency=float(dev.get("default_low_input_latency", 0.0) or 0.0),
            )
        )
    return materialize_microphones(
        rows, include_loopback=include_loopback, default_index=_system_default_input()
    )


def _system_default_input() -> int | None:
    """系统默认输入设备的下标（拿不到就 None）。"""
    try:
        import sounddevice as sd

        value = sd.default.device
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        if value is None:
            return None
        index = int(value)
        return index if index >= 0 else None
    except Exception:  # noqa: BLE001 - 只是排序提示，拿不到不影响功能
        return None


def default_persist_path() -> Path:
    """默认持久化位置：`<工作区>/data/devices.json`（`data/` 已在 .gitignore 里）。

    运行期状态，不进源码树。
    """
    return Path(__file__).resolve().parents[2] / "data" / "devices.json"


# ──────────────────────────────────────────────────────────────
# DeviceRegistry
# ──────────────────────────────────────────────────────────────
class DeviceRegistry:
    """设备注册表：枚举 / 查询 / 稳定 ID / 选择持久化 / 热插拔回调。

    - 一个进程一份实例（`Node` 的 `setup()` 里建）。
    - 所有公开方法线程安全（内部 `RLock`）：热插拔轮询线程 + 事件循环会并发访问。
    - `refresh()` 返回"列表是否变化"，变化时同步触发 `on_change` 回调。
    """

    def __init__(
        self,
        *,
        camera_enumerator: Callable[[], list[DeviceInfo]] | None = None,
        microphone_enumerator: Callable[[], list[DeviceInfo]] | None = None,
        persist_path: str | os.PathLike[str] | None = None,
        auto_load: bool = True,
        preload: bool = True,
        logger: Any = None,
    ) -> None:
        """``preload=True``（默认）→ 构造时就枚举一次，**新建后立即 ``list()`` 就有设备**。

        历史 bug（真机实测）：只加载持久化选择、不枚举 → ``DeviceRegistry().list("camera")``
        返回空，必须显式 ``refresh()`` 才对。现在默认构造即枚举。
        只想要空表再手动 ``refresh()`` 时传 ``preload=False``（测试 / 自检用）。
        """
        self._camera_enumerator = camera_enumerator or enumerate_cameras
        self._microphone_enumerator = microphone_enumerator or enumerate_microphones
        self._persist_path = Path(persist_path) if persist_path else default_persist_path()
        self.log = logger
        self._lock = threading.RLock()
        self._cameras: list[DeviceInfo] = []
        self._microphones: list[DeviceInfo] = []
        self._others: dict[str, dict[str, DeviceInfo]] = {}
        self._callbacks: list[Callable[[list[DeviceInfo]], None]] = []
        self._selected: dict[str, str] = {}
        self._errors: dict[str, str] = {}
        self._enumerated_at: dict[str, float] = {}
        self._watch_thread: threading.Thread | None = None
        self._watch_stop = threading.Event()

        if auto_load:
            self._load_state()
        # ★ 新建即枚举：不 refresh 也能立刻 list() / get() / resolve()。
        # 枚举失败不会抛（refresh 内部兜底），最坏结果是空表 + last_error。
        if preload:
            self.refresh()

    # —— 查询 ——
    def list(self, kind: str | None = None, *, include_loopback: bool = False) -> list[DeviceInfo]:
        """列出设备（可按 kind 过滤）。

        `kind="microphone"` 默认**不含**回环项（立体声混音 / 电脑扬声器，见坑 2）；
        要显示它们（前端置灰标注）就传 `include_loopback=True`。
        """
        with self._lock:
            if kind is None:
                out = [*self._cameras, *self._microphones]
                for group in self._others.values():
                    out.extend(group.values())
            elif kind == CAMERA:
                out = list(self._cameras)
            elif kind == MICROPHONE:
                out = list(self._microphones)
            else:
                out = list(self._others.get(kind, {}).values())
        if not include_loopback and kind in (None, MICROPHONE):
            out = [d for d in out if not d.is_loopback]
        return out

    def get(self, device_id: str) -> DeviceInfo | None:
        """按稳定 ID 查设备（不存在返回 None）。"""
        if not device_id:
            return None
        with self._lock:
            for device in (*self._cameras, *self._microphones):
                if device.id == device_id:
                    return device
            for group in self._others.values():
                if device_id in group:
                    return group[device_id]
        return None

    def kinds(self) -> list[str]:
        with self._lock:
            kinds: set[str] = set()
            if self._cameras or self._microphones:
                kinds.update({CAMERA, MICROPHONE})
            kinds.update(name for name, group in self._others.items() if group)
        return sorted(kinds)

    def state(self) -> dict[str, Any]:
        """一次性快照（供 `event/device` 的 payload 用）。"""
        with self._lock:
            return {
                "devices": [d.to_dict() for d in self.list(include_loopback=True)],
                "selected": dict(self._selected),
                "errors": dict(self._errors),
                "enumerated_at": dict(self._enumerated_at),
            }

    def last_error(self, kind: str) -> str | None:
        """最近一次枚举失败的原因（成功时为 None）。设备不存在时给用户看的解释。"""
        with self._lock:
            return self._errors.get(kind)

    # —— 注册（BLE / remote 由各自节点上报）——
    def register(self, kind: str, info: DeviceInfo) -> None:
        with self._lock:
            self._others.setdefault(kind, {})[info.id] = info
        self._notify()

    def unregister(self, kind: str, device_id: str) -> None:
        with self._lock:
            group = self._others.get(kind)
            if group and device_id in group:
                del group[device_id]
        self._notify()

    # —— 枚举 ——
    def refresh(self) -> bool:
        """重新枚举摄像头 + 麦克风；返回**列表是否发生变化**。

        枚举失败（例如 pygrabber 抛异常）时**保留上一次的结果**并把原因记进 `last_error`，
        绝不清空列表 —— 拔一下摄像头不该让整份设备列表消失。
        """
        with self._lock:
            changed = self._refresh_locked(camera=True, microphone=True)
        if changed:
            self._notify()
        return changed

    def _refresh_locked(self, *, camera: bool, microphone: bool) -> bool:
        changed = False
        if camera:
            try:
                devices = self._camera_enumerator()
            except Exception as exc:  # noqa: BLE001 - 枚举失败要外显但不能打死节点
                self._errors[CAMERA] = f"{type(exc).__name__}: {exc}"
                self._log_warn("摄像头枚举失败：%s", self._errors[CAMERA])
            else:
                self._errors.pop(CAMERA, None)
                self._enumerated_at[CAMERA] = time.time()
                if _signature(devices) != _signature(self._cameras):
                    self._cameras = list(devices)
                    changed = True
        if microphone:
            try:
                # 内部保留回环项：list() 负责默认隐藏，这样 include_loopback=True 才有东西可显示
                devices = self._microphone_variants()
            except Exception as exc:  # noqa: BLE001
                self._errors[MICROPHONE] = f"{type(exc).__name__}: {exc}"
                self._log_warn("麦克风枚举失败：%s", self._errors[MICROPHONE])
            else:
                self._errors.pop(MICROPHONE, None)
                self._enumerated_at[MICROPHONE] = time.time()
                if _signature(devices) != _signature(self._microphones):
                    self._microphones = list(devices)
                    changed = True
        return changed

    def _microphone_variants(self) -> list[DeviceInfo]:
        """枚举并保留回环项（内部用）。测试注入的枚举器可能不吃 `include_loopback`。"""
        try:
            return list(self._microphone_enumerator(include_loopback=True))  # type: ignore[call-arg]
        except TypeError:
            return list(self._microphone_enumerator())

    # —— 变更回调 ——
    def on_change(self, cb: Callable[[list[DeviceInfo]], None]) -> None:
        """注册热插拔变更回调（`refresh()` 检测到变化时触发，参数是**全量**列表）。"""
        if not callable(cb):
            raise TypeError("on_change() 需要可调用对象")
        with self._lock:
            self._callbacks.append(cb)

    def _notify(self) -> None:
        with self._lock:
            callbacks = list(self._callbacks)
            devices = self.list(include_loopback=True)
        for cb in callbacks:
            try:
                cb(devices)
            except Exception as exc:  # noqa: BLE001 - 回调坏了不影响其它回调
                self._log_warn("on_change 回调抛异常：%s", exc)

    # —— 选择与持久化（B12）——
    def selected(self, kind: str) -> str | None:
        with self._lock:
            return self._selected.get(kind)

    def set_selected(self, kind: str, device_id: str | None) -> None:
        """记住某类型的用户选择（落盘 `data/devices.json`，存的是**名字不是 index**）。

        允许记住当前不存在的设备（用户可能只是暂时拔掉了），`resolve()` 会负责回退。
        """
        with self._lock:
            if device_id:
                self._selected[kind] = str(device_id)
            else:
                self._selected.pop(kind, None)
            snapshot = dict(self._selected)
        self._save_state(snapshot)

    def resolve(self, kind: str, preferred: str | None = None) -> Resolution:
        """挑一台可用设备：`preferred` → 上次选择 → 第一个可用；都没有则 `reason="empty"`。

        找不到请求的设备时**回退并说明原因**（§6.5 硬性要求 4），不抛异常、不静默换设备。
        """
        candidates = [d for d in self.list(kind) if d.available]
        requested = preferred or self.selected(kind)
        if requested:
            for device in candidates:
                if device.id == requested:
                    return Resolution(device, requested, "ok")
            # 请求的设备不在（拔了 / 被回环规则隐藏 / 换了名字）→ 回退并说明原因
            if candidates:
                return Resolution(candidates[0], requested, "fallback")
            return Resolution(None, requested, "empty")
        if candidates:
            return Resolution(candidates[0], None, "ok")
        return Resolution(None, None, "empty")

    # —— 摄像头 index（坑 4 的对策）——
    def camera_index(self, device_id: str) -> int | None:
        """按稳定 ID **重新枚举**定位 DirectShow index；找不到返回 None。

        打开摄像头前必须走这里，绝不能缓存上次的 index。
        """
        device = self.get(device_id)
        if device is None or device.kind != CAMERA:
            return None
        if str(device_id).startswith("cam:file:"):
            return None
        with self._lock:
            try:
                devices = self._camera_enumerator()
            except Exception as exc:  # noqa: BLE001
                self._errors[CAMERA] = f"{type(exc).__name__}: {exc}"
                return None
            self._cameras = list(devices)
        for fresh in devices:
            if fresh.id == device_id:
                index = fresh.detail.get("index")
                return int(index) if index is not None else None
        return None

    # —— 热插拔轮询（§6.5 硬性要求 3；可降级为手动 refresh）——
    def start_watch(self, interval: float = 5.0) -> None:
        """起后台线程定时 `refresh()`（默认 5s），列表变化时触发 `on_change`。"""
        if interval <= 0:
            raise ValueError(f"轮询间隔必须为正，得到 {interval}")
        with self._lock:
            if self._watch_thread is not None and self._watch_thread.is_alive():
                return
            self._watch_stop.clear()
            thread = threading.Thread(
                target=self._watch_loop, args=(float(interval),), name="device-watch", daemon=True
            )
            self._watch_thread = thread
        thread.start()
        self._log_debug("设备热插拔轮询已启动（%.1fs）", interval)

    def stop_watch(self) -> None:
        """停掉轮询线程（幂等）。"""
        self._watch_stop.set()
        with self._lock:
            thread = self._watch_thread
            self._watch_thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._log_debug("设备热插拔轮询已停止")

    def _watch_loop(self, interval: float) -> None:
        while not self._watch_stop.wait(interval):
            try:
                self.refresh()
            except Exception as exc:  # noqa: BLE001 - 轮询线程绝不能死
                self._log_warn("设备轮询失败：%s", exc)

    # —— 持久化 ——
    def _load_state(self) -> None:
        path = self._persist_path
        if path is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._log_warn("设备选择状态读取失败（用默认值继续）：%s", exc)
            return
        if not isinstance(data, dict):
            self._log_warn("设备选择状态格式不对（顶层应为对象），已忽略")
            return
        selected = data.get("selected")
        if isinstance(selected, dict):
            with self._lock:
                for kind, value in selected.items():
                    if isinstance(value, str) and value:
                        self._selected[str(kind)] = value
        self._log_debug("已恢复设备选择：%s", self._selected)

    def _save_state(self, selected: dict[str, str]) -> None:
        path = self._persist_path
        if path is None:
            return
        payload = {
            "version": 1,
            "selected": selected,
            "updated": int(time.time()),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            # UTF-8 + ensure_ascii=False：设备名里的中文/® 必须原样落盘（坑 3）
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, path)
        except OSError as exc:  # 落盘失败不能打死节点：只是下次要重新选
            self._log_warn("设备选择保存失败（%s）：%s", path, exc)

    # —— 日志（misc 小工具）——
    def _log_warn(self, msg: str, *args: Any) -> None:
        if self.log is not None:
            self.log.warning(msg, *args)

    def _log_debug(self, msg: str, *args: Any) -> None:
        if self.log is not None:
            self.log.debug(msg, *args)


def _signature(devices: Sequence[DeviceInfo]) -> tuple[tuple[Any, ...], ...]:
    """设备列表的"变化指纹"（不含 variants 细节，避免噪声导致误报变化）。"""
    return tuple(
        (d.id, d.name, d.available, d.detail.get("index"), bool(d.detail.get(LOOPBACK_DETAIL_KEY)))
        for d in devices
    )


# ──────────────────────────────────────────────────────────────
# CameraSource
# ──────────────────────────────────────────────────────────────
class CameraSource:
    """摄像头打开 / 读取 / 热切换（屏蔽 index 细节）。

    - `open()` 前会走 `registry.camera_index()` **重新枚举定位**（坑 4）；
    - 设备忙 / 不存在 → `DeviceError`（不崩，错误可读，B13）；
    - `device_id` 以 `cam:file:` 开头 → 用 **视频文件**当输入源（无摄像头时的开发降级）；
    - `epoch` 每次成功打开 / 切换 +1，供下游（MediaPipe）重建 timestamp 基准（§6.5 硬性要求 2）。
    """

    def __init__(
        self,
        registry: DeviceRegistry,
        device_id: str | None = None,
        *,
        width: int = _DEFAULT_CAMERA_WIDTH,
        height: int = _DEFAULT_CAMERA_HEIGHT,
        fps: int = 0,
        backend: str = "dshow",
        max_failed_reads: int = 30,
    ) -> None:
        if backend not in _CV2_BACKENDS:
            raise ValueError(
                f"未知采集后端 {backend!r}；可选：{'/'.join(_CV2_BACKENDS)}"
            )
        self.registry = registry
        self.device_id = device_id
        self.width = int(width)
        self.height = int(height)
        self.fps = int(fps)
        self.backend = backend
        self.max_failed_reads = max(1, int(max_failed_reads))
        self._lock = threading.RLock()
        self._cap: Any = None
        self._current: DeviceInfo | None = None
        self._epoch = 0
        self._failed_reads = 0
        self._last_error: str | None = None

    # —— 只读状态 ——
    @property
    def current(self) -> DeviceInfo | None:
        with self._lock:
            return self._current

    @property
    def epoch(self) -> int:
        """打开 / 切换计数。变化 = 下游必须重建时间基准（MediaPipe timestamp）。"""
        with self._lock:
            return self._epoch

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._cap is not None

    @property
    def is_file_source(self) -> bool:
        return bool(self.device_id and str(self.device_id).startswith("cam:file:"))

    # —— 生命周期 ——
    def open(self) -> None:
        """打开设备（或视频文件）。失败抛 `DeviceError`，并保留 `last_error` 供上层展示。"""
        with self._lock:
            resolution = self._resolve_target()
            if resolution.device is None:
                if resolution.requested:
                    raise self._fail(
                        f"设备不存在：{resolution.requested}"
                        "（可能已被拔掉 / 改名，或正被别的程序占用）"
                    )
                raise self._fail("本机没有可用摄像头（可用 --source 指定视频文件当输入）")
            device = resolution.device
            self._last_error = None
            self._open_device(device)
            self.device_id = device.id
            self._current = device
            self._epoch += 1
            self._failed_reads = 0

    def _resolve_target(self) -> Resolution:
        """确定打开目标。

        **严格语义**（B13）：`device_id` 一旦指定（构造时传入 / `switch()` 传入），
        就必须精确命中 —— 不存在的设备报错，**绝不静默换成别的设备**
        （"静默换设备"是最坑的一类 bug：用户以为在用佳能，其实在用内置摄像头）。
        `device_id` 为空时才走 `resolve()`（此时才允许"回退到第一个可用 + 提示"）。
        """
        if self.is_file_source:
            path = str(self.device_id)[len("cam:file:") :]
            if not Path(path).exists():
                raise self._fail(f"视频文件不存在：{path}")
            return Resolution(
                DeviceInfo(
                    id=str(self.device_id),
                    kind=CAMERA,
                    name=Path(path).name,
                    detail={"path": path, "file": True},
                    available=True,
                ),
                self.device_id,
                "ok",
            )
        if self.device_id:
            device = self.registry.get(self.device_id)
            if device is None or not device.available:
                return Resolution(None, self.device_id, "empty")
            return Resolution(device, self.device_id, "ok")
        return self.registry.resolve(CAMERA)

    def _open_device(self, device: DeviceInfo) -> None:
        try:
            import cv2
        except ImportError as exc:  # pragma: no cover - 环境问题，明确报错胜过 NameError
            raise self._fail(f"未安装 opencv-python，无法采集：{exc}") from exc

        backend_code = getattr(cv2, _CV2_BACKENDS[self.backend])
        if device.detail.get("file"):
            target: Any = device.detail["path"]
            backend_code = cv2.CAP_ANY
        else:
            index = self.registry.camera_index(device.id)
            if index is None:
                raise self._fail(f"设备已不在（枚举里找不到）：{device.name}")
            target = index

        cap = cv2.VideoCapture(target, backend_code)
        if not cap.isOpened():
            cap.release()
            name = device.name
            raise self._fail(
                f"打开失败：{name}（设备忙 / 被其它程序占用 / 已被拔掉）"
            )
        try:
            # Windows 上 OpenCV 默认会缓冲好几帧 → 延迟飙升（§8 风险表）
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            # ★ 只在"实际值不等于目标值"时才 set：
            #   DSHOW 驱动上一次 `cap.set` 实测能阻塞 1~3 秒（本机 HDI Webcam USB 就是），
            #   三次无脑 set 会让"打开摄像头"慢到十几秒（用户反馈"摄像头启动极慢"）。
            #   设备本来就报着我们要的分辨率/帧率时，跳过即可。
            def _need(prop: int, want: int) -> bool:
                if want <= 0:
                    return False
                try:
                    now = cap.get(prop)
                except Exception:  # noqa: BLE001 - 某些后端不支持读
                    return True
                return not now or abs(float(now) - want) > 0.5

            if _need(cv2.CAP_PROP_FRAME_WIDTH, self.width):
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            if _need(cv2.CAP_PROP_FRAME_HEIGHT, self.height):
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            if _need(cv2.CAP_PROP_FPS, self.fps):
                cap.set(cv2.CAP_PROP_FPS, self.fps)
        except Exception as exc:  # noqa: BLE001 - 某些后端不支持这些属性，不影响打开
            self._last_error = f"设置采集参数失败（继续）：{exc}"
        with self._lock:
            self._release_locked()
            self._cap = cap

    def read(self) -> Any:
        """读一帧 BGR（`numpy.ndarray`）；读不到返回 `None`（含连续失败时的重连尝试）。"""
        with self._lock:
            cap = self._cap
            if cap is None:
                raise self._fail("摄像头未打开（先调 open()）")
            try:
                ok, frame = cap.read()
            except Exception as exc:  # noqa: BLE001 - 拔线时 OpenCV 会抛
                self._failed_reads += 1
                self._last_error = f"读取异常（{self._failed_reads}）：{exc}"
                return None
            if not ok or frame is None:
                self._failed_reads += 1
                if self._failed_reads >= self.max_failed_reads:
                    self._last_error = (
                        f"连续 {self._failed_reads} 帧读取失败（设备被拔掉或独占）"
                    )
                    self._reopen_locked()
                return None
            self._failed_reads = 0
            return frame

    def reopen(self) -> None:
        """关掉再开（设备重新插回后上层可主动调；也会在连续读失败时自动触发）。"""
        with self._lock:
            self._reopen_locked()

    def _reopen_locked(self) -> None:
        device = self._current
        if device is None:
            return
        try:
            self._release_locked(cap_only=True)
            self._open_device(device)
        except DeviceError as exc:
            self._last_error = str(exc)
            return
        self._epoch += 1
        self._failed_reads = 0

    def switch(self, device_id: str) -> None:
        """热切换：释放旧 `VideoCapture` → 打开新设备（失败会**回滚到旧设备**）。

        切换成功后 `epoch` 变化，下游据此重建 MediaPipe 的 timestamp 基准（§6.5）。

        ⚠️ **幂等**：请求的就是当前正在用的那台 → 直接返回，不拆不重开。
        实测踩过：面板/节点重复下发一次"选 HDI"（甚至就是当前这台），这里就会把
        VideoCapture 释放再打开 → 日志出现 `已热切换到 HDI（epoch=2）`、紧接着
        `采集帧率 ≈ 0.2 FPS`，页面那几秒什么都看不到（用户原话："帧率 0.2，你看你改了什么"）。
        同一台设备没有任何理由重启采集。
        """
        with self._lock:
            if device_id and self.device_id == device_id and self.is_open:
                return
            previous = self.device_id
            previous_device = self._current
            self.device_id = device_id
            try:
                resolution = self._resolve_target()
                if resolution.device is None:
                    raise self._fail(f"设备不存在：{device_id!r}")
                self._last_error = None
                self._open_device(resolution.device)
            except DeviceError as exc:
                self.device_id = previous
                self._current = previous_device
                if previous_device is not None and not self.is_open:
                    try:
                        self._open_device(previous_device)
                        self._epoch += 1
                    except DeviceError:
                        self._current = None
                raise exc
            self._current = resolution.device
            self._epoch += 1
            self._failed_reads = 0

    def close(self) -> None:
        with self._lock:
            self._release_locked()
            self._current = None

    def _release_locked(self, *, cap_only: bool = False) -> None:
        cap = self._cap
        self._cap = None
        if cap is not None:
            try:
                cap.release()
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"释放摄像头失败：{exc}"
        if not cap_only:
            self._failed_reads = 0

    def _fail(self, message: str) -> DeviceError:
        self._last_error = message
        return DeviceError(message)

    def capture_info(self) -> dict[str, Any]:
        """当前采集参数（分辨率 / fps / epoch），用于排查"画面为什么这么糊"。"""
        with self._lock:
            cap = self._cap
            if cap is None:
                return {"open": False, "device": None, "epoch": self._epoch}
            import cv2

            return {
                "open": True,
                "device": self._current.to_dict() if self._current else None,
                "epoch": self._epoch,
                "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0),
                "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0),
                "fps": float(cap.get(cv2.CAP_PROP_FPS) or 0.0),
                "backend": str(cap.getBackendName()) if hasattr(cap, "getBackendName") else "",
            }


def microphone_stream_kwargs(
    registry: DeviceRegistry, device_id: str, *, samplerate: int | None = None, channels: int = 1
) -> dict[str, Any]:
    """给 `sounddevice.InputStream` 用的参数（按稳定 ID **重新定位** index）。

    找不到设备时抛 `DeviceError`；采样率默认用设备自己的（避免 WASAPI 重采样失败）。
    """
    device = registry.get(device_id)
    if device is None or device.kind != MICROPHONE:
        raise DeviceError(f"麦克风不存在：{device_id!r}")
    index = device.detail.get("index")
    if index is None:
        raise DeviceError(f"麦克风缺少 index（先 refresh()）：{device_id!r}")
    return {
        "device": int(index),
        "channels": min(channels, int(device.detail.get("max_input_channels", 1)) or 1),
        "samplerate": int(samplerate or device.detail.get("default_samplerate") or 16000),
    }


if __name__ == "__main__":  # pragma: no cover - 手工自检用
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    reg = DeviceRegistry(auto_load=False)
    reg.refresh()
    print("=" * 62)
    print("摄像头")
    for dev in reg.list(CAMERA):
        print(f"  {dev.name}\n    id={dev.id}\n    detail={dev.detail}")
    print("麦克风（去重后）")
    for dev in reg.list(MICROPHONE):
        print(f"  {dev.name}\n    id={dev.id}\n    hostapi={dev.detail.get('hostapi')}")
    print("被隐藏的回环项")
    for dev in reg.list(MICROPHONE, include_loopback=True):
        if dev.is_loopback:
            print(f"  {dev.name}  id={dev.id}")
    print("默认选择：", reg.resolve(CAMERA), reg.resolve(MICROPHONE))
    print("枚举错误：", reg.last_error(CAMERA), reg.last_error(MICROPHONE))
