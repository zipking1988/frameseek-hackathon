/* Spatial investigation model and DOM adapter; graph lines represent actual parent turns. */
(function (root) {
  "use strict";
  const display = (value, query) => root.FrameSeekWorkbenchModel?.displayText(value, query) ?? value;
  const clamp = (n, lo, hi) => Math.min(hi, Math.max(lo, n));
  const densityFor = (scale, current = "detail") => {
    if (scale <= 0.38 || (scale < 0.44 && current === "map")) return "map";
    if (scale <= 0.72) return "overview";
    if (scale >= 0.82) return "detail";
    return current === "map" ? "overview" : current;
  };
  function createModel() {
    const items = new Map();
    const collapsed = new Set();
    const groups = new Map();
    const undoStack = [], redoStack = [];
    const layout = () => { const {view, ...saved} = model.snapshot(); return saved; };
    let foldedLayout = false;
    let lastArrangement = "threads";
    let view = { x: 0, y: 0, scale: 1 };
    const model = {
      items,
      collapsed,
      groups,
      get canUndo() { return undoStack.length > 0; },
      get canRedo() { return redoStack.length > 0; },
      checkpoint: () => layout(),
      commit(before, label = "Canvas action") {
        if (JSON.stringify(before) === JSON.stringify(layout())) return false;
        undoStack.push({saved: before, label});
        if (undoStack.length > 60) undoStack.shift();
        redoStack.length = 0;
        return true;
      },
      perform(label, action) { const before = layout(); action(); return model.commit(before, label); },
      undo() {
        const entry = undoStack.pop(); if (!entry) return null;
        redoStack.push({saved:layout(), label:entry.label});
        model.restore(entry.saved); return entry.label;
      },
      redo() {
        const entry = redoStack.pop(); if (!entry) return null;
        undoStack.push({saved:layout(), label:entry.label});
        model.restore(entry.saved); return entry.label;
      },
      resetHistory() { undoStack.length=0; redoStack.length=0; },
      snapshot() {
        return { groups: [...groups.values()].map(g=>({...g,members:[...g.members]})), view: { ...view }, collapsed: [...collapsed], lastArrangement, foldedLayout,
          nodes: [...items.values()].map(({id,x,y,branchName}) => ({id,x,y,branchName})) };
      },
      restore(saved) {
        for (const node of saved?.nodes || []) {
          const item = items.get(node.id);
          if (item && Number.isFinite(node.x) && Number.isFinite(node.y)) {
            item.x = clamp(node.x, -100000, 100000);
            item.y = clamp(node.y, -100000, 100000);
            item.branchName = typeof node.branchName === "string" ? node.branchName.slice(0,60) : "";
          }
        }
        groups.clear();
        for (const group of (Array.isArray(saved?.groups) ? saved.groups : []).slice(0,20)) {
          if (typeof group.id !== "string" || typeof group.name !== "string" || !Array.isArray(group.members)) continue;
          const members=[...new Set(group.members.filter(id=>items.has(id)))].slice(0,24);
          if(members.length>1) groups.set(group.id.slice(0,100), {id:group.id.slice(0,100), name:group.name.slice(0,60), members});
        }
        foldedLayout = saved?.foldedLayout === true;
        collapsed.clear();
        for (const id of saved?.collapsed || []) if (items.has(id)) collapsed.add(id);
        if (saved?.view && [saved.view.x,saved.view.y,saved.view.scale].every(Number.isFinite))
          view = { x: clamp(saved.view.x,-100000,100000), y: clamp(saved.view.y,-100000,100000), scale: clamp(saved.view.scale,0.2,1.5) };
        if (["threads","spread","workspace","time","status"].includes(saved?.lastArrangement)) lastArrangement = saved.lastArrangement;
      },
      get view() {
        return { ...view };
      },
      add(id, parent, metadata = {}) {
        if (items.has(id)) return items.get(id);
        const n = items.size;
        const origin = items.get(parent);
        const siblings = [...items.values()].filter(
          (i) => origin && i.parent === parent,
        ).length;
        const item = {
          id,
          parent: origin ? parent : null,
          ...metadata,
          x: origin ? origin.x + (n === 2 ? 540 : 440) : 84 + (n % 3) * 440,
          y: origin
            ? origin.y + (n === 2 ? 200 : 40) + siblings * 460
            : 175 + Math.floor(n / 3) * 480,
          width: 330,
          height: 420,
        };
        items.set(id, item);
        return item;
      },
      remove(id) {
        items.delete(id);
        for(const [key,group] of groups) {
          group.members=group.members.filter(member=>member!==id);
          if(group.members.length<2)groups.delete(key);
        }
        const wasFolded = collapsed.delete(id);
        for (const item of items.values())
          if (item.parent === id) item.parent = null;
        if (wasFolded && foldedLayout) model.arrange(lastArrangement);
      },
      descendants(id) {
        const result = new Set();
        const queue = [id];
        while (queue.length) {
          const parent = queue.pop();
          for (const item of items.values())
            if (
              item.parent === parent &&
              item.id !== id &&
              !result.has(item.id)
            ) {
              result.add(item.id);
              queue.push(item.id);
            }
        }
        return result;
      },
      visible() {
        return [...items.values()].filter((item) => {
          const visited = new Set([item.id]);
          let parent = items.get(item.parent);
          while (parent && !visited.has(parent.id)) {
            if (collapsed.has(parent.id)) return false;
            visited.add(parent.id);
            parent = items.get(parent.parent);
          }
          return true;
        });
      },
      fold(id, value) {
        if (!items.has(id) || items.get(id).kind || !model.descendants(id).size)
          return;
        if (value) collapsed.add(id);
        else {
          const changed = collapsed.delete(id);
          if (changed && foldedLayout) model.arrange(lastArrangement);
        }
      },
      expandPath(id) {
        let item = items.get(id);
        const visited = new Set();
        let changed = false;
        while (item && !visited.has(item.id)) {
          visited.add(item.id);
          if (item.id !== id) changed = collapsed.delete(item.id) || changed;
          item = items.get(item.parent);
        }
        if (changed && foldedLayout) model.arrange(lastArrangement);
      },
      expandAll() {
        collapsed.clear();
        if (foldedLayout) model.arrange(lastArrangement);
      },
      renameBranch(id, name) {
        const item = items.get(id);
        if (item && !item.kind && typeof name === "string")
          item.branchName = name.trim().slice(0, 60);
      },
      move(id, x, y) {
        const item = items.get(id);
        if (item && Number.isFinite(x) && Number.isFinite(y)) {
          if (collapsed.has(id))
            for (const child of model.descendants(id)) {
              const descendant = items.get(child);
              descendant.x += x - item.x;
              descendant.y += y - item.y;
            }
          Object.assign(item, { x, y });
        }
      },
      moveMany(ids, dx, dy) {
        if (![dx,dy].every(Number.isFinite)) return;
        const moving = new Set([...ids].filter(id=>items.has(id)));
        for (const id of [...moving]) if(collapsed.has(id)) for(const child of model.descendants(id))moving.add(child);
        for(const id of moving) { const item=items.get(id); item.x=clamp(item.x+dx,-100000,100000); item.y=clamp(item.y+dy,-100000,100000); }
      },
      group(ids, name, id) {
        const members=[...new Set([...ids].filter(key=>items.has(key)))].slice(0,24);
        if(members.length<2 || groups.size>=20 || typeof name!=="string" || !name.trim() || typeof id!=="string" || groups.has(id)) return false;
        groups.set(id,{id,name:name.trim().slice(0,60),members}); return true;
      },
      ungroup(ids) {
        const selected=new Set(ids);
        for(const [id,group] of groups) if(group.members.some(key=>selected.has(key)))groups.delete(id);
      },
      align(ids, mode) {
        const visibleIds=new Set(model.visible().map(n=>n.id));
        const nodes=[...new Set(ids)].filter(id=>visibleIds.has(id)).map(id=>items.get(id));
        if(nodes.length<2) return;
        const minimum=Math.min(...nodes.map(n=>mode==="left" ? n.x : n.y));
        if(["left","top"].includes(mode)) for(const n of nodes)model.moveMany([n.id],mode==="left" ? minimum-n.x : 0,mode==="top" ? minimum-n.y : 0);
        else if(nodes.length>2 && ["horizontal","vertical"].includes(mode)) {
          const axis=mode==="horizontal" ? "x" : "y", dimension=axis==="x" ? "width" : "height";
          nodes.sort((a,b)=>a[axis]-b[axis]);
          const first=nodes[0][axis], end=nodes.at(-1)[axis]+nodes.at(-1)[dimension];
          const gap=(end-first-nodes.reduce((sum,n)=>sum+n[dimension],0))/(nodes.length-1);
          let at=first;
          for(const n of nodes) {model.moveMany([n.id],axis==="x" ? at-n.x : 0,axis==="y" ? at-n.y : 0); at+=n[dimension]+gap;}
        }
      },
      snap(id, x, y, tolerance = 8) {
        const item = items.get(id);
        if (!item || ![x, y, tolerance].every(Number.isFinite))
          return { x, y, guides: [] };
        const guides = [];
        const result = { x, y };
        for (const [axis, dimension] of [
          ["x", "width"],
          ["y", "height"],
        ]) {
          let best = tolerance + 1;
          let guide = null;
          for (const other of model.visible()) {
            if (other.id === id) continue;
            for (const factor of [0, 0.5, 1]) {
              const target = other[axis] + other[dimension] * factor;
              const delta = target - (result[axis] + item[dimension] * factor);
              if (Math.abs(delta) < best && Math.abs(delta) <= tolerance) {
                best = Math.abs(delta);
                guide = { axis, at: target, delta };
              }
            }
          }
          if (guide) {
            result[axis] += guide.delta;
            guides.push({ axis, at: guide.at });
          }
        }
        return { ...result, guides };
      },
      related(id) {
        const related = new Set();
        if (!items.has(id)) return related;
        let current = items.get(id);
        while (current && !related.has(current.id)) {
          related.add(current.id);
          current = items.get(current.parent);
        }
        const descendants = [id];
        const visited = new Set();
        while (descendants.length) {
          const parent = descendants.pop();
          if (visited.has(parent)) continue;
          visited.add(parent);
          for (const item of items.values())
            if (item.parent === parent) {
              related.add(item.id);
              descendants.push(item.id);
            }
        }
        return related;
      },
      overview(width, height, mapWidth = 184, mapHeight = 106) {
        const visible = {
          x: -view.x / view.scale,
          y: -view.y / view.scale,
          width: width / view.scale,
          height: height / view.scale,
        };
        const nodes = model.visible();
        const all = [...nodes, visible];
        const left = Math.min(...all.map((item) => item.x)) - 40;
        const top = Math.min(...all.map((item) => item.y)) - 40;
        const right = Math.max(...all.map((item) => item.x + item.width)) + 40;
        const bottom =
          Math.max(...all.map((item) => item.y + item.height)) + 40;
        const scale = Math.min(
          mapWidth / (right - left),
          mapHeight / (bottom - top),
        );
        const ox = (mapWidth - (right - left) * scale) / 2;
        const oy = (mapHeight - (bottom - top) * scale) / 2;
        const project = (item) => ({
          id: item.id,
          kind: item.kind,
          x: ox + (item.x - left) * scale,
          y: oy + (item.y - top) * scale,
          width: item.width * scale,
          height: item.height * scale,
        });
        return {
          nodes: nodes.map(project),
          viewport: project(visible),
        };
      },
      pan(dx, dy) {
        if (Number.isFinite(dx) && Number.isFinite(dy)) {
          view.x += dx;
          view.y += dy;
        }
      },
      zoom(scale, x, y) {
        if (![scale, x, y].every(Number.isFinite)) return;
        const next = clamp(scale, 0.1, 1.6);
        const ratio = next / view.scale;
        view = {
          x: x - (x - view.x) * ratio,
          y: y - (y - view.y) * ratio,
          scale: next,
        };
      },
      arrange(mode = "threads") {
        lastArrangement = mode;
        foldedLayout = collapsed.size > 0;
        const ordered = model.visible();
        const rowHeight =
          Math.max(490, ...ordered.map((item) => item.height)) + 90;
        if (mode === "time")
          ordered.sort((a, b) => (a.time ?? Infinity) - (b.time ?? Infinity));
        if (mode === "status")
          ordered.sort(
            (a, b) =>
              (({ ok: 0, partial: 1, no_result: 2, error: 3 })[a.status] ?? 4) -
              ({ ok: 0, partial: 1, no_result: 2, error: 3 }[b.status] ?? 4),
          );
        if (mode === "workspace") {
          const questions = ordered.filter((item) => !item.kind);
          const findings = ordered.filter((item) => item.kind);
          questions.forEach((item, n) => {
            item.x = 84 + (n % 3) * 420;
            item.y = 175 + Math.floor(n / 3) * rowHeight;
          });
          const findingsTop = questions.length
            ? Math.max(...questions.map((item) => item.y + item.height)) + 120
            : 175;
          const findingRow =
            Math.max(360, ...findings.map((item) => item.height)) + 80;
          findings.forEach((item, n) => {
            item.x = 84 + (n % 3) * 420;
            item.y = findingsTop + Math.floor(n / 3) * findingRow;
          });
        } else if (mode !== "threads") {
          ordered.forEach((item, n) => {
            item.x = 84 + (n % 3) * 440;
            item.y = 175 + Math.floor(n / 3) * rowHeight;
          });
        } else {
          const depths = new Map();
          const rows = new Map();
          ordered.forEach((item) => {
            const depth =
              item.parent && depths.has(item.parent)
                ? depths.get(item.parent) + 1
                : 0;
            const row = rows.get(depth) || 0;
            item.x = 84 + depth * 440 + (depth >= 2 ? 100 : 0);
            item.y = 175 + row * rowHeight + (depth >= 2 ? 240 : depth * 40);
            depths.set(item.id, depth);
            rows.set(depth, row + 1);
          });
        }
      },
      fit(width, height, options = null) {
        if (!items.size) {
          view = { x: 0, y: 0, scale: 1 };
          return;
        }
        const all = model.visible();
        const left = Math.min(...all.map((i) => i.x));
        const top = Math.min(...all.map((i) => i.y));
        const right = Math.max(...all.map((i) => i.x + i.width));
        const bottom = Math.max(
          ...all.map(
            (i) =>
              i.y +
              (options?.full && !collapsed.has(i.id)
                ? i.detailHeight || i.height
                : i.height),
          ),
        );
        const insetTop = options?.top ?? (height < 850 ? 120 : 175);
        const availableHeight = options
          ? height - insetTop - options.bottom
          : height - (height < 850 ? 355 : 365);
        const scale = clamp(
          Math.min(
            (width - 80) / (right - left),
            availableHeight / (bottom - top),
            1,
          ),
          0.1,
          1,
        );
        view = {
          scale,
          x: (width - (right - left) * scale) / 2 - left * scale,
          y: insetTop - top * scale,
        };
      },
      clear() {
        items.clear(); groups.clear(); model.resetHistory();
        collapsed.clear();
        foldedLayout = false;
        lastArrangement = "threads";
        view = { x: 0, y: 0, scale: 1 };
      },
    };
    return model;
  }
  function start(callbacks) {
    const $ = (id) => document.getElementById(id);
    const viewport = $("chat-content");
    const plane = $("canvas-plane");
    const lines = $("canvas-links");
    const model = createModel();
    const records = new Map();
    let selected = null;
    const selectedIds = new Set();
    const groupElements = new Map();
    const groupLayer = document.createElement("div");
    groupLayer.className = "canvas-user-groups";
    plane.prepend(groupLayer);
    const layoutChanged = () => window.dispatchEvent(new Event("frameseek-canvas-changed"));
    function perform(label, action) {
      if(model.perform(label,action))layoutChanged();
      draw();
    }
    function replay(redo = false) {
      const label = redo ? model.redo() : model.undo();
      if(!label)return;
      draw();layoutChanged();callbacks.announce(`${redo ? "Redo" : "Undo"}：${label}`);
    }
    let gesture = null;
    let guides = [];
    let minimapPreference = null;
    let density = "detail";
    let detailFocus = null;
    let namingId = null;
    let pointerFocus = false;
    const minimapButtons = new Map();
    const size = () => ({
      width: viewport.clientWidth,
      height: viewport.clientHeight,
    });
    let pendingDraw = null;
    function scheduleDraw() {
      if (pendingDraw !== null) return;
      pendingDraw = requestAnimationFrame(() => {
        pendingDraw = null;
        draw();
      });
    }
    function draw() {
      for(const id of selectedIds)if(!model.items.has(id))selectedIds.delete(id);
      const { x, y, scale } = model.view;
      density = densityFor(scale, density);
      const related = model.related(selected);
      const visible = model.visible();
      const visibleIds = new Set(visible.map((item) => item.id));
      plane.style.transform = `translate(${x}px,${y}px) scale(${scale})`;
      plane.style.setProperty("--canvas-scale", Math.max(0.1, scale));
      for (const [id, item] of model.items) {
        const record = records.get(id);
        const turn = record.turn;
        turn.hidden = !visibleIds.has(id);
        if (turn.hidden) continue;
        const descendants = model.descendants(id);
        const folded = model.collapsed.has(id);
        turn.dataset.density = folded
          ? density === "map"
            ? "map"
            : "overview"
          : detailFocus === id
            ? "detail"
            : density;
        turn.classList.toggle("folded", folded);
        record.branchTools.hidden =
          Boolean(item.kind) || descendants.size === 0;
        record.branchName.textContent =
          item.branchName || `path ${id.split("-")[1]}`;
        record.branchFold.textContent = folded
          ? `Expand ${descendants.size}`
          : `Collapse ${descendants.size}`;
        record.branchFold.setAttribute("aria-expanded", String(!folded));
        record.branchFold.setAttribute(
          "aria-label",
          `${folded ? "Expand" : "Collapse"}Investigation path:${display(record.query.question, record.query)} (${descendants.size} subsequent objects)`,
        );
        record.overviewOpen.setAttribute(
          "aria-label",
          `${folded ? "Expand path" : "Expand objects"}：${display(record.query.question, record.query)}`,
        );
        record.overviewAction.textContent = folded ? "Expand path" : "check the details";
        record.overviewMeta.textContent = folded
          ? `${1 + [...descendants].filter((key) => !model.items.get(key).kind).length} questions · ${[...descendants].filter((key) => model.items.get(key).kind).length} discovery`
          : record.overviewDescription;
        turn.style.left = `${item.x}px`;
        turn.style.top = `${item.y}px`;
        item.height = turn.offsetHeight;
        item.width = turn.offsetWidth;
        if (turn.dataset.density === "detail") item.detailHeight = item.height;
        turn.classList.toggle("selected", id === selected);
        turn.classList.toggle("multi-selected", selectedIds.size>1 && selectedIds.has(id));
        turn.classList.toggle("connected", id !== selected && related.has(id));
        turn.classList.toggle(
          "unrelated",
          Boolean(selected) && !related.has(id),
        );
        const select = records.get(id).select;
        select.textContent = selectedIds.has(id) ? "selected" : "selected";
        select.setAttribute("aria-pressed",String(selectedIds.has(id)));
      }
      if($("canvas-undo"))$("canvas-undo").disabled=!model.canUndo;
      if($("canvas-redo"))$("canvas-redo").disabled=!model.canRedo;
      if($("canvas-selection-tools"))$("canvas-selection-tools").hidden=selectedIds.size<2;
      if($("canvas-selection-count"))$("canvas-selection-count").textContent=`${selectedIds.size} objects`;
      for(const [id,region] of groupElements)if(!model.groups.has(id)){region.remove();groupElements.delete(id);}
      for(const group of model.groups.values()) {
        const members=visible.filter(n=>group.members.includes(n.id));
        let region=groupElements.get(group.id);
        if(!region) {
          region=document.createElement("div");region.className="canvas-user-group";
          const label=document.createElement("button");
          const id=group.id;
          label.addEventListener("click",()=>{
            const current=model.groups.get(id);if(!current)return;
            choose(current.members.find(key=>records.has(key)));selectedIds.clear();for(const key of current.members)selectedIds.add(key);draw();
          });
          region.append(label);groupLayer.append(region);groupElements.set(id,region);
        }
        region.hidden=!members.length;if(!members.length)continue;
        const left=Math.min(...members.map(n=>n.x))-22,top=Math.min(...members.map(n=>n.y))-54;
        Object.assign(region.style,{left:`${left}px`,top:`${top}px`,width:`${Math.max(...members.map(n=>n.x+n.width))-left+22}px`,height:`${Math.max(...members.map(n=>n.y+n.height))-top+22}px`});
        region.firstElementChild.textContent=`${group.name} · ${group.members.length}`;
        region.firstElementChild.setAttribute("aria-label",`Select group:${group.name}`);
      }
      $("zoom-reset").textContent = `${Math.round(scale * 100)}%`;
      $("arrange-canvas").disabled = model.items.size === 0;
      if ($("expand-paths"))
        $("expand-paths").hidden = model.collapsed.size === 0;
      if ($("board-label"))
        $("board-label").textContent =
          density === "map"
            ? "Survey Canvas · Map"
            : density === "overview"
              ? "Survey Canvas · Summary"
              : "Investigation canvas";
      const group = $("finding-group-region");
      if (group) {
        const artifacts = visible.filter((item) => item.kind);
        group.hidden = !artifacts.length;
        if (artifacts.length) {
          const left = Math.min(...artifacts.map((item) => item.x)) - 18;
          const top = Math.min(...artifacts.map((item) => item.y)) - 46;
          group.style.left = `${left}px`;
          group.style.top = `${top}px`;
          group.style.width = `${Math.max(...artifacts.map((item) => item.x + item.width)) - left + 18}px`;
          group.style.height = `${Math.max(...artifacts.map((item) => item.y + item.height)) - top + 18}px`;
          $("finding-group-label").textContent =
            window.FrameSeekWorkbench?.getSnapshot().groupTitle || "Key findings";
        }
      }
      const { width, height } = size();
      const dpr = window.devicePixelRatio || 1;
      lines.width = width * dpr;
      lines.height = height * dpr;
      const ctx = lines.getContext("2d");
      ctx.scale(dpr, dpr);
      ctx.clearRect(0, 0, width, height);
      if (window.FrameSeekWorkbench && model.items.size) {
        const grid = 32 * scale;
        if (grid >= 10) {
          ctx.fillStyle = "#45607838";
          for (let gx = ((x % grid) + grid) % grid; gx < width; gx += grid)
            for (let gy = ((y % grid) + grid) % grid; gy < height; gy += grid)
              ctx.fillRect(gx, gy, 1, 1);
        }
      }
      ctx.translate(x, y);
      ctx.scale(scale, scale);
      for (const item of visible) {
        const parent = model.items.get(item.parent);
        if (!parent || !visibleIds.has(parent.id)) continue;
        const vertical = item.y >= parent.y + parent.height + 40;
        const from = {
          x: vertical ? parent.x + parent.width / 2 : parent.x + parent.width,
          y: vertical
            ? parent.y + parent.height
            : parent.y + Math.min(parent.height / 2, 210),
        };
        const to = {
          x: vertical ? item.x + item.width / 2 : item.x,
          y: vertical ? item.y : item.y + Math.min(item.height / 2, 210),
        };
        const bend = Math.max(45, Math.abs(to.x - from.x) / 2);
        const active = item.id === selected || item.parent === selected;
        const connected = related.has(item.id) && related.has(parent.id);
        ctx.strokeStyle = active
          ? "#ff884b"
          : connected
            ? "#7896ae"
            : "#3e566b";
        ctx.lineWidth = (active ? 1.8 : 1.15) / scale;
        ctx.setLineDash(item.kind === "evidence" ? [5 / scale, 5 / scale] : []);
        ctx.beginPath();
        ctx.moveTo(from.x, from.y);
        const crossesCard = visible.some(
          (other) =>
            other !== parent &&
            other !== item &&
            other.x > from.x &&
            other.x < to.x &&
            Math.abs(other.y - item.y) < 40,
        );
        if (vertical) {
          const gap = (to.y - from.y) / 2;
          ctx.bezierCurveTo(from.x, from.y + gap, to.x, to.y - gap, to.x, to.y);
        } else if (crossesCard) {
          const lane = Math.min(parent.y, item.y) - 35;
          ctx.bezierCurveTo(
            from.x + 35,
            from.y,
            from.x + 35,
            lane,
            from.x + 55,
            lane,
          );
          ctx.lineTo(to.x - 55, lane);
          ctx.bezierCurveTo(to.x - 35, lane, to.x - 35, to.y, to.x, to.y);
        } else {
          ctx.bezierCurveTo(
            from.x + bend,
            from.y,
            to.x - bend,
            to.y,
            to.x,
            to.y,
          );
        }
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.beginPath();
        ctx.moveTo(to.x - (vertical ? 4 : 7), to.y - (vertical ? 7 : 4));
        ctx.lineTo(to.x, to.y);
        ctx.lineTo(to.x + (vertical ? 4 : -7), to.y + (vertical ? -7 : 4));
        ctx.stroke();
        for (const port of [from, to]) {
          ctx.beginPath();
          ctx.arc(port.x, port.y, 2.5 / scale, 0, Math.PI * 2);
          ctx.fillStyle = ctx.strokeStyle;
          ctx.fill();
        }
      }
      ctx.setLineDash([4 / scale, 4 / scale]);
      ctx.strokeStyle = "#a3ced9";
      ctx.lineWidth = 1 / scale;
      for (const guide of guides) {
        ctx.beginPath();
        if (guide.axis === "x") {
          ctx.moveTo(guide.at, -y / scale);
          ctx.lineTo(guide.at, (height - y) / scale);
        } else {
          ctx.moveTo(-x / scale, guide.at);
          ctx.lineTo((width - x) / scale, guide.at);
        }
        ctx.stroke();
      }
      $("focus-card")?.toggleAttribute("disabled", !selected);
      $("canvas-node-count")?.replaceChildren(
        document.createTextNode(
          `${visible.length} / ${model.items.size} objects`,
        ),
      );
      drawMinimap();
    }
    function drawMinimap() {
      const map = $("minimap-content");
      if (!map) return;
      const s = size();
      const show = minimapPreference ?? (s.width >= 1000 && s.height >= 780);
      $("canvas-minimap").hidden = !show;
      $("toggle-minimap").setAttribute("aria-expanded", String(show));
      const overview = model.overview(s.width, s.height);
      const visible = new Set(overview.nodes.map((item) => item.id));
      for (const [id, button] of minimapButtons)
        if (!visible.has(id)) {
          button.remove();
          minimapButtons.delete(id);
        }
      const position = (element, rect) => {
        Object.assign(element.style, {
          left: `${rect.x}px`,
          top: `${rect.y}px`,
          width: `${Math.max(4, rect.width)}px`,
          height: `${Math.max(4, rect.height)}px`,
        });
      };
      position($("minimap-window"), overview.viewport);
      for (const item of overview.nodes) {
        let button = minimapButtons.get(item.id);
        if (!button) {
          button = document.createElement("button");
          button.type = "button";
          button.className = "minimap-node";
          button.addEventListener("click", () => {
            choose(item.id);
            reveal(item.id, true);
          });
          minimapButtons.set(item.id, button);
          map.append(button);
        }
        position(button, item);
        button.dataset.kind = item.kind || "question";
        button.setAttribute("aria-pressed", String(item.id === selected));
        button.setAttribute(
          "aria-label",
          `Canvas navigation:${display(records.get(item.id).query.question, records.get(item.id).query)}`,
        );
        button.title = display(records.get(item.id).query.question, records.get(item.id).query);
      }
      $("canvas-minimap").classList.toggle("empty", !model.items.size);
    }
    const cardObserver = new ResizeObserver(draw);
    window.FrameSeekWorkbench?.subscribe(draw);
    function toggleBranch(id) {
      const fold = !model.collapsed.has(id);
      const descendants = model.descendants(id);
      perform("Collapse investigation path",()=>model.fold(id, fold));
      if (fold && descendants.has(selected)) choose(id);
      fit();
      callbacks.announce(
        `${fold ? "Collapsed" : "Expanded"} ${descendants.size} connected objects; evidence links remain.`,
      );
    }
    function openNaming(id) {
      namingId = id;
      $("branch-name-input").value =
        model.items.get(id).branchName || `Path ${id.split("-")[1]}`;
      $("branch-name-source").textContent =
        `from"${display(records.get(id).query.question, records.get(id).query)}"Investigation launched`;
      $("branch-name-dialog").showModal();
      $("branch-name-input").focus();
      $("branch-name-input").select();
    }
    $("branch-name-form")?.addEventListener("submit", (e) => {
      e.preventDefault();
      const name = $("branch-name-input").value.trim();
      $("branch-name-input").setCustomValidity(name ? "" : "Enter a path name");
      if (!$("branch-name-input").reportValidity()) return;
      if (!name || !namingId || !model.items.has(namingId)) return;
      const record = records.get(namingId);
      perform("Name path",()=>model.renameBranch(namingId, name));
      draw();
      $("branch-name-dialog").close();
      record.branchName.focus({ preventScroll: true });
      callbacks.announce(`The path has been named${name}。`);
    });
    $("cancel-branch-name")?.addEventListener("click", () =>
      $("branch-name-dialog").close(),
    );
    $("branch-name-input")?.addEventListener("input", () =>
      $("branch-name-input").setCustomValidity(""),
    );
    $("branch-name-dialog")?.addEventListener("close", () => {
      namingId = null;
    });
    $("expand-paths")?.addEventListener("click", () => {
      perform("Expand all paths",()=>model.expandAll());
      fit();
      $("fit-canvas").focus({ preventScroll: true });
      callbacks.announce("All investigation paths have been opened.");
    });
    function fit() {
      detailFocus = null;
      draw();
      const s = size();
      model.fit(
        s.width,
        s.height,
        window.FrameSeekWorkbench
          ? { top: s.height < 850 ? 70 : 100, bottom: 240, full: true }
          : null,
      );
      draw();
    }
    function choose(id, focus = false, additive = false, preserve = false) {
      const record = records.get(id);
      if (!record) return;
      model.expandPath(id);
      if (detailFocus !== id) detailFocus = null;
      if(additive && selectedIds.has(id)) {
        selectedIds.delete(id);
        if(selectedIds.size && selected===id) { choose([...selectedIds].at(-1),false,false,true); return; }
        if(!selectedIds.size) {clearScope();return;}
        draw(); return;
      } else if(additive) selectedIds.add(id);
      else if(!preserve || !selectedIds.has(id)) {selectedIds.clear();selectedIds.add(id);}
      selected = id;
      callbacks.select(record.query.response, record.history);
      const scope = records.get(record.query.sourceTurn)?.query || record.query;
      $("composer-label").textContent = scope.mode === "real" && !scope.response.events.length
        ? `Re-question:${scope.question}`
        : `Continue to ask:${scope.question}`;
      $("clear-context").hidden = false;
      for (const [key, value] of records)
        value.select.setAttribute("aria-pressed", String(key === id));
      if (focus) $("question").focus();
      draw();
    }
    function clearScope() {
      selected = null; selectedIds.clear();
      detailFocus = null;
      $("composer-label").textContent = "Ask FrameSeek a question";
      $("clear-context").hidden = true;
      for (const record of records.values()) {
        record.turn.classList.remove("selected");
        record.select.setAttribute("aria-pressed", "false");
      }
      draw();
    }
    function register(turn, query, history) {
      for (const [id, record] of records)
        if (!record.turn.isConnected) {
          cardObserver.unobserve(record.turn);
          record.controller.abort();
          records.delete(id);
          model.remove(id);
        }
      const parent = query.parentTurn || null;
      model.add(turn.id, parent, {
        time: query.response.events[0]?.start,
        status: query.response.status,
        kind: query.kind || null,
      });
      const header = document.createElement("div");
      header.className = "card-heading";
      const drag = document.createElement("button");
      drag.type = "button";
      drag.className = "card-drag";
      drag.textContent =
        query.kind === "evidence"
          ? "Evidence frame"
          : query.kind === "conclusion"
            ? "Conclusion"
            : `Question ${turn.id.split("-")[1]}${parent ? " · Follow-up" : ""}`;
      drag.setAttribute(
        "aria-label",
        `Mobile card:${display(query.question, query)}, arrow keys to move`,
      );
      const select = document.createElement("button");
      select.type = "button";
      select.className = "card-select";
      select.textContent = "selected";
      select.setAttribute(
        "aria-label",
        `selected${query.kind === "evidence" ? "Evidence" : query.kind === "conclusion" ? "Conclusion" : "Question"}：${display(query.question, query)}`,
      );
      select.addEventListener("click", e => choose(turn.id,false,e.shiftKey));
      header.append(drag, select);
      if (!query.kind) {
        const remove = document.createElement("button");
        remove.type = "button"; remove.className = "card-delete";
        remove.textContent = "Delete"; remove.title = "Delete issues (can be restored on the left)";
        remove.setAttribute("aria-label", `Delete question:${display(query.question, query)}`);
        remove.addEventListener("click", e => {e.stopPropagation();root.FrameSeekUI?.hideTurn(turn.id);});
        header.append(remove);
      }
      turn.prepend(header);
      turn.dataset.status = query.response.status;
      const branchTools = document.createElement("div");
      branchTools.className = "branch-tools";
      branchTools.hidden = true;
      const branchName = document.createElement("button");
      branchName.type = "button";
      branchName.className = "branch-name";
      branchName.setAttribute("aria-label", `Name the investigation path:${display(query.question, query)}`);
      branchName.addEventListener("click", () => openNaming(turn.id));
      const branchFold = document.createElement("button");
      branchFold.type = "button";
      branchFold.className = "branch-fold";
      branchFold.addEventListener("click", () => toggleBranch(turn.id));
      branchTools.append(branchName, branchFold);
      header.after(branchTools);
      const overview = document.createElement("div");
      overview.className = "object-overview";
      const overviewOpen = document.createElement("button");
      overviewOpen.type = "button";
      overviewOpen.className = "overview-title";
      overviewOpen.textContent = display(query.question, query);
      const openObject = () => {
        model.fold(turn.id, false);
        choose(turn.id);
        reveal(turn.id, true);
      };
      overviewOpen.addEventListener("click", openObject);
      const overviewAnswer = document.createElement("p");
      overviewAnswer.className = "overview-answer";
      overviewAnswer.textContent =
        query.kind === "evidence"
          ? turn.querySelector(".artifact-caption").textContent
          : display(query.response.answer, query);
      const overviewMeta = document.createElement("p");
      overviewMeta.className = "overview-meta";
      const frames = new Set(
        query.response.events.flatMap((event) =>
          event.evidence.map((frame) => `${frame.uri}:${frame.t}`),
        ),
      );
      const overviewDescription =
        query.kind === "evidence"
          ? "Standalone evidence · Source retained"
          : query.kind === "conclusion"
            ? query.response.unresolved.length
              ? "Conclusion · Review needed"
              : "Conclusion · Source retained"
            : query.engine === "index" ? `${query.index_search?.total || 0} ${query.response.events.length ? "saved observation intervals" : "text candidates · coverage incomplete"}` : `${frames.size} evidence frames · ${query.response.status === "partial" ? "Review needed" : query.response.status === "no_result" ? "No result" : query.response.status === "error" ? "Search failed" : query.mode === "real" ? "Reviewed" : "Preset result"}`;
      const overviewAction = document.createElement("button");
      overviewAction.type = "button";
      overviewAction.className = "overview-action";
      overviewAction.setAttribute(
        "aria-label",
        `View object details:${display(query.question, query)}`,
      );
      overviewAction.addEventListener("click", openObject);
      overview.append(
        overviewOpen,
        overviewAnswer,
        overviewMeta,
        overviewAction,
      );
      branchTools.after(overview);
      const actions = document.createElement("div");
      actions.className = "response-actions";
      const follow = document.createElement("button");
      follow.type = "button";
      follow.className = "text-button card-followup";
      follow.textContent = query.mode === "real" && !query.response.events.length ? "Ask again" : "Keep asking";
      follow.addEventListener("click", () => root.FrameSeekUI
        ? root.FrameSeekUI.continueFrom(turn.id) : choose(turn.id, true));
      const replies =
        query.suggested_replies ||
        (query.id === "center"
          ? ["What happens next?"]
          : query.id === "right"
            ? ["Who moved it?"]
            : []);
      (query.kind ? [] : replies).slice(0, 2).forEach((reply) => {
        const suggested = document.createElement("button");
        suggested.type = "button";
        suggested.className = "text-button";
        suggested.textContent = display(reply, query);
        suggested.addEventListener("click", () => callbacks.ask(display(reply, query), turn.id));
        actions.append(suggested);
      });
      if (!query.kind) actions.append(follow);
      turn.append(actions);
      const controller = new AbortController();
      records.set(turn.id, {
        turn,
        query,
        history,
        select,
        controller,
        branchTools,
        branchName,
        branchFold,
        overviewOpen,
        overviewAction,
        overviewMeta,
        overviewDescription,
      });
      model.items.get(turn.id).detailHeight = turn.offsetHeight;
      if (query.kind) {
        const questions = [...model.items.values()].filter(
          (item) => !item.kind,
        );
        const pins = [...model.items.values()].filter((item) => item.kind);
        const top = questions.length
          ? Math.max(...questions.map((item) => item.y + item.height)) + 120
          : 175;
        let slot = 0;
        const rowHeight =
          Math.max(400, ...pins.map((item) => item.height)) + 80;
        while (slot < 30) {
          const x = 84 + (slot % 3) * 420;
          const y = top + Math.floor(slot / 3) * rowHeight;
          const overlaps = pins.some(
            (item) =>
              item.id !== turn.id &&
              x < item.x + item.width &&
              x + 350 > item.x &&
              y < item.y + item.height &&
              y + 420 > item.y,
          );
          if (!overlaps) {
            model.move(turn.id, x, y);
            break;
          }
          slot++;
        }
      }
      drag.addEventListener("pointerdown", (e) => {
        if (e.button !== 0) return;
        e.preventDefault();
        const item = model.items.get(turn.id);
        choose(turn.id,false,false,true);
        if(!selectedIds.has(turn.id))return;
        gesture = {
          before:model.checkpoint(),
          ids:[...selectedIds],
          kind: "card",
          id: turn.id,
          pointer: e.pointerId,
          px: e.clientX,
          py: e.clientY,
          x: item.x,
          y: item.y,
          scale: model.view.scale,
        };
        drag.focus({ preventScroll: true });
        drag.setPointerCapture(e.pointerId);
        turn.classList.add("dragging");
      });
      const end = () => {
        if (gesture?.id !== turn.id) return;
        if(model.commit(gesture.before,"Move objects"))layoutChanged();
        gesture = null;
        guides = [];
        turn.classList.remove("dragging");
        draw();
      };
      drag.addEventListener("lostpointercapture", end);
      // The document fallback also handles native browser retargeting during a drag.
      document.addEventListener(
        "pointermove",
        (e) => {
          if (gesture?.id !== turn.id || gesture.pointer !== e.pointerId)
            return;
          if (!(e.buttons & 1)) {
            end();
            return;
          }
          const x = gesture.x + (e.clientX - gesture.px) / gesture.scale;
          const y = gesture.y + (e.clientY - gesture.py) / gesture.scale;
          const next = e.shiftKey || gesture.ids.length>1
            ? { x, y, guides: [] }
            : model.snap(turn.id, x, y, 7 / gesture.scale);
          const item=model.items.get(turn.id);
          model.moveMany(gesture.ids,next.x-item.x,next.y-item.y);
          guides = next.guides;
          scheduleDraw();
        },
        { signal: controller.signal },
      );
      document.addEventListener("pointerup", end, {
        signal: controller.signal,
      });
      document.addEventListener("pointercancel", end, {
        signal: controller.signal,
      });
      drag.addEventListener("keydown", (e) => {
        const steps = {
          ArrowLeft: [-1, 0],
          ArrowRight: [1, 0],
          ArrowUp: [0, -1],
          ArrowDown: [0, 1],
        };
        if (!steps[e.key]) return;
        e.preventDefault();
        const step = e.shiftKey ? 50 : 10;
        choose(turn.id,false,false,true);
        perform("Move objects with keyboard",()=>model.moveMany(selectedIds,steps[e.key][0]*step,steps[e.key][1]*step));
        draw();
        callbacks.announce("Card location updated.");
      });
      history.addEventListener("click", () => {
        choose(turn.id);
        reveal(turn.id);
      });
      turn.addEventListener("click", (e) => {
        if (!e.target.closest("button,summary,details")) choose(turn.id,false,e.shiftKey);
      });
      turn.addEventListener("toggle", draw, true);
      choose(turn.id);
      const viewportSize = size();
      if (viewportSize.height < 850) {
        model.zoom(
          Math.min(model.view.scale, 0.88),
          viewportSize.width / 2,
          viewportSize.height / 2,
        );
        reveal(turn.id);
      } else if (model.items.size === 3) fit();
      else if (model.items.size > 3) reveal(turn.id);
      else draw();
      cardObserver.observe(turn);
    }
    function reveal(id, readable = false) {
      const item = model.items.get(id);
      if (!item) return;
      if (readable) detailFocus = id;
      draw();
      if (window.FrameSeekWorkbench) {
        const s = size();
        const composerTop =
          $("question-form").getBoundingClientRect().top -
          viewport.getBoundingClientRect().top;
        const targetScale = Math.min(
          readable ? 1 : model.view.scale,
          1,
          (composerTop - 48) /
            (readable ? item.detailHeight || item.height : item.height),
        );
        model.zoom(targetScale, s.width / 2, s.height / 2);
        draw();
        const v = model.view;
        const targetTop = Math.max(
          24,
          Math.min(100, (composerTop - item.height * v.scale) / 2),
        );
        model.pan(
          s.width * 0.46 -
            (item.x * v.scale + v.x) -
            (item.width * v.scale) / 2,
          targetTop - (item.y * v.scale + v.y),
        );
        draw();
        return;
      }
      const v = model.view,
        s = size();
      const left = item.x * v.scale + v.x,
        top = item.y * v.scale + v.y;
      model.pan(
        s.width * 0.42 - left - (item.width * v.scale) / 2,
        Math.max(
          120,
          Math.min(175, (s.height - 260 - item.height * v.scale) / 2),
        ) - top,
      );
      draw();
    }
    viewport.addEventListener("pointerdown", (e) => {
      if (viewport.dataset.readingMode === "true") return;
      if (
        gesture?.kind === "card" ||
        e.button !== 0 ||
        e.target.closest(".turn,button,a")
      )
        return;
      viewport.setPointerCapture(e.pointerId);
      gesture = {
        kind: "pan",
        pointer: e.pointerId,
        px: e.clientX,
        py: e.clientY,
      };
      viewport.classList.add("panning");
    });
    viewport.addEventListener(
      "pointerdown",
      () => {
        pointerFocus = viewport.dataset.readingMode !== "true";
      },
      true,
    );
    document.addEventListener("pointerup", () => {
      pointerFocus = false;
    });
    document.addEventListener("pointercancel", () => {
      pointerFocus = false;
    });
    viewport.addEventListener("pointermove", (e) => {
      if (gesture?.kind !== "pan" || gesture.pointer !== e.pointerId) return;
      model.pan(e.clientX - gesture.px, e.clientY - gesture.py);
      gesture.px = e.clientX;
      gesture.py = e.clientY;
      scheduleDraw();
    });
    const stopPan = () => {
      if (gesture?.kind === "pan") gesture = null;
      viewport.classList.remove("panning");
    };
    viewport.addEventListener("pointerup", stopPan);
    viewport.addEventListener("pointercancel", stopPan);
    viewport.addEventListener("lostpointercapture", stopPan);
    document.addEventListener("pointerup", stopPan);
    document.addEventListener("pointercancel", stopPan);
    viewport.addEventListener(
      "wheel",
      (e) => {
        if (viewport.dataset.readingMode === "true") return;
        if (e.target.closest("details,textarea")) return;
        e.preventDefault();
        if (e.ctrlKey || e.metaKey) {
          detailFocus = null;
          const box = viewport.getBoundingClientRect();
          model.zoom(
            model.view.scale * Math.exp(-e.deltaY * 0.005),
            e.clientX - box.left,
            e.clientY - box.top,
          );
        } else model.pan(-e.deltaX, -e.deltaY);
        scheduleDraw();
      },
      { passive: false },
    );
    viewport.addEventListener("keydown", (e) => {
      if (viewport.dataset.readingMode === "true") return;
      if (e.target !== viewport) return;
      if (e.key === "Home") {
        e.preventDefault();
        fit();
        return;
      }
      if (e.key.toLowerCase() === "f" && selected) {
        e.preventDefault();
        reveal(selected, true);
        return;
      }
      const steps = {
        ArrowLeft: [50, 0],
        ArrowRight: [-50, 0],
        ArrowUp: [0, 50],
        ArrowDown: [0, -50],
      };
      if (steps[e.key]) {
        e.preventDefault();
        model.pan(...steps[e.key]);
        draw();
      }
    });
    $("zoom-in").addEventListener("click", () => {
      detailFocus = null;
      const s = size();
      model.zoom(model.view.scale * 1.15, s.width / 2, s.height / 2);
      draw();
    });
    $("zoom-out").addEventListener("click", () => {
      detailFocus = null;
      const s = size();
      model.zoom(model.view.scale / 1.15, s.width / 2, s.height / 2);
      draw();
    });
    $("zoom-reset").addEventListener("click", () => {
      detailFocus = null;
      const s = size();
      model.zoom(1, s.width / 2, s.height / 2);
      draw();
    });
    $("fit-canvas").addEventListener("click", fit);
    $("focus-card")?.addEventListener("click", () => {
      reveal(selected, true);
      callbacks.announce("The current object is focused. Adapt the view to get back to the big picture.");
    });
    $("toggle-minimap")?.addEventListener("click", () => {
      minimapPreference = $("canvas-minimap").hidden;
      draw();
    });
    viewport.addEventListener("dblclick", (e) => {
      if (viewport.dataset.readingMode === "true") return;
      if (!e.target.closest(".turn,button,a")) fit();
    });
    function arrange(mode) {
      callbacks.close();
      $("arrange-mode").value = mode;
      perform("Arrange automatically",()=>model.arrange(mode));
      fit();
      callbacks.announce("Survey cards have been automatically sorted.");
    }
    $("arrange-canvas").addEventListener("click", () =>
      arrange($("arrange-mode").value),
    );
    $("clear-context").addEventListener("click", () => {
      clearScope();
      $("question").focus();
    });
    $("rail-canvas").addEventListener("click", () => {
      callbacks.close();
      fit();
    });
    $("canvas-undo")?.addEventListener("click",()=>replay());
    $("canvas-redo")?.addEventListener("click",()=>replay(true));
    $("canvas-align")?.addEventListener("click",()=>perform("Align objects",()=>model.align(selectedIds,$("canvas-alignment").value)));
    $("canvas-ungroup")?.addEventListener("click",()=>perform("Ungroup objects",()=>model.ungroup(selectedIds)));
    $("canvas-clear-selection")?.addEventListener("click",()=>{selectedIds.clear();if(selected)selectedIds.add(selected);draw();});
    let groupingIds = [];
    $("canvas-group")?.addEventListener("click",()=>{groupingIds=[...selectedIds];$("canvas-group-name").value="";$("canvas-group-dialog").showModal();$("canvas-group-name").focus();});
    $("canvas-group-name")?.addEventListener("input",()=>$("canvas-group-name").setCustomValidity(""));
    $("canvas-group-cancel")?.addEventListener("click",()=>$("canvas-group-dialog").close());
    $("canvas-group-form")?.addEventListener("submit",e=>{
      e.preventDefault(); const name=$("canvas-group-name").value.trim();
      $("canvas-group-name").setCustomValidity(name ? "" : "Enter a group name");
      if(!$("canvas-group-name").reportValidity())return;
      let created=false;
      perform("Create group",()=>{created=model.group(groupingIds,name,`group-${crypto.randomUUID()}`);});
      if(created){$("canvas-group-dialog").close();callbacks.announce(`Group created:${name}`);}
      else callbacks.announce("Unable to create a group: at least two valid objects are required, and a maximum of 20 groups are reserved.");
    });
    function searchResults() {
      const text=$("canvas-search-input").value.trim().toLocaleLowerCase();
      const results=$("canvas-search-results");results.replaceChildren();
      const matches=[...records].filter(([id,r])=>{
        const groups=[...model.groups.values()].filter(g=>g.members.includes(id)).map(g=>g.name);
        const response=r.query.response;
        return [r.query.question,response.answer,...(response.events||[]).flatMap(e=>[e.summary,...e.evidence.map(f=>f.caption)]),model.items.get(id)?.branchName,...groups].join(" ").toLocaleLowerCase().includes(text);
      });
      $("canvas-search-status").textContent=matches.length ? `${matches.length} objects · Enter locates the first item` : "No matches. Search questions, answers, frame descriptions, or group names.";
      for(const [id,r] of matches) {
        const button=document.createElement("button");button.type="button";
        const title=document.createElement("strong");title.textContent=display(r.query.question,r.query);
        const subtitle=document.createElement("span");subtitle.textContent=(display(r.query.response.answer,r.query)||"").slice(0,100);
        button.append(title,subtitle);button.addEventListener("click",()=>{$("canvas-search-dialog").close();choose(id);reveal(id,true);records.get(id).select.focus({preventScroll:true});});results.append(button);
      }
    }
    function openSearch() {$("canvas-search-dialog").showModal();$("canvas-search-input").value="";searchResults();$("canvas-search-input").focus();}
    $("canvas-search")?.addEventListener("click",openSearch);
    $("canvas-search-close")?.addEventListener("click",()=>$("canvas-search-dialog").close());
    $("canvas-search-input")?.addEventListener("input",searchResults);
    $("canvas-search-input")?.addEventListener("keydown",e=>{if(e.key==="Enter"){$("canvas-search-results").querySelector("button")?.click();e.preventDefault();}if(e.key==="ArrowDown"){$("canvas-search-results").querySelector("button")?.focus();e.preventDefault();}});
    $("canvas-shortcuts")?.addEventListener("click",()=>$("canvas-shortcuts-dialog").showModal());
    $("canvas-shortcuts-close")?.addEventListener("click",()=>$("canvas-shortcuts-dialog").close());
    document.addEventListener("keydown",e=>{
      if (viewport.dataset.readingMode === "true") return;
      if(document.querySelector("dialog[open]"))return;
      if((e.metaKey||e.ctrlKey) && e.key.toLowerCase()==="k") {e.preventDefault();openSearch();return;}
      if(e.target.closest("input,textarea,select,[contenteditable=true]"))return;
      if(!viewport.contains(e.target) && !e.target.closest(".view-controls,.canvas-selection-tools,.canvas-actions"))return;
      if((e.metaKey||e.ctrlKey) && ["z","y"].includes(e.key.toLowerCase())) {e.preventDefault();replay(e.shiftKey||e.key.toLowerCase()==="y");}
      else if((e.metaKey||e.ctrlKey) && e.key.toLowerCase()==="a") {e.preventDefault();selectedIds.clear();for(const n of model.visible())selectedIds.add(n.id);draw();}
      else if(e.key==="Escape"){selectedIds.clear();if(selected)selectedIds.add(selected);draw();}
    });
    viewport.addEventListener("focusin", (e) => {
      if (gesture || pointerFocus) return;
      const card = e.target.closest(".turn");
      if (!card) return;
      const rect = e.target.getBoundingClientRect(),
        bounds = viewport.getBoundingClientRect();
      if (
        rect.left < bounds.left ||
        rect.right > bounds.right ||
        rect.top < bounds.top + 120 ||
        rect.bottom > bounds.bottom - 45
      )
        reveal(card.id);
    });
    let lastSize = size();
    new ResizeObserver(() => {
      const next = size();
      draw();
      if (
        selected &&
        !gesture &&
        (next.width < lastSize.width || next.height < lastSize.height)
      )
        reveal(selected, true);
      lastSize = next;
    }).observe(viewport);
    return {
      snapshot: () => ({ ...model.snapshot(), selected }),
      restore: (saved) => { model.restore(saved); model.resetHistory(); if (saved?.selected && records.has(saved.selected)) choose(saved.selected); else if (saved?.selected === null) clearScope(); draw(); },
      register,
      choose,
      arrange,
      clearScope,
      context: () => {
        const record = selected ? records.get(selected) : null;
        const scope = record?.query.sourceTurn
          ? records.get(record.query.sourceTurn) || record
          : record;
        // A failed real turn is selectable for inspection, but cannot anchor a query.
        return scope?.query.mode === "real" && !scope.query.response.events.length ? null : scope;
      },
      reveal,
      remove: (id) => {
        const record = records.get(id);
        if (record) {
          cardObserver.unobserve(record.turn);
          record.controller.abort();
        }
        records.delete(id);
        model.remove(id);
        if (gesture?.id === id) {
          gesture = null;
          guides = [];
        }
        if (selected === id) clearScope();
        draw();
      },
      clear: () => {
        cardObserver.disconnect();
        for (const record of records.values()) record.controller.abort();
        gesture = null;
        viewport.classList.remove("panning");
        records.clear();
        model.clear();
        detailFocus = null;
        density = "detail";
        guides = [];
        clearScope();
        draw();
      },
      source: (local) => {
        $("source-thumbnail").hidden = local;
        $("canvas-hint").textContent = local
          ? "The current file is only available for local playback and has not been indexed yet"
          : "Drag blank space to pan · Ctrl / ⌘ + scroll wheel to zoom";
      },
      evidence: (query) => {
        $("evidence-question").textContent = query?.question || "Material preview";
      },
      fit,
    };
  }
  const api = { createModel, densityFor, start };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FrameSeekCanvas = api;
})(typeof window !== "undefined" ? window : globalThis);
