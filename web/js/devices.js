/* devices.js —— 设备下拉框（显示真实设备名，提交稳定 ID，记住上次选择） */
(function () {
  "use strict";
  window.CL = window.CL || {};

  const KINDS = { camera: "摄像头", microphone: "麦克风", ble: "BLE 设备", serial: "串口" };
  const STORE = "coyotelink.selected.";

  function load(kind) {
    try { return localStorage.getItem(STORE + kind) || ""; } catch (e) { return ""; }
  }
  function save(kind, id) {
    try { if (id) localStorage.setItem(STORE + kind, id); else localStorage.removeItem(STORE + kind); }
    catch (e) { /* 隐私模式忽略 */ }
  }

  class Devices {
    /** selects: {camera, microphone, ble, serial} 的 <select> 元素；onSelect(kind,id) 回调 */
    constructor(selects, hintEl, onSelect) {
      this.selects = selects;
      this.hint = hintEl;
      this.onSelect = onSelect;
      for (const kind of Object.keys(selects)) {
        const sel = selects[kind];
        if (!sel) continue;
        sel.addEventListener("change", () => {
          const id = sel.value;
          save(kind, id);
          if (this.onSelect) this.onSelect(kind, id);
        });
      }
    }

    apply(msg) {
      const devices = (msg && msg.devices) || [];      const selected = (msg && msg.selected) || {};
      const groups = { camera: [], microphone: [], ble: [], serial: [] };
      for (const d of devices) {
        const k = d.kind === "microphone" ? "microphone" : d.kind;
        if (groups[k]) groups[k].push(d);
      }
      // 串口不走 devices 数组（它不属于设备注册表），单独挂在 msg.serial.items 上
      for (const d of ((msg && msg.serial && msg.serial.items) || [])) {
        groups.serial.push(d);
      }
      for (const kind of Object.keys(this.selects)) {
        this._fill(kind, groups[kind] || [], selected[kind]);
      }
      if (this.hint) {
        const n = groups.serial.length;
        this.hint.textContent =
          `摄像头 ${groups.camera.length} · 麦克风 ${groups.microphone.length} · BLE ${groups.ble.length}` +
          (n ? ` · 串口 ${n}` : "") +
          (msg && msg.serial && msg.serial.error ? "（串口枚举失败）" : "") +
          (msg && msg.mock ? "（mock）" : "");
      }
    }

    /** 外部（如「连接」按钮）设定某类设备的选择：更新下拉 + 记住（**不触发** onSelect，免得重复发指令） */
    setActive(kind, id) {
      const sel = this.selects[kind];
      if (!sel) return;
      if (id && [...sel.options].some((o) => o.value === id)) sel.value = id;
      save(kind, id || "");
    }

    _fill(kind, list, backendSelected) {
      const sel = this.selects[kind];
      if (!sel) return;
      const current = sel.value || load(kind);
      const autoLabel = kind === "ble" ? "（无）" : "（自动）";
      sel.innerHTML = `<option value="">${autoLabel}</option>`;
      let shown = 0;
      for (const d of list) {
        // ★ `hidden` = 不该出现在 UI 里的设备（蓝牙虚拟串口 / 回环输出）：直接跳过，
        //   不置灰、不占一行（用户要求："不要置灰，UI 屏蔽就行"）。
        if (d.hidden) continue;
        const opt = document.createElement("option");
        opt.value = d.id;
        let suffix = "";
        if (d.available === false) suffix = "（不可用）";
        else if (d.likely_esp32) suffix = "（像 ESP32）";     // 串口里最该选的那个
        else if (d.detail && d.detail.internal) suffix = "（内置）";
        opt.textContent = (d.label || d.name) + suffix;
        if (d.available === false) opt.disabled = true;
        sel.appendChild(opt);
        shown++;
      }
      const want = backendSelected || current;
      if (want && [...sel.options].some((o) => o.value === want)) sel.value = want;
      return shown;
    }
  }

  window.CL.Devices = Devices;
  window.CL.DEVICE_KINDS = KINDS;
})();
