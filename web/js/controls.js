/* controls.js —— 开火 / 强度 / 急停 / 波形下拉 / 波形导入（V2+V3，前端先做基本校验） */
(function () {
  "use strict";
  window.CL = window.CL || {};

  function hexOk(s) { return /^[0-9a-fA-F]+$/.test(s); }

  /** 解析粘贴文本：每行一帧，支持 # 注释与空行、行内空白/逗号分隔（与后端 import_text 一致）。 */
  function parseText(text) {
    const frames = [];
    const lines = String(text).split(/\r?\n/);
    for (let i = 0; i < lines.length; i++) {
      const body = lines[i].split("#", 1)[0].trim();
      if (!body) continue;
      for (const tok of body.split(/[\s,]+/)) {
        if (tok) frames.push(tok);
      }
    }
    return frames;
  }

  /** 前端校验（与后端 detect_version 同规则），错误信息**必须带帧号**（E18）。 */
  function validateFrames(frames) {
    if (!frames.length) throw new Error("没有任何帧");
    let first = null;
    for (let i = 0; i < frames.length; i++) {
      const f = String(frames[i]).trim();
      if (!f) throw new Error(`第 ${i + 1} 帧：空帧`);
      if (!hexOk(f)) throw new Error(`第 ${i + 1} 帧：含非 hex 字符（${f}）`);
      if (f.length % 2) throw new Error(`第 ${i + 1} 帧：hex 长度为奇数（${f.length}）`);
      if (first === null) first = f.length;
      else if (f.length !== first) {
        throw new Error(`第 ${i + 1} 帧长 ${f.length}，与第 1 帧的 ${first} 不一致（V2=6 / V3=16 不可混用）`);
      }
    }
    if (first !== 6 && first !== 16) {
      throw new Error(`第 1 帧长 ${first} 个 hex 字符，既不是 V2 的 6 也不是 V3 的 16`);
    }
    return first === 6 ? 2 : 3;
  }

  class Controls {
    constructor(els, cbs) {
      this.els = els;
      this.cbs = cbs;              // {fire, clear, importWave, deleteWave, renameWave, setWaves}
      this.waveforms = [];
      this._bind();
      this._syncDuration();
    }

    _bind() {
      const e = this.els;
      if (e.durRange) e.durRange.addEventListener("input", () => this._syncDuration());
      if (e.btnFire) e.btnFire.addEventListener("click", () => this.fire());
      if (e.btnStop) e.btnStop.addEventListener("click", () => this.cbs.clear && this.cbs.clear());
      if (e.btnImport) e.btnImport.addEventListener("click", () => this.importWaveforms());
      if (e.impFile) e.impFile.addEventListener("change", () => this._loadFile());
      this._bindHold(e.btnDeleteWave, () => this._deleteSelected());
    }

    /** 破坏性但不紧急的操作 → 长按确认（防误删）；没有 hold.js 时退回单击 */
    _bindHold(el, fn, holdTime) {
      if (!el) return;
      if (window.CL.HoldButton) {
        window.CL.HoldButton.attach(el, {
          holdTime: holdTime || 1200, doneLabel: "已删除", onHold: fn,
        });
      } else {
        el.addEventListener("click", fn);
      }
    }

    _syncDuration() {
      const e = this.els;
      if (e.durRange && e.durVal) e.durVal.textContent = Number(e.durRange.value).toFixed(1);
    }

    durationMs() { return Math.round(Number(this.els.durRange ? this.els.durRange.value : 2) * 1000); }

    fire() {
      const e = this.els;
      const wave = e.waveSel ? e.waveSel.value : "";
      if (!wave) { this._importMsg("没有可用波形（先导入或用官方波形）", true); return; }
      const mode = e.intensityMode ? e.intensityMode.value : "delta";
      const ival = e.intensityValue ? Number(e.intensityValue.value) : 0;
      const body = {
        op: "fire",
        waveform: wave,
        channel: e.channelSel ? e.channelSel.value : "A",
        duration_ms: this.durationMs(),
        priority: e.prioritySel ? Number(e.prioritySel.value) : 1,
        overlay: !!(e.overlayChk && e.overlayChk.checked),
        intensity_mode: mode,
        // ★ 打哪台机器上连着的郊狼：local（默认 = 只打本机）/ 对端主机名 / all
        target: e.fireTarget ? e.fireTarget.value : "local",
      };
      if (mode === "delta") body.intensity_delta = ival;
      else if (mode === "abs") body.intensity = ival;
      if (this.cbs.fire) this.cbs.fire(body);
    }

    /** 后端回的波形列表（官方 + 导入）。 */
    setWaveforms(list) {
      this.waveforms = Array.isArray(list) ? list : [];
      const e = this.els;
      const prev = e.waveSel ? e.waveSel.value : "";
      if (e.waveSel) e.waveSel.innerHTML = "";
      const imported = [];
      for (const w of this.waveforms) {
        const tag = `[${w.source === "official" ? "官方" : "导入"}·V${w.version}·${w.frames}帧]`;
        const opt = document.createElement("option");
        opt.value = w.name;
        opt.textContent = `${w.name}  ${tag}`;
        if (e.waveSel) e.waveSel.appendChild(opt);
        if (w.source !== "official") imported.push(w);
      }
      if (prev && e.waveSel && [...e.waveSel.options].some((o) => o.value === prev)) e.waveSel.value = prev;
      if (e.waveCount) e.waveCount.textContent = `共 ${this.waveforms.length} 条`;
      if (this.cbs.setWaves) this.cbs.setWaves(this.waveforms);
      this._renderImported(imported);
    }

    _renderImported(imported) {
      const box = this.els.importedList;
      if (!box) return;
      box.innerHTML = "";
      if (!imported.length) { box.innerHTML = '<div class="empty">还没有导入的波形（官方波形不可删）</div>'; return; }
      for (const w of imported) {
        const row = document.createElement("div");
        row.className = "rule-item";
        row.innerHTML = `<span class="grow">${w.name} <span class="tag">V${w.version} · ${w.frames} 帧</span></span>`;
        const ren = document.createElement("button");
        ren.className = "ghost small"; ren.textContent = "重命名";
        ren.addEventListener("click", () => {
          const nn = prompt("新名字：", w.name);
          if (nn) this.cbs.renameWave && this.cbs.renameWave(w.name, nn);
        });
        const del = document.createElement("button");
        del.className = "ghost small"; del.textContent = "删除";
        this._bindHold(del, () => this.cbs.deleteWave && this.cbs.deleteWave(w.name), 1000);
        row.appendChild(ren); row.appendChild(del);
        box.appendChild(row);
      }
    }

    _deleteSelected() {
      const name = this.els.waveSel ? this.els.waveSel.value : "";
      if (!name) return;
      const w = this.waveforms.find((x) => x.name === name);
      if (w && w.source === "official") { this._importMsg("官方波形不可删除", true); return; }
      this.cbs.deleteWave && this.cbs.deleteWave(name);
    }

    async _loadFile() {
      const f = this.els.impFile.files && this.els.impFile.files[0];
      if (!f) return;
      try {
        const text = await f.text();
        let frames = null;
        if (/\.json$/i.test(f.name)) {
          const obj = JSON.parse(text);
          if (!this.els.impName.value && obj.name) this.els.impName.value = obj.name;
          if (Array.isArray(obj.frames)) frames = obj.frames.map(String);
          else throw new Error("JSON 缺少 frames 数组");
        } else {
          if (!this.els.impName.value) this.els.impName.value = f.name.replace(/\.[^.]+$/, "");
          frames = parseText(text);
        }
        // ★ 没有"粘贴文本"框了（用户明确不要这个功能）：读到就直接提交，少一次点击。
        this._fileFrames = frames;
        this._importMsg(`已读取 ${f.name}：${frames.length} 帧，提交后端…`, false);
        this.importWaveforms();
      } catch (err) {
        this._importMsg("读取文件失败：" + err.message, true);
      }
    }

    /** 取待导入的帧：优先用刚选的文件，其次用（仍存在的）粘贴框 */
    _pendingFrames() {
      if (this._fileFrames && this._fileFrames.length) return this._fileFrames;
      const box = this.els.impText;
      return box ? parseText(box.value || "") : [];
    }

    importWaveforms() {
      const e = this.els;
      const name = (e.impName.value || "").trim();
      if (!name) { this._importMsg("请填波形名（选文件会自动填）", true); return; }
      let frames;
      try {
        frames = this._pendingFrames();
      } catch (err) { this._importMsg("解析失败：" + err.message, true); return; }
      let version = null;
      const forced = e.impVersion ? e.impVersion.value : "auto";
      try {
        const detected = validateFrames(frames);
        if (forced === "auto") version = detected;
        else {
          version = Number(forced);
          if (version !== detected) {
            this._importMsg(`声明 V${version}，但帧长看起来是 V${detected}`, true);
            return;
          }
        }
      } catch (err) {
        this._importMsg(err.message, true);   // ★ 带帧号的明确错误（E18）
        return;
      }
      this._importMsg(`校验通过：V${version}，共 ${frames.length} 帧，提交后端…`, false);
      this.cbs.importWave && this.cbs.importWave({ name, version, frames });
    }

    _importMsg(text, isErr) {
      if (!this.els.impMsg) return;
      this.els.impMsg.textContent = text || "";
      this.els.impMsg.className = "msg" + (isErr ? " err" : " ok");
    }
    showImportError(text) { this._importMsg(text, true); }
    /** 成功也要说一声（★ 以前一律走 showImportError → 导入成功却显示成红字报错） */
    showImportOk(text) { this._importMsg(text, false); }
  }

  window.CL.controls = {
    Controls: Controls,
    parseText: parseText,
    validateFrames: validateFrames,
  };
})();
