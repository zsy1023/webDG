/* link.js —— 「连接与配对」卡片：四条链路的状态灯 + 该有的按钮
 *
 * 数据来源：后端 `event/link`（web 节点把它摊平成 `{type:"link", kind, state, …}`）
 *   kind = relay（郊狼 Relay）| serial（ESP32 假主机）| camera（摄像头）
 * 配对：`{type:"pairing", url, ws_url, qr_link, target_id}` —— **给手机 DG-LAB App 扫的码**
 *   （房间那个二维码是"给人扫了打开面板"的邀请码，两者不是一回事）。
 *
 * 为什么要它：以前"连没连上"只在节点日志里，用户得翻控制台才知道；
 * 而"怎么连"更是**一个按钮都没有**（实测反馈："连接按钮在哪里？""郊狼 socket 没有码怎么连？"）。
 *
 * 元素键名**跟 DOM id 一一对应**（`dotRelay` / `dotEsp32` / …），
 * 由 app.js 把那张 `el` 表原样传进来（见 app.js 里 `new CL.LinkStatus(el)`）。
 */
(function () {
  "use strict";
  window.CL = window.CL || {};

  /** Relay 侧状态 → 灯的样式 + 人话 */
  const RELAY_STATES = {
    connected: ["on", "已连接"],
    connecting: ["wait", "连接中…"],
    error: ["off", "连不上"],
    manual: ["wait", "手动模式"],
  };
  /** 串口侧状态 → 灯的样式 + 人话 */
  const SERIAL_STATES = {
    ready: ["on", "已打开"],
    error: ["off", "起不来"],
    idle: ["wait", "未启动"],
  };
  /** 摄像头侧状态 → 灯的样式 + 人话 */
  const CAMERA_STATES = {
    ready: ["on", "采集中"],
    error: ["off", "打不开"],
    idle: ["wait", "未打开"],
  };
  /** 心率带侧状态 → 灯的样式 + 人话 */
  const HEART_STATES = {
    connected: ["on", "已连接"],
    scanning: ["wait", "扫描中…"],
    error: ["off", "连不上"],
    idle: ["wait", "未连接"],
  };

  /**: 卡片头部总览的优先级 —— 谁更该被用户看见排在前面（固定顺序 = 不闪） */
  const HINT_ORDER = ["relay", "camera", "serial", "heart"];

  class LinkStatus {
    /** els: app.js 的 el 表（键=DOM id） */
    constructor(els) {
      this.els = els || {};
      this.last = {};          // kind -> {at, state}（用来判"多久没消息了"）
      this.pairing = null;     // 最近一次配对信息
      this.qrShown = false;
      //: 每条链路**各自**的提示文案（★ 见 _hintFor：以前四条链路共用一个提示位，
      //  每秒互相覆盖 → 卡片里那句提示一闪一闪的，用户实测就是这么反馈的）
      this.hints = { relay: "", serial: "", camera: "", heart: "" };
      this.staleTimer = setInterval(() => this._checkStale(), 3000);
    }

    /** 收到一条 event/link */
    apply(msg) {
      if (!msg) return;
      const kind = msg.kind || (msg.port !== undefined ? "serial" : "relay");
      this.last[kind] = { at: Date.now(), state: msg.state };
      if (kind === "relay") this._relay(msg);
      else if (kind === "serial") this._serial(msg);
      else if (kind === "camera") this._camera(msg);
      else if (kind === "heart") this._heart(msg);
    }

    _relay(msg) {
      const e = this.els;
      const [cls, text] = RELAY_STATES[msg.state] || ["wait", msg.state || "未知"];
      this._dot(e.dotRelay, cls, msg.hint || msg.detail || text);
      if (e.relayState) e.relayState.textContent = text;
      if (e.relayUrl) e.relayUrl.textContent = msg.url || "（未配置）";
      if (e.relayTid) e.relayTid.textContent = msg.target_id || "—";
      // ★ 被控端（手机 App）在不在：socket 这条路上以前**没人**告诉面板，
      //   所以"手机 DG-LAB App"那行永远停在旧状态、断开也不更新（实测反馈）。
      if (msg.app_attached !== undefined) {
        const on = !!msg.app_attached;
        this._dot(e.dotPhone, on ? "on" : "wait", on ? "已被 App 连上" : "App 还没连上（手机扫码配对）");
        if (e.phoneState) e.phoneState.textContent = on ? "已连接" : "未连接";
      }
      // 强度/上限：留给仪表盘（见 app.js 的 renderDash）
      if (msg.strength && Object.keys(msg.strength).length) this.lastStrength = msg.strength;
      // targetId 换了 = relay 重启过 → 手机必须重新扫码（否则一直连不上，用户会以为坏了）
      if (msg.target_id && this._lastTid && String(msg.target_id) !== this._lastTid) {
        this._hintFor("relay", `targetId 变成 ${msg.target_id}（relay 重启过）→ 手机 App 要重新扫码配对`);
      }
      if (msg.target_id) this._lastTid = String(msg.target_id);
      this._hintFor("relay", msg.state !== "connected" ? (msg.hint || msg.detail || "") : "");
      this._renderDash(msg);
    }

    /** 最近一次开火（面板的 event/fire）→ 仪表盘「最近开火」那一行 */
    noteFire(r) {
      if (!r) return;
      const ok = r.ok === false ? "失败" : "成功";
      const when = new Date().toLocaleTimeString("zh-CN");
      const why = r.ok === false && r.error ? `（${r.error}）` : "";
      this._renderDash(null, `${r.waveform || "?"} 通道${r.channel || "?"} ${ok}${why} · ${when}`);
    }

    /**
     * 仪表盘：**左 = 本机链路**（relay 通不通 / targetId / 手机 App 在不在 / 房间），
     * **右 = 对端郊狼**（手机 App 那侧的设备 + 通道 A/B 真实强度与上限）。
     *
     * 数据来源：`coyote_v4` 节点每 2 秒重播的 `event/link`（`app_attached` / `slots` /
     * `strength` 都是它给的），所以**不需要前端自己算**，也不用轮询。
     */
    _renderDash(relayMsg, fireLine) {
      const e = this.els;
      if (!e.dashRelay) return;
      if (relayMsg) {
        e.dashRelay.textContent = RELAY_STATES[relayMsg.state] ? RELAY_STATES[relayMsg.state][1]
          : (relayMsg.state || "—");
        e.dashRelay.style.color = relayMsg.state === "connected" ? "var(--gold)" : "var(--dim)";
        if (e.dashTid) e.dashTid.textContent = relayMsg.target_id || "—";
        if (e.dashApp) {
          e.dashApp.textContent = relayMsg.app_attached
            ? `已连接${relayMsg.app_client_id ? " · " + relayMsg.app_client_id : ""}`
            : "未连接（手机扫码配对）";
        }
        const s = relayMsg.strength || {};
        if (e.dashDev) {
          e.dashDev.textContent = relayMsg.app_attached
            ? (relayMsg.slots && relayMsg.slots.length ? relayMsg.slots.join("、") : s.slot_id || "（未报设备）")
            : "—";
        }
        const ch = (key) => {
          const v = s[key];
          if (!v || (v.real == null && v.limit == null)) return "—";
          return `强度 ${v.real ?? "?"} / 上限 ${v.limit ?? "?"}`;
        };
        if (e.dashA) e.dashA.textContent = ch("A");
        if (e.dashB) e.dashB.textContent = ch("B");
        if (e.dashHint && !e.dashHint.dataset.set) {
          e.dashHint.textContent = "本机链路 / 对端郊狼（来自 coyote_v4 的 event/link，2 秒重播）";
          e.dashHint.dataset.set = "1";
        }
      }
      if (e.dashRoom) {
        const chip = this.els.roomLabel || document.getElementById("roomLabel");
        e.dashRoom.textContent = chip ? (chip.textContent || "—") : "—";
      }
      if (fireLine && e.dashFire) e.dashFire.textContent = fireLine;
    }

    _serial(msg) {
      const e = this.els;
      const [cls, text] = SERIAL_STATES[msg.state] || ["wait", msg.state || "未知"];
      this._dot(e.dotEsp32, cls, msg.detail || text);
      if (e.esp32State) e.esp32State.textContent = text;
      if (e.esp32Port) e.esp32Port.textContent = msg.port || "（未选）";
      // 手机灯：假主机的串口通了才谈得上 App 连接
      const phone = !!msg.phone_connected;
      this._dot(e.dotPhone, phone ? "on" : (msg.state === "ready" ? "wait" : "off"),
                phone ? "已连接" : "未连接");
      if (e.phoneState) e.phoneState.textContent = phone ? "已连接" : "未连接";
      this._hintFor("serial", msg.state !== "ready" ? (msg.hint || "") : "");
    }

    _camera(msg) {
      const e = this.els;
      let [cls, text] = CAMERA_STATES[msg.state] || ["wait", msg.state || "未知"];
      // ★ 在用**虚拟摄像头**（OBS Virtual Camera 这类占位画面）→ 灯变黄 + 明说"不会有姿态数据"。
      //   实测：真摄像头被微信/浏览器页面占着时只能落到虚拟摄像头上，画面里没有人，
      //   MediaPipe 一直 present=False —— 用户会以为"模型没装"。
      if (msg.virtual) { cls = "warn"; text = "虚拟摄像头（没画面）"; }
      this._dot(e.dotCamera, cls, msg.detail || text);
      if (e.cameraDevice) {
        const name = msg.device || "（没打开）";
        const suffix = msg.virtual ? "（虚拟·没画面）" : (msg.count ? `（共 ${msg.count} 台）` : "");
        e.cameraDevice.textContent = name + suffix;
      }
      if (e.cameraFps) {
        e.cameraFps.textContent = msg.state === "ready"
          ? `${(msg.fps ?? 0).toFixed ? Number(msg.fps || 0).toFixed(1) : msg.fps} FPS`
          : "— FPS";
      }
      if (msg.state === "error" && e.cameraFps) e.cameraFps.textContent = "0 FPS";
      // ★ "下拉里选的那台"和"真在用的那台"不一致 —— 这就是用户说的"选了摄像头但好像选错了"。
      //   说清楚：选的是哪台、实际在用哪台、为什么、会不会自己好。（不闪：只有状态变了才换字）
      let hint = msg.hint || "";
      const sel = e.cameraSel ? String(e.cameraSel.value || "") : "";
      const actual = String(msg.device_id || "");
      if (sel && actual && sel !== actual) {
        const want = this._labelOf(e.cameraSel, sel);
        hint = `选的是「${want}」，实际在用「${msg.device || "?"}」—— `
             + "选的那台当时打不开（设备忙/被别的程序占着）。节点每 20 秒会自动重试，"
             + "切过去以后这一行会自己变；" + hint;
      }
      this._hintFor("camera", hint);
    }

    /* ─────────── 心率带：状态 + **可用设备列表** ─────────── */
    _heart(msg) {
      const e = this.els;
      const [cls, text] = HEART_STATES[msg.state] || ["wait", msg.state || "未知"];
      this._dot(e.dotHeart, cls, msg.detail || text);
      if (e.heartDevice) {
        e.heartDevice.textContent = msg.device || (msg.auto ? "（自动扫描）" : "—");
      }
      if (e.heartBpm) e.heartBpm.textContent = msg.bpm ? `${msg.bpm} BPM` : "— BPM";
      this._fillCandidates(e.heartSel, msg.candidates || [], msg.address || "");
      this._hintFor("heart", msg.state !== "connected" ? (msg.hint || "") : "");
    }

    /** 下拉里某个 value 对应的展示名（找不到就退回 value 本身） */
    _labelOf(sel, value) {
      if (!sel) return value;
      const opt = [...sel.options].find((o) => o.value === value);
      return opt ? String(opt.textContent || value).trim() : value;
    }

    /**
     * 心跳带候选下拉：**只在内容真变了**才重建。
     *
     * （状态是每 2s 重播的，每次重建会把用户正在选的项挤掉 —— 那正是"点了没反应"的一种成因。）
     * 用 createElement 而不是 innerHTML：设备名来自系统/广播，不拼进 HTML。
     */
    _fillCandidates(sel, list, current) {
      if (!sel) return;
      const want = current || sel.value || "";
      const sig = list.map((c) => c.id).join(",") + "|" + want;
      if (this._candSig === sig) return;
      this._candSig = sig;
      const keep = sel.value;
      sel.textContent = "";
      const auto = document.createElement("option");
      auto.value = "";
      auto.textContent = list.length ? "（自动挑信号最强的）" : "（还没扫到心率带）";
      sel.appendChild(auto);
      for (const c of list) {
        const opt = document.createElement("option");
        opt.value = c.id;
        const rssi = typeof c.rssi === "number" && c.rssi > -900 ? `  ${c.rssi} dBm` : "";
        opt.textContent = `${c.name || c.id}${rssi}`;
        sel.appendChild(opt);
      }
      const target = want && list.some((c) => c.id === want) ? want : keep;
      if (target && [...sel.options].some((o) => o.value === target)) sel.value = target;
    }

    /* ─────────── 郊狼配对（App 扫的二维码）─────────── */
    applyPairing(msg) {
      if (!msg || msg.type !== "pairing") return;
      this.pairing = msg;
      const e = this.els;
      if (e.relayTid && msg.target_id) e.relayTid.textContent = msg.target_id;
      if (e.pairingHint) {
        e.pairingHint.textContent = msg.error
          ? `配对信息生成失败：${msg.error}`
          : (msg.hint || "");
      }
      const box = e.pairingQr;
      if (!box) return;
      const link = msg.qr_link || "";
      if (!link) {                 // 还没拿到 tid / 没配 url → 先不给假二维码
        box.hidden = true;
        box.innerHTML = "";
        this.qrShown = false;
        return;
      }
      if (!this.qrShown) return;   // 用户没点开就先不渲染（别占版面）
      this._renderQr(box, link);
    }

    /** 「📱 配对二维码」按钮：显示/隐藏 */
    togglePairing() {
      const e = this.els;
      this.qrShown = !this.qrShown;
      if (e.btnPairing) e.btnPairing.textContent = this.qrShown ? "📱 收起二维码" : "📱 配对二维码";
      if (this.pairing) this.applyPairing(this.pairing);
      else this._hintFor("relay", "还没拿到配对信息 —— 等 Relay 灯变绿（要先把中继起起来）");
    }

    _renderQr(box, link) {
      if (!window.CL.QR) return false;
      try {
        box.innerHTML = "";
        box.appendChild(window.CL.QR.svg(link, {
          ecl: "M", border: 3,
          label: "手机 DG-LAB App 扫描这个二维码接入 socket",
        }));
        box.hidden = false;
        return true;
      } catch (err) {
        box.hidden = true;
        box.innerHTML = "";
        return false;
      }
    }

    /** 复制配对链接（发到手机上点开也能连） */
    pairingLinkText() {
      const p = this.pairing || {};
      if (!p.qr_link) return "";
      return [
        "webDG 郊狼配对（手机 DG-LAB App 用）",
        `配对链接：${p.qr_link}`,
        `socket 地址：${p.ws_url || ""}`,
        "用法：手机 App 扫上面的链接生成的二维码，或用支持扫码的浏览器打开配对链接。",
      ].join("\n");
    }

    /* ─────────── 工具 ─────────── */
    _dot(el, cls, title) {
      if (!el) return;
      el.classList.remove("on", "off", "wait", "pulse");
      if (cls) el.classList.add(cls);
      if (cls === "wait") el.classList.add("pulse");   // 黄灯闪 = 正在努力
      if (title) el.title = title;
    }

    /**
     * 把一条提示写到**它自己那条链路的那一行**（`relayHint` / `cameraHint` / …），
     * 再刷新卡片头部那句总览。
     *
     * 为什么要拆开：以前四条链路共用一个 `#linkHint`，而它们都是**每秒/每两秒重播状态**
     * （camera 1s / heart 2s / sim_coyote 2s）—— 于是那句话每秒被另一个节点改写一次，
     * 用户看到的就是"卡片里的提示一闪一闪的"。分片 + 固定优先级的总览就稳定了。
     */
    _hintFor(kind, text) {
      const t = String(text || "").trim();
      if (this.hints[kind] === t) return;      // 内容没变就别再动 DOM
      this.hints[kind] = t;
      const box = this.els[kind + "Hint"];
      if (box) {
        box.textContent = t;
        box.hidden = !t;
      }
      this._refreshSummary();
    }

    /** 卡片头部那句总览：按固定优先级挑"最该被看见"的那条（顺序固定 = 不会闪） */
    _refreshSummary() {
      const el = this.els.linkHint;
      if (!el) return;
      const kind = HINT_ORDER.find((k) => this.hints[k]);
      const text = kind ? this.hints[kind] : "四条链路的状态与操作";
      if (el.textContent !== text) el.textContent = text;
    }

    /** 超过 10 秒没收到某个 kind 的状态 → 灯转灰（后端可能没了） */
    _checkStale() {
      const now = Date.now();
      const dots = {
        relay: this.els.dotRelay, serial: this.els.dotEsp32,
        camera: this.els.dotCamera, heart: this.els.dotHeart,
      };
      const states = {
        relay: this.els.relayState, serial: this.els.esp32State,
        camera: this.els.cameraFps, heart: this.els.heartBpm,
      };
      for (const [kind, rec] of Object.entries(this.last)) {
        if (now - rec.at < 10000) continue;
        this._dot(dots[kind], "", "10 秒没收到状态（那个节点没在跑？）");
        if (states[kind]) states[kind].textContent = "无状态";
        this._hintFor(kind, "");     // 节点都没了，它那条提示也别赖着
      }
    }
  }

  window.CL.LinkStatus = LinkStatus;
})();
