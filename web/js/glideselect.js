/* glideselect.js —— 自定义下拉（原生移植自 React Bits <GlideSelect/>）
 *
 * 为什么需要它：波形要显示「官方 · V3 · 30 帧」这样的 tag、设备要标「不可用 / 回环」，
 * 原生 <select> 做不到，而且下拉展开时是系统渲染，没法跟黑金主题统一。
 *
 * ★ 零侵入策略：**原生 <select> 一个都不删**（只是视觉隐藏），
 *   选中某行时写回 select.value 并派发 change 事件 ——
 *   于是 controls.js / devices.js / rules.js 的取值逻辑完全不用改。
 *   反过来，如果别的代码改了 option 列表或 value，MutationObserver 会重建 UI。
 *
 * tag 来源：直接解析 option 的显示文本，不要求调用方改代码
 *   "脉冲  [官方·V3·30帧]"  → name=脉冲, tag=官方·V3·30帧
 *   "USB 音频设备（回环）"    → name=USB 音频设备, tag=回环
 */
(function () {
  "use strict";
  window.CL = window.CL || {};

  const ROW = 32;      // 行高（与 CSS 的 .gs-option / .gs-pill 保持一致）
  const GAP = 2;
  //: 只有这些"已知后缀"才从括号里拆出来当 tag ——
  //: 否则设备名自带的括号（如「麦克风阵列 (Intel 智音)」）会被误判
  const KNOWN_TAGS = ["不可用", "回环", "内置", "虚拟", "自动", "默认", "无"];

  /** "名称 [tag]" / "名称（已知后缀）" → {name, tag} */
  function split(text) {
    const s = String(text || "").trim();
    let m = s.match(/^(.*?)\s*\[(.+?)\]\s*$/);
    if (m && m[1].trim()) return { name: m[1].trim(), tag: m[2].trim() };
    m = s.match(/^(.*?)\s*[（(]\s*([^（()）]+?)\s*[）)]\s*$/);
    // 括号前面必须真有名字（否则「（自动）」这种整体会被误拆）
    if (m && m[1].trim() && KNOWN_TAGS.indexOf(m[2].trim()) !== -1) {
      return { name: m[1].trim(), tag: m[2].trim() };
    }
    return { name: s, tag: "" };
  }

  class GlideSelect {
    /** @param {HTMLSelectElement} sel */
    static attach(sel, opts) {
      if (!sel || sel.__glide) return sel && sel.__glide;
      const cfg = Object.assign({ align: "left", placeholder: "选择…" }, opts || {});

      sel.classList.add("gs-native");
      sel.setAttribute("tabindex", "-1");

      const root = document.createElement("div");
      root.className = "glide-select";
      root.innerHTML =
        '<button type="button" class="gs-trigger" role="combobox" aria-haspopup="listbox" aria-expanded="false">' +
          '<span class="gs-label"></span>' +
          '<span class="gs-trigger-tag" aria-hidden="true"></span>' +
          '<span class="gs-chevron" aria-hidden="true">' +
            '<svg viewBox="0 0 12 12" width="12" height="12"><path d="M2.5 4.6 6 8.1l3.5-3.5" fill="none" ' +
            'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>' +
          "</span>" +
        "</button>" +
        '<div class="gs-menu" data-state="closed" data-side="bottom">' +
          '<div class="gs-list" role="listbox"><span class="gs-pill" aria-hidden="true"></span></div>' +
        "</div>";
      sel.insertAdjacentElement("afterend", root);

      const trigger = root.querySelector(".gs-trigger");
      const labelEl = root.querySelector(".gs-label");
      const menu = root.querySelector(".gs-menu");
      const list = root.querySelector(".gs-list");
      const pill = root.querySelector(".gs-pill");

      const st = { cfg, root, trigger, labelEl, menu, list, pill, rows: [], active: -1, open: false, closeTimer: 0, sig: "" };

      function rows() {
        return [...sel.options].map((o) => ({
          value: o.value,
          text: (o.textContent || "").trim(),
          disabled: !!o.disabled,
          ...split(o.textContent),
        }));
      }

      function rebuild() {
        const items = rows();
        // ★ 内容没变就别重建：波形/设备快照每秒都会重发（`setWaveforms`/`devices.apply`
        //   无条件调过来），原来每次重建都把 DOM 换一遍 —— 正在看的下拉会闪、
        //   `gs-pill` 高亮（按索引算的）也会跟着错位（实测反馈"高亮是乱的"）。
        const sig = items.map((i) => [i.value, i.name, i.tag, i.disabled ? 1 : 0].join("\u0001")).join("\u0002");
        if (sig === st.sig) { syncLabel(); return; }
        st.sig = sig;
        list.querySelectorAll(".gs-option").forEach((n) => n.remove());
        st.rows = items;
        if (!items.length) {
          const empty = document.createElement("div");
          empty.className = "gs-empty";
          empty.textContent = "（暂无选项）";
          list.appendChild(empty);
          syncLabel();
          return;
        }
        items.forEach((it, i) => {
          const row = document.createElement("div");
          row.className = "gs-option" + (it.disabled ? " is-disabled" : "");
          row.setAttribute("role", "option");
          row.dataset.index = String(i);
          row.dataset.value = it.value;
          row.innerHTML =
            '<span class="gs-name"></span>' +
            (it.tag ? '<span class="gs-tag"></span>' : "") +
            '<span class="gs-check" aria-hidden="true">' +
              '<svg viewBox="0 0 12 12" width="13" height="13"><path d="M2 6.4 4.7 9 10 3.4" fill="none" ' +
              "stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\"/></svg>" +
            "</span>";
          row.querySelector(".gs-name").textContent = it.name;
          if (it.tag) row.querySelector(".gs-tag").textContent = it.tag;
          list.appendChild(row);
        });
        syncLabel();
        // ★ 重建后把高亮重新对准"当前选中项"：菜单开着时列表被换掉过，
        //   老索引就指到别的行上了（"高亮是乱的"）。
        setActive(st.open ? st.rows.findIndex((x) => x.value === sel.value) : -1, true);
      }

      function syncLabel() {
        const cur = sel.value;
        const it = st.rows.find((r) => r.value === cur);
        labelEl.textContent = it ? it.name : (st.rows.length ? st.cfg.placeholder : "（暂无选项）");
        labelEl.dataset.empty = it ? "" : "1";
        const tagEl = root.querySelector(".gs-trigger-tag");
        if (tagEl) tagEl.textContent = it && it.tag ? it.tag : "";
        list.querySelectorAll(".gs-option").forEach((row) => {
          const on = row.dataset.value === cur;
          if (on) row.setAttribute("aria-selected", "true");
          else row.removeAttribute("aria-selected");
        });
      }

      /** 行步长：从真实行量出来（CSS 改行高也不会错位） */
      function rowStep() {
        const r = list.querySelector(".gs-option");
        return (r ? r.offsetHeight : ROW) + GAP;
      }

      function setActive(i, instant) {
        st.active = i;
        const row = st.rows[i];
        if (!row || i < 0) {
          pill.style.opacity = "0";
          return;
        }
        pill.style.transition = instant ? "opacity .1s ease" : "";
        pill.style.transform = "translateY(" + i * rowStep() + "px)";
        pill.style.opacity = "1";
      }

      function openMenu(viaKey) {
        if (st.open) return;
        st.open = true;
        clearTimeout(st.closeTimer);
        trigger.setAttribute("aria-expanded", "true");
        const r = trigger.getBoundingClientRect();
        const room = menu.offsetHeight || Math.min(st.rows.length * (ROW + GAP) + 8, 260);
        menu.dataset.side = (r.bottom + room + 8 > window.innerHeight && r.top - room > 8) ? "top" : "bottom";
        menu.dataset.state = "open";
        const cur = st.rows.findIndex((x) => x.value === sel.value);
        setActive(viaKey ? (cur < 0 ? 0 : cur) : -1, true);
      }

      function closeMenu(instant) {
        if (!st.open) return;
        st.open = false;
        trigger.setAttribute("aria-expanded", "false");
        setActive(-1, true);
        menu.dataset.state = "closed";
        if (instant) { menu.style.transitionDuration = "0ms"; st.closeTimer = setTimeout(() => { menu.style.transitionDuration = ""; }, 0); }
      }

      function pick(i) {
        const it = st.rows[i];
        if (!it || it.disabled) { closeMenu(true); return; }
        if (it.value !== sel.value) {
          sel.value = it.value;
          sel.dispatchEvent(new Event("change", { bubbles: true }));
          root.dataset.swap = "1";
          setTimeout(() => { delete root.dataset.swap; }, 200);
        }
        syncLabel();
        closeMenu(true);
        trigger.focus({ preventScroll: true });
      }

      /* ── 事件 ── */
      trigger.addEventListener("pointerdown", (e) => {
        if (e.button !== 0) return;
        e.preventDefault();
        trigger.focus({ preventScroll: true });
        if (st.open) closeMenu(true); else openMenu(false);
      });
      trigger.addEventListener("keydown", (e) => {
        const n = st.rows.length;
        if (!n) return;
        const cur = st.active >= 0 ? st.active : Math.max(0, st.rows.findIndex((x) => x.value === sel.value));
        const go = (i) => { e.preventDefault(); setActive(Math.min(n - 1, Math.max(0, i)), true); };
        if (!st.open) {
          if (["Enter", " ", "ArrowDown", "ArrowUp"].includes(e.key)) { e.preventDefault(); openMenu(true); }
          return;
        }
        if (e.key === "ArrowDown") go(cur + 1);
        else if (e.key === "ArrowUp") go(cur - 1);
        else if (e.key === "Home") go(0);
        else if (e.key === "End") go(n - 1);
        else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(cur); }
        else if (e.key === "Escape" || e.key === "Tab") { if (e.key === "Escape") e.preventDefault(); closeMenu(true); }
        else if (e.key.length === 1 && !e.metaKey && !e.ctrlKey && !e.altKey) {
          const c = e.key.toLowerCase();
          for (let k = 1; k <= n; k++) {
            const idx = (cur + k) % n;
            if (st.rows[idx].name.toLowerCase().startsWith(c)) { go(idx); break; }
          }
        }
      });
      list.addEventListener("pointerover", (e) => {
        if (e.pointerType === "touch") return;
        const row = e.target.closest(".gs-option");
        if (!row) return;
        const i = Number(row.dataset.index);
        if (i !== st.active) setActive(i, false);
      });
      list.addEventListener("click", (e) => {
        const row = e.target.closest(".gs-option");
        if (!row) return;
        pick(Number(row.dataset.index));
      });
      document.addEventListener("pointerdown", (e) => {
        if (st.open && !root.contains(e.target)) closeMenu(false);
      }, true);
      sel.addEventListener("change", syncLabel);

      /* option 列表变化（后端推波形 / 设备热插拔）→ 重建 */
      const obs = new MutationObserver(() => rebuild());
      obs.observe(sel, { childList: true, subtree: true, attributes: true });

      const api = { rebuild, syncLabel, root, element: sel };
      sel.__glide = api;
      rebuild();
      return api;
    }
  }

  window.CL.GlideSelect = GlideSelect;
  GlideSelect.split = split;          // 导出以便自测

  /* ── 自动挂到**所有**下拉上（隐藏的原生 select 仍可编程取值）──
   *
   * ⚠️ 以前只挂了 8 个，剩下的（声道 / 强度模式 / 导入版本 / 规则形状 / 心率带 / 串口…）
   * 还是系统原生渲染 —— 用户一眼就看出"有些是新的有些是原始的"（实测反馈）。
   * 现在按 DOM 全量扫描，以后新加的 <select> 也自动跟随。
   */
  function autoAttach() {
    const ids = [
      "waveSel", "cameraSel", "micSel", "bleSel", "serialSel",          // 波形 / 设备
      "heartSel", "channelSel", "prioritySel", "intensityMode",         // 郊狼参数
      "impVersion", "ruleCond", "ruleTarget", "ruleShape", "ruleChannel", "ruleWave",
    ];
    ids.forEach((id) => {
      const el = document.getElementById(id);
      if (el && el.tagName === "SELECT") GlideSelect.attach(el);
    });
    // 兜底：以后在 HTML 里新加的下拉也一并接管（要保留原生的就写 data-native="1"）
    document.querySelectorAll("select").forEach((el) => {
      if (el.dataset && el.dataset.native !== undefined) return;
      if (!el.classList.contains("gs-native")) GlideSelect.attach(el);
    });
  }
  if (typeof document !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", autoAttach);
    else autoAttach();
  }
})();
