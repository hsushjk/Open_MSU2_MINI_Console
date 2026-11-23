
let currentPages = [];
let allWidgets = {};
let allBgEnabled = false;
let configWidgetName = '';
let currentWidgetName = '';

const TOUCH_ACTIONS = [
  ["passthrough", "未定义（透传给插件）", "通用"],
  ["next_page", "下一页", "小屏"],
  ["prev_page", "上一页", "小屏"],
  ["flip", "上下翻转", "小屏"],
  ["toggle_orientation", "横竖屏切换", "小屏"],
  ["lock", "锁定/解锁当前页", "小屏"],
  ["dim_up", "亮度 +15%", "小屏"],
  ["dim_down", "亮度 -15%", "小屏"],
  ["screenshot", "截屏", "宿主机"],
  ["mute_toggle", "静音 / 解除", "宿主机"],
  ["macro", "自定义键盘宏", "宿主机"],
  ["launch", "启动可执行文件", "宿主机"],
  ["shutdown", "关闭上位机", "宿主机"],
];

function fillTouchSelect(sel) {
  sel.innerHTML = '';
  let group = null, last = null;
  TOUCH_ACTIONS.forEach(([v, l, g]) => {
    if (g !== last) {
      group = document.createElement('optgroup');
      group.label = g || '其他';
      sel.appendChild(group);
      last = g;
    }
    const o = document.createElement('option');
    o.value = v; o.textContent = l;
    group.appendChild(o);
  });
}

function syncTouchSelects(t) {
  ['single', 'double', 'long'].forEach(k => {
    const sel = document.getElementById('tk-' + k);
    if (!sel || !sel.options.length) return;
    if (document.activeElement === sel) return;
    const v = t[k];
    if (v && sel.value !== v) sel.value = v;
  });
}

function updateArgVisibility() {
  const triples = [
    ['tk-single', 'tk-macro-single', 'tk-program-single'],
    ['tk-double', 'tk-macro-double', 'tk-program-double'],
    ['tk-long',   'tk-macro-long',   'tk-program-long'],
  ];
  triples.forEach(([sid, mid, pid]) => {
    const v = document.getElementById(sid).value;
    document.getElementById(mid).style.visibility =
      (v === 'macro') ? 'visible' : 'hidden';
    document.getElementById(pid).style.visibility =
      (v === 'launch') ? 'visible' : 'hidden';
  });
}

async function loadTouch() {
  try {
    const j = await getJSON('/api/touch_config');
    if (!j.ok) return;
    fillTouchSelect(document.getElementById('tk-single'));
    fillTouchSelect(document.getElementById('tk-double'));
    fillTouchSelect(document.getElementById('tk-long'));
    document.getElementById('tk-single').value = j.values.single;
    document.getElementById('tk-double').value = j.values.double;
    document.getElementById('tk-long').value = j.values.long;
    document.getElementById('tk-macro-single').value = j.values.macro_single || '';
    document.getElementById('tk-macro-double').value = j.values.macro_double || '';
    document.getElementById('tk-macro-long').value = j.values.macro_long || '';
    document.getElementById('tk-program-single').value = j.values.program_single || '';
    document.getElementById('tk-program-double').value = j.values.program_double || '';
    document.getElementById('tk-program-long').value = j.values.program_long || '';
    updateArgVisibility();
  } catch(e) {
    console.error(e);
  }
}

async function saveTouch() {
  const values = {
    single: document.getElementById('tk-single').value,
    double: document.getElementById('tk-double').value,
    long: document.getElementById('tk-long').value,
    macro_single: document.getElementById('tk-macro-single').value.trim(),
    macro_double: document.getElementById('tk-macro-double').value.trim(),
    macro_long: document.getElementById('tk-macro-long').value.trim(),
    program_single: document.getElementById('tk-program-single').value.trim(),
    program_double: document.getElementById('tk-program-double').value.trim(),
    program_long: document.getElementById('tk-program-long').value.trim(),
  };
  setMsg('msg-touch', '保存中...', false);
  try {
    const j = await getJSON('/api/touch_config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({values})
    });
    if (j.ok) setMsg('msg-touch', '已应用', false);
    else setMsg('msg-touch', j.err || '保存失败', true);
  } catch(e) {
    setMsg('msg-touch', '失败: ' + e, true);
  }
}


async function loadDim() {
  try {
    const j = await getJSON('/api/dim');
    const el = document.getElementById('disp-dim');
    if (el && document.activeElement !== el) {
      el.value = String(100 - (j.dim || 0));
    }
  } catch(e) {}
}

/* 写设备 NOR 的四个控件：动一个就把四个全锁住，等设备回执再放开。
 * 写一次 NOR 要二三十毫秒，期间设备不服务串口，所以这段时间也不推帧。 */
const DEV_LOCK_IDS = ['disp-dim', 'btn-orient', 'btn-rev'];
let devBusy = false;

function lockDev(text) {
  devBusy = true;
  for (const id of DEV_LOCK_IDS) {
    const el = document.getElementById(id);
    if (el) el.disabled = true;
  }
  if (text) setMsg('msg-display', text, false);
}

function unlockDev() {
  devBusy = false;
  for (const id of DEV_LOCK_IDS) {
    const el = document.getElementById(id);
    if (el) el.disabled = false;
  }
}

function devResult(j, okText, where) {
  const box = where || 'msg-display';
  if (!j || j.ok === false) {
    setMsg(box, (j && j.err) || '设置失败', true);
    return;
  }
  if (j.stored === false) {
    setMsg(box, '设备没有应答（可能没连设备）', true);
    return;
  }
  setMsg(box, okText, false);
}

async function applyDim() {
  const el = document.getElementById('disp-dim');
  if (!el) return;
  const bright = parseInt(el.value, 10);
  const dim = isNaN(bright) ? 0 : Math.max(0, Math.min(90, 100 - bright));
  try {
    const j = await postJSON('/api/dim', { dim: dim });
    setMsg('msg-display', '亮度 ' + (100 - j.dim) + '%', false);
  } catch(e) {
    setMsg('msg-display', '失败: ' + e, true);
  }
}

async function applyOrient() {
  try {
    const j = await postJSON('/api/orientation', {});
    setMsg('msg-display', (j && j.ok === false) ? '失败' : '方向已切换', false);
    loadStatus();
  } catch(e) {
    setMsg('msg-display', '失败: ' + e, true);
  }
}

async function applyReverse() {
  try {
    const j = await postJSON('/api/reverse', {});
    setMsg('msg-display', (j && j.ok === false) ? '失败' : '已翻转', false);
    loadStatus();
  } catch(e) {
    setMsg('msg-display', '失败: ' + e, true);
  }
}



async function getJSON(url, opts) {
  const r = await fetch(url, Object.assign({cache:'no-store'}, opts||{}));
  if(!r.ok) throw new Error(r.status + ' ' + r.statusText);
  return r.json();
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
  }[c]));
}

function setMsg(id, text, isErr) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.classList.toggle('err', !!isErr);
}

async function loadDevices() {
  try {
    const j = await getJSON('/api/devices');
    const sel = document.getElementById('dev-select');
    const active = j.active || '';
    sel.innerHTML = '';
    (j.devices || []).forEach(d => {
      const o = document.createElement('option');
      o.value = d.id;
      o.textContent = `${d.id}  ${d.port || 'auto'}`;
      sel.appendChild(o);
    });
    if (active) sel.value = active;
  } catch(e) {}
}

async function selectDevice() {
  const id = document.getElementById('dev-select').value;
  const j = await postJSON('/api/select', { id });
  document.getElementById('msg-dev').textContent = j.ok ? '已切换' : (j.err || '失败');
  if (j.ok) {
    await refresh();
    await loadPages();
  }
}

async function sendNotify() {
  const text = document.getElementById('notify-text').value;
  const secs = parseInt(document.getElementById('notify-secs').value || '5', 10);
  const j = await postJSON('/api/notify', { text, secs });
  document.getElementById('msg-notify').textContent =
    j.ok ? '已推送' : (j.err || '失败');
}

function setBgBlurLabel() {
  document.getElementById('bg-blur-v').textContent =
    document.getElementById('bg-blur').value;
}

async function applyBgLook() {
  const blur = parseInt(document.getElementById('bg-blur').value, 10);
  const j = await postJSON('/api/background', {
    enabled: allBgEnabled,
    path: document.getElementById('bg-path').value.trim(),
    paths: bgPaths(),
    mode: document.getElementById('bg-mode').value,
    blur,
  });
  document.getElementById('msg-bg').textContent =
    j.ok ? `模糊 ${j.blur}` : (j.err || '失败');
}

function bgSource() {
  const el = document.getElementById('bg-source');
  return el ? el.value : 'single';
}

function setBgSource() {
  const pl = bgSource() === 'playlist';
  const pw = document.getElementById('bg-iv-wrap');
  const sw = document.getElementById('bg-sh-wrap');
  const single = document.getElementById('bg-single-wrap');
  const multi = document.getElementById('bg-multi-wrap');
  if (pw) pw.style.display = pl ? '' : 'none';
  if (sw) sw.style.display = pl ? '' : 'none';
  if (single) single.style.display = pl ? 'none' : '';
  if (multi) multi.style.display = pl ? '' : 'none';
}

function bgInterval() {
  const el = document.getElementById('bg-interval');
  return el ? parseInt(el.value || '12', 10) : 12;
}

function bgShuffle() {
  const el = document.getElementById('bg-shuffle');
  return !!(el && el.checked);
}

function bgPaths() {
  return document.getElementById('bg-paths').value
    .split('\n').map(s => s.trim()).filter(s => s.length > 0);
}

async function refresh() {
  try {
    const j = await getJSON('/api/status');
    document.getElementById('status').textContent =
      JSON.stringify(j, null, 2);
    document.getElementById('btn-orient').textContent =
      j.orientation === 'portrait' ? '切换到横屏' : '切换到竖屏';
    document.getElementById('btn-rev').textContent =
      j.reverse ? '取消翻转' : '上下翻转';
    if (document.activeElement !== document.getElementById('bg-path')) {
      document.getElementById('bg-path').value = j.background_path || '';
    }
    const bgBtn = document.getElementById('btn-bg');
    bgBtn.textContent = j.background_enabled ? '关闭背景' : '启用背景';
    bgBtn.classList.toggle('active', !!j.background_enabled);
    allBgEnabled = !!j.background_enabled;
    const bgModeEl = document.getElementById('bg-mode');
    if (bgModeEl && document.activeElement !== bgModeEl) {
      bgModeEl.value = j.background_mode || 'fit';
    }
    const dimEl = document.getElementById('disp-dim');
    if (dimEl && document.activeElement !== dimEl) {
      dimEl.value = String(100 - (j.dim || 0));
    }
    const blurEl = document.getElementById('bg-blur');
    if (blurEl && document.activeElement !== blurEl) {
      blurEl.value = j.background_blur || 0;
      setBgBlurLabel();
    }
    const pathsEl = document.getElementById('bg-paths');
    if (pathsEl && document.activeElement !== pathsEl) {
      const cur = (j.background_paths || []).join('\n');
      if (pathsEl.value.trim() !== cur.trim()) pathsEl.value = cur;
    }
    const sel = document.getElementById('dev-select');
    if (sel && j.active && document.activeElement !== sel) {
      sel.value = j.active;
    }
    const m = j.metrics || {};
    if (m.fps !== undefined) {
      const rk = Object.keys(m.render_ms || {})
        .map(k => `${k} ${m.render_ms[k]}ms`).join('  ');
      document.getElementById('metrics-line').textContent =
        `FPS ${m.fps}   吞吐 ${m.bytes_per_sec} B/s   帧 ${m.frames}   ` +
        `重连 ${m.reconnects}   渲染 ${rk}`;
    }
    const extEl = document.getElementById('ext-line');
    if (extEl) {
      const ef = j.ext_failed || [];
      extEl.textContent = ef.length
        ? `外部插件加载失败 ${ef.length} 个：` + ef.map(function (x) {
            const p = String(x[0] || '').split(/[\\/]/).pop();
            return p + '（' + (x[1] || '') + '）';
          }).join('   ')
        : '';
    }
    syncTouchSelects(j.touch || {});
    const pv = document.getElementById('dev-preview');
    if (pv && j.current_widget) {
      const want = '/api/preview?name=' + encodeURIComponent(j.current_widget) + '&t=' + Date.now();
      if (pv.dataset.want !== want) { pv.dataset.want = want; pv.src = want; }
    }
    const srcEl = document.getElementById('bg-source');
    if (srcEl && document.activeElement !== srcEl) {
      srcEl.value = j.background_source || 'single';
      setBgSource();
    }
    const pthEl = document.getElementById('bg-paths');
    if (pthEl && document.activeElement !== pthEl) {
      pthEl.value = (j.background_paths || []).join('\n');
    }
    const ivEl = document.getElementById('bg-interval');
    if (ivEl && document.activeElement !== ivEl) {
      ivEl.value = j.background_interval || 12;
    }
    const shEl = document.getElementById('bg-shuffle');
    if (shEl && document.activeElement !== shEl) {
      shEl.checked = !!j.background_shuffle;
    }

    currentWidgetName = j.current_widget || '';
    document.querySelectorAll('#pages-list .pitem').forEach(el => {
      el.classList.toggle('current', el.dataset.widget === currentWidgetName);
    });
  } catch(e) {
    document.getElementById('status').textContent = '状态获取失败: ' + e;
  }
}

async function loadPages() {
  const j = await getJSON('/api/widgets');
  allWidgets = {};
  (j.widgets || []).forEach(w => { allWidgets[w.name] = w; });
  currentPages = (j.active || []).slice();
  renderPages();
}

function renderPages() {
  const box = document.getElementById('pages-list');
  closeActMenu();
  box.innerHTML = '';

  if (currentPages.length === 0) {
    const e = document.createElement('div');
    e.className = 'pitem empty';
    e.textContent = '未启用任何页面';
    box.appendChild(e);
  }

  currentPages.forEach((name, i) => {
    const w = allWidgets[name] || { orientations: [], has_config: false, actions: {} };
    const row = document.createElement('div');
    row.className = 'pitem';
    row.dataset.widget = name;
    row.draggable = true;
    if (name === currentWidgetName) row.classList.add('current');

    row.addEventListener('dragstart', ev => {
      dragIndex = i;
      ev.dataTransfer.effectAllowed = 'move';
      try { ev.dataTransfer.setData('text/plain', name); } catch(e) {}
    });
    row.addEventListener('dragend', () => { dragIndex = null; });
    row.addEventListener('dragover', ev => {
      ev.preventDefault();
      ev.dataTransfer.dropEffect = 'move';
      if (dragIndex !== null && dragIndex !== i) row.style.borderTop = '2px solid #2b5fd9';
    });
    row.addEventListener('dragleave', () => { row.style.borderTop = ''; });
    row.addEventListener('drop', ev => {
      ev.preventDefault();
      row.style.borderTop = '';
      dragMove(i);
    });
    row.addEventListener('dblclick', ev => {
      if (ev.target.closest('button')) return;
      switchTo(name);
    });

    const cfgBtn = '';
    const actCount = (w.has_config ? 1 : 0) +
                     Object.keys(w.actions || {}).length;
    const actBtn = actCount > 0
      ? `<button class="mini actbtn" title="插件动作" ` +
        `onclick="toggleActMenu(event,'${esc(name)}')">⋯</button>`
      : '';

    const nameTag = w.source === 'external'
      ? `<span class="name" title="双击切换到该页">${esc(name)}</span>` +
        `<span class="tag-ext" title="外挂插件">外挂</span>`
      : `<span class="name" title="双击切换到该页">${esc(name)}</span>`;

    row.innerHTML =
      `<button class="mini" onclick="moveUp(${i})" ${i===0?'disabled':''}>↑</button>` +
      `<button class="mini" onclick="moveDown(${i})" ${i===currentPages.length-1?'disabled':''}>↓</button>` +
      `<button class="mini" onclick="removeAt(${i})">×</button>` +
      cfgBtn +
      nameTag +
      actBtn +
      `<span class="tags">[${(w.orientations||[]).join('/')}]</span>`;
    box.appendChild(row);
  });

  const disabled = Object.keys(allWidgets).filter(n => !currentPages.includes(n));
  if (disabled.length > 0) {
    const sep = document.createElement('div');
    sep.className = 'pitem';
    sep.style.borderTop = '1px dashed #ccc';
    sep.style.color = '#999';
    sep.style.fontSize = '11px';
    sep.textContent = '已停用';
    box.appendChild(sep);
    disabled.forEach(name => {
      const w = allWidgets[name] || { orientations: [] };
      const row = document.createElement('div');
      row.className = 'pitem';
      row.style.opacity = '0.6';
      row.innerHTML =
        `<button class="mini" onclick="addWidget('${esc(name)}')">+</button>` +
        `<span class="name">${esc(name)}</span>` +
        `<span class="tags">[${(w.orientations||[]).join('/')}]</span>`;
      box.appendChild(row);
    });
  }
}

let actMenu = null;
let dragIndex = null;

function closeActMenu() {
  if (!actMenu) return;
  actMenu.remove();
  actMenu = null;
}

function placeActMenu(box, btn) {
  const r = btn.getBoundingClientRect();
  const bw = box.offsetWidth, bh = box.offsetHeight;
  const sx = window.scrollX || window.pageXOffset || 0;
  const sy = window.scrollY || window.pageYOffset || 0;
  let left = r.right + sx + 6;
  if (left + bw > sx + window.innerWidth - 8) left = r.left + sx - bw - 6;
  if (left < sx + 8) left = sx + Math.max(8, window.innerWidth - bw - 8);
  let top = r.top + sy + r.height / 2 - bh / 2;
  if (top < sy + 8) top = sy + 8;
  box.style.left = left + 'px';
  box.style.top = top + 'px';
}

function toggleActMenu(ev, name) {
  ev.stopPropagation();
  if (ev.preventDefault) ev.preventDefault();
  const btn = ev.currentTarget;
  const same = actMenu && actMenu.dataset.widget === name;
  closeActMenu();
  if (same) return;
  const w = allWidgets[name] || {};
  const entries = [];
  if (w.has_config) entries.push(['__cfg__', '设置']);
  Object.entries(w.actions || {}).forEach(([a, label]) => entries.push([a, label]));
  if (!entries.length) return;
  const box = document.createElement('div');
  box.className = 'actpop';
  box.dataset.widget = name;
  const ttl = document.createElement('div');
  ttl.className = 'ttl';
  ttl.textContent = name;
  box.appendChild(ttl);
  entries.forEach(([act, label]) => {
    const b = document.createElement('button');
    b.textContent = label;
    b.onclick = () => {
      if (act === '__cfg__') openConfig(name); else doAction(name, act);
    };
    box.appendChild(b);
  });
  document.body.appendChild(box);
  placeActMenu(box, btn);
  actMenu = box;
}

async function pushPages(newPages) {
  try {
    const j = await getJSON('/api/pages', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({pages: newPages})
    });
    if (j.ok) {
      currentPages = j.pages.slice();
      renderPages();
      setMsg('msg-pages', '已更新', false);
    } else {
      setMsg('msg-pages', j.err || '被拒绝', true);
    }
  } catch(e) {
    setMsg('msg-pages', '失败: ' + e, true);
  }
  setTimeout(refresh, 200);
}

function moveUp(i) {
  if (i <= 0) return;
  const p = currentPages.slice();
  [p[i-1], p[i]] = [p[i], p[i-1]];
  pushPages(p);
}
function moveDown(i) {
  if (i >= currentPages.length - 1) return;
  const p = currentPages.slice();
  [p[i+1], p[i]] = [p[i], p[i+1]];
  pushPages(p);
}
function removeAt(i) {
  const p = currentPages.slice();
  p.splice(i, 1);
  pushPages(p);
}
function addWidget(name) {
  const p = currentPages.slice();
  p.push(name);
  pushPages(p);
}
function dragMove(target) {
  if (dragIndex === null || target === null) return;
  if (dragIndex === target) return;
  if (dragIndex < 0 || dragIndex >= currentPages.length) return;
  const p = currentPages.slice();
  const [item] = p.splice(dragIndex, 1);
  p.splice(target, 0, item);
  dragIndex = null;
  pushPages(p);
}
async function switchTo(name) {
  try {
    const j = await getJSON('/api/switch', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name})
    });
    if (j.ok) setMsg('msg-pages', '已切换到 ' + name, false);
    else setMsg('msg-pages', j.err || '切换失败', true);
  } catch(e) {
    setMsg('msg-pages', '切换失败: ' + e, true);
  }
  setTimeout(refresh, 200);
}
async function setBackgroundMode() {
  const modeEl = document.getElementById('bg-mode');
  const mode = modeEl ? modeEl.value : 'fit';
  const path = (document.getElementById('bg-path') || {}).value || '';
  const enabled = !!allBgEnabled;
  try {
    const j = await getJSON('/api/background', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled: enabled, path: path.trim(), mode: mode})
    });
    if (j.ok) setMsg('msg-bg', '显示方式已更新', false);
    else setMsg('msg-bg', j.err || '被拒绝', true);
  } catch(e) {
    setMsg('msg-bg', '失败: ' + e, true);
  }
  setTimeout(refresh, 200);
}

async function doAction(name, action) {
  setMsg('msg-pages', '执行中...', false);
  try {
    const j = await getJSON('/api/widget_action', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, action, payload: {}})
    });
    if (j.ok) setMsg('msg-pages', '已执行', false);
    else setMsg('msg-pages', j.err || '失败', true);
  } catch(e) {
    setMsg('msg-pages', '失败: ' + e, true);
  }
  setTimeout(refresh, 200);
}

async function openConfig(name) {
  try {
    const j = await getJSON(`/api/widget_config?name=${encodeURIComponent(name)}`);
    if (!j.ok) { alert(j.err || '加载失败'); return; }
    configWidgetName = name;

    document.getElementById('config-title').textContent = `配置: ${name}`;
    const hint = document.getElementById('config-hint');
    if (hint) {
      const parts = [];
      if (j.touch_help) parts.push('透传触控：' + j.touch_help);
      hint.textContent = parts.length ? parts.join('　|　') : '';
      hint.style.display = parts.length ? 'block' : 'none';
    }
    const box = document.getElementById('config-body');
    box.innerHTML = '';

    if (!j.schema || j.schema.length === 0) {
      box.innerHTML = '<div style="color:#999">此插件暂无配置项</div>';
      document.getElementById('config-panel').style.display = 'block';
      return;
    }

    j.schema.forEach(f => {
      const v = j.values[f.key];
      const div = document.createElement('div');
      div.className = 'field';

      const lbl = document.createElement('label');
      lbl.textContent = f.label;
      if (f.help) {
        const h = document.createElement('span');
        h.className = 'help';
        h.textContent = '(' + f.help + ')';
        lbl.appendChild(h);
      }
      div.appendChild(lbl);

      let input;
      if (f.type === 'bool') {
        input = document.createElement('input');
        input.type = 'checkbox'; input.checked = !!v;
      } else if (f.type === 'select') {
        input = document.createElement('select');
        (f.options || []).forEach(o => {
          const opt = document.createElement('option');
          opt.value = o; opt.textContent = o;
          if (o === v) opt.selected = true;
          input.appendChild(opt);
        });
      } else if (f.type === 'list_str') {
        input = document.createElement('textarea');
        input.rows = 4;
        input.value = (Array.isArray(v) ? v : []).join('\n');
      } else if (f.type === 'int' || f.type === 'float') {
        input = document.createElement('input');
        input.type = 'number';
        input.value = v ?? '';
        if (f.min != null) input.min = f.min;
        if (f.max != null) input.max = f.max;
        if (f.step != null) input.step = f.step;
      } else {
        input = document.createElement('input');
        input.type = 'text';
        input.value = v ?? '';
      }
      input.dataset.key = f.key;
      input.dataset.type = f.type;
      div.appendChild(input);
      box.appendChild(div);
    });

    document.getElementById('config-panel').style.display = 'block';
    document.getElementById('config-panel').scrollIntoView({
      behavior: 'smooth', block: 'start' });
    setMsg('msg-config', '', false);
  } catch(e) {
    alert('加载配置失败: ' + e);
  }
}

async function saveConfig() {
  if (!configWidgetName) return;
  const box = document.getElementById('config-body');
  const values = {};
  box.querySelectorAll('input,select,textarea').forEach(el => {
    const k = el.dataset.key;
    const t = el.dataset.type;
    if (t === 'bool') values[k] = el.checked;
    else if (t === 'int') values[k] = parseInt(el.value || 0, 10);
    else if (t === 'float') values[k] = parseFloat(el.value || 0);
    else if (t === 'list_str') {
      values[k] = el.value.split('\n').map(s => s.trim()).filter(Boolean);
    } else {
      values[k] = el.value;
    }
  });
  setMsg('msg-config', '保存中...', false);
  try {
    const j = await getJSON('/api/widget_config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: configWidgetName, values})
    });
    if (j.ok) setMsg('msg-config', '已保存', false);
    else setMsg('msg-config', j.err || '保存失败', true);
  } catch(e) {
    setMsg('msg-config', '失败: ' + e, true);
  }
}

function closeConfig() {
  document.getElementById('config-panel').style.display = 'none';
}

async function postJSON(path, body) {
  try {
    return await getJSON(path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body || {}),
    });
  } catch(e) {
    return {ok: false, err: String(e)};
  }
}

async function post(path) {
  try {
    const j = await getJSON(path, {method:'POST'});
    if (j.ok === false) setMsg('msg-display', j.err || '拒绝', true);
    else setMsg('msg-display', '已应用', false);
  } catch(e) {
    setMsg('msg-display', '失败: ' + e, true);
  }
  setTimeout(refresh, 250);
}

async function toggleBackground() {
  const btn = document.getElementById('btn-bg');
  const currentlyOn = btn.classList.contains('active');
  const next = !currentlyOn;
  const path = document.getElementById('bg-path').value.trim();
  const modeEl = document.getElementById('bg-mode');
  const mode = modeEl ? modeEl.value : 'fit';
  const paths = bgPaths();
  const blur = parseInt(document.getElementById('bg-blur').value, 10);
  const source = bgSource();
  const interval = bgInterval();
  const shuffle = bgShuffle();

  if (next && source === 'single' && !path) {
    setMsg('msg-bg', '请先填写图片路径', true);
    return;
  }
  if (next && source === 'playlist' && paths.length === 0) {
    setMsg('msg-bg', '请先填写图片路径', true);
    return;
  }
  setMsg('msg-bg', '应用中...', false);
  try {
    const j = await getJSON('/api/background', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled: next, path, paths, mode, blur,
                           source, interval, shuffle})
    });
    if (j.ok) setMsg('msg-bg', next ? '已启用' : '已关闭', false);
    else setMsg('msg-bg', j.err || '被拒绝', true);
  } catch(e) {
    setMsg('msg-bg', '失败: ' + e, true);
  }
  setTimeout(refresh, 250);
}

function assetSlotIndex() {
  return (parseInt(document.getElementById('asset-slot').value || '1', 10) || 1) - 1;
}

async function refreshAsset() {
  const j = await getJSON('/api/asset');
  if (!j || !j.ok) { setMsg('msg-asset', '读取失败', true); return; }
  document.getElementById('asset-boot').value = String(j.boot_mode || 0);
  document.getElementById('asset-standby').value = String(j.standby_mode);
  document.getElementById('asset-count').value = String(j.count || 1);
  document.getElementById('asset-dwell').value = String(j.dwell || 3);
  const d = j.device;
  const bhEl = document.getElementById('asset-boot-hold');
  if (bhEl) bhEl.value = String((d && d.boot_hold) || j.boot_hold || 4);
  if (!d) { document.getElementById('asset-state').textContent = '设备没有回应资产查询（固件需要 v32+）'; return; }
  const slots = [];
  for (let i = 0; i < 8; i++) if (d.standby_mask & (1 << i)) slots.push(i + 1);
  document.getElementById('asset-state').textContent =
    '设备上：开机图 ' + (d.boot_valid ? '有' : '无') +
    '；待机图 ' + (slots.length ? ('第 ' + slots.join('/') + ' 张') : '无') +
    '；当前 ' + (d.boot_mode ? '开机画图' : '无开机画面') + ' / ' + d.standby_mode;
}

async function saveAssetSettings() {
  const body = {
    boot_mode: parseInt(document.getElementById('asset-boot').value, 10) || 0,
    standby_mode: parseInt(document.getElementById('asset-standby').value, 10),
    count: parseInt(document.getElementById('asset-count').value, 10) || 1,
    dwell: parseInt(document.getElementById('asset-dwell').value, 10) || 3,
    boot_hold: parseInt(document.getElementById('asset-boot-hold').value, 10) || 4,
  };
  const j = await postJSON('/api/asset/settings', body);
  setMsg('msg-asset', j && j.ok ? '已保存到设备' : ('失败：' + ((j && j.err) || '设备没有确认')), !(j && j.ok));
  if (j && j.ok) refreshAsset();
}

async function uploadAsset(kind, slot) {
  const id = kind ? 'asset-standby-path' : 'asset-boot-path';
  const msg = kind ? 'msg-asset-standby' : 'msg-asset-boot';
  const path = (document.getElementById(id).value || '').trim();
  if (!path) { setMsg(msg, '先填图片路径', true); return; }
  setMsg(msg, '正在写入设备…', false);
  const j = await postJSON('/api/asset/upload', { kind: kind, slot: slot, path: path });
  setMsg(msg, j && j.ok ? '已写入' : ('失败：' + ((j && j.err) || '未知错误')), !(j && j.ok));
  if (j && j.ok) refreshAsset();
}

function uploadStandby() { uploadAsset(1, assetSlotIndex()); }

async function drawAsset(kind, slot) {
  const j = await postJSON('/api/asset/draw', { kind: kind, slot: slot });
  setMsg(kind ? 'msg-asset-standby' : 'msg-asset-boot',
         j && j.ok ? '已显示' : ('失败：' + ((j && j.err) || '没有这张图')), !(j && j.ok));
}

async function eraseStandby() {
  const j = await postJSON('/api/asset/erase', { kind: 1, slot: assetSlotIndex() });
  setMsg('msg-asset-standby', j && j.ok ? '已擦除' : '擦除失败', !(j && j.ok));
  refreshAsset();
}

async function shutdownApp() {
  if (!confirm('确定要关闭 MSU2_MINI 上位机吗？\n关闭后需要重新启动才能继续使用。')) {
    return;
  }
  setMsg('msg-shutdown', '正在关闭...', false);
  try {
    const r = await fetch('/api/shutdown', {method:'POST'});
    const j = await r.json();
    if (j.ok) {
      setMsg('msg-shutdown', '已发送关闭指令，窗口即将失效', false);
      setTimeout(() => {
        document.body.innerHTML =
          '<div style="text-align:center; margin-top:8em; color:#666;' +
          ' font-family:Consolas,monospace">' +
          '<h1 style="color:#a11">程序已关闭</h1>' +
          '<p>可以关闭此页面，或重新启动 MSU2_MINI 后再刷新。</p></div>';
      }, 800);
    } else {
      setMsg('msg-shutdown', j.err || '失败', true);
    }
  } catch(e) {
    setMsg('msg-shutdown', '程序已关闭', false);
    setTimeout(() => {
      document.body.innerHTML =
        '<div style="text-align:center; margin-top:8em; color:#666;' +
        ' font-family:Consolas,monospace">' +
        '<h1 style="color:#a11">程序已关闭</h1>' +
        '<p>可以关闭此页面，或重新启动 MSU2_MINI 后再刷新。</p></div>';
    }, 800);
  }
}

loadTouch();
loadDim(); refreshAsset();
loadDevices();
setBgSource();
loadPages();
refresh();
setInterval(refresh, 1000);
setInterval(loadDevices, 5000);
