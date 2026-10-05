/* app.js —— 装配：路由消息、连接状态、mock 回退、各卡片更新 */
(function () {
  "use strict";
  const CL = window.CL;
  const $ = (id) => document.getElementById(id);

  const EMOTION_EMOJI = {
    neutral: "🙂", happy: "😄", sad: "😢", angry: "😠", surprise: "😮",
    surprised: "😮", fear: "😨", disgust: "🤢", pain: "😖", contempt: "😏",
  };
  const MOCKWAVE_STORE = "coyotelink.mockwaves";

  document.addEventListener("DOMContentLoaded", main);

  function main() {
    // ── 元素 ──
    const el = {
      connDot: $("connDot"), connText: $("connText"), roomLabel: $("roomLabel"),
      peerLabel: $("peerLabel"), fpsChip: $("fpsChip"), latChip: $("latChip"),
      sourceChip: $("sourceChip"), btnConnect: $("btnConnect"),
      stage: $("stage"), poseState: $("poseState"), visCount: $("visCount"),
      seqNum: $("seqNum"), dropNum: $("dropNum"), poseLat: $("poseLat"),
      emoEmoji: $("emoEmoji"), emoText: $("emoText"), emoBar: $("emoBar"), emoConf: $("emoConf"),
      audioEvent: $("audioEvent"), audioBar: $("audioBar"), audioText: $("audioText"),
      heartVal: $("heartVal"), heartCanvas: $("heartCanvas"),
      connEmoji: $("connEmoji"), connStateText: $("connStateText"), connDetail: $("connDetail"),
      peerWho: $("peerWho"),
      waveSel: $("waveSel"), waveCount: $("waveCount"), channelSel: $("channelSel"),
      prioritySel: $("prioritySel"), durRange: $("durRange"), durVal: $("durVal"),
      intensityMode: $("intensityMode"), intensityValue: $("intensityValue"), overlayChk: $("overlayChk"),
      btnFire: $("btnFire"), btnStop: $("btnStop"), btnStopFixed: $("btnStopFixed"),
      fireAck: $("fireAck"),
      impName: $("impName"), impVersion: $("impVersion"), impText: $("impText"),
      impFile: $("impFile"), btnImport: $("btnImport"), impMsg: $("impMsg"),
      importedList: $("importedList"), btnDeleteWave: $("btnDeleteWave"),
      cameraSel: $("cameraSel"), micSel: $("micSel"), bleSel: $("bleSel"), devHint: $("devHint"),
      ruleName: $("ruleName"), ruleCond: $("ruleCond"), ruleParam: $("ruleParam"),
      ruleWave: $("ruleWave"), ruleChannel: $("ruleChannel"), ruleDur: $("ruleDur"),
      ruleCd: $("ruleCd"), btnAddRule: $("btnAddRule"), ruleList: $("ruleList"),
      ruleTarget: $("ruleTarget"), ruleShape: $("ruleShape"), btnDrawRule: $("btnDrawRule"),
      ruleDebounce: $("ruleDebounce"), ruleMinVis: $("ruleMinVis"),
      serialSel: $("serialSel"),
      dotRelay: $("dotRelay"), relayState: $("relayState"), relayUrl: $("relayUrl"),
      dotEsp32: $("dotEsp32"), esp32State: $("esp32State"), esp32Port: $("esp32Port"),
      dotPhone: $("dotPhone"), phoneState: $("phoneState"), linkHint: $("linkHint"),
      relayTid: $("relayTid"), btnPairing: $("btnPairing"), btnCopyPairing: $("btnCopyPairing"),
      pairingQr: $("pairingQr"), pairingHint: $("pairingHint"),
      relayHint: $("relayHint"), serialHint: $("serialHint"),
      cameraHint: $("cameraHint"), heartHint: $("heartHint"),
      // 仪表盘（📟）：左链路状态 / 右本机 socket 郊狼
      dashRelay: $("dashRelay"), dashTid: $("dashTid"), dashApp: $("dashApp"),
      dashRoom: $("dashRoom"), dashDev: $("dashDev"), dashA: $("dashA"),
      dashB: $("dashB"), dashFire: $("dashFire"), dashHint: $("dashHint"),
      // 视角切换（控制端 / 被控端）+ 开火目标
      uiTabs: $("uiTabs"), viewControl: $("viewControl"), viewControlled: $("viewControlled"),
      fireTarget: $("fireTarget"), fireTargetHint: $("fireTargetHint"),
      ctlMyHost: $("ctlMyHost"), ctlRoom: $("ctlRoom"), ctlPeers: $("ctlPeers"),
    peerGate: $("peerGate"),
      ctlDevice: $("ctlDevice"), ctlA: $("ctlA"), ctlB: $("ctlB"), ctlFireList: $("ctlFireList"),
      btnConnectSerial: $("btnConnectSerial"),
      dotCamera: $("dotCamera"), cameraDevice: $("cameraDevice"), cameraFps: $("cameraFps"),
      btnConnectCamera: $("btnConnectCamera"),
      dotHeart: $("dotHeart"), heartDevice: $("heartDevice"), heartBpm: $("heartBpm"),
      heartSel: $("heartSel"), btnConnectHeart: $("btnConnectHeart"), btnScanHeart: $("btnScanHeart"),
      logList: $("logList"), logHint: $("logHint"),
      btnExportLog: $("btnExportLog"), btnFireBar: $("btnFireBar"), btnStopBar: $("btnStopBar"),
      dbgList: $("dbgList"), dbgCount: $("dbgCount"),
      ruleSync: $("ruleSync")
    };

    const online = { value: false, mock: false };
    //: node → 最近一次心跳时间。（旧实现只增不减，节点停了还挂着）
    const statusNodes = new Map();
    const NODE_TTL_MS = 10000;
    //: host → 心跳次数（判断哪台是"本机"）+ 本机主机名
    const hostHits = new Map();
    let myHost = "";
    //: 被控端视图里的"最近收到的开火"（最新的在最上面）
    const ctlFires = [];

    // ── 模块 ──
    const skeleton = new CL.Skeleton(el.stage, {
      fps: el.fpsChip, lat: el.poseLat, state: el.poseState,
      vis: el.visCount, seq: el.seqNum, drop: el.dropNum,
    });
    const log = new CL.AuditLog(el.logList, el.logHint);
    const devices = new CL.Devices(
      { camera: el.cameraSel, microphone: el.micSel, ble: el.bleSel, serial: el.serialSel }, el.devHint,
      (kind, id) => send({ op: "select_device", kind, id }, () => log.add({ dir: "out", type: "device", kind: "选设备", summary: `${kind} → ${id}` }))
    );
    //: 「连接与配对」卡片（四条链路的状态灯 + 按钮）—— 状态来自 event/link，配对来自 op:pairing
    const link = new CL.LinkStatus(el);

    // ── 「连接与配对」卡片上的按钮 ──
    // ① 郊狼：显示/隐藏**App 扫的配对二维码**；复制配对链接
    if (el.btnPairing) {
      el.btnPairing.addEventListener("click", () => {
        send({ op: "pairing" });        // 点开之前先拉一次最新（tid 可能刚变）
        link.togglePairing();
      });
    }
    if (el.btnCopyPairing) {
      el.btnCopyPairing.addEventListener("click", () => {
        const text = link.pairingLinkText();
        if (!text) {
          log.add({ dir: "in", type: "device", kind: "配对", summary: "还没有配对信息（Relay 灯变绿后才有）" });
          send({ op: "pairing" });
          return;
        }
        copyText(text);
        log.add({ dir: "out", type: "device", kind: "配对", summary: "配对链接已复制（发到手机上点开也能连）" });
      });
    }
    // ② ESP32 假主机：「连接」= 用下拉里选中的那个串口（重）连一次
    if (el.btnConnectSerial) {
      el.btnConnectSerial.addEventListener("click", () => {
        const port = el.serialSel ? el.serialSel.value : "";
        if (!port) {
          log.add({ dir: "in", type: "device", kind: "串口", summary: "先在右边下拉里选一个串口（COMx）再点连接" });
          return;
        }
        devices.setActive("serial", port);      // 让下拉显示成"当前选择"
        send({ op: "select_device", kind: "serial", id: port },
             () => log.add({ dir: "out", type: "device", kind: "串口", summary: `连接 ${port}` }));
      });
    }
    // ③ 摄像头：「连接」= 用下拉里选中的那台（和"选了没反应"对着干：发完看实测帧率）
    if (el.btnConnectCamera) {
      el.btnConnectCamera.addEventListener("click", () => {
        const id = el.cameraSel ? el.cameraSel.value : "";
        if (!id) {
          log.add({ dir: "in", type: "device", kind: "摄像头", summary: "先在下拉里选一台摄像头再点连接" });
          return;
        }
        devices.setActive("camera", id);
        send({ op: "select_device", kind: "camera", id },
             () => log.add({ dir: "out", type: "device", kind: "摄像头", summary: `连接 ${id}（看右边的实测 FPS）` }));
      });
    }
    // ④ 心率带：「连接」用扫到的那台；「重新扫描」让 heart 节点重扫一遍
    if (el.btnConnectHeart) {
      el.btnConnectHeart.addEventListener("click", () => {
        const id = el.heartSel ? el.heartSel.value : "";
        send({ op: "select_device", kind: "heart", id },
             () => log.add({
               dir: "out", type: "device", kind: "心率带",
               summary: id ? `连接 ${id}` : "回到自动扫描（挑信号最强的那台）",
             }));
      });
    }
    if (el.btnScanHeart) {
      el.btnScanHeart.addEventListener("click", () => {
        send({ op: "select_device", kind: "heart", action: "list" },
             () => log.add({ dir: "out", type: "device", kind: "心率带", summary: "重新扫描附近的心率带…" }));
      });
    }

    function copyText(text) {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).catch(() => {});
        return;
      }
      const ta = document.createElement("textarea");   // file:// 下没有 clipboard API
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch (e) { /* 忽略 */ }
      ta.remove();
    }
    const controls = new CL.controls.Controls(el, {
      fire: onFire,
      clear: onClear,
      importWave: onImport,
      deleteWave: onDelete,
      renameWave: onRename,
      setWaves: (list) => rules.setWaves(list),
    });
    const rules = new CL.Rules(el, { onChange: (list) => syncRules(list, true) });

    // ── 规则可视化编辑：在骨骼画布上「画线 / 画框 / 放圆」──
    // 画完**不直接建规则**，而是把生成的 param 填进输入框，让人再确认一次（防误操作）。
    const stageEdit = new CL.StageEdit(el.stage, {
      onCommit: (param) => rules.applyDrawn(param),
      onCancel: () => { /* 右键取消，不改任何东西 */ },
    });
    skeleton.editor = stageEdit;        // 渲染循环每帧把线/框/圆画上去
    rules.bindEditor(stageEdit);
    rules.setTargets(CL.LANDMARKS || []);

    // ── 传输 ──
    const transport = new CL.Transport({
      onOpen: (url) => { online.value = true; online.mock = false; CL.Mock.stop(); setConn(true, false, url); room.setOnline(true); requestAll(); },
      onClose: () => { online.value = false; setConn(false, online.mock, "已断开，重试中…"); room.setOnline(false); },
      onMessage: handleMessage,
      // ★ 后端连不上时**不再自动塞假数据**（用户明确要求：没连上就是没连上，不许占位/编造）。
      //   以前这里会 startMock() → 界面被 mock 的假设备/假波形/假状态填满，
      //   看起来"有数据"，其实全是本地编的 —— 这是错误的默认行为。
      //   现在：保持空白 + 明确显示"未连接"，只自动重试真实后端；
      //   需要离线演示时显式在地址后加 `?mock=1`。
      onOffline: (reason) => {
        online.value = false;
        online.mock = false;
        setConn(false, false, "未连接（" + (reason || "连不上 /ws") + "）");
        log.add({ dir: "local", type: "info", kind: "系统",
          summary: "后端未连接：" + (reason || "连不上 /ws") + " —— 界面保持空白，不会用假数据填充（要离线演示请用 ?mock=1）" });
      },
    });

    // ── 房间入口层：打开即控制台，先选「创建 / 加入房间」（也可跳过走本地模式）──
    const room = new CL.RoomLayer({
      send: (obj) => transport.send(obj),
      onEnter: (info) => log.add({
        dir: "local", type: "info", kind: "房间",
        summary: info.mode === "local"
          ? "本地模式（room = local）"
          : `${info.mode === "create" ? "创建" : "加入"}房间 ${info.code}${info.url ? " @ " + info.url : ""}`,
      }),
    });

    transport.connect();
    room.open();

    el.btnConnect.addEventListener("click", () => { if (online.mock) { restartMock(); } else transport.connect(); });
    if (el.btnExportLog) el.btnExportLog.addEventListener("click", () => log.export());

    // ── 开火 / 急停 ──
    function onFire(body) {
      const payload = Object.assign({ op: "fire" }, body);
      const sent = transport.send(payload);        // ★ 先发出去，UI 反馈一点不拖它
      log.add({
        dir: "out", type: "fire", kind: "开火",
        summary: fireSummary(payload),
      });
      if (sent) {
        firePending = performance.now();           // 收到 ack 时算"后端受理耗时"
        sayFireAck("已发送，等后端回执…", true);
      } else {
        sayFireAck("离线演示：没有后端可发", false);
        // 无后端：本地回执一条 event/fire，保证日志与画面有反馈
        setTimeout(() => handleMessage(CL.Mock.makeFire(payload)), 60);
      }
    }
    function onClear() {
      const sent = transport.send({ op: "clear" });
      log.add({ dir: "out", type: "clear", kind: "急停", summary: "清空全部通道（command/clear）" });
      if (!sent) log.add({ dir: "in", type: "fire", kind: "急停", summary: "本地演示：已清空本地排队" });
    }
    el.btnStopFixed.addEventListener("click", onClear);
    // 移动端底部操作条（窄屏才可见）：开火 / 急停随手可点
    if (el.btnStopBar) el.btnStopBar.addEventListener("click", onClear);
    if (el.btnFireBar) el.btnFireBar.addEventListener("click", () => controls.fire());

    // ── 导入 / 删除 / 重命名 ──
    function onImport(payload) {
      const sent = transport.send({ op: "import_waveform", name: payload.name, version: payload.version, frames: payload.frames });
      if (sent) return;
      // mock：本地保存
      const waves = loadMockWaves();
      waves.push({ name: payload.name, source: "imported", version: payload.version, frames: payload.frames.length });
      saveMockWaves(waves);
      log.add({ dir: "local", type: "import", kind: "波形", summary: `导入 ${payload.name}（V${payload.version}，${payload.frames.length} 帧）` });
      sendWaveforms(mockWaveforms());
    }
    function onDelete(name) {
      const sent = transport.send({ op: "delete_waveform", name });
      if (sent) return;
      const waves = loadMockWaves().filter((w) => w.name !== name);
      saveMockWaves(waves);
      log.add({ dir: "local", type: "import", kind: "波形", summary: `删除 ${name}` });
      sendWaveforms(mockWaveforms());
    }
    function onRename(name, newName) {
      const sent = transport.send({ op: "rename_waveform", name, new_name: newName });
      if (sent) return;
      const waves = loadMockWaves().map((w) => (w.name === name ? Object.assign({}, w, { name: newName }) : w));
      saveMockWaves(waves);
      log.add({ dir: "local", type: "import", kind: "波形", summary: `${name} → ${newName}` });
      sendWaveforms(mockWaveforms());
    }

    // ── 消息路由 ──
    function handleMessage(msg) {
      if (!msg || typeof msg !== "object") return;
      switch (msg.type) {
        case "pose": skeleton.setFrame(msg); break;
        case "face": updateEmotion(msg); skeleton.setHead(msg); break;
        case "audio": updateAudio(msg); break;
        case "heart": updateHeart(msg); break;
        case "devices": devices.apply(msg); break;
        case "fire": addFireLive(msg); break;
        case "waveforms":
          controls.setWaveforms(msg.waveforms || []);
          if (msg.imported) controls.showImportOk(`已导入 ${msg.imported.name}（V${msg.imported.version}，${msg.imported.frames} 帧）`);
          if (msg.deleted) controls.showImportOk(`已删除 ${msg.deleted}`);
          if (msg.renamed) controls.showImportOk(`已重命名 ${msg.renamed.from} → ${msg.renamed.to}`);
          break;
        case "import_error": controls.showImportError(msg.error || "导入失败"); break;
        case "hello":
          // 后端一次握手就把**郊狼配对信息**要来（二维码内容随 targetId 变）
          send({ op: "pairing" });
          // ★ 本机主机名由**后端**直接告知（后端就跑在这台机器上）——
          //   别再用"哪个主机名出现 ≥3 次"去猜：两端在同一房间时，对端的 status 也会刷够次数，
          //   猜错就会把「本机 / 对端」整个搞反（控制栏打到对面、被控栏打到自己）。
          if (msg.host) setMyHost(msg.host);
          // 房间名以后端为准（后端没接房间功能时，它给的还是 room=local）
          if (room.applyHello(msg)) {
            log.add({ dir: "in", type: "hello", kind: "房间",
              summary: `后端房间为 ${msg.room}，与本地选择不一致 —— 顶栏以后端为准` });
          }
          break;
        case "room": room.applyMessage(msg); break;
        case "status": noteStatus(msg.node, msg.payload && msg.payload.host); break;
        case "link": link.apply(msg); break;
        case "pairing":
          link.applyPairing(msg);
          // 后端知道对外地址（樱花 `[coyote_v4] public_url`）→ 顺手帮房间层预填，省得手抄
          if (room.prefillTunnel && (msg.phone_url || msg.ws_url)) {
            room.prefillTunnel(msg.phone_url || msg.ws_url);
          }
          break;
        case "pong": notePong(); break;
        case "ack": noteAck(msg); break;
        case "topic": noteTopic(msg); break;
        default: break;
      }
    }

    function requestAll() {
      transport.send({ op: "list_devices" });
      transport.send({ op: "waveforms" });
      syncRules(rules.snapshot(), false);   // 连上就同步一次：规则的真源在前端
    }

    /* ── 规则：整体下发给后端（command/rules）──
       语义是**整体替换**，所以任何增删启停都推整份，不做增量。
       ⚠️ web 节点目前**还没有** set_rules 分支（只有 topics 里的 COMMAND_RULES），
          所以这里只说"已发送"，等后端回 ack 才敢说"已受理"。 */
    function syncRules(list, announce) {
      const sent = transport.send({ op: "set_rules", rules: list });
      if (el.ruleSync) {
        el.ruleSync.textContent = sent
          ? `已发送 ${list.length} 条，等后端受理`
          : `离线：${list.length} 条只存在本机`;
      }
      if (announce) {
        log.add({
          dir: sent ? "out" : "local", type: "rules", kind: "规则",
          summary: `${sent ? "下发" : "本机保存"} ${list.length} 条规则（整体替换）`,
        });
      }
    }

    /* ── 链路往返（真 RTT）：每 3 秒 ping 一次，用 pong 回来算 ── */
    let pingPending = null;
    function pingLoop() {
      if (online.value && pingPending == null) {
        pingPending = performance.now();
        transport.send({ op: "ping" });
      }
      setTimeout(pingLoop, 3000);
    }
    function notePong() {
      if (pingPending == null) return;
      const ms = Math.max(0, Math.round(performance.now() - pingPending));
      pingPending = null;
      el.latChip.textContent = `RTT ${ms} ms`;
      el.latChip.dataset.q = ms < 60 ? "good" : ms < 200 ? "ok" : "bad";
    }

    /* ── 后端受理回执（ack）：让"开火到底发出去没有"有确定答案 ── */
    let firePending = null;
    let ackTimer = 0;
    function noteAck(msg) {
      if (msg.op === "fire") {
        const ms = firePending == null ? null : Math.round(performance.now() - firePending);
        firePending = null;
        // ★ 这句只能代表"**面板后端**收到了"，不代表郊狼真的动了 ——
        //   真正成/败由郊狼节点回的那条 event/fire（ok / error）说了算（见 addFireLive）。
        sayFireAck(
          msg.ok === false ? "面板后端拒绝了这条指令"
            : (ms == null ? "面板后端已收到，等郊狼回执" : `面板后端已收到（${ms} ms），等郊狼回执`),
          msg.ok !== false
        );
      } else if (msg.op === "clear") {
        log.add({ dir: "in", type: "ack", kind: "急停", summary: "后端已受理急停" });
      } else if (msg.op === "set_rules") {
        // 后端补上 set_rules 分支后，这条就会亮
        if (el.ruleSync) {
          el.ruleSync.textContent = `后端已受理 ${msg.count != null ? msg.count + " 条规则" : "规则"}`;
        }
      }
    }
    function sayFireAck(text, ok) {
      if (!el.fireAck) return;
      el.fireAck.textContent = text || "";
      el.fireAck.className = "hint " + (ok ? "ack-ok" : "ack-err");
      clearTimeout(ackTimer);
      ackTimer = setTimeout(() => { el.fireAck.textContent = ""; el.fireAck.className = "hint"; }, 2500);
    }

    /* ── 活跃节点：心跳过期就摘掉（不再只增不减）── */
    function noteStatus(node, host) {
      statusNodes.set(String(node || "?"), Date.now());
      // 心跳里带 host（跨机时用来"认机器"）：同一台机器的 11 个节点会报同一个 host，
      // 出现次数最多的那个就是**本机**，其余就是房间里别的机器（开火目标下拉读它）。
      if (host) {
        const h = String(host);
        hostHits.set(h, (hostHits.get(h) || 0) + 1);
        if (!myHost && (hostHits.get(h) || 0) >= 3) setMyHost(h);
        renderTargets();
      }
      renderNodes();
    }

    function setMyHost(h) {
      myHost = String(h);
      if (el.ctlMyHost) el.ctlMyHost.textContent = myHost;
      if (el.fireTargetHint) el.fireTargetHint.textContent = `（本机 = ${myHost}）`;
    }

    /** 对端是否在房间里（有别人机器的心跳 = 对端上了） */
    function peerConnected() {
      return [...hostHits.keys()].some((h) => h && h !== myHost);
    }

    /**
     * 对端未连接 → 「控制」这个 tab **直接禁用**（点不进去），并在控制页顶部摆一张说明卡。
     *
     * ⚠️ 这里**绝不能隐藏页面内容**（实测踩过）：一藏就把本机那整套卡片也藏没了，
     * 用户看到的是"原来的标签全被删了"。用户原话："你控制，你直接点不进去不就行了？"
     * 所以：只禁用 tab + 显示提示卡，页面结构原样保留。
     */
    function updatePeerGate(peerCount) {
      const n = typeof peerCount === "number" ? peerCount : [...hostHits.keys()].filter((h) => h !== myHost).length;
      const locked = n === 0;
      if (el.peerGate) el.peerGate.hidden = !locked;
      const tab = el.uiTabs ? el.uiTabs.querySelector('.tab[data-tab="control"]') : null;
      if (tab) {
        tab.classList.toggle("dead", locked);
        tab.disabled = locked;
        tab.setAttribute("data-tip", locked ? "对端未连接 —— 控制页暂时不可用（先让另一台加入同一房间）" : "去操作另一台机器");
      }
      // 对端没上、而用户正停在控制页 → 自动回到「被控（本机设备）」，别让人对着不能用的页
      if (locked && el.viewControl && !el.viewControl.hidden) setTab("controlled");
    }

    /** 开火目标下拉：本机 / 全部 / 房间里认出来的每一台机器 */
    function renderTargets() {      const sel = el.fireTarget;
      if (!sel) return;
      const prev = sel.value || "local";
      const peers = [...hostHits.keys()].filter((h) => h !== myHost).sort();
      sel.innerHTML = "";
      const add = (value, text) => {
        const o = document.createElement("option");
        o.value = value; o.textContent = text;
        sel.appendChild(o);
      };
      add("local", `只打本机${myHost ? "（" + myHost + "）" : ""} —— 我自己这台的郊狼`);
      peers.forEach((h) => add(h, `打对端 ${h}（另一台电脑的郊狼）`));
      add("all", "房间里所有机器一起打");
      sel.value = [...sel.options].some((o) => o.value === prev) ? prev : "local";
      // 目标列表建好后，按当前栏位把默认值摆正（控制→对端 / 被控→本机）
      preferTargetForTab(el.viewControlled && !el.viewControlled.hidden ? "controlled" : "control");
      if (el.ctlPeers) {
        el.ctlPeers.textContent = peers.length ? peers.join("、") : "（房间里暂时只有本机）";
      }
      updatePeerGate(peers.length);
    }

    /* ── 视角切换：🎮 控制端 / 🎯 被控端（同一套节点，只是看向哪一边）── */
    function setTab(name) {
      const which = name === "controlled" ? "controlled" : "control";
      if (el.viewControl) el.viewControl.hidden = which !== "control";
      if (el.viewControlled) el.viewControlled.hidden = which !== "controlled";
      if (el.uiTabs) {
        [...el.uiTabs.querySelectorAll(".tab")].forEach((b) => {
          b.classList.toggle("active", b.dataset && b.dataset.tab === which);
        });
      }
      try { localStorage.setItem("webdg.view", which); } catch (e) { /* 隐私模式忽略 */ }
      // ★ 整页切换过渡：整个 .layout（下面所有卡片一起）横向滑入，方向跟着栏位
      //   （用户要求："我跟你说的切换是指整个页面、下面所有的卡片切换过去"）
      const shown = which === "control" ? el.viewControl : el.viewControlled;
      if (shown) {
        shown.classList.remove("view-enter", "view-enter-left", "view-enter-right");
        void shown.offsetWidth;          // 强制重排 → 让动画每次都能重放
        shown.classList.add(which === "control" ? "view-enter-left" : "view-enter-right");
      }
      // ★ 「控制」= 操作对端、「被控」= 本机：切栏时把开火目标跟着摆正
      //   （下拉仍可手动改，只是不让默认值与栏位相反）
      preferTargetForTab(which);
    }

    /** 按栏位把「开火目标」摆到自然默认：控制 → 对端那台机器；被控 → 本机。 */
    function preferTargetForTab(which) {
      const sel = el.fireTarget;
      if (!sel) return;
      const peers = [...hostHits.keys()].filter((h) => h !== myHost).sort();
      if (which === "control" && peers.length) sel.value = peers[0];
      else if (which === "controlled") sel.value = "local";
    }

    function renderCtlFires() {
      const box = el.ctlFireList;
      if (!box) return;
      if (!ctlFires.length) { box.innerHTML = '<div class="hint">还没有人开火</div>'; return; }
      box.innerHTML = "";
      ctlFires.slice(0, 12).forEach((line) => {
        const row = document.createElement("div");
        row.className = "hint";
        row.textContent = line;
        box.appendChild(row);
      });
    }
    function renderNodes() {
      const now = Date.now();
      const list = [];
      for (const [n, ts] of statusNodes) {
        if (now - ts < NODE_TTL_MS) list.push(n);
        else statusNodes.delete(n);
      }
      list.sort();
      el.peerLabel.textContent = list.length ? `${list.length} 个` : "—";
      // "谁在房间" 挪进顶栏那个「节点」chip 的悬浮提示里（原来单占一张卡片，跟顶栏重复）
      if (el.peerLabel) {
        el.peerLabel.parentElement.title = list.length
          ? "活跃节点：" + list.join("、")
          : "活跃节点：—（10 秒内没有心跳）";
      }
    }
    setInterval(renderNodes, 2000);

    /* ── 调试区：后端转发的"未认领 key"（默认折叠，最多留 50 条）── */
    const DBG_MAX = 50;
    function noteTopic(msg) {
      const key = String((msg && msg.key) || "");
      // 本节点订阅了全部 key，所以自己发出的 command/* 会被收回来 —— 纯噪声，丢掉
      if (/^command\//.test(key)) return;
      if (!el.dbgList) return;
      const payload = msg && msg.payload != null ? JSON.stringify(msg.payload) : "—";
      const row = document.createElement("div");
      row.className = "rec";
      row.innerHTML = '<div class="top"><code></code><span></span></div><div class="dbg-body"></div>';
      row.querySelector("code").textContent = key;
      row.querySelector(".top span").textContent = new Date().toLocaleTimeString("zh-CN");
      row.querySelector(".dbg-body").textContent =
        payload.length > 160 ? payload.slice(0, 160) + "…" : payload;   // textContent：不解析 HTML
      el.dbgList.prepend(row);
      while (el.dbgList.children.length > DBG_MAX) el.dbgList.removeChild(el.dbgList.lastChild);
      if (el.dbgCount) el.dbgCount.textContent = el.dbgList.children.length + " 条";
    }

    function send(obj, onSent) {
      const ok = transport.send(obj);
      if (ok && onSent) onSent();
      return ok;
    }

    // ── mock ──
    function startMock(reason) {
      online.value = false; online.mock = true;
      setConn(false, true, reason || "本地演示");
      room.setOnline(false);
      CL.Mock.start(handleMessage);
      sendWaveforms(mockWaveforms());
      devices.apply(CL.Mock.devices ? CL.Mock.devices() : { devices: [] });
      log.add({ dir: "local", type: "info", kind: "系统", summary: "无后端 → 本地 mock 演示（" + (reason || "") + "）" });
    }
    function restartMock() { CL.Mock.stop(); startMock("手动重连（mock）"); }

    function mockWaveforms() {
      // 官方名单在 mock.js 里（实测的 24 条）；真实运行时一律用后端推来的列表
      const stored = loadMockWaves();
      return CL.Mock.waveforms ? CL.Mock.waveforms(stored) : stored;
    }
    function loadMockWaves() { try { return JSON.parse(localStorage.getItem(MOCKWAVE_STORE) || "[]"); } catch (e) { return []; } }
    function saveMockWaves(w) { try { localStorage.setItem(MOCKWAVE_STORE, JSON.stringify(w)); } catch (e) { /* ignore */ } }

    function sendWaveforms(list) { handleMessage({ type: "waveforms", waveforms: list }); }

    // ── 状态卡 ──
    function updateEmotion(face) {
      const emo = face.emotion || "unknown";
      el.emoEmoji.textContent = EMOTION_EMOJI[emo] || "🙂";
      el.emoText.textContent = emo;
      const c = typeof face.confidence === "number" ? face.confidence : 0;
      el.emoBar.style.width = Math.round(Math.min(1, Math.max(0, c)) * 100) + "%";
      el.emoConf.textContent = face.confidence == null ? "置信度 —" : "置信度 " + c.toFixed(2);
    }
    function updateAudio(a) {
      const lv = typeof a.level === "number" ? a.level : 0;
      el.audioBar.style.width = Math.round(Math.min(1, Math.max(0, lv)) * 100) + "%";
      const map = { silent: "🔈 静音", sound: "🔊 有声", loud: "📢 大声" };
      el.audioEvent.textContent = map[a.event] || a.event || "—";
      el.audioText.textContent = a.text || "—";
    }
    const heartHist = [];
    function updateHeart(h) {
      if (typeof h.bpm === "number") {
        el.heartVal.textContent = h.bpm + " bpm";
        heartHist.push(h.bpm); while (heartHist.length > 60) heartHist.shift();
        drawHeart();
      }
    }
    function drawHeart() {
      const c = el.heartCanvas, ctx = c.getContext("2d");
      const W = c.width, H = c.height;
      ctx.clearRect(0, 0, W, H);
      if (heartHist.length < 2) return;
      const lo = Math.min(...heartHist) - 5, hi = Math.max(...heartHist) + 5;
      ctx.strokeStyle = "#f85149"; ctx.lineWidth = 2; ctx.beginPath();
      heartHist.forEach((bpm, i) => {
        const x = (i / (heartHist.length - 1)) * W;
        const y = H - ((bpm - lo) / (hi - lo || 1)) * H;
        i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      });
      ctx.stroke();
    }
    function fireSummary(p) {
      const inten = p.intensity_delta != null ? `强度 +${p.intensity_delta}`
        : p.intensity != null ? `强度 → ${p.intensity}` : "强度 不动";
      const mode = p.overlay ? "叠加" : "抢占";
      return `${p.waveform} · 通道 ${p.channel} · ${p.duration_ms}ms · 优先级 ${p.priority} · ${mode} · ${inten}`;
    }
    function addFireLive(r) {
      // ★ 开火**失败**也要说清楚：web 节点那句"后端已受理"只代表"面板后端收到了"，
      //   真正的失败原因（没有已接入的 App / 没有可用设备）只有郊狼节点知道。
      if (link && link.noteFire) link.noteFire(r);      // 仪表盘「最近开火」
      const who = r.by ? `${r.by} 开火` : (r.source === "rules" ? "规则开火" : "面板开火");
      const tgt = r.target && r.target !== "local" ? `目标=${r.target}` : "目标=本机";
      ctlFires.unshift(
        `${new Date().toLocaleTimeString("zh-CN")} · ${who} · ${tgt} · ${r.waveform || "?"} `
        + `通道${r.channel || "?"} ${r.duration_ms || "?"}ms · `
        + (r.ok === false ? `失败（${r.error || "?"}）` : "完成"),
      );
      renderCtlFires();
      if (r.ok === false) {
        const why = r.error || "郊狼侧拒绝了这次开火";
        sayFireAck(`开火失败：${why}`, false);
        log.add({ dir: "in", type: "fire", kind: "开火失败",
          summary: `${r.waveform || "?"} · 通道 ${r.channel || "?"} · ${why}` });
        return;
      }
      log.add({ dir: "in", type: "fire", kind: "开火", summary:
        `${r.waveform || "?"} · 通道 ${r.channel || "?"} · ${r.duration_ms || "?"}ms · 来源 ${r.source || "?"}` });
    }

    // ── 连接 UI ──
    // ⚠️ 状态条那张"连接"卡片已经删掉（顶栏 chip 里已经有"已连接/节点数"，重复；
    //    用户明确要求别摆两遍）。这里保留函数但对缺失元素**全部空值保护**。
    function setConn(isOnline, isMock, detail) {
      el.connDot.className = "dot" + (isOnline ? " on" : isMock ? " mock" : "");
      el.connText.textContent = isOnline ? "已连接" : isMock ? "本地演示" : "未连接";
      if (el.connEmoji) el.connEmoji.textContent = isOnline ? "🟢" : isMock ? "🟠" : "⚪";
      if (el.connStateText) {
        el.connStateText.textContent = isOnline ? "后端已连接" : isMock ? "mock 演示中" : "未连接";
      }
      if (el.connDetail) el.connDetail.textContent = detail || "—";
      el.sourceChip.textContent = "数据：" + (isOnline ? "后端" : isMock ? "mock" : "无");
    }

    // 启动渲染循环与心跳
    requestAnimationFrame(skeleton.loop);

    // 视角切换（🎮 控制端 / 🎯 被控端）：同一套卡片，只是看向哪一边；选择记在本机
    if (el.uiTabs) {
      el.uiTabs.addEventListener("click", (ev) => {
        const b = ev.target.closest ? ev.target.closest(".tab") : null;
        if (b && b.dataset) setTab(b.dataset.tab);
      });
      let saved = "control";
      try { saved = localStorage.getItem("webdg.view") || "control"; } catch (e) { /* 忽略 */ }
      setTab(saved);
    }
    renderTargets();
    renderCtlFires();

    pingLoop();
    // 离线演示必须**显式**开启（?mock=1）；默认绝不填充假数据
    let wantMock = false;
    try { wantMock = /[?&]mock=1\b/.test(location.search || ""); } catch (e) { /* 忽略 */ }
    if (wantMock) {
      startMock("?mock=1 显式启用离线演示");
      log.add({ dir: "local", type: "info", kind: "系统",
        summary: "离线演示已开启（?mock=1）：界面显示的是**本地假数据**，不是后端数据" });
    } else {
      log.add({ dir: "local", type: "info", kind: "系统",
        summary: "面板已就绪，尝试连接后端 /ws…（连不上就是空白 + 自动重试，不会用假数据填充）" });
    }
  }
})();
