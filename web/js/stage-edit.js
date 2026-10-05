/* stage-edit.js —— 骨骼舞台上的「规则编辑层」（画线 / 画框 / 放圆）
 *
 * 为什么要有它：手敲 `left_wrist:y>0.5` 太不直观。这里让人**直接在骨骼画布上画**，
 * 再把画出来的形状翻译成判定引擎认识的 `param`。
 *
 * ⚠️ **坐标有两套，别搞混**（这是最容易错的地方）：
 *   · 规则用**归一化坐标** `0~1`，与 `feature/pose` 的 landmarks 同一套（左上角 = 0,0）
 *   · 画布是**镜像**的（`skeleton.js`：`px = (1-x)*W`），像照镜子
 *   → 所以"画布像素 → 归一化"必须**反镜像**：`x = 1 - px/W`，`y = py/H`
 *
 * 生成的 param（与后端 agent/nodes/rules/condition.py 的语法一一对应）：
 *   横线  left_wrist:y>0.62     （画布上"线下方的区域"）
 *   横线  left_wrist:y<0.62     （"线上方的区域"）
 *   竖线  left_wrist:x>0.40 / left_wrist:x<0.40
 *   框    rect:left_wrist,0.2,0.3,0.5,0.6
 *   圆    circle:left_wrist,0.5,0.5,0.08
 */
(function () {
  "use strict";
  window.CL = window.CL || {};

  //: MediaPipe Pose 的 33 个关键点（与后端 `agent/nodes/rules/condition.py` 的 LANDMARKS 一致）
  const LANDMARKS = [
    "nose",
    "left_eye_inner", "left_eye", "left_eye_outer",
    "right_eye_inner", "right_eye", "right_eye_outer",
    "left_ear", "right_ear",
    "mouth_left", "mouth_right",
    "left_shoulder", "right_shoulder",
    "left_elbow", "right_elbow",
    "left_wrist", "right_wrist",
    "left_pinky", "right_pinky",
    "left_index", "right_index",
    "left_thumb", "right_thumb",
    "left_hip", "right_hip",
    "left_knee", "right_knee",
    "left_ankle", "right_ankle",
    "left_heel", "right_heel",
    "left_foot_index", "right_foot_index",
  ];
  window.CL.LANDMARKS = LANDMARKS;

  const COLORS = {
    line: "#ffd479",
    rect: "#58a6ff",
    circle: "#bc8cff",
    draft: "#ff4d4f",
  };

  class StageEdit {
    /** cbs: {onCommit(param, meta), onCancel()} */
    constructor(canvas, cbs) {
      this.canvas = canvas;
      this.cbs = cbs || {};
      this.mode = null;        // null | "line-h" | "line-v" | "rect" | "circle"
      this.point = "";         // 目标关键点名（如 left_wrist）
      this.draft = null;       // 拖拽中的形状（归一化坐标）
      this.overlays = [];      // 已保存规则的可视化 [{shape, point, geo, ok}]
      this.hover = null;
      this._bind();
    }

    // ── 坐标系换算（★ 反镜像）──────────────────────────────
    toNorm(ev) {
      const r = this.canvas.getBoundingClientRect();
      const px = (ev.clientX - r.left) * (this.canvas.width / r.width);
      const py = (ev.clientY - r.top) * (this.canvas.height / r.height);
      return {
        x: Math.min(1, Math.max(0, 1 - px / this.canvas.width)),
        y: Math.min(1, Math.max(0, py / this.canvas.height)),
      };
    }

    toCanvas(p) {
      return {
        px: (1 - p.x) * this.canvas.width,
        py: p.y * this.canvas.height,
      };
    }

    // ── 进入/退出绘制模式 ─────────────────────────────────
    begin(mode, point) {
      if (!mode || !point) return;
      this.mode = mode;
      this.point = point;
      this.draft = null;
      this.canvas.style.cursor = "crosshair";
      this.canvas.classList.add("editing");
    }

    cancel() {
      const was = this.mode;
      this.mode = null;
      this.draft = null;
      this.canvas.style.cursor = "";
      this.canvas.classList.remove("editing");
      if (was && this.cbs.onCancel) this.cbs.onCancel();
    }

    get editing() { return !!this.mode; }

    /** 把"已保存的规则"画上去（可视反馈） */
    setOverlays(list) {
      this.overlays = Array.isArray(list) ? list : [];
    }

    // ── 事件 ──────────────────────────────────────────────
    _bind() {
      const c = this.canvas;
      c.addEventListener("pointerdown", (ev) => {
        if (!this.mode) return;
        ev.preventDefault();
        c.setPointerCapture(ev.pointerId);
        this.draft = { from: this.toNorm(ev), to: this.toNorm(ev) };
      });
      c.addEventListener("pointermove", (ev) => {
        if (!this.mode) return;
        if (this.draft) this.draft.to = this.toNorm(ev);
        this.hover = this.toNorm(ev);
      });
      c.addEventListener("pointerup", (ev) => {
        if (!this.mode || !this.draft) return;
        this.draft.to = this.toNorm(ev);
        const geo = this._finish(this.mode, this.draft);
        this.draft = null;
        if (!geo) return;                     // 太短/太小 → 当误点，留在编辑态
        const param = this._paramOf(this.mode, this.point, geo);
        this.cancel();
        if (this.cbs.onCommit) this.cbs.onCommit(param, { mode: this.mode, geo, point: this.point });
      });
      c.addEventListener("pointercancel", () => { this.draft = null; });
      c.addEventListener("contextmenu", (ev) => {
        if (this.mode) { ev.preventDefault(); this.cancel(); }
      });
    }

    /** 拖动结束 → 归一化几何（太短/太小返回 null） */
    _finish(mode, draft) {
      const { from, to } = draft;
      const MIN = 0.03;
      if (mode === "line-h" || mode === "line-v") {
        // ⚠️ 横线是**水平**拖出来的：线的位置看 y，但"拖够长了没"要看 **x 差**
        //    （拿 y 差当长度的话永远是 0，线就永远画不出来）
        const horizontal = mode === "line-h";
        const len = Math.abs((horizontal ? to.x : to.y) - (horizontal ? from.x : from.y));
        if (len < MIN) return null;
        const value = horizontal ? to.y : to.x;
        return { axis: horizontal ? "y" : "x", value: Number(value.toFixed(3)) };
      }
      if (mode === "rect") {
        const w = Math.abs(to.x - from.x), h = Math.abs(to.y - from.y);
        if (w < MIN || h < MIN) return null;
        return { rect: [from.x, from.y, to.x, to.y].map((v) => Number(v.toFixed(3))) };
      }
      if (mode === "circle") {
        const r = Math.hypot(to.x - from.x, to.y - from.y);
        if (r < MIN) return null;
        return { circle: [Number(from.x.toFixed(3)), Number(from.y.toFixed(3)), Number(r.toFixed(3))] };
      }
      return null;
    }

    /** 几何 → 判定引擎的 param 串 */
    _paramOf(mode, point, geo) {
      if (mode === "line-h") return `${point}:y>${geo.value}`;   // 线下方
      if (mode === "line-v") return `${point}:x<${geo.value}`;   // 线左侧（归一化 x 小）
      if (mode === "rect") return `rect:${point},${geo.rect.join(",")}`;
      if (mode === "circle") return `circle:${point},${geo.circle.join(",")}`;
      return "";
    }

    /** 几何 → 绘制用的"画布坐标点" */
    _canvasGeo(mode, geo) {
      if (mode === "line-h") return { y: geo.value };
      if (mode === "line-v") return { x: geo.value };
      if (mode === "rect") return { rect: geo.rect };
      return { circle: geo.circle };
    }

    // ── 绘制（由骨架的渲染循环调用）──────────────────────
    draw(ctx) {
      // 已保存的规则
      for (const ov of this.overlays) {
        this._paint(ctx, ov.mode, this._canvasGeo(ov.mode, ov.geo), COLORS[ov.shape] || COLORS.line,
          ov.ok === false ? 0.25 : 0.5);
      }
      // 拖拽中的形状
      if (this.draft) {
        const geo = this._finish(this.mode, this.draft);
        if (geo) {
          this._paint(ctx, this.mode, this._canvasGeo(this.mode, geo), COLORS.draft, 0.9);
        }
      }
      // 编辑态提示
      if (this.mode) {
        ctx.save();
        ctx.fillStyle = COLORS.draft;
        ctx.font = "13px sans-serif";
        ctx.fillText(this._hintText(), 10, 20);
        ctx.restore();
      }
    }

    _hintText() {
      const map = {
        "line-h": "拖一条横线（线下方为触发区）",
        "line-v": "拖一条竖线（线左侧为触发区）",
        rect: "拖一个框（点落在框内触发）",
        circle: "从圆心拖出半径（点落在圆内触发）",
      };
      return `${map[this.mode] || ""} · 目标 ${this.point} · 右键取消`;
    }

    _paint(ctx, mode, geo, color, alpha) {
      const c = this.canvas;
      ctx.save();
      ctx.globalAlpha = alpha;
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      ctx.lineWidth = 2;
      ctx.setLineDash([6, 4]);

      if (mode === "line-h") {
        const { py } = this.toCanvas({ x: 0, y: geo.y });
        ctx.beginPath(); ctx.moveTo(0, py); ctx.lineTo(c.width, py); ctx.stroke();
        ctx.globalAlpha = alpha * 0.10;
        ctx.fillRect(0, py, c.width, c.height - py);      // 触发区：线下
      } else if (mode === "line-v") {
        const { px } = this.toCanvas({ x: geo.x, y: 0 });
        ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, c.height); ctx.stroke();
        ctx.globalAlpha = alpha * 0.10;
        ctx.fillRect(0, 0, px, c.height);                  // 触发区：线左（归一化 x 小）
      } else if (mode === "rect") {
        const [x0, y0, x1, y1] = geo.rect;
        const a = this.toCanvas({ x: x0, y: y0 });
        const b = this.toCanvas({ x: x1, y: y1 });
        const rx = Math.min(a.px, b.px), ry = Math.min(a.py, b.py);
        const rw = Math.abs(a.px - b.px), rh = Math.abs(a.py - b.py);
        ctx.strokeRect(rx, ry, rw, rh);
        ctx.globalAlpha = alpha * 0.10;
        ctx.fillRect(rx, ry, rw, rh);
      } else if (mode === "circle") {
        const [cx, cy, r] = geo.circle;
        const o = this.toCanvas({ x: cx, y: cy });
        const rr = r * c.width;                            // 归一化半径按宽度换算
        ctx.beginPath(); ctx.arc(o.px, o.py, rr, 0, Math.PI * 2); ctx.stroke();
        ctx.globalAlpha = alpha * 0.10;
        ctx.fill();
      }
      ctx.restore();
    }
  }

  window.CL.StageEdit = StageEdit;
})();
