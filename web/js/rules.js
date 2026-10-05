/* rules.js —— 规则编辑器（控制端判定；每次改动**整体下发**给后端）
 *
 * 契约（协调者确认）：前端发 { op:"set_rules", rules:[…] }，后端 publish 到 command/rules。
 *   · **语义是整体替换** —— 所以 add / toggle / remove 之后都推整份，不做增量
 *   · 字段：id / name / cond / param / wave / channel / dur / cd / enabled
 *           + debounce_ms（缓冲）/ min_visibility（可信度门槛）
 *   · 规则存在本机 localStorage（前端是真源），后端只负责执行
 *
 * ★ 可视化编辑：选「关键点 + 形状」点「✏️ 画」，然后去左边的骨骼画布上拖 ——
 *   由 `CL.StageEdit` 把形状翻成 `param` 串填回输入框（坐标系见 stage-edit.js）。
 */
(function () {
  "use strict";
  window.CL = window.CL || {};

  const STORE = "coyotelink.rules";

  /** 关键点名 → 中文（只用于显示，规则里永远用英文名） */
  const POINT_ZH = {
    nose: "鼻子", left_eye: "左眼", right_eye: "右眼",
    left_ear: "左耳", right_ear: "右耳",
    left_shoulder: "左肩", right_shoulder: "右肩",
    left_elbow: "左肘", right_elbow: "右肘",
    left_wrist: "左手腕", right_wrist: "右手腕",
    left_hip: "左髋", right_hip: "右髋",
    left_knee: "左膝", right_knee: "右膝",
    left_ankle: "左踝", right_ankle: "右踝",
    left_heel: "左脚跟", right_heel: "右脚跟",
    left_foot_index: "左脚尖", right_foot_index: "右脚尖",
  };
  const COND_HINT = {
    警戒线: "left_wrist:y>0.5（可画，也可手敲；支持 a&&b / a||b / rect:点,… / circle:点,…）",
    维持姿势: "left_wrist:y<0.3@5（条件@秒：保持满算过，中途破坏才罚）",
    按钮: "A 或 B",
    心率阈值: ">120 或 <50",
  };

  function load() {
    try { return JSON.parse(localStorage.getItem(STORE) || "[]"); } catch (e) { return []; }
  }
  function save(rules) {
    try { localStorage.setItem(STORE, JSON.stringify(rules)); } catch (e) { /* ignore */ }
  }
  function numOr(value, fallback) {
    const n = Number(value);
    return Number.isFinite(n) ? n : fallback;
  }

  /** `param` → 人话（列表显示用；规则里存的永远是原始串） */
  function humanize(param) {
    if (!param) return "";
    let s = String(param);
    s = s.replace(/\s*&&\s*/g, " <i>且</i> ").replace(/\s*\|\|\s*/g, " <i>或</i> ");
    s = s.replace(/rect:([a-z_]+),([\d.,]+)/g, (m, p, a) => `${zh(p)} 落在框(${a})内`);
    s = s.replace(/circle:([a-z_]+),([\d.,]+)/g, (m, p, a) => `${zh(p)} 落在圆(${a})内`);
    s = s.replace(/\b([a-z_]+):y>([\d.]+)/g, (m, p, v) => `${zh(p)} 低于 ${pct(v)}`);
    s = s.replace(/\b([a-z_]+):y<([\d.]+)/g, (m, p, v) => `${zh(p)} 高于 ${pct(v)}`);
    s = s.replace(/\b([a-z_]+):x>([\d.]+)/g, (m, p, v) => `${zh(p)} 在 ${v} 右侧`);
    s = s.replace(/\b([a-z_]+):x<([\d.]+)/g, (m, p, v) => `${zh(p)} 在 ${v} 左侧`);
    s = s.replace(/@([\d.]+)/g, " 保持 $1s");
    s = s.replace(/(^|[^<>])>(\d+)/g, "$1高于 $2");
    s = s.replace(/(^|[^<>])<(\d+)/g, "$1低于 $2");
    return s;
  }
  function zh(point) {
    return POINT_ZH[point] || (window.CL.LANDMARK_ZH && window.CL.LANDMARK_ZH[point]) || point;
  }
  function pct(v) {
    const n = Number(v);
    return Number.isFinite(n) ? `${Math.round(n * 100)}% 高度` : v;
  }

  class Rules {
    /** cbs: {onChange(list)} —— 每次改动后调用，由 app.js 整体下发 */
    constructor(els, cbs) {
      this.els = els;
      this.cbs = cbs || {};
      this.rules = load();
      this.editor = null;      // CL.StageEdit（由 app.js 注入）

      if (els.btnAdd) els.btnAdd.addEventListener("click", () => this.add());
      if (els.btnDrawRule) els.btnDrawRule.addEventListener("click", () => this.startDraw());
      if (els.ruleCond) els.ruleCond.addEventListener("change", () => this._hint());
      this._hint();
      this.render();
    }

    /** 交给后端的干净快照（只带契约里的字段，整体替换用） */
    snapshot() {
      const keys = ["id", "name", "cond", "param", "wave", "channel", "dur", "cd",
                    "enabled", "debounce_ms", "min_visibility"];
      return this.rules.map((r) => {
        const out = {};
        for (const k of keys) out[k] = r[k];
        return out;
      });
    }

    /** app.js 把画布编辑层交进来（没有也能用，只是不能画） */
    bindEditor(editor) {
      this.editor = editor;
      if (this.els.btnDrawRule) this.els.btnDrawRule.disabled = !editor;
      this.syncOverlays();
    }

    /** 关键点下拉（33 点，带中文名） */
    setTargets(points) {
      const sel = this.els.ruleTarget;
      if (!sel || !points || !points.length) return;
      const prev = sel.value;
      sel.innerHTML = "";
      for (const p of points) {
        const o = document.createElement("option");
        o.value = p;
        o.textContent = POINT_ZH[p] ? `${POINT_ZH[p]}（${p}）` : p;
        sel.appendChild(o);
      }
      if (prev) sel.value = prev;
    }

    /** 波形下拉同步（官方 + 导入） */
    setWaves(list) {
      const sel = this.els.ruleWave;
      if (!sel) return;
      const prev = sel.value;
      sel.innerHTML = '<option value="">波形…</option>';
      for (const w of list || []) {
        const o = document.createElement("option");
        o.value = w.name; o.textContent = w.name;
        sel.appendChild(o);
      }
      if (prev && [...sel.options].some((o) => o.value === prev)) sel.value = prev;
    }

    /** 把已配好的规则画到骨骼画布上（只在"警戒线"且能解析时） */
    syncOverlays() {
      if (!this.editor) return;
      const list = [];
      for (const r of this.rules) {
        const parsed = parseParam(r.param);
        if (!parsed) continue;
        list.push({ shape: parsed.shape, mode: parsed.mode, geo: parsed.geo, ok: r.enabled });
      }
      this.editor.setOverlays(list);
    }

    /** ✏️ 画：进入绘制模式，去画布上拖一个形状出来 */
    startDraw() {
      if (!this.editor) return;
      const point = (this.els.ruleTarget && this.els.ruleTarget.value) || "left_wrist";
      const shape = (this.els.ruleShape && this.els.ruleShape.value) || "line-h";
      this.editor.begin(shape, point);
      if (this.els.ruleParam) this.els.ruleParam.focus();
    }

    /** 画布那边画完回调进来：填进 param 输入框（不直接建规则，让人再确认一下） */
    applyDrawn(param) {
      if (this.els.ruleParam) {
        this.els.ruleParam.value = param;
        this.els.ruleParam.classList.add("drawn");
        setTimeout(() => this.els.ruleParam.classList.remove("drawn"), 1200);
      }
    }

    add() {
      const e = this.els;
      const name = (e.ruleName.value || "").trim();
      if (!name) { e.ruleName.focus(); return; }
      const param = (e.ruleParam.value || "").trim();
      if (!param) { e.ruleParam.focus(); return; }
      this.rules.push({
        id: "r" + Date.now() + Math.floor(Math.random() * 1000),
        name,
        cond: e.ruleCond.value,
        param,
        wave: e.ruleWave.value,
        channel: e.ruleChannel.value,
        dur: Number(e.ruleDur.value || 2000),
        cd: Number(e.ruleCd.value || 0),
        enabled: true,
        debounce_ms: numOr(e.ruleDebounce && e.ruleDebounce.value, 250),
        min_visibility: numOr(e.ruleMinVis && e.ruleMinVis.value, 0.5),
      });
      e.ruleName.value = ""; e.ruleParam.value = "";
      this._changed();
    }

    toggle(id) {
      const r = this.rules.find((x) => x.id === id);
      if (!r) return;
      r.enabled = !r.enabled;
      this._changed();
    }

    remove(id) {
      this.rules = this.rules.filter((x) => x.id !== id);
      this._changed();
    }

    _hint() {
      if (this.els.ruleParam && this.els.ruleCond) {
        this.els.ruleParam.placeholder = COND_HINT[this.els.ruleCond.value] || "条件参数";
      }
    }

    _changed() {
      save(this.rules);
      this.render();
      this.syncOverlays();
      if (this.cbs.onChange) this.cbs.onChange(this.snapshot());
    }

    render() {
      const box = this.els.ruleList;
      if (!box) return;
      box.innerHTML = "";
      if (!this.rules.length) {
        box.innerHTML = '<div class="empty">还没有规则 —— 上面的表单加一条，或点「✏️ 画」直接在骨骼上画</div>';
        return;
      }
      for (const r of this.rules) {
        const row = document.createElement("div");
        row.className = "rule-item";

        // 全部用 textContent（除人话里的 <i> 用一次性 innerHTML，内容是我们自己生成的）
        const grow = document.createElement("span");
        grow.className = "grow";
        const strong = document.createElement("b");
        strong.textContent = r.name;
        const tag = document.createElement("span");
        tag.className = "tag";
        const extra = [
          `冷却 ${r.cd}s`,
          r.debounce_ms ? `缓冲 ${r.debounce_ms}ms` : "无缓冲",
          (r.min_visibility != null && Number(r.min_visibility) > 0)
            ? `可信度 ≥${r.min_visibility}` : "",
        ].filter(Boolean).join(" · ");
        tag.textContent = `${r.cond} → 开火 ${r.wave || "?"} ${r.channel} ${r.dur}ms · ${extra}`;

        const human = document.createElement("div");
        human.className = "param";
        human.innerHTML = humanize(r.param);   // ← 这里的内容由 humanize 生成，不是用户原样注入

        grow.appendChild(strong);
        grow.appendChild(document.createElement("br"));
        grow.appendChild(human);
        grow.appendChild(tag);

        const sw = document.createElement("input");
        sw.type = "checkbox";
        sw.checked = !!r.enabled;
        sw.title = "启停";
        sw.addEventListener("change", () => this.toggle(r.id));

        const del = document.createElement("button");
        del.className = "ghost small";
        del.textContent = "删除";
        del.addEventListener("click", () => this.remove(r.id));

        row.appendChild(grow);
        row.appendChild(sw);
        row.appendChild(del);
        box.appendChild(row);
      }
    }
  }

  /**
   * 反着解析 `param`（只为**把规则画回画布**；判定的真源在后端）。
   * 只认**单条**原子（组合/维持不画）—— 画不准不如不画。
   */
  function parseParam(param) {
    if (!param) return null;
    const s = String(param).trim();
    let m = /^([a-z_]+)\s*:\s*([xy])\s*([<>])\s*([\d.]+)$/.exec(s);
    if (m) {
      const point = m[1], axis = m[2], op = m[3], value = Number(m[4]);
      // 判定：y>v = 线下方；x<v = 线左侧 → 画一条线在 value 处
      return { shape: axis === "y" ? "line" : "line", mode: axis === "y" ? "line-h" : "line-v",
               geo: { axis, value, op } };
    }
    m = /^rect:([a-z_]+),([\d.,\s]+)$/.exec(s);
    if (m) {
      const nums = m[2].split(",").map(Number).filter(Number.isFinite);
      if (nums.length === 4) return { shape: "rect", mode: "rect", geo: { rect: nums } };
    }
    m = /^circle:([a-z_]+),([\d.,\s]+)$/.exec(s);
    if (m) {
      const nums = m[2].split(",").map(Number).filter(Number.isFinite);
      if (nums.length === 3) return { shape: "circle", mode: "circle", geo: { circle: nums } };
    }
    return null;
  }

  window.CL.Rules = Rules;
  window.CL.rulesHumanize = humanize;
  window.CL.RULE_POINT_ZH = POINT_ZH;
})();
