# sim_coyote —— 探针（T1 §9 第一步：可行性验证）

## 这是什么

`probe_peripheral.py` 是**最小可行性探针**，只回答一个问题：

> 这台 Windows 11 能不能用 Python（原生 PyWinRT）广播出一个手机能搜到、能连接的
> 自定义 GATT 服务？

它不是完整实现，只做：建 `0x180C` 服务 + `0x150A`(WRITE) / `0x150B`(NOTIFY) 特征 →
开始广播 → 打印状态 → 收到写入时打印原始字节。

## 怎么跑

```powershell
python agent\nodes\sim_coyote\probe_peripheral.py            # 广播 300 秒
python agent\nodes\sim_coyote\probe_peripheral.py --seconds 600 --verbose
```

然后用手机 **nRF Connect** 或 **DG-LAB App** 搜索并连接，观察控制台输出。
Ctrl+C 提前退出。

## 本机实测结果（2025，本机 Intel Wireless Bluetooth）

| 检查项 | 结果 |
|---|---|
| 适配器 `is_low_energy_supported` | ✅ True |
| 适配器 `is_peripheral_role_supported`（关键） | ✅ True |
| `GattServiceProvider.create_async(0x180C)` | ✅ error=0 (SUCCESS)，**已无历史 `Not implemented`** |
| 创建 `0x150A`(WRITE) 特征 | ✅ |
| 创建 `0x150B`(NOTIFY) 特征 | ✅ |
| `start_advertising_with_parameters(discoverable, connectable)` | ✅ 最终 `advertisement_status = STARTED` |

> 注意：广播启动瞬间会先出现一次 `ABORTED(3)`，随后自动重试到 `STARTED(2)`。
> 枚举：`CREATED=0, STOPPED=1, STARTED=2, ABORTED=3, STARTED_WITHOUT_ALL_ADVERTISEMENT_DATA=4`。

**手机能否搜到 / 能否连接：需真机确认** —— 本机只有一个蓝牙适配器，无法边广播边自扫，
且周边无其它 BLE 设备，所以这一条只能在手机上实测。

## PyWinRT 3.2.1 用法要点（踩过的坑）

1. **GUID 用标准 `uuid.UUID`**，不再有 `winrt.system.Guid`（该属性已不存在）。
   直接 `GattServiceProvider.create_async(uuid.UUID("0000180c-..."))`。
2. `create_async(...)` 返回 **`GattServiceProviderResult`**，取 `.service_provider`；
   先查 `.error`（0=SUCCESS，非 0 常见 `DisabledByPolicy`）。
3. `service.create_characteristic_async(uuid, params)` 返回
   **`GattLocalCharacteristicResult`**，取 `.characteristic`（不是特征本身），同样查 `.error`。
4. 读 `IBuffer`：`DataReader.from_buffer(buf).read_bytes(buf.length)`。
5. 事件回调在 WinRT 后台线程触发；要 `await`（如 `args.get_request_async()`）时，
   先 `args.get_deferral()`，再 `asyncio.run_coroutine_threadsafe(..., loop)`，最后 `deferral.complete()`。

## `client_info` 预期字段（真机连接后由脚本打印）

来自 `GattSubscribedClient` / `GattSession`：

- `device_id`：形如 `BluetoothLE#BluetoothLE<适配器MAC>-<对端MAC>`
- `peer_mac` / `adapter_mac`：从上解析出的对端/本机 MAC
- `connected_at`：连接时刻（脚本收到订阅事件时的 `time.time()`）
- `session_status`、`max_pdu_size`、`can_maintain_connection`
- `device_name`：用对端 MAC 经 `BluetoothLEDevice.from_bluetooth_address_async` 反查（可能需成功连接后才可见）

## 已知风险 / 待实测

- **本地名不可自定义**：`GattServiceProvider` 广播的本地名取自系统蓝牙名（本机 `DESKTOP-FSC3NLO`），
  而郊狼协议里主机名为 `47L121000`。若 App 靠名字前缀识别设备，可能需要在 Windows
  蓝牙设置/注册表里把本机名改成 `47L121000`（待真机验证 App 是否认名字）。
- 首次出现 `ABORTED` 的稳定性、被多次连断后能否持续广播（验收 A9），需真机反复测。
- `180A/1501`、`180A/2A59`、电量编码、广播厂商数据等，见任务书 §4.6，仍需真机抓包对齐。

## 依赖

`winrt-runtime`、`winrt-Windows.Devices.Bluetooth`、
`winrt-Windows.Devices.Bluetooth.GenericAttributeProfile`、`winrt-Windows.Foundation`、
`winrt-Windows.Storage.Streams`。**不要用 `bless`**（与 bleak 3.x 冲突）。
