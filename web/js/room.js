/* room.js —— 房间入口层
 * 打开页面就是控制台，第一件事：创建房间 / 加入房间（也可跳过走本地模式）。
 *
 * 与后端的约定（**前端已按此发送，待后端实现**）：
 *   { op:"room", action:"create", code:"AB12CD" }
 *   { op:"room", action:"join",   code:"AB12CD", url:"ws://…" }   // url 可选
 *   { op:"room", action:"leave" }                                   // 离开房间
 * 后端若支持，请回一条下行：
 *   { "type":"room", "ok":true, "action":"create", "code":"AB12CD", … }
 *   { "type":"room", "ok":false, "error":"…" }
 *
 * 诚实原则：后端没有这个 op 时，前端**不会**谎报房间已生效 ——
 *   顶栏显示的一律以 `hello.room` 为准，房间层里明确写出"后端实际 room=…"。
 * 房间码的 `id` 就是**裸串口名风格的短码**（与 relay 的 `?room=` 同格式，无前缀）。
 */
(function () {
  "use strict";
  window.CL = window.CL || {};

  const STORE = "webdg.room";                             // {mode, code, url}
  const CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";  // 去掉易混的 I / O / 0 / 1
  const CODE_LEN = 6;
  const RE_CODE = /^[A-Z0-9]{6,16}$/;                     // ≥6 位（relay 生成的码也是 6 位）
  const RE_DIGITS = /^[0-9]+$/;                           // 纯数字 = 多半是把端口号当房间码了
  const RE_URL = /^wss?:\/\/\S+$/i;

  const $ = (id) => document.getElementById(id);

  /** 邀请信息里"对端地址"那一行的标签（生成 / 解析两边都用它，别写两处） */
  const LABEL_CODE = "房间码";
  const LABEL_URL = "对端地址";
  const LABEL_LINK = "一键直达";

  /**
   * 从一段文本里认出「房间码 + 对端地址」。
   *
   * 收的口径（按优先级）：
   *   1. 带标签的那一行 `房间码：AB12CD`（「复制邀请信息」生成的就是这个形状，最可信）
   *   2. http(s) 直达链接里的 `?room=AB12CD`（对面只发了链接时）
   *   3. 整段就是一个裸码 `AB12CD`
   *   4. 兜底①：去掉 URL 后第一个**全大写**的 4~16 位串（排除 WEBDG 这类词）
   *   5. 兜底②：任何 `?room=`（⚠️ 放最后：对端地址 `ws://…?room=XXX` 里的 room
   *      是 **relay 口令**，跟房间码是两码事，别抢在前面认错）
   *
   * 为什么要它：对方发来的是「一键复制」出来的**一整段邀请信息**，
   * 让人手动从里面挑出房间码再抄进输入框，既蠢又容易抄错。
   */
  function parseInvite(text) {
    const raw = String(text == null ? "" : text);
    let url = "";
    const ws = raw.match(/wss?:\/\/[^\s"'<>、，）)]+/i);
    if (ws) url = ws[0];

    let code = "";
    const byLabel = raw.match(new RegExp(LABEL_CODE + "\\s*[:：=]?\\s*([A-Za-z0-9]{4,16})"));
    if (byLabel) code = byLabel[1];

    if (!code) {
      const http = raw.match(/https?:\/\/[^\s"'<>、，）)]*/i);
      const m = http && http[0].match(/[?&]room=([A-Za-z0-9]{4,16})/i);
      if (m) code = m[1];
    }
    if (!code) {
      const bare = raw.trim().match(/^[A-Za-z0-9]{4,16}$/);
      if (bare) code = bare[0];
    }
    if (!code) {
      // 兜底①：把 URL 抠掉再找，且只认全大写（英文句子里的 webDG 这类词不会中招）
      const stripped = raw.replace(/https?:\/\/\S+/gi, " ").replace(/wss?:\/\/\S+/gi, " ");
      const caps = stripped.match(/\b[A-Z0-9]{4,16}\b/g) || [];
      const bad = ["WEBDG", "HTTP", "HTTPS", "WS", "WSS", "LOCAL"];
      const hit = caps.find((t) => !bad.includes(t));
      if (hit) code = hit;
    }
    if (!code) {
      const anyRoom = raw.match(/[?&]room=([A-Za-z0-9]{4,16})/i);
      if (anyRoom) code = anyRoom[1];
    }
    return { code: code ? code.toUpperCase() : "", url };
  }

  /** 读剪贴板；没权限 / 不支持 / 被拒 → 空串（调用方给"手动粘也行"的提示） */
  function readClipboard() {
    try {
      if (navigator.clipboard && navigator.clipboard.readText) {
        return navigator.clipboard.readText().then((t) => t || "", () => "");
      }
    } catch (e) { /* 旧浏览器没有 clipboard */ }
    return Promise.resolve("");
  }

  /** 这段文字"像一整段邀请信息"吗（而不是用户正在逐字敲房间码） */
  function looksLikeInvite(text) {
    const t = String(text == null ? "" : text).trim();
    if (!t) return false;
    if (t.includes(LABEL_CODE)) return true;    // "房间码：…" —— 一眼就是粘进来的
    if (/[\n\r]/.test(t)) return true;          // 多行 = 一整段
    if (/[:：=]/.test(t)) return true;          // 带标签 / 带 ws:// 地址
    if (/[?&]room=/i.test(t)) return true;      // 直达链接
    return t.length > 16;                       // 房间码最长 16 位，再长就不是在敲码
  }

  function load() {
    try { return JSON.parse(localStorage.getItem(STORE) || "null") || null; } catch (e) { return null; }
  }
  function save(v) {
    try { localStorage.setItem(STORE, JSON.stringify(v)); } catch (e) { /* 隐私模式忽略 */ }
  }
  function newCode() {
    let s = "";
    for (let i = 0; i < CODE_LEN; i++) s += CODE_CHARS[Math.floor(Math.random() * CODE_CHARS.length)];
    return s;
  }

  class RoomLayer {
    /** cbs: {send(obj)->bool, onEnter(info)} */
    constructor(cbs) {
      this.cbs = cbs || {};
      this.el = {
        layer: $("roomLayer"),
        choice: $("roomChoicePane"), create: $("roomCreatePane"),
        join: $("roomJoinPane"), active: $("roomActivePane"),
        codeOut: $("roomCodeOut"), qrActive: $("roomQrActive"),
        createHint: $("roomCreateHint"),
        codeIn: $("roomCodeIn"), urlIn: $("roomUrlIn"), joinMsg: $("roomJoinMsg"),
        urlPort: $("roomUrlPort"),
        tunnelHost: $("roomTunnelHost"), tunnelPort: $("roomTunnelPort"),
        tunnelUrl: $("roomTunnelUrl"),
        activeCode: $("roomActiveCode"), activeBackend: $("roomActiveBackend"),
        activeState: $("roomActiveState"),
        btnCreate: $("btnCreateRoom"), btnJoin: $("btnJoinRoom"), btnLocal: $("btnLocalMode"),
        btnCreateBack: $("btnCreateBack"), btnCreateEnter: $("btnCreateEnter"),
        btnJoinBack: $("btnJoinBack"), btnJoinEnter: $("btnJoinEnter"),
        btnJoinPaste: $("btnJoinPaste"), joinHint: $("roomJoinHint"),
        btnCopyCode: $("btnCopyCode"), btnCopyInvite: $("btnCopyInvite"),
        btnActiveCopy: $("btnActiveCopy"), btnActiveQr: $("btnActiveQr"),
        btnActiveSwitch: $("btnActiveSwitch"),
        btnActiveLeave: $("btnActiveLeave"), btnActiveEnter: $("btnActiveEnter"),
        chip: $("roomChip"), roomLabel: $("roomLabel"), roomDot: $("roomDot"),
        btnRoom: $("btnRoom"),
        toast: $("roomToast"),
        foot: $("roomFoot"),
      };
      const saved = load();
      this.state = {
        mode: saved && saved.mode ? saved.mode : "local",
        code: (saved && saved.code) || "",
        url: (saved && saved.url) || "",
      };
      this.backendRoom = null;         // 后端 hello 给的真实 room（权威）
      this.backendHasRoomOp = false;   // 后端是否真的回了 type:"room"
      this._qrActiveShown = false;     // 「邀请二维码」是否被点开过（默认不显示，见 toggleActiveQr）
      this.online = false;             // 最近一次 WS 发送是否真的发出去
      this.urlRoom = this._fromUrl();      // 扫码带进来的房间码（?room=）
      this._bind();
      this._prefill();
      this._renderChip();
      // 房间码槽位（codeslots.js）；缺了也能降级成纯文本
      if (window.CL.CodeSlots && this.el.codeOut) {
        window.CL.CodeSlots.attach(this.el.codeOut, { length: 6 });
        this._showCode(this.state.code || "", "idle");
      }
    }

    /* ─────────── 对外 ─────────── */
    open() {
      this._renderActive();
      const pane = this.hasRoom() ? "active" : (this.urlRoom ? "join" : "choice");
      this._pane(pane);
      this._show(true);
      if (pane === "join" && this.urlRoom) {
        this._joinMsg("已带上房间码（扫二维码 / 点邀请链接进来的）—— 确认无误就点「加入」", false);
      }
    }
    close() { this._show(false); }

    /** 后端连接状态（app.js 在 onOpen / onClose / onOffline 里调） */
    setOnline(online) {
      this.online = !!online;
      this._hint();
      this._renderActive();
      this._renderChip();
    }

    /** 后端 hello（房间名的权威来源）→ 返回 true 表示与本地选择不一致 */
    applyHello(msg) {
      const room = msg && msg.room;
      if (!room) return false;
      this.backendRoom = String(room);
      this.online = true;
      this._renderChip();
      this._renderActive();
      this._hint();
      return this.backendRoom !== this.localCode();
    }

    /** 后端下行 {type:"room"} —— 后端实现该 op 后走这里 */
    applyMessage(msg) {
      if (!msg || msg.type !== "room") return false;
      this.backendHasRoomOp = true;
      if (msg.code) this.backendRoom = String(msg.code);
      if (msg.ok === false) {
        this._joinMsg(msg.error || "后端拒绝了这次房间操作");
        this._showCode(this.state.code, "error");
      } else {
        // 后端确认接管 → 房间码槽位亮成"已生效"
        this._showCode(this.state.code, "success");
      }
      this._renderChip();
      this._renderActive();
      this._hint();
      return true;
    }

    hasRoom() { return this.state.mode !== "local" && !!this.state.code; }
    localCode() { return this.hasRoom() ? this.state.code : "local"; }
    current() { return { mode: this.state.mode, code: this.state.code, url: this.state.url }; }

    /* ─────────── 内部 ─────────── */
    _bind() {
      const e = this.el;
      const on = (elm, fn) => { if (elm) elm.addEventListener("click", fn); };
      on(e.chip, () => this.open());
      on(e.btnRoom, () => this.open());
      on(e.btnCreate, () => this._create());
      on(e.btnJoin, () => this._pane("join"));
      on(e.btnLocal, () => this._goLocal());
      on(e.btnCreateBack, () => this._pane("choice"));
      on(e.btnJoinBack, () => this._pane("choice"));
      on(e.btnCreateEnter, () => this._enterRoom(this.state));
      on(e.btnJoinEnter, () => this._join());
      on(e.btnJoinPaste, () => this._pasteInvite());
      on(e.btnCopyCode, () => this._copy(this.state.code || "", "房间码已复制"));
      on(e.btnCopyInvite, () => this._copyInvite("邀请信息已复制"));
      on(e.btnActiveCopy, () => this._copyInvite("邀请信息已复制"));
      on(e.btnActiveQr, () => this.toggleActiveQr());
      on(e.btnActiveSwitch, () => this._pane("choice"));
      on(e.btnActiveLeave, () => this._leave());
      on(e.btnActiveEnter, () => this.close());
      if (e.codeIn) {
        e.codeIn.addEventListener("input", () => this._onJoinInput());
        e.codeIn.addEventListener("keydown", (ev) => { if (ev.key === "Enter") this._join(); });
      }
      if (e.urlIn) {
        e.urlIn.addEventListener("input", () => this._onJoinInput());
        e.urlIn.addEventListener("keydown", (ev) => { if (ev.key === "Enter") this._join(); });
      }
      this._wireTunnel();      // 樱花：域名 + 端口 ↔ wss://地址
    }

    /** 给 app.js 用：后端配对信息里的对外地址（樱花 public_url）→ 预填域名/端口 */
    prefillTunnel(url) { this._prefillTunnel(url); }

    /**
     * 加入面板里任何一个框发生变化 —— 顺手把**整段邀请信息**拆成"码 + 地址"。
     *
     * 为什么挂在 input 上而不是只挂「粘贴」按钮：用户多半直接 Ctrl+V 到框里，
     * 而且 `file://` 打开时 `navigator.clipboard` 根本不可用（不是安全上下文），
     * 按钮读不到剪贴板 —— 手动粘贴这条路必须一直通。
     */
    _onJoinInput() {
      const e = this.el;
      const raw = String((e.codeIn && e.codeIn.value) || "");
      const inUrl = String((e.urlIn && e.urlIn.value) || "");
      // 用户正一个字符一个字符敲房间码 → 别插嘴
      if (!looksLikeInvite(raw) && !looksLikeInvite(inUrl)) { this._joinMsg(""); return; }
      const parsed = parseInvite(raw + "\n" + inUrl);
      if (parsed.code && e.codeIn && e.codeIn.value.trim().toUpperCase() !== parsed.code) {
        e.codeIn.value = parsed.code;
      }
      if (parsed.url && e.urlIn && !e.urlIn.value.trim()) e.urlIn.value = parsed.url;
      // 整段粘进了「房间码」框、里面只有地址没有码 → 别把地址留在码框里，清掉
      if (!parsed.code && /^wss?:\/\//i.test(raw.trim())) e.codeIn.value = "";
      const extra = parsed.url ? `，对端地址 ${parsed.url}` : "";
      this._joinMsg(
        parsed.code ? `已认出来：房间码 ${parsed.code}${extra}`
          : (parsed.url ? "认出了对端地址，房间码还得填一下" : ""),
        false,
      );
    }

    /** 「📋 粘贴邀请」按钮：读剪贴板 → 拆出码和地址 */
    async _pasteInvite() {
      const text = await readClipboard();
      if (!text.trim()) {
        this._joinMsg("读不到剪贴板（浏览器权限，或 file:// 打开时不支持）—— 直接 Ctrl+V 粘到下面的框里一样管用", true);
        if (this.el.codeIn) this.el.codeIn.focus();
        return;
      }
      this._applyInvite(text);
    }

    /** 把一段邀请信息填进加入表单 */
    _applyInvite(text) {
      const e = this.el;
      const parsed = parseInvite(text);
      if (!parsed.code && !parsed.url) {
        this._joinMsg("这段文字里没找到房间码 —— 手填一个吧", true);
        return;
      }
      if (parsed.code && e.codeIn) e.codeIn.value = parsed.code;
      if (parsed.url && e.urlIn) e.urlIn.value = parsed.url;
      const extra = parsed.url ? `，对端地址 ${parsed.url}` : "";
      this._joinMsg(
        parsed.code ? `已从剪贴板识别：房间码 ${parsed.code}${extra} —— 确认无误就点「加入」`
          : "只认出了对端地址，房间码还得填一下",
        false,
      );
      if (parsed.code && !parsed.url && e.urlIn) e.urlIn.focus();
      else if (e.btnJoinEnter) e.btnJoinEnter.focus();
    }

    /**
     * 樱花穿透的两个输入框（域名 + 端口）↔ `state.url`（`wss://域名:端口`）。
     *
     * 用户原话："房间的创建现在默认用樱花穿透，在创建页给出端口，还要填入樱花给出的端口，
     * 走 https 协议，证书没有验证无所谓，加入页同理" —— 所以：
     *   · 域名/端口**分开填**（樱花面板上就是分两栏给的）；
     *   · 拼出来的地址**一律 wss://**（走 https/wss，证书有效性由樱花那侧决定，我们不管）；
     *   · 改一下就把 `state.url` 更新掉，于是「复制邀请信息」里带的就是最新地址。
     */
    _wireTunnel() {
      const e = this.el;
      const sync = (fromJoin) => {
        let host = "";
        let port = "";
        if (fromJoin) {
          const raw = String((e.urlIn && e.urlIn.value) || "").trim();
          const m = raw.match(/^(wss?:\/\/)?([^:/]+)(?::(\d+))?/i);
          if (m) { host = m[2] || ""; port = m[3] || ""; }
          if (port && e.urlPort) e.urlPort.value = port;
        } else {
          host = String((e.tunnelHost && e.tunnelHost.value) || "").trim();
          port = String((e.tunnelPort && e.tunnelPort.value) || "").trim();
        }
        const url = host && port ? `wss://${host}:${port}` : "";
        if (!fromJoin) {
          if (e.tunnelUrl) e.tunnelUrl.textContent = url || "wss://…（填了域名 + 端口就有）";
        } else if (e.urlIn && url && e.urlIn.value.trim() !== url) {
          e.urlIn.value = url;
        } else if (e.urlPort && !port) {
          // 端口栏改了 → 回填对端地址
          const base = String((e.urlIn && e.urlIn.value) || "").trim().replace(/:\d+$/, "");
          if (base) e.urlIn.value = `${base}:${e.urlPort.value}`;
        }
        if (url) { this.state.url = url; save(this.state); }
        return url;
      };
      ["input", "change"].forEach((ev) => {
        if (e.tunnelHost) e.tunnelHost.addEventListener(ev, () => sync(false));
        if (e.tunnelPort) e.tunnelPort.addEventListener(ev, () => sync(false));
        if (e.urlIn) e.urlIn.addEventListener(ev, () => sync(true));
        if (e.urlPort) e.urlPort.addEventListener(ev, () => sync(true));
      });
      if (e.urlIn && !this.state.url) {
        const m = String(e.urlIn.value || "").match(/^wss?:\/\/([^:/]+)(?::(\d+))?/i);
        if (m && e.tunnelHost && !e.tunnelHost.value) {
          e.tunnelHost.value = m[1] || "";
          if (e.tunnelPort && m[2]) e.tunnelPort.value = m[2];
        }
      }
    }

    /**
     * 后端配对信息里带了对外地址（樱花 `[coyote_v4] public_url`）→ 顺手预填，
     * 省得用户手抄。
     */
    _prefillTunnel(url) {
      const e = this.el;
      const m = String(url || "").match(/^wss?:\/\/([^:/]+)(?::(\d+))?/i);
      if (!m) return;
      if (e.tunnelHost && !e.tunnelHost.value) e.tunnelHost.value = m[1] || "";
      if (e.tunnelPort && !e.tunnelPort.value && m[2]) e.tunnelPort.value = m[2];
      if (e.tunnelUrl) {
        e.tunnelUrl.textContent = `wss://${m[1]}${m[2] ? ":" + m[2] : ""}（来自后端配对信息）`;
      }
    }

    _show(show) {
      if (this.el.layer) this.el.layer.hidden = !show;
      if (show && this.el.codeIn && window.matchMedia("(pointer: fine)").matches) this.el.codeIn.focus();
    }

    _pane(which) {
      const e = this.el;
      const map = { choice: e.choice, create: e.create, join: e.join, active: e.active };
      for (const key of Object.keys(map)) {
        if (map[key]) map[key].hidden = key !== which;
      }
      if (which === "create") this._hint();
      if (which === "active") this._renderActive();
    }

    _prefill() {
      const e = this.el;
      const saved = this.state.code && this.state.code !== "local" ? this.state.code : "";
      const code = this.urlRoom || saved;          // 扫码带进来的优先
      if (e.codeIn && code) e.codeIn.value = code;
      if (e.urlIn) e.urlIn.value = this.state.url || "";
      // 樱花那两栏也从记住的地址里回填（用户原话：创建页要给出端口、能填樱花给的端口）
      const m = String(this.state.url || "").match(/^wss?:\/\/([^:/]+)(?::(\d+))?/i);
      if (m) {
        if (e.tunnelHost && !e.tunnelHost.value) e.tunnelHost.value = m[1] || "";
        if (e.tunnelPort && !e.tunnelPort.value && m[2]) e.tunnelPort.value = m[2];
        if (e.urlPort && !e.urlPort.value && m[2]) e.urlPort.value = m[2];
        if (e.tunnelUrl) e.tunnelUrl.textContent = `wss://${m[1]}${m[2] ? ":" + m[2] : ""}`;
      }
    }

    /** 地址栏 ?room=XXX（扫邀请二维码进来的那种） */
    _fromUrl() {
      try {
        const raw = (new URL(location.href).searchParams.get("room") || "").trim().toUpperCase();
        return RE_CODE.test(raw) ? raw : "";
      } catch (e) { return ""; }
    }

    /** 邀请链接：本页地址 + ?room=码 —— 对方扫一下就直达并带上房间码 */
    inviteLink() {
      if (!this.state.code || this.state.code === "local") return "";
      if (!/^https?:$/.test(location.protocol)) return "";   // file:// 扫码没意义
      try {
        const u = new URL(location.href);
        u.hash = "";
        u.search = "?room=" + encodeURIComponent(this.state.code);
        return u.toString();
      } catch (e) { return ""; }
    }

    /** 渲染邀请二维码（qrcode.js 手写实现，无依赖）；失败就静默降级到只有房间码 */
    _renderQrInto(box) {
      if (!box) return;
      const link = this.inviteLink();
      if (!link || !window.CL.QR) { this._clearQr(box); return; }
      try {
        box.innerHTML = "";
        box.appendChild(window.CL.QR.svg(link, { ecl: "M", border: 3, label: "房间邀请：" + link }));
        box.hidden = false;
      } catch (err) {
        this._clearQr(box);
      }
    }

    _clearQr(box) {
      if (!box) return;
      box.hidden = true;
      box.innerHTML = "";
    }

    /**
     * 「📱 邀请二维码」按钮：显示/隐藏**已经在房间里**时出示的那个邀请码。
     *
     * ★ 为什么不在"创建房间"面板里直接摊开二维码（用户明确要求去掉）：
     *   面板多半是 `http://127.0.0.1:8780` 打开的，那个码里装的是**本页地址**，
     *   手机扫了只会去连它自己 —— **扫了没用**，还占着版面让人以为"扫了就有人来"。
     *   真有跨机需求时，用「📋 复制邀请信息」把房间码 + 对端地址直接发过去更靠谱。
     */
    toggleActiveQr() {
      this._qrActiveShown = !this._qrActiveShown;
      if (this.el.btnActiveQr) {
        this.el.btnActiveQr.textContent = this._qrActiveShown ? "📱 收起二维码" : "📱 邀请二维码";
      }
      if (!this._qrActiveShown) { this._clearQr(this.el.qrActive); return; }
      if (!this.inviteLink()) {
        this._flash("本页地址是 127.0.0.1 / 不是 http(s) → 二维码扫了没用，请用「复制邀请」");
        this._qrActiveShown = false;
        if (this.el.btnActiveQr) this.el.btnActiveQr.textContent = "📱 邀请二维码";
        return;
      }
      this._renderQrInto(this.el.qrActive);
    }

    /** 创建：本地生成码 → 发给后端 */
    _create() {
      const code = newCode();
      this.state = { mode: "create", code, url: this.state.url || "" };
      save(this.state);
      this._showCode(code, "idle");
      this._send({ op: "room", action: "create", code });
      this._pane("create");
    }

    /** 加入：校验房间码 / 地址 → 发给后端 */
    _join() {
      const e = this.el;
      const rawCode = String((e.codeIn && e.codeIn.value) || "").trim();
      const rawUrl = String((e.urlIn && e.urlIn.value) || "").trim();
      // 兜底：整段邀请信息被粘进来但没触发过 input（比如脚本设值）也照样认
      const parsed = looksLikeInvite(rawCode) ? parseInvite(rawCode + "\n" + rawUrl) : null;
      const code = String((parsed && parsed.code) || rawCode).trim().toUpperCase();
      const url = String((parsed && parsed.url) || rawUrl).trim();
      if (!code) { this._joinMsg("请填房间码（或点「📋 粘贴邀请」）"); return; }
      if (RE_DIGITS.test(code)) {
        this._joinMsg("房间码不能是纯数字 —— 这看着像端口号；樱花端口请填在下面的「穿透地址 / 端口」框里");
        return;
      }
      if (!RE_CODE.test(code)) { this._joinMsg("房间码只能是大写字母 / 数字，6~16 位（例如 AB12CD）"); return; }
      if (url && !RE_URL.test(url)) { this._joinMsg("对端地址要写成 ws://… 或 wss://…（同机演示可留空）"); return; }
      if (e.codeIn) e.codeIn.value = code;
      if (e.urlIn) e.urlIn.value = url;
      this.state = { mode: "join", code, url };
      save(this.state);
      this._send({ op: "room", action: "join", code, url });
      // ★ 别报"加入成功"：这只是"本机切到了这个码"。对方是不是也在用同一个码，
      //   由**能不能收到对方的消息**决定（房间码没有服务器校验，谁都填得进去）。
      this._flash(`本机已切到房间 ${code}（共享口令：对方也得填 ${code} 才连得上）`);
      this._enterRoom(this.state);
    }

    /** 离开房间：回到本地模式 */
    _leave() {
      this.state = { mode: "local", code: "", url: "" };
      save(this.state);
      this._send({ op: "room", action: "leave" });
      if (this.el.codeIn) this.el.codeIn.value = "";
      this._showCode("", "idle");
      this._qrActiveShown = false;
      if (this.el.btnActiveQr) this.el.btnActiveQr.textContent = "📱 邀请二维码";
      this._clearQr(this.el.qrActive);
      this._renderChip();
      this._pane("choice");
    }

    /**
     * 「跳过 · 本地模式」：房间 = `local`，**同样要告诉后端**。
     *
     * 不给后端发的话，各节点会留在上一个房间里，而面板显示"本地"—— 两边不一致。
     * （`leave` 就是"回默认房间"，语义与这里的"本地模式"是同一件事。）
     */
    _goLocal() {
      this._send({ op: "room", action: "leave" });
      this._enterRoom({ mode: "local", code: "local", url: "" });
    }

    /** 房间码写入槽位（没有 codeslots.js 时退化成纯文本） */
    _showCode(code, status) {
      const el = this.el.codeOut;
      if (!el) return;
      if (window.CL.CodeSlots) window.CL.CodeSlots.set(el, code || "", status || "idle");
      else el.textContent = code || "------";
    }

    _enterRoom(state) {
      this.state = Object.assign({}, this.state, state);
      save(this.state);
      this._renderChip();
      this.close();
      if (this.cbs.onEnter) this.cbs.onEnter(this.current());
    }

    _send(obj) {
      const ok = this.cbs.send ? !!this.cbs.send(obj) : false;
      this.online = ok;
      return ok;
    }

    /** 邀请文案：房间码 + 对端地址 + 一句话用法 */
    inviteText() {
      const lines = ["webDG 房间邀请", `${LABEL_CODE}：${this.state.code}`];
      if (this.state.url) lines.push(`${LABEL_URL}：${this.state.url}`);
      else lines.push(`${LABEL_URL}：（同机演示不用填；异地组网时填穿透地址）`);
      const link = this.inviteLink();
      if (link) lines.push(`${LABEL_LINK}：${link}`);
      lines.push(`用法：打开 webDG 面板 → 「加入房间」→ 点「📋 粘贴邀请」（或把这段整个粘进去），房间码会自动填好。`);
      return lines.join("\n");
    }

    _copyInvite(okMsg) { this._copy(this.inviteText(), okMsg); }

    _copy(text, okMsg) {
      if (!text) return;
      const done = () => {
        if (okMsg) this._flash(okMsg);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, () => this._flash("复制失败，请手动选中"));
      } else {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand("copy"); done(); } catch (e) { /* 忽略 */ }
        ta.remove();
      }
    }

    _flash(text) {
      const t = this.el.toast;
      if (!t) return;
      t.textContent = text;
      t.hidden = false;
      clearTimeout(this._toastTimer);
      this._toastTimer = setTimeout(() => { t.hidden = true; }, 1800);
    }

    _joinMsg(text, isErr) {
      if (!this.el.joinMsg) return;
      this.el.joinMsg.textContent = text || "";
      this.el.joinMsg.className = "msg" + (isErr === false ? " ok" : " err");
    }

    /** 房间状态的一句话（诚实说明后端是否真接管了） */
    _stateText() {
      if (!this.online) return "后端未连接 —— 房间码只存在本机";
      if (this.backendHasRoomOp) {
        // ★ 不能写成"加入成功"：房间码是**共享口令**，后端不校验它存不存在 ——
        //   谁都能填任意码，只有填了同一个码的两台机器才在同一条链路上。
        return "本机各节点已切到这条 key 前缀 ✓（房间码＝共享口令：跨机链路与 AEAD 密钥都由它派生；"
             + "后端不校验这个码存不存在，对方填同一个码才在同一条链路上）";
      }
      if (this.backendRoom) return `后端未接入 room op，实际仍为 room=${this.backendRoom}`;
      return "已发给后端，等它的房间下行";
    }

    _renderActive() {
      const e = this.el;
      if (e.activeCode) e.activeCode.textContent = this.state.code || "—";
      if (e.activeBackend) e.activeBackend.textContent = this.backendRoom || "—";
      if (e.activeState) {
        e.activeState.textContent = this._stateText();
        e.activeState.className = this.backendHasRoomOp ? "ok" : "dim";
      }
      // 邀请二维码是**点按钮才出**的（见 toggleActiveQr）—— 不再一打开房间层就糊上来
      if (this._qrActiveShown) this._renderQrInto(e.qrActive);
    }
    /** 创建完成后的提示 */
    _hint() {
      const box = this.el.createHint;
      if (!box) return;
      const lines = [];
      if (this.online) lines.push("已把房间码发给后端 —— 本机各节点都会切到这条 key 前缀（op: room/create）。");
      else if (this.backendRoom) lines.push(`后端未接入房间功能，当前仍按 room = ${this.backendRoom} 运行。`);
      else lines.push("后端未连接：房间码只保存在本机。");
      lines.push(
        "房间码＝共享口令：跨机链路的 key 前缀和 AEAD 密钥都由它派生，没有服务器校验这个码存不存在 ——" +
        "谁都能填任意码，但只有两台机器填了同一个码才会落在同一条链路上（码不对就互相看不见）。",
      );
      lines.push("这个选择会记在本机（data/room.json），下次起节点直接用它 —— 不用再选一遍。");
      lines.push(
        "跨网（不在同一个 WiFi）就走樱花穿透：樱花 → TCP 隧道 → 本机 7451 端口（链路走 TLS，"
        + "自签证书即可，加入方关掉证书校验）。启动命令：",
      );
      lines.push("创建方： start.cmd -Frp \"樱花域名:端口\"  （可选 -FrpRelay \"樱花域名:端口\" 让手机 App 也走樱花）");
      lines.push("加入方： start.cmd -Join \"对方的樱花域名:端口\" -Role b");
      lines.push("上面两栏填樱花给的那两个值 —— 填完这里显示的就是要发给对方的 wss 地址。");
      lines.push(
        "把房间码给对方：点「📋 复制邀请信息」把整段发过去（里面有房间码 + 对端地址 + 一键直达链接），" +
        "对面在「加入房间」里点「📋 粘贴邀请」就自动填好。",
      );
      lines.push(
        "⚠️ 这里说的房间是**跨机链路**用的；连手机 DG-LAB App 到郊狼 socket 用的是" +
        "「🔌 连接与配对」卡片里那个【配对二维码】—— 两个码不是一回事。",
      );
      if (!this.inviteLink()) {
        lines.push("本页不是 http(s)（file:// 打开）→ 没有「一键直达」链接，走「复制邀请信息」。");
      } else if (!this._linkIsReachable()) {
        lines.push(
          "⚠️ 本页地址是 127.0.0.1 / localhost：邀请链接里的地址**别的设备连不上**" +
          "（那是它自己的本机）。跨机请用局域网 IP 打开面板，或直接走「复制邀请信息」并手填对端地址。",
        );
      }
      box.textContent = lines.join(" ");
    }

    /** 本页地址对**本机之外**的设备有没有意义（loopback 扫了也连不上） */
    _linkIsReachable() {
      try {
        const h = new URL(location.href).hostname.toLowerCase();
        return !["127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"].includes(h);
      } catch (e) { return false; }
    }

    /** 顶栏：后端有回话就以后端为准，否则显示本地选择 */
    _renderChip() {
      const e = this.el;
      const local = this.localCode();
      const text = this.backendRoom || local;
      if (e.roomLabel) e.roomLabel.textContent = text;
      if (e.roomDot) e.roomDot.className = "dot" + (this.backendRoom || this.hasRoom() ? " on" : "");
      if (e.foot) {
        e.foot.textContent = this.backendRoom && this.backendRoom !== local
          ? `后端当前房间：${this.backendRoom}（本地选择：${local}）`
          : "未选择房间时，面板以本地模式运行（数据只在本机）。";
      }
    }
  }

  window.CL.RoomLayer = RoomLayer;
  window.CL.RoomLayer.newCode = newCode;
  window.CL.RoomLayer.RE_CODE = RE_CODE;
  window.CL.RoomLayer.parseInvite = parseInvite;
  window.CL.RoomLayer.looksLikeInvite = looksLikeInvite;
})();
