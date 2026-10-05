# 任务 TF：节点框架与消息总线（**最高优先级 · 所有节点的地基**）

> **为什么最高优先**：v3 决策是"**12 个独立进程节点 + Zenoh 通信**"。所有节点都依赖本任务产出的
> `agent/core/`，所以**它是新的关键路径** —— 不做完它，其他任务无从开工。
> **上位文档**：`docs/架构设计与路线图.md` §5（节点清单与 key space 的唯一真源）
> **预估**：1~2 天

---

## 0. 一句话目标

让"写一个节点"变成三步：**继承 `Node` → 声明订阅/发布 → 写业务逻辑**。

---

## 1. 背景

v2 的 `agent/` 是单体程序：摄像头模块崩了会拖垮郊狼控制；姿态数据只能靠函数调用耦合给别的模块；加功能要动整个程序。

v3 改成节点化（用户决策）：
- **多路可复用** —— 数据走主题，任意多消费者订阅
- **故障隔离** —— 每个功能独立进程，崩一个不影响其他
- **像 ROS2** —— 节点 / 主题 / 服务 / 自动发现

中间件选 **Zenoh**（ROS2 官方选定的非 DDS 中间件）。

---

## 2. 工作区地图

```
web控制\
├── agent\
│   ├── core\                    ← ★★ 本任务主战场【新建】
│   │   ├── __init__.py          对外导出 Node / Config / topics
│   │   ├── node.py              Node 基类
│   │   ├── topics.py            ★ key space 唯一真源
│   │   ├── messages.py          消息 schema
│   │   ├── config.py            配置 + **UTF-8 强制**
│   │   ├── logging.py           统一日志
│   │   ├── devices.py           M0 设备管理（接口在 T2 §6.5 定义，本任务可先留桩）
│   │   ├── supervisor.py        进程管理 + 自动重启
│   │   └── cli.py               命令行入口
│   ├── nodes.toml               ★ supervisor 的节点启动清单
│   └── nodes\                   ← 各功能节点（**不属于本任务**，别动）
└── tests\
    ├── test_node.py             ← ★ 本任务
    ├── test_topics.py           ← ★ 本任务
    ├── test_messages.py         ← ★ 本任务
    └── test_supervisor.py       ← ★ 本任务
```

| 位置 | 权限 |
|---|---|
| `agent/core/`、`agent/nodes.toml` | ✅ 主战场 |
| `tests/test_node.py`、`test_topics.py`、`test_messages.py`、`test_supervisor.py` | ✅ |
| `agent/nodes/` | ❌ **别动**（各功能任务的领地） |
| `tools/doctor.py` | 👀 只读（`cli doctor` 复用它） |
| `reference/`、`docs/架构设计与路线图.md` | ❌ 只读 |

---

## 3. 环境（Zenoh **已装并实测**）

| 项 | 值 |
|---|---|
| Python | 3.13.13 @ `D:\Miniconda3\python.exe` |
| **Zenoh** | `eclipse-zenoh` **1.10.1** ✅ **已安装**（`eclipse_zenoh-1.10.1-cp39-abi3-win_amd64.whl`） |
| 其他已装 | `msgpack`、`pydantic`、`websockets`、`fastapi` |
| 代理 | `http://127.0.0.1:7897` |

### 3.1 实测可用的 Zenoh API（**照这个写，别猜**）

```python
import json, zenoh

cfg = zenoh.Config()
session = zenoh.open(cfg)                       # → Session

# 订阅 —— ★ 支持通配符 ** 和 *
def on_sample(s):
    key  = str(s.key_expr)                      # ★ 必须 str() 转换
    data = bytes(s.payload)
sub = session.declare_subscriber("coyotelink/local/**", on_sample)

# 发布
pub = session.declare_publisher("coyotelink/local/feature/pose")
pub.put(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

# 服务式（query / reply）—— 设备枚举这类"请求-响应"用它
def on_query(q):
    q.reply("coyotelink/local/query/devices", b'{"devices":[]}')   # ★ 两个参数！
qable = session.declare_queryable("coyotelink/local/query/devices", on_query)

for reply in session.get("coyotelink/local/query/devices", timeout=2.0):
    if reply.ok:
        raw = bytes(reply.ok.payload)

session.close()
```

> ⚠️ **实测踩到的坑**：`Query.reply()` **必须传两个参数** `(key_expr, payload)`。
> 只传 payload 会 `TypeError: Query.reply() missing 1 required positional argument: 'payload'`。

**跨机配置**（本任务**不用**，跨机归 T3；实现时确认键名）：
```python
cfg.insert_json5("listen/endpoints",  '["tcp/0.0.0.0:7447"]')
cfg.insert_json5("connect/endpoints", '["tcp/<对端地址>:7447"]')
```

---

## 4. 组件与接口设计

### 4.1 `topics.py` —— key space 唯一真源 ⭐

**硬性要求**：所有 key 只在这里定义，**其他任何地方不许出现裸 key 字符串**。

```python
# agent/core/topics.py
PREFIX = "coyotelink"

def _k(room: str, suffix: str) -> str:
    return f"{PREFIX}/{room}/{suffix}"

def feature(topic: str, room: str = "local") -> str: ...
def command(topic: str, room: str = "local") -> str: ...
def event(topic: str, room: str = "local") -> str: ...
def status(node: str,   room: str = "local") -> str: ...

# —— 常量（后缀）——
FEATURE_FRAME_RAW     = "feature/frame.raw"      # ❌ 永不跨机
FEATURE_POSE          = "feature/pose"
FEATURE_FACE          = "feature/face"
FEATURE_AUDIO         = "feature/audio"
FEATURE_HEART         = "feature/heart"
EVENT_PAWPRINT        = "event/pawprint"
EVENT_DEVICE          = "event/device"
EVENT_FIRE            = "event/fire"
COMMAND_FIRE          = "command/fire"
COMMAND_CLEAR         = "command/clear"
COMMAND_SELECT_DEVICE = "command/select_device"
COMMAND_RAW_V4        = "command/raw_v4"         # ❌ 仅本机

# ★ 跨机白名单（bridge 节点据此过滤；与框架文档 §5.3 表格一致）
CROSS_MACHINE_ALLOWLIST: tuple[str, ...] = (
    FEATURE_POSE, FEATURE_FACE, FEATURE_AUDIO, FEATURE_HEART,
    EVENT_PAWPRINT, EVENT_DEVICE, EVENT_FIRE,
    COMMAND_FIRE, COMMAND_CLEAR, COMMAND_SELECT_DEVICE,
)
```

> `FEATURE_FRAME_RAW` 与 `COMMAND_RAW_V4` **不在白名单里** —— 这是需求 1d"不传视频"在代码层的落地。

### 4.2 `node.py` —— Node 基类

```python
from abc import ABC, abstractmethod

class Node(ABC):
    name: str                       # 子类必须定义，如 "pose"
    room: str = "local"

    def __init__(self, config: Config | None = None): ...

    @abstractmethod
    async def setup(self) -> None:
        """声明订阅/发布、初始化资源。"""

    async def teardown(self) -> None:
        """清理资源。默认空实现。"""

    # —— 便捷方法 ——
    def publish(self, topic: str) -> Publisher: ...
    def subscribe(self, topic: str, handler) -> Subscriber: ...
    def query(self, topic: str, payload: bytes | None = None, timeout: float = 2.0) -> bytes | None: ...
    def declare_queryable(self, topic: str, handler) -> Queryable: ...

    # —— 生命周期 ——
    async def run(self) -> None:
        """setup() → 阻塞直到 stop() 被调用（或收到 SIGINT / SIGTERM）。"""

    def stop(self) -> None:
        """请求退出（**幂等、可重入**）。"""

    # —— 健康 ——
    def heartbeat(self) -> None:
        """向 status/<name> 发一次心跳（基类默认定时发）。"""
```

**硬性要求**

1. **优雅退出（Windows 要特别处理）**：
   - **`SIGINT` + `SIGTERM` + `SIGBREAK`(21) 三件套都要注册** → `stop()` → `teardown()` → `session.close()`
   - **不留残留 Zenoh session、不留僵尸子进程**
   - ⚠️ **实测事实**：Windows 上 `Popen.terminate()` 等价于 `TerminateProcess`，
     **Python 侧捕获不到任何信号**（子进程直接死，returncode=1，handler 一概不触发）。
     唯一能被 Python 捕获的"优雅停止"是 **`CTRL_BREAK_EVENT`（需 `CREATE_NEW_PROCESS_GROUP`）→ `SIGBREAK`**
     → 因此 supervisor 停子进程必须走**三级兜底**（见 §4.6）
2. **异常必须外显**：节点内未捕获异常 → **记日志 + 进程以非 0 退出**（让 supervisor 重启），
   **绝不静默吞掉**
   - ⚠️ **时机坑（F5 实际踩过两次）**：必须**先经 logger 记一次**，**再做 teardown / `close_handlers()`**。
     日志 handler 一旦关闭，之后的 `log.*` 调用会**静默丢消息**（不报错、不抛异常）——
     结果异常只在 stderr 上、日志文件里既没有 traceback 也没有退出码。
   - ✅ 正确姿势（两条都要做到）：
     1. `setup()` 外层 try/except 捕获 → **当场 `self._fatal(exc)`**（内部记日志）→ 再 `raise`
     2. **`close_handlers()` 只在 `run_node()` 的 finally 里调一次**（那是唯一知道退出码的地方）；
        **不要在 `Node.run()` / `_shutdown()` 里提前关** —— 否则 "exit=1" 这条永远落不了盘
3. **UTF-8 强制**（`config.py` 里做）：
   ```python
   sys.stdout.reconfigure(encoding="utf-8", errors="replace")
   sys.stderr.reconfigure(encoding="utf-8", errors="replace")
   ```
   否则设备名里的 `®` 会炸 GBK 控制台（见 `T2-姿态感知与骨骼可视化.md` §6.5 坑 3）
4. **JSON 一律 `ensure_ascii=False` + UTF-8 编码**
5. **不做隐式全局状态**：一个进程 = 一个 Node 实例；**不要在模块级建 session**
6. **回调线程桥接** ⚠️ **（实测发现，最容易踩的坑）**：
   Zenoh 的订阅回调跑在**非主线程**（实测线程名形如 `Thread-1 (pyo3-closure)`），
   而节点业务处理多为 `async` → **基类必须负责桥接**
   （`asyncio.run_coroutine_threadsafe(coro, loop)`），
   否则 `async def` handler **根本不会被 await**（静默失效，极难排查）
7. **`agent/__init__.py` 可以建**（已批准）：`python -m agent.core.cli` 需要它。
   建了它就是共享文件，其他任务不得修改。

### 4.2.1 订阅与查询的调用约定 —— **T2~T5 都照此写（这是对外契约）**

**订阅**
```python
def subscribe(self, topic: str, handler, *, with_key: bool = False) -> Subscriber: ...
# with_key=False（默认） → handler(msg)
# with_key=True          → handler(key, msg)
```
- handler 可同步可 `async`（基类负责桥接，见 §4.2 要求 6）
- 🔒 **坏消息不能打死节点**：JSON 解码失败时原样传 `bytes` 并记 warning，**继续运行**

**查询（请求-响应）**
```python
def declare_queryable(self, topic: str, handler) -> Queryable: ...
# handler(q)：q.key / q.data(bytes) / q.json() / q.reply(payload) / q.raw
```
- `q.reply(payload)` **自动补 key** —— 避开原生 `Query.reply()` 必须传两个参数的那个坑
- handler **返回非 None 时自动 reply**
- 需要原生对象时用 `q.raw`（逃生舱）

**发布**
```python
def publish(self, topic: str) -> Publisher: ...   # pub.put(obj) 自动 JSON 序列化
```

### 4.3 `messages.py` —— 消息 schema

用标准库 `dataclass` + `to_json()/from_json()`，**不强制引入 pydantic**：

```python
@dataclass
class FeaturePose:            # feature/pose
    ts: float
    seq: int
    present: bool
    landmarks: list[list[float]]          # 33×3 归一化 0~1
    visibility: list[float] | None = None

@dataclass
class FeatureFace:            # feature/face
    ts: float
    emotion: str | None
    confidence: float | None
    head: dict | None                     # {"yaw":..,"pitch":..,"roll":..}

@dataclass
class CommandFire:            # command/fire
    waveform: str
    channel: str = "A"
    duration_ms: int = 2000
    priority: int = 1
    overlay: bool = True                  # True → 叠加（不抢占）

@dataclass
class EventFire:              # event/fire（审计记录）
    ts: float
    source: str                           # "rules" | "web"
    waveform: str
    channel: str
    duration_ms: int
```

> **契约规则**：
> 1. 本任务**精确实现上面这 4 个消息** + `Message` 基类 + **`TOPIC_MESSAGES` 注册表**
> 2. 其余 8 个 key 的 payload 由各自节点任务（T2 / T4 / T7）定义，并**注册进 `TOPIC_MESSAGES`**
> 3. 🔒 **字段只增不改**：可以新增字段（向后兼容），但**改已有字段名或语义必须先回报**
>    —— 否则会静默破坏别的节点，而这种 bug 最难查

### 4.4 `config.py`

```python
class Config:
    room: str = "local"
    project_root: Path
    models_dir: Path        # project_root/models
    data_dir: Path          # project_root/data
    log_dir: Path           # project_root/logs
    log_level: str = "INFO"
    zenoh: dict = {}        # connect / listen endpoints

    @classmethod
    def load(cls, path: str | None = None) -> "Config": ...
```

- **入口处强制 UTF-8**（见 §4.2 要求 3）
- 支持环境变量覆盖：`COYOTELINK_ROOM`、`COYOTELINK_LOG_LEVEL`
- 配置文件 `agent/config.toml`（可选，缺省走默认值）

### 4.5 `logging.py`

- 格式：`2026-01-01 12:00:00 [pose] INFO  消息`
- **所有 FileHandler 显式 `encoding="utf-8"`**
- 控制台 handler 也设 UTF-8
- 每节点自动带节点名前缀

### 4.6 `supervisor.py` + `agent/nodes.toml`

```toml
[supervisor]
restart = true
backoff_initial_ms = 500
backoff_max_ms = 30000
max_restarts = 10          # 超过则标记 failed，不再重启
stop_timeout_ms = 5000     # ★ 三级兜底里"等优雅退出"的时间
skip_missing = true        # ★ 模块不存在的节点标记 missing 并跳过，不进入重启循环

[[node]]
name = "camera"
enabled = true
module = "agent.nodes.camera"    # ★ 可覆盖，默认 agent.nodes.<name>（测试注入假节点用）

[[node]]
name = "pose"
enabled = true
```

**supervisor 行为**
1. 读 `nodes.toml`，为每个 `enabled=true` 的节点起子进程（默认 `python -m agent.nodes.<name>`，
   可用 `module` 字段覆盖）
2. 监控退出码；非正常退出 → 按**指数退避**重启
3. **supervisor 自身崩溃 → 已启动的子进程继续运行**（子进程不依赖它存活）
4. `Ctrl+C` → 优雅停止全部子进程
5. ★ **`skip_missing = true`**：模块不存在的节点标记为 `missing` 并**跳过**（不进入重启循环）。
   这样 `nodes.toml` 可以**一次写全 12 个节点** —— `agent/nodes/` 还没建时不报错，
   后续节点落地后**不用改 toml 就自动生效**；`cli nodes` 如实显示 `missing`
6. ★ **停子进程三级兜底（Windows 必须）**：
   `CTRL_BREAK`（先试优雅）→ 等 `stop_timeout_ms` → `terminate()` → 再等 → `kill()`

### 4.7 `cli.py`

```bash
python -m agent.core.cli doctor              # 环境自检（复用 tools/doctor.py）
python -m agent.core.cli nodes               # 列出 nodes.toml 的节点与状态
python -m agent.core.cli run pose            # 单跑一个节点（调试用）
python -m agent.core.cli run-all             # 起 supervisor
python -m agent.core.cli pub <key> '<json>'  # ★ 手动发一条消息（调试神器）
python -m agent.core.cli sub <key>           # ★ 订阅并打印（调试神器）
```

---

## 5. 验收标准

| # | 标准 | 怎么验 |
|---|---|---|
| F1 | 写个 `EchoNode`（订阅 `test/echo` → 原样发到 `test/echo.out`）能跑通 | 单测 |
| F2 | `cli sub "coyotelink/local/test/**"` 能看到另一进程发的消息 | 双进程 |
| F3 | **通配符订阅 `**` 生效** | F2 验证 |
| F4 | `Ctrl+C` 后**干净退出**，无残留 Zenoh session、无僵尸进程 | 反复 5 次 |
| F5 | 节点内抛异常 → 日志清晰 → 退出码非 0 | 故意 raise |
| F6 | supervisor 起 2 个假节点，kill 一个 → **自动重启**，另一个**不受影响** ⭐ | 手动 kill |
| F7 | kill supervisor 本身 → 已启动子进程**继续运行** ⭐ | 手动 kill |
| F8 | 含中文 / `®` 的日志**不炸** | 写一条含 `®` 的日志 |
| F9 | `topics.py` 覆盖框架文档 §5.3 全部 key，且**代码里无裸 key 字符串** | grep 检查 |
| F10 | `query` / `queryable` 一问一答成功 | 单测 |
| F11 | `messages.py` 的 dataclass JSON 往返一致 | 单测 |
| F12 | `nodes.toml` 缺文件 / 格式错时有清晰报错 | 故意写坏 |

---

## 6. 风险与对策

| 风险 | 对策 |
|---|---|
| Zenoh API 版本漂移 | **已实测 1.10.1 用法**（§3.1），照抄 |
| 跨机配置键名未验证 | 本任务**只做本机**；跨机归 T3，届时实测 |
| Windows 无法优雅停止子进程 | `terminate()` = `TerminateProcess`（捕获不到）→ 必须 `CTRL_BREAK_EVENT` + `SIGBREAK`；supervisor 三级兜底（§4.6） |
| **Zenoh 回调在非主线程** | 基类用 `asyncio.run_coroutine_threadsafe` 桥接到事件循环（§4.2 要求 6） |
| 重启风暴 | 指数退避 + `max_restarts` |
| 消息 schema 不兼容 | schema 集中 `messages.py`，改一处 |
| 节点间死锁（互等 query） | query 一律带 timeout |

---

## 7. 第一步（先做这个，做完回报）

1. `config.py`（含 **UTF-8 强制**）+ `logging.py`
2. `topics.py`（key space，覆盖框架文档 §5.3 全部 key）
3. `node.py` + 一个 `EchoNode` 冒烟脚本 → **验证 pub/sub、通配符、优雅退出**
4. 最后做 `supervisor.py`（进程隔离与自动重启）

---

## 8. 参考资源

| 资源 | 位置/链接 | 用途 |
|---|---|---|
| **框架文档 §5** | `docs/架构设计与路线图.md` | **节点清单与 key space 的唯一真源** |
| Zenoh 官方文档 | https://zenoh.io/docs/ | API 参考 |
| Zenoh Python 示例 | https://github.com/eclipse-zenoh/zenoh-python | 各模式示例 |
| 环境自检 | `tools/doctor.py` | `cli doctor` 复用 |
| 编码坑 | `docs/tasks/T2-姿态感知与骨骼可视化.md` §6.5 坑 3 | UTF-8 强制的原因 |

**下载一律走代理 `http://127.0.0.1:7897`。**

---

## 附：允许自行决定
- 序列化用 JSON 还是 msgpack（**建议 JSON**，可调试性优先）
- 心跳间隔
- CLI 是否加彩色输出

## 附：绝对不要做
- ❌ 在 `topics.py` 之外硬编码 key 字符串
- ❌ 让节点依赖 supervisor 存活（必须能独立 `run`）
- ❌ 静默吞异常（必须记日志并退出）
- ❌ 修改 `reference/`、`docs/架构设计与路线图.md`、`tools/doctor.py`
- ❌ 动 `agent/nodes/`（那是各功能任务的领地）
