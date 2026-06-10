/**
 * MockFolio Theme Engine
 * Injects a 🎨 color-wheel picker into the nav on every page.
 * Saves the chosen hue to localStorage and applies it immediately on load.
 */
(function () {
  'use strict';

  // ── palette generation ───────────────────────────────────────────────────────
  const PRESETS = [
    { name: 'Forest',  hue: 105 },
    { name: 'Pink',    hue: 330 },
    { name: 'Purple',  hue: 270 },
    { name: 'Blue',    hue: 215 },
    { name: 'Cyan',    hue: 185 },
    { name: 'Orange',  hue: 25  },
    { name: 'Red',     hue: 0   },
    { name: 'Gold',    hue: 45  },
  ];

  function applyHue(hue) {
    const r = document.documentElement;
    r.style.setProperty('--bg',      `hsl(${hue},40%,4%)`);
    r.style.setProperty('--surface', `hsl(${hue},35%,6%)`);
    r.style.setProperty('--card',    `hsl(${hue},32%,8%)`);
    r.style.setProperty('--border',  `hsl(${hue},28%,15%)`);
    r.style.setProperty('--accent',  `hsl(${hue},70%,62%)`);
    r.style.setProperty('--green',   `hsl(${hue},60%,55%)`);
    r.style.setProperty('--muted',   `hsl(${hue},20%,48%)`);
  }

  function saveHue(h) { try { localStorage.setItem('mf_hue', h); } catch (_) {} }
  function loadHue()   { try { return parseInt(localStorage.getItem('mf_hue') || '105', 10); } catch (_) { return 105; } }

  // Apply immediately (before DOM renders to prevent flash)
  let _hue = loadHue();
  applyHue(_hue);

  // ── color wheel (canvas) ─────────────────────────────────────────────────────
  const WHEEL_SIZE = 200;
  const OUTER_R    = WHEEL_SIZE / 2 - 6;
  const INNER_R    = OUTER_R * 0.62;

  function drawWheel(canvas, hue) {
    const ctx = canvas.getContext('2d');
    const cx  = WHEEL_SIZE / 2;
    const cy  = WHEEL_SIZE / 2;
    ctx.clearRect(0, 0, WHEEL_SIZE, WHEEL_SIZE);

    // ── hue ring (360 thin slices) ────────────────────────────────────────────
    for (let deg = 0; deg < 360; deg++) {
      const a0 = (deg - 1)   * Math.PI / 180;
      const a1 = (deg + 1.5) * Math.PI / 180;
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.arc(cx, cy, OUTER_R, a0, a1);
      ctx.closePath();
      ctx.fillStyle = `hsl(${deg},80%,60%)`;
      ctx.fill();
    }

    // ── dark inner circle ─────────────────────────────────────────────────────
    ctx.beginPath();
    ctx.arc(cx, cy, INNER_R, 0, Math.PI * 2);
    const bg = getComputedStyle(document.documentElement).getPropertyValue('--card').trim() || '#101f12';
    ctx.fillStyle = bg;
    ctx.fill();

    // ── selected color preview (filled dot in center) ─────────────────────────
    ctx.beginPath();
    ctx.arc(cx, cy, INNER_R - 10, 0, Math.PI * 2);
    ctx.fillStyle = `hsl(${hue},70%,62%)`;
    ctx.fill();

    // ── label hue number in center ────────────────────────────────────────────
    ctx.fillStyle = '#fff';
    ctx.font = 'bold 13px Inter, system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(hue + '°', cx, cy);

    // ── draggable marker ──────────────────────────────────────────────────────
    const midR    = (OUTER_R + INNER_R) / 2;
    const mAngle  = (hue - 90) * Math.PI / 180;
    const mx      = cx + midR * Math.cos(mAngle);
    const my      = cy + midR * Math.sin(mAngle);

    // white ring
    ctx.beginPath();
    ctx.arc(mx, my, 10, 0, Math.PI * 2);
    ctx.strokeStyle = '#fff';
    ctx.lineWidth   = 3;
    ctx.stroke();
    // filled with hue color
    ctx.beginPath();
    ctx.arc(mx, my, 8, 0, Math.PI * 2);
    ctx.fillStyle = `hsl(${hue},80%,60%)`;
    ctx.fill();
  }

  function angleFromEvent(canvas, e) {
    const rect = canvas.getBoundingClientRect();
    const cx   = rect.left + rect.width  / 2;
    const cy   = rect.top  + rect.height / 2;
    const pt   = e.touches ? e.touches[0] : e;
    const x    = pt.clientX - cx;
    const y    = pt.clientY - cy;
    let deg    = Math.atan2(y, x) * 180 / Math.PI + 90;
    if (deg < 0)   deg += 360;
    if (deg >= 360) deg -= 360;
    return Math.round(deg);
  }

  // ── panel HTML builder ───────────────────────────────────────────────────────
  function buildPanel() {
    const panel = document.createElement('div');
    panel.id = 'themePanel';
    panel.style.cssText = `
      display:none; position:fixed; top:72px; right:16px; z-index:9999;
      background:var(--card); border:1px solid var(--border);
      border-radius:18px; padding:1.25rem 1.25rem 1rem;
      width:260px; box-shadow:0 20px 60px rgba(0,0,0,.55);
      animation:themeFadeIn .18s ease;
    `;

    const style = document.createElement('style');
    style.textContent = `
      @keyframes themeFadeIn { from{opacity:0;transform:translateY(-8px)} to{opacity:1;transform:translateY(0)} }
      #themePanel .preset-dot {
        width:30px; height:30px; border-radius:50%; cursor:pointer;
        border:2px solid transparent; transition:transform .15s,border-color .15s;
        flex-shrink:0;
      }
      #themePanel .preset-dot:hover  { transform:scale(1.15); }
      #themePanel .preset-dot.active { border-color:#fff; transform:scale(1.1); }
      #themePanel input[type=range] {
        -webkit-appearance:none; appearance:none;
        width:100%; height:5px; border-radius:3px; outline:none; cursor:pointer;
        background: linear-gradient(to right,
          hsl(0,80%,60%), hsl(45,80%,60%), hsl(90,80%,60%),
          hsl(135,80%,60%), hsl(180,80%,60%), hsl(225,80%,60%),
          hsl(270,80%,60%), hsl(315,80%,60%), hsl(360,80%,60%));
      }
      #themePanel input[type=range]::-webkit-slider-thumb {
        -webkit-appearance:none; width:16px; height:16px;
        border-radius:50%; background:#fff; border:2px solid rgba(0,0,0,.3);
        box-shadow:0 1px 4px rgba(0,0,0,.4); cursor:pointer;
      }
      #themePanel input[type=range]::-moz-range-thumb {
        width:16px; height:16px; border-radius:50%; background:#fff;
        border:2px solid rgba(0,0,0,.3); cursor:pointer;
      }
    `;
    document.head.appendChild(style);

    // ── title row ──────────────────────────────────────────────────────────────
    panel.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem;">
        <span style="font-weight:800;font-size:.95rem;color:var(--text);">🎨 Theme Color</span>
        <button id="themePanelClose" style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:1.1rem;line-height:1;padding:.1rem .3rem;">✕</button>
      </div>

      <!-- canvas wheel -->
      <div style="display:flex;justify-content:center;margin-bottom:1rem;cursor:crosshair;">
        <canvas id="hueCanvas" width="${WHEEL_SIZE}" height="${WHEEL_SIZE}" style="border-radius:50%;display:block;touch-action:none;"></canvas>
      </div>

      <!-- hue slider (backup / fine-tune) -->
      <div style="margin-bottom:1rem;">
        <div style="display:flex;justify-content:space-between;font-size:.72rem;color:var(--muted);margin-bottom:.4rem;">
          <span>Fine-tune hue</span>
          <span id="hueVal" style="color:var(--accent);font-weight:700;">${_hue}°</span>
        </div>
        <input type="range" id="hueSlider" min="0" max="359" value="${_hue}"/>
      </div>

      <!-- presets -->
      <div style="margin-bottom:1rem;">
        <div style="font-size:.72rem;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:.5rem;">Presets</div>
        <div id="presetDots" style="display:flex;flex-wrap:wrap;gap:.4rem;"></div>
      </div>

      <!-- reset -->
      <button id="themeReset" style="width:100%;padding:.4rem;background:none;border:1px solid var(--border);border-radius:8px;color:var(--muted);font-size:.78rem;cursor:pointer;transition:all .2s;">
        Reset to Default Green
      </button>
    `;

    return panel;
  }

  // ── wire everything up ───────────────────────────────────────────────────────
  function init() {
    const navLinks = document.querySelector('.nav-links');
    if (!navLinks) return;

    // inject 🎨 button before the first child of nav-links
    const btn = document.createElement('button');
    btn.id        = 'themeToggleBtn';
    btn.title     = 'Choose theme color';
    btn.innerHTML = '🎨';
    btn.style.cssText = `
      background:none; border:none; cursor:pointer;
      font-size:1.2rem; padding:.2rem .35rem; line-height:1;
      color:var(--muted); transition:color .2s; border-radius:8px;
    `;
    btn.onmouseover = () => { btn.style.color = 'var(--accent)'; btn.style.background = 'rgba(255,255,255,.06)'; };
    btn.onmouseout  = () => { btn.style.color = 'var(--muted)';  btn.style.background = 'none'; };
    navLinks.insertBefore(btn, navLinks.firstChild);

    // build & attach panel
    const panel = buildPanel();
    document.body.appendChild(panel);

    // canvas + controls refs
    const canvas  = panel.querySelector('#hueCanvas');
    const slider  = panel.querySelector('#hueSlider');
    const hueVal  = panel.querySelector('#hueVal');
    const dotsWrap = panel.querySelector('#presetDots');

    // render initial wheel
    drawWheel(canvas, _hue);

    // ── preset dots ──────────────────────────────────────────────────────────
    PRESETS.forEach(p => {
      const dot = document.createElement('div');
      dot.className = 'preset-dot' + (p.hue === _hue ? ' active' : '');
      dot.title = p.name;
      dot.style.background = `hsl(${p.hue},70%,60%)`;
      dot.onclick = () => {
        updateHue(p.hue);
        dotsWrap.querySelectorAll('.preset-dot').forEach(d => d.classList.remove('active'));
        dot.classList.add('active');
      };
      dotsWrap.appendChild(dot);
    });

    function updateHue(h) {
      _hue = ((h % 360) + 360) % 360;
      applyHue(_hue);
      saveHue(_hue);
      slider.value    = _hue;
      hueVal.textContent = _hue + '°';
      drawWheel(canvas, _hue);
    }

    // ── canvas drag ───────────────────────────────────────────────────────────
    let dragging = false;

    function onDown(e) {
      dragging = true;
      updateHue(angleFromEvent(canvas, e));
      e.preventDefault();
    }
    function onMove(e) {
      if (!dragging) return;
      updateHue(angleFromEvent(canvas, e));
      e.preventDefault();
    }
    function onUp() { dragging = false; }

    canvas.addEventListener('mousedown',  onDown);
    canvas.addEventListener('touchstart', onDown, { passive: false });
    window.addEventListener('mousemove',  onMove);
    window.addEventListener('touchmove',  onMove, { passive: false });
    window.addEventListener('mouseup',    onUp);
    window.addEventListener('touchend',   onUp);

    // ── slider (fine-tune) ────────────────────────────────────────────────────
    slider.addEventListener('input', () => {
      updateHue(parseInt(slider.value, 10));
      dotsWrap.querySelectorAll('.preset-dot').forEach(d => d.classList.remove('active'));
    });

    // ── toggle panel ─────────────────────────────────────────────────────────
    btn.onclick = (e) => {
      e.stopPropagation();
      const open = panel.style.display !== 'none' && panel.style.display !== '';
      panel.style.display = open ? 'none' : 'block';
      if (!open) drawWheel(canvas, _hue); // redraw in case card bg changed
    };

    panel.querySelector('#themePanelClose').onclick = () => { panel.style.display = 'none'; };
    panel.querySelector('#themeReset').onclick = () => {
      updateHue(105);
      dotsWrap.querySelectorAll('.preset-dot').forEach((d, i) => {
        d.classList.toggle('active', i === 0);
      });
    };

    document.addEventListener('click', (e) => {
      if (!panel.contains(e.target) && e.target !== btn) {
        panel.style.display = 'none';
      }
    });
  }

  // run after DOM
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
