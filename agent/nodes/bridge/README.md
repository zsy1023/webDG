# `bridge` 节点（T3）—— 跨机白名单桥接

把本机 Zenoh 网络与对端机器的 Zenoh 网络**只在白名单 key 上**打通。

> 🔒 **隐私铁律**：`feature/frame.raw` 与 `command/raw_v4` **永不跨机**。
> 这里的"永不"是**代码级**的：bridge **根本不订阅**这两条 key，且链路侧收到也用
> `topics.is_cross_machine()` **复核后丢弃**。不是"约定别传"，是"根本没放行"。

T3 收缩后（Zenoh 接管了跨机连通 / 自动发现 / TLS），本节点只负责：

1. **白名单转发**（本页重点）；
2. **房间语义**：6 位房间码 + `a`/`b` 角色 + 握手（`room.py` + 链路 `_hello`）；
3. **可选应用层 AEAD**（`crypto.py`，`encrypt=true` 打开）；
4. **中继**：NAT 双向失败时两端都连 `zenohd`（见下）。

---

## 1. 网络拓扑

```
        A 机器                                     B 机器
 ┌───────────────────────┐                 ┌───────────────────────┐
 │ pose/web/rules/...    │                 │ pose/web/rules/...    │
 │        │ 本机 Zenoh    │                 │  本机 Zenoh │         │
 │        ▼              │   跨机链路       │             ▼         │
 │   bridge A ───────────┼──(tcp/7451)─────┼─────────► bridge B     │
 └───────────────────────┘                 └───────────────────────┘
```

* bridge 有**两个 Zenoh session**：本机 session（基类管的 `self.session`）+ 跨机 session
  （本节点自开）。两者互不相通，唯一的"桥"就是白名单转发逻辑。
* 链路 key：`coyotelink/<房间码>/bridge/<发送方角色>/<后缀>`。
  `a` 端只发 `bridge/a/...`、只订 `bridge/b/...`，`b` 端相反 → 天然无自环。

---

## 2. 运行

### 2.1 独立运行（不依赖 supervisor）

A 端（建房方）：

```powershell
python -m agent.nodes.bridge --role a --room-code AB12CD --listen tcp/0.0.0.0:7451
```

B 端（加入方）：

```powershell
python -m agent.nodes.bridge --role b --room-code AB12CD --connect tcp/<A的公网IP>:7451
```

> 暴露 `7451` 到公网由**用户自行配樱花 frpc**（部署阶段，不在本节点内）。
> 局域网内若开 multicast scouting，两端也能**自动发现**（无需显式 connect）。

### 2.2 配置 `agent/config.toml`

```toml
room = "local"                # 本机节点用的 room（本机网络）

[zenoh]                       # 本机 session（通常不用配）
# listen = ["tcp/0.0.0.0:7447"]

[bridge]                      # 跨机 session
role = "a"                    # "a" | "b"
listen = ["tcp/0.0.0.0:7451"] # A 端：对外监听
# connect = ["tcp/<对端>:7451"]# B 端：连对端
# room_code = "AB12CD"        # 可选：钉死链路 room（不配就跟着面板选的房间走）
# encrypt = false             # 可选：覆盖"有真房间码就自动加密"的默认规则
hello_interval_s = 5.0        # 握手/心跳周期

[bridge.raw_json5]            # 透传到跨机 session 的原生配置（可选）
"scouting/multicast/enabled" = "false"
```

> **`room_code` 配不配是有区别的**（2026-10 补，房间切换落地之后）：
>
> - **配了** → 链路 room **钉死**成这个值，跟业务房间（面板上创建/加入的那个）各走各的。
>   面板换房间时链路侧一个 key 都不动 —— 这是"两条业务房间之间的固定通道"。
> - **没配**（**推荐**：只要房间码就够）→ 链路 room 就**等于业务房间**，
>   所以面板换房间时链路侧也跟着重建
>   （`BridgeNode.on_room_switched` → `_rebuild_link_side`：换 key、**重派生 AEAD 密钥**、
>   清去重表、重发 hello）。不重建的话本机换到新房间、链路还订在旧前缀上 ——
>   跨机转发整条断掉，而且**不报任何错**。
>
> 两端各自在面板上选同一个房间码即可 —— 房间、链路、密钥一次配齐，不做自动协商。
> 加密规则见 §4。

CLI 参数（`--role/--room-code/--listen/--connect/--encrypt`）**只在你显式给出时**才覆盖配置文件
（`selftest.py` 与 `run-all` 都靠配置文件里的 `role`；`--role` 的默认值必须是 `None` 而不是
`"a"` —— 曾经写死成 `"a"`，结果两端都变成 role=a、链路 key 对不上、跨机**静默不通**，见 §5）。

### 2.3 跨机不通怎么查（**必读**）

三个环节各自独立，任何一环断了都表现为"对端什么都没收到"，所以**先看日志再猜**：

| 现象 | 看哪里 |
|---|---|
| `bridge 就绪：role=… peer=…` 里 role 不对 | 两端必须**一 a 一 b**。role 取自 `[bridge] role`（只有 CLI `--role` 显式给才覆盖）。两端同为 a → 链路 key 根本对不上 → 静默不通 |
| 没有「对端 online」 | 链路没连上：查 `listen`/`connect` endpoint、防火墙、樱花穿透 |
| 只有「本机 → 跨机」、没有对应的「跨机 → 本机」 | 链路没送到（端口/穿透），或**解密失败**（两端房间码不同 → 日志里会有"解密失败"） |
| 两边都有这两条、下游节点还是收不到 | 下游没订对 key（`room` 不一致；换过房间就等它重新声明订阅） |

> 「本机 → 跨机」「跨机 → 本机」是 **DEBUG 级**（`--log-level DEBUG` 才打）。
> 以前这三环一盏灯都没有，跨机不通时日志里干干净净 —— 为此烧掉过半小时。
>
> 一条命令自检：`python agent/nodes/bridge/selftest.py`（本机 4 进程模拟两台机器，
> 判据是"白名单 key 过河 + 原始帧被拦"，PASS 才算桥真的通）。

### 2.4 supervisor

`agent/nodes.toml` 已登记 `bridge`，`cli run-all` 会自动带起来；
`cli run bridge` 可单跑。

---

## 3. 中继（NAT 双方都打不了洞时）

两端都 `connect` 到同一台公共 **`zenohd`（router 模式）**，由它中继：

```powershell
# 公共服务器
zenohd
```

A 端配置：

```toml
[bridge]
role = "a"
room_code = "AB12CD"
connect = ["tcp/<服务器>:7447"]
```

B 端配置：

```toml
[bridge]
role = "b"
room_code = "AB12CD"
connect = ["tcp/<服务器>:7447"]
```

> 中继**只转发密文**：开了 `encrypt=true` 时它连 payload 都看不到；
> 不开时链路仍是 Zenoh 的 TCP/TLS 传输。中继不持有房间码对应的应用层密钥。

**可选的第二层防护**：`zenohd` 支持 `access_control` 里按 key 表达式配 ACL，
可在中继侧再挡一次非白名单 key（纵深防御）。本节点已有两道应用层闸门，
router ACL 属额外加固，部署时按需开启。

---

## 4. 可选的应用层 AEAD（E2E）

Zenoh 的 `transport/link/tls` 是**链路加密**；AEAD 再叠一层**端到端**，
让"连中继都看不到明文"。密钥由**房间码**经 HKDF-SHA256 派生（房间码即口令，
安全性依赖房间码不外泄），两端一致即可，无需额外握手。线格式见 `crypto.py`。

**一个房间码搞定房间 + 链路**（2026-10 定）：

| 情况 | 链路房间 | 加密 |
|---|---|---|
| 面板选了房间码（如 `AB12CD`），`[bridge]` 里**什么都不配** | = 面板那个码 | **自动开**，密钥 = `derive_key(码)` |
| `[bridge] room_code = "XXXXXX"` | 钉死成它 | 自动开（密钥 = `derive_key("XXXXXX")`） |
| 房间是默认的 `local` | `local` | **不加密** + 警告（那个密钥人人可推，"看着像加密"更危险） |
| 房间是人起的弱名字（`bedroom`） | 它 | 不加密 + 警告；显式 `encrypt = true` 则照做（只算混淆） |
| 显式 `encrypt = false` | — | 关（排障看明文） |

> 所以跨机只要**两端各自在面板上选同一个房间码**就行 —— 不用再单独记一个 `room_code`。
> `room_code` 现在只用于"想钉死一条固定链路、跨多个业务房间"的场景。
>
> ⚠️ 房间码只有 ~29 位熵（6 位 × 31 字符表），离线爆破是可行的 ——
> 这一层是**挡中继/旁观者**的，不是抗定向攻击的。要更强的机密性就上 Zenoh TLS。
> 之前那句"默认关闭"已经不成立：现在是"有真房间码就默认开"。

---

## 5. 自测

```powershell
# 单元测试（假 session，不连网）
python -m pytest tests/test_bridge.py -q

# 键名探测：验证 listen/connect 配置键 + "直连会泄漏 frame.raw"对照实验
python agent/nodes/bridge/_probe_zenoh.py

# 同机双进程端到端实测（白名单内能过、frame.raw 过不去）—— 最终证据
python agent/nodes/bridge/selftest.py
```

`selftest.py` 用**四个独立进程**模拟两台机器（每侧本地网络用独立端口 + 关 multicast
以隔离），打印 `RESULT=PASS` 表示：白名单 key 过河、原始帧被拦。

⚠️ **2026-10 实测记录**：这个 selftest 曾经一直 FAIL，根因是 CLI 的 `--role` 默认值
被写死成 `"a"`（而 `main()` 无条件覆盖配置）→ 两端同为 role=a → 链路 key 对不上 →
**跨机静默不通**。修掉默认值之后 selftest 变 PASS，且上面的"本机 → 跨机 / 跨机 → 本机"
两条日志都能看到（payload 是密文，说明房间码派生密钥那条路也通了）。
教训：跨机链路的每一环都得留痕，否则"不通"和"没在跑"在日志里长得一模一样。

---

## 6. 文件

| 文件 | 职责 |
|---|---|
| `node.py` | `BridgeNode`：白名单转发 + 自环破除 + 握手 + 生命周期 |
| `room.py` | 6 位房间码生成/校验、`a`/`b` 角色 |
| `crypto.py` | 可选应用层 AEAD（HKDF-SHA256 + AES-256-GCM） |
| `selftest.py` | 同机四进程端到端实测（证据生成器） |
| `_probe_zenoh.py` | Zenoh 跨机配置键实测探针 |
