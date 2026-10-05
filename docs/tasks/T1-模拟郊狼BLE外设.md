# 任务 T1：模拟郊狼 BLE 外设（App 蓝牙控制）

> ⚡ **v3 节点化适配**
> - 本任务实现节点 **`sim_coyote`**（控制侧，接收 App 的蓝牙指令）
> - 路径：**`agent/nodes/sim_coyote/`**（下文若见 `agent/controller/sim_coyote/`，按此对照）
> - 必须继承 `agent/core/`（TF 产物）的 `Node` 基类 → 见 `TF-节点框架与消息总线.md`
> - **发布** `command/pulse_raw`（**本机专用，不跨机**，见 §8.2 的 ③）
> - **订阅** `command/select_device`（选中/热切换假主机所在的串口 —— M0 通用设备层，T2 已定）
> - 对外形态是 `class SimCoyoteNode(Node)`，不是裸类

> **交付方**：任意 AI agent / 开发者（可独立开工）
> **上位文档**：`docs/架构设计与路线图.md`（先读它了解全局）
> **优先级**：属于 P2，但 **§9 的可行性验证可以立刻做** —— 它是整个需求 8 的最大风险点
> **预估**：验证 0.5 天 → 完整实现 2~3 天

---

## 0. 一句话目标

在 Windows 上跑一个程序，**冒充 DG-LAB 郊狼主机**（蓝牙 BLE GATT 外设）。让 DG-LAB App 能像连接真硬件一样搜到它、连上它、用它 —— 我们则在中间把 App 发出的**全部控制指令**（强度、波形、软上限）完整截获并结构化，交给上层。

**为什么需要它**：DG-LAB App 只能当"socket 控制"的**被控方**，第三方无法让 App 当控制方。所以要让 App 保留原生操作体验，就必须让它以为自己在连一台真主机 —— 那台"真主机"就是我们。

---

## 1. 在整体架构中的位置

```
   控制者手机 DG-LAB App
        │ ① BLE（App 以为连的是真主机）
        ▼
   ┌──────────────────────────────┐
   │ 【★ 本任务】模拟郊狼 BLE 外设 │   agent/controller/sim_coyote/
   └──────────────┬───────────────┘
                  │ ② 结构化 CoyoteCommand（强度/波形/时间戳）
                  ▼
        bridge（BLE ⇄ V4 语义转换 + **叠加合并**）   ← 不属于本任务
                  │
                  ▼
        room / transport（加密通道 → 对端）
```

**边界**：本任务**只做"忠实还原一台郊狼主机"**。不碰网络、不碰房间、不碰惩罚逻辑。

---

## 2. 工作区地图（哪些位置能动）

> ⚠️ **路径已按 v3 迁移**：下列地图与表格里的 `agent/controller/sim_coyote/` 就是现在的
> **`agent/nodes/sim_coyote/`**；`agent/controlled/`、`agent/room/`、`agent/transport/` 已并入
> `agent/nodes/`。v2→v3 完整映射见 `docs/tasks/README.md` §三。

工作区根目录：`C:\Users\zsy\Desktop\web控制`

```
web控制\
├── docs\
│   ├── 架构设计与路线图.md            ← 主设计文档【只读，不要改】
│   └── tasks\
│       └── T1-模拟郊狼BLE外设.md      ← 本文档
├── reference\                          ← 参考仓库【只读！只读！只读！】
│   ├── dglab-bluetooth-protocol\       ★★ 协议权威来源，必须读
│   │   └── coyote\v3\README.md         ← B0/B1/BF 逐字节规格
│   │   └── coyote\v3\example.md
│   │   └── coyote\v2\README.md         ← 旧协议（可选参考）
│   ├── dglab-kit\                      V4 RPC 定义 + 25 个官方波形
│   ├── dglab-websocket-server\         V4/V3 中继服务端
│   ├── Coyote-in-Cradle-Android\       Android 端 BLE 实现参考（BleCoyote.kt）
│   └── DG-LAB-contral-zhuaying-for-py\ 爪印 BLE Python 实现（bleak 用法参考）
├── agent\                              ← 代码根（可能要新建）
│   ├── core\                           ← 配置/日志（如不存在可新建最小实现）
│   └── controller\
│       └── sim_coyote\                 ← ★★ 本任务主战场【新建，可自由发挥】
├── web\
├── capture-app\
├── deploy\
└── tests\                              ← 测试放这里【可新建】
```

| 位置 | 权限 |
|---|---|
| `agent/controller/sim_coyote/` | ✅ 主战场，自由创建文件 |
| `agent/core/` | ✅ 可新建最小配置/日志工具 |
| `tests/` | ✅ 写测试 |
| `agent/controlled/`、`agent/room/`、`agent/transport/` | ❌ 其他 agent 的领地，别动 |
| `reference/` | ❌ **只读**，绝不修改 |
| `docs/架构设计与路线图.md` | ❌ 只读 |

---

## 3. 运行环境与约定

| 项 | 值 |
|---|---|
| Python | **3.13.13** @ `D:\Miniconda3\python.exe`（`python` 命令即指向它） |
| 系统 | Windows 11 专业工作站版 `10.0.26200` |
| 蓝牙适配器 | **Intel(R) Wireless Bluetooth(R)**（BLE 4.0+） |
| 已装包 | `bleak 3.0.2`、`numpy`、`opencv 5.0`、`fastapi`、`websockets`、`PIL`、`onnxruntime`、`torch(cpu)`、`mediapipe` |
| **需要安装** | `winrt-Windows.Devices.Bluetooth.GenericAttributeProfile`、`winrt-Windows.Devices.Bluetooth`、`winrt-Windows.Foundation` 等 PyWinRT 模块包 |
| **代理** | `http://127.0.0.1:7897` —— **pip / git / curl 下载国外资源必须走** |

安装示例（PowerShell）：
```powershell
pip install --proxy http://127.0.0.1:7897 winrt-Windows.Devices.Bluetooth.GenericAttributeProfile winrt-Windows.Devices.Bluetooth winrt-Windows.Foundation winrt-Windows.Foundation.Collections winrt-Windows.Storage.Streams winrt-runtime
```

**代码风格约定**
- 全异步优先（`asyncio`），因为 WinRT 全是 `IAsyncOperation`
- 模块化：协议编解码（`protocol.py`）与 BLE 传输（`transport.py`）分离，**编解码要能脱离蓝牙单独单测**
- 日志用标准 `logging`，关键指令要打印可读摘要
- 中文注释/日志可以，标识符用英文

---

## 4. 协议规格（逐字节，必须严格实现）

> 权威来源：`reference/dglab-bluetooth-protocol/coyote/v3/README.md`。以下为提炼，**有疑问一律以该文件为准**。

### 4.1 GATT 服务与特征

基础 UUID 补全规则：`0000{xxxx}-0000-1000-8000-00805f9b34fb`

**必须提供**（官方协议文档 + DG-LAB App APK 内 `assets/test4.html` 的 WebView Bridge 初始化代码，再经**真机实测**校正）：

> ✅ **2026-10-03 已用 `bleak` 直连真机（`47L121000` / MAC `FB:C5:9A:4F:34:58`）实测全部读写** ——
> 权威依据改为 `docs/research/郊狼真机GATT实测.md`，下表已按真机更正（**别照旧文档写**）。

| Service | Characteristic | 真机属性 | 说明 |
|---|---|---|---|
| `0x180C` | `0x150A` | **WRITE-WITHOUT-RESPONSE**（≤20 字节） | App 的所有指令写入这里。⚠️ **不是普通 WRITE**（旧文档写错） |
| `0x180C` | `0x150B` | **NOTIFY**（≤20 字节） | 我们的所有回应/回调走这里 |
| `0x180A` | `0x1500` | **READ + NOTIFY**（1 字节） | 电量百分比（真机读回 `0x64` = 100%） |
| `0x180A` | `0x1501` | **READ**（2 字节） | 真机读回 `0c 03`，`03` 疑似**协议版本 V3** |
| `0x180A` | `0x1502` | **READ**（6 字节） | 真机读回 = **本机 MAC**（官方文档未提） |
| `0x180A` | `0x2A25` | **READ** | 序列号 = ASCII 前缀 + MAC（真机 `"CEB3%Q"` + MAC） |
| `0x180A` | `0x2A59` | **READ + NOTIFY**（10 字节） | 真机实测 = **输出状态回读**（随 B0 波形变化） |
| `0x1800` | `0x2A00` | **READ + WRITE** | Device Name，真机 = `"47L121000"`。★ **可写 → 模拟硬件靠它改名** |
| `0x1800` | `0x2A01` / `0x2A04` / `0x2AA6` | READ | Appearance / 首选连接参数 / 地址解析 |
| `0x1801` | `0x2A05` | INDICATE | Service Changed（标准） |
| `0x2004` | `0x0009` | READ + WRITE | 未知（真机有） |
| `0x2003` | `0x0007` / `0x0008` | WRITE / READ+NOTIFY | 未知（真机有） |
| `0xFE59` | `8EC90003-…` | WRITE + INDICATE | Nordic DFU，**可不提供** |

> App 初始化时依次执行（原始证据，来自 APK）：
> `read(180A/1500)` → `read(180A/1501)` → `notify(180A/1500)` → `notify(180C/150B)` → `check(180C/150A)` → `notify(180A/2A59)`

### 4.2 `0xB0` 指令 —— App → 我们（**核心，每 100ms 一条，固定 20 字节**）

```
偏移  长度  含义
0     1     0xB0（指令 HEAD）
1     1     高4bit = 序列号(0~15) | 低4bit = 强度值解读方式
2     1     A 通道强度设定值 (0~200)
3     1     B 通道强度设定值 (0~200)
4     4     A 通道波形频率 ×4
8     4     A 通道波形强度 ×4
12    4     B 通道波形频率 ×4
16    4     B 通道波形强度 ×4
```

**强度值解读方式**（1 字节里的低 4 bit）：高 2 bit 为 A 通道、低 2 bit 为 B 通道
| 值 | 含义 |
|---|---|
| `0b00` | 该通道强度**不做改变**（后面的设定值无效） |
| `0b01` | 该通道强度**相对增加**设定值 |
| `0b10` | 该通道强度**相对减少**设定值 |
| `0b11` | 该通道强度**绝对设置为**设定值 |

> 举例：`0b1101` → A 通道绝对设置、B 通道相对增加。

**强度**：`0~200`，超范围按 0 处理。
**波形频率**：写入值域 `10~240`。若上游给的是 `10~1000`，按下面算法换算：
```
输入值 10..100   → 输入值
输入值 101..600  → (输入值 - 100) / 5 + 100
输入值 601..1000 → (输入值 - 600) / 10 + 200
其他             → 10
```
**波形强度**：`0~100`。**若某通道的 4 组里出现任一非法值（>100），该通道 4 组数据整组作废、不输出。**
> 👉 这个特性是**单通道输出**的开关：只想驱动 A 通道时，B 通道塞一个 `101` 即可。

**序列号语义**：写 B0 时若修改了通道强度，把序列号设为 `>0`，主机会用**相同序列号**通过 `B1` 回传修改后的强度；不需要回传则设 `0`。
> 协议特别提醒：序列号非 0 时，**建议等 `B1` 回传相同序列号后再发下一条强度修改**。

**每组频率+强度代表 25ms 波形，4 组 = 100ms。**

### 4.3 `0xB1` —— 我们 → App（强度回调，4 字节）

```
0xB1 | 序列号(1B) | A 通道当前实际强度(1B) | B 通道当前实际强度(1B)
```
**只要强度发生变化就必须立刻用 B1 上报**（无论是 App 的 B0 导致，还是内部因素）。

> ⚠️ **真机实测修正（2026-10-03）**：不是「强度变化才回」，而是 **只要 `B0` 的序列号 ≠ 0 就必回一条相同序列号的 `B1`**，
> 哪怕强度一个字节都没变（实测：seq=7 + mode=`00/00` → 真机回 `B1 07 00 00`）。序列号**原样回传**。
> 这条对 App 的交互很关键：App 会「等 B1 回来再发下一条强度修改」，漏回就会卡住。

### 4.4 `0xBF` —— App → 我们（软上限与平衡参数，固定 7 字节）

```
0xBF | AB 两通道强度软上限(2B) | AB 两通道波形频率平衡参数(2B) | AB 两通道波形强度平衡参数(2B)
```
- 强度软上限：`0~200`，超范围则不修改。**断电保存**（我们的实现里持久化到配置文件）
- 频率平衡参数 1、强度平衡参数 2：`0~255`，**断电保存**

> 🚨 **重要坑**：`BF` 写入后**直接生效、没有返回值**。每次重连设备后 **App 都必须重新写 BF**，所以我们的实现要在"新连接建立"时把软上限重置为默认值（或已持久化的值），不要沿用上次连接的内存状态。

### 4.5 电量特征 `0x180A/0x1500`

- 1 字节，`READ` + `NOTIFY`
- 值：**电量百分比 0~100**（真机实测读回 `0x64` = 100%，就是 1 字节百分比）
- 应支持 App 订阅后的**定时推送**（比如每 30~60 秒一次，或电量变化时）

### 4.6 ✅ 待实测项 —— 2026-10-03 已全部实测出结论

> 原始数据与逐条判读见 `docs/research/郊狼真机GATT实测.md`。**不要再猜了。**

| 项 | 真机实测结论 |
|---|---|
| `180A/1501` 返回什么 | `0c 03`（2 字节）。`03` 疑似**协议版本 V3**；`0c`(12) 未解 |
| `180A/2A59` 推什么 | 10 字节，是**输出状态回读**（随 B0 波形实时变）。字节 0-1 恒 `01 01`；字节 2/3 ↔ A/B 通道；精确编码未完全解出 |
| `180A/1500` 电量编码 | 1 字节百分比，真机读回 `0x64` = **100%** |
| 广播内容 | 空闲时**确实在广播**：`name=47L121000`，**不带任何服务 UUID，也无厂商数据** |
| 设备名 | 真机 `0x2A00` 是 **READ + WRITE**，所以可以在连接后写入改名成 `47L121000` |
| （补充）`0x1502` | 官方文档没有的特征，返回 **6 字节本机 MAC** |
| （补充）连接后固定回调 | 订阅 `0x150B` 成功后立刻收到 6 字节 `53 00 <MAC 后 4 字节>` |
| （补充）Windows 订阅限制 | `0x1500`/`0x2A59` 在未加密链路上 `start_notify` 被拒（`Access Denied`），`0x150B` 正常 |

**仍未解**：`0x1501` 的 `0c`、`0x2A59` 的精确编码规则、`0x2003`/`0x2004` 的用途。

> 已落地的编解码实现：`agent/nodes/sim_coyote/protocol.py`，单测 `tests/test_sim_coyote_protocol.py`（28 项，含与真机逐条对拍）。

---

## 5. 与"叠加"的关系（重要设计约束）

需求明确：**郊狼的输出是手机控制信号 + 惩罚信号可以叠加的**（不是二选一）。协议依据是 V4 的 `p`（优先级 0~2）与 `im`（是否替换同类任务）——`im=false` + 高优先级就是"叠加而不抢占"。

**本任务的职责边界**：
- ❌ 本任务**不做叠加合并** —— 叠加发生在 `bridge` 层（本任务之外）
- ✅ 本任务只需**忠实透出 App 的原始意图**，且信息足够上层做合并

所以输出接口必须保留：
1. A/B 两通道各自的**强度变化**（增量 or 绝对值，**不要自己合并掉**，把解读方式原样带出）
2. 两通道各自 4 组波形（**不要丢弃被作废的那组信息**，标记出来即可）
3. **时间戳** —— 上层要靠它把 App 流和惩罚流对齐
4. `BF` 的软上限变化

> 换句话说：**把 App 当成一个"诚实的数据源"，不要做任何有损压缩。**

---

## 6. 对外接口

> ⚠️ **定稿（以此为准）**：本节点**发布 `command/pulse_raw`**（`agent/core/topics.py` 里的唯一真源，
> 见 §8.2 的 ③），由 `coyote_v4` 节点订阅消费；对外形态是 **`agent/nodes/sim_coyote/node.py` 里的 `SimCoyoteNode(Node)`**。
> 下面的 `CoyoteCommand` 只作为**内部结构化表示**（已落地为 `protocol.py` 的 `B0Command` / `BfCommand`）；
> 跨进程发的是**结构化脉冲**（强度 op + 每通道 8 字节波形帧），映射见 `agent/nodes/sim_coyote/v4_map.py`。
> **BF（软上限/平衡）是主机本地设置，不跨进程。**

建议在 `agent/nodes/sim_coyote/__init__.py` 暴露：

```python
from dataclasses import dataclass
from typing import Callable, Literal

@dataclass
class CoyoteCommand:
    """从 App 收到的一次指令，已结构化但未做任何合并。"""
    ts: float                      # 收到时刻（time.time()）
    kind: Literal["b0", "bf"]
    # kind == "b0"
    seq: int = 0                   # 0~15
    channel_mode: dict = None      # {"A": "none|add|sub|set", "B": "..."}
    strength: dict = None          # {"A": int, "B": int}  设定值，仅 mode != none 时有意义
    waveform: dict = None          # {"A": [(freq,amp)*4], "B": [...]} 或标记作废
    # kind == "bf"
    soft_caps: dict = None         # {"A": int, "B": int}
    freq_balance: dict = None
    amp_balance: dict = None

class SimCoyoteHost:
    def __init__(self,
                 name: str = "DGLAB-SIM",                # 见 §4.6 待实测
                 on_command: Callable[[CoyoteCommand], None] = None,
                 battery_provider: Callable[[], int] = None,   # 返回 0~100
                 ): ...

    async def start(self) -> None:
        """创建 GATT 服务、开始广播、等待 App 连接。"""

    async def stop(self) -> None: ...

    async def notify_strength(self, a: int, b: int, seq: int = 0) -> None:
        """主动通过 0x150B 推送 B1（例如上层干预后同步强度回显给 App）。"""

    @property
    def is_connected(self) -> bool: ...

    @property
    def client_info(self) -> dict:
        """当前连接的 App 信息：设备名、MAC、连接时刻 —— 用于"看到是谁访问了"。"""
```

> `client_info` 是需求 7"最好能看到是谁访问了"的一环，尽量提供。

---

## 7. 验收标准（逐条可测）

| # | 标准 | 怎么验 |
|---|---|---|
| A1 | DG-LAB App 的蓝牙扫描里**能看到本设备** | 手机 App 里搜索 |
| A2 | App **能连接成功**，并能读到服务/特征 | 连接不报错，特征齐全 |
| A3 | App 上**调节 A 通道强度** → 我们收到 `B0` 且解析出的模式/数值与 App 显示一致 | 打日志对照 |
| A4 | App 上**切换波形** → 我们收到对应 4 组频率/强度 | 打日志对照 |
| A5 | 我们回 `B1` → App 界面上的强度**显示正确** | 目视 |
| A6 | App 设置软上限（`BF`）→ 我们持久化，且**重连后行为正确** | 断开重连再验 |
| A7 | 电量显示正确 | 目视 |
| A8 | 协议编解码**可脱离蓝牙单测**：给定 20 字节 → 结构化输出；反向亦然 | `tests/` 里的单测 |
| A9 | 断连后能重新广播、再次被连接（不卡死） | 反复连断 5 次 |

---

## 8. 技术路线（**2026-10-03 实测后重定**）

### 8.1 ❌ 原首选路线：Windows PyWinRT —— **已实测否决**

本机实测（脚本见 `agent/nodes/sim_coyote/probe_peripheral.py`，结论同步在 `docs/research/郊狼真机GATT实测.md`）：

| 服务 | `GattServiceProvider.create_async` 结果 |
|---|---|
| `0x180C`（郊狼核心）、`0x2003`、`0x2004` | ✅ SUCCESS |
| **`0x180A`**（电量 / 1501 / 1502 / 2A25 / 2A59） | ❌ **`DISABLED_BY_POLICY` (error=5)** |
| **`0x1800`**（Device Name） | ❌ `DISABLED_BY_POLICY` |
| `0x1801`（GATT） | ❌ `DISABLED_BY_POLICY` |

**原因是设计使然、无解**：Windows 把 DIS/GAP/GATT/Scan Parameters 列为**系统保留服务**，
第三方 app 不能发布（[MS 官方 GATT Server 文档](https://learn.microsoft.com/en-us/windows/uwp/devices-sensors/gatt-server)）；
Windows 只会自动发布一个**只含 PnP 特征、且不可增改**的 DIS。

后果：假主机**永远缺 `0x180A/0x1500`、`0x180A/0x1501`、`0x180A/0x2A59`、`0x1800/0x2A00`**，
而 DG-LAB App 初始化时要 read/notify 它们（`0x1501` 还是它判定 V2/V3 的依据）；
并且**广播名只能取系统蓝牙名、改不了**（本机 `DESKTOP-FSC3NLO`，App 认不出 `47L121000`）。

→ **结论：Windows 纯软件冒充郊狼主机这条路走不通。**

### 8.2 ✅ 现行路线：ESP32 硬件模拟 + 主机通信

用 ESP32（NimBLE / ESP-IDF）跑真正的 BLE 外设，**服务 / 特征 / 广播名可以 100% 复刻真机**
（含 Local Name = `47L121000`、完整 GATT 全表、厂商数据等）。

```
控制者手机 DG-LAB App
    │ ① BLE（App 以为连的是真郊狼主机）
    ▼
ESP32（NimBLE 外设：复刻真机 GATT 全表 + 广播名 47L121000）
    │ ② 串口 / WiFi（本机链路）—— ★ 本任务在 PC 侧要留的接口
    ▼
agent/nodes/sim_coyote   ← PC 侧剩下的部分
    │ ③ command/pulse_raw（本机 Zenoh）—— 强度 op + 每通道 8 字节波形帧
    ▼
coyote_v4 ── device.op ──> 被控端
```

**PC 侧的职责**：把"BLE 外设"抽象成**可插拔后端**（`transport.py`），
ESP32 后端只需实现「收字节 / 发字节 / 连接事件」三个动作 ——
协议编解码（`protocol.py`）与节点逻辑（`node.py`）与后端**完全解耦**，换后端不动上层。

### 8.3 已废弃的备选

- `bless` 库：与 bleak 3.x 冲突，且底层同样是 WinRT，**同样发布不了保留服务** —— 无用。
- Windows `GattServiceProvider` 代码路径**保留**在 `transport.py` 里（作参考 / 回归对照），
  但**不作为交付路线**。

---

## 9. 第一步（✅ 已完成 —— 结论：**走 B，用 ESP32**）

最小可行性验证已做完：

| 检查项 | 结果 |
|---|---|
| 适配器 `is_peripheral_role_supported` | ✅ True |
| `0x180C` / `0x2003` / `0x2004` 能否发布 | ✅ SUCCESS |
| **`0x180A` / `0x1800` / `0x1801`** 能否发布 | ❌ **`DISABLED_BY_POLICY`（无解）** |
| 广播名能否自定义成 `47L121000` | ❌ 只能取系统蓝牙名（本机 `DESKTOP-FSC3NLO`） |

→ **Windows 纯软件路线否决，改用 ESP32**（详见 §8.2）。
PC 侧改为实现「可插拔 BLE 后端 + 协议编解码 + 节点逻辑」，其中 ESP32 链路按 §8.2 的 ② 留接口。

---

## 10. 参考资源

| 资源 | 位置/链接 | 用途 |
|---|---|---|
| 郊狼 BLE V3 协议 | `reference/dglab-bluetooth-protocol/coyote/v3/README.md` | **权威规格，逐字节照做** |
| 郊狼 BLE V3 示例 | `reference/dglab-bluetooth-protocol/coyote/v3/example.md` | 报文实例 |
| 郊狼 BLE V2 协议 | `reference/dglab-bluetooth-protocol/coyote/v2/README.md` | 旧版对比（App 若是 V2 需兼容） |
| Android 端 BLE 实现 | `reference/Coyote-in-Cradle-Android/app/src/main/java/com/indhg/aiforcoyote/game/BleCoyote.kt` | 看别人怎么写郊狼 BLE 客户端（正好是我们在模拟的那一端） |
| 爪印 BLE（bleak 用法） | `reference/DG-LAB-contral-zhuaying-for-py/backend/ble_manager.py` | bleak 3.x 的实战用法参考 |
| DG-LAB App 的 WebView Bridge | APK 内 `assets/test4.html`（可从 `C:\Users\zsy\Downloads\dglab_online_426_0903_220556.apk` 解包） | §4.1 特征清单的来源 |
| Microsoft GATT Server 文档 | https://learn.microsoft.com/windows/apps/develop/devices-sensors/gatt-server | WinRT 官方用法 |
| PyWinRT BLE 模块 | `winrt-Windows.Devices.Bluetooth.GenericAttributeProfile` (PyPI) | Python 绑定 |

**下载/安装一律走代理 `http://127.0.0.1:7897`。**

---

## 附：允许自行决定的事

- 内部代码结构与命名
- 是否额外实现 V2 协议兼容
- 电量模拟策略（真实读电池？固定值？配置文件？）
- 日志粒度
- 是否把 `BF` 持久化到 JSON 文件（**建议做**）

## 附：绝对不要做的事

- ❌ 修改 `reference/` 下任何文件
- ❌ 修改 `docs/架构设计与路线图.md`
- ❌ 动 `agent/controlled/`、`agent/room/`、`agent/transport/`（其他 agent 的领地）
- ❌ 在本模块里实现叠加/惩罚/网络逻辑（那是 bridge 的活）
- ❌ 硬编码"猜"出来的 §4.6 待实测值 —— 要么实测拿到，要么留 TODO + 可配置
