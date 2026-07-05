/**
 * MockFolio UI runtime
 * - Market-hours badge in the nav
 * - Mobile bottom navigation (app pages only)
 */
(function () {
  'use strict';

  // Drop any theme override saved by the old color-picker build.
  try { localStorage.removeItem('mf_hue'); } catch (_) {}

  // ── market hours badge ───────────────────────────────────────────────────────
  function marketStatus() {
    const now = new Date();
    // Get current time in America/New_York (handles EST/EDT automatically)
    const et = new Date(now.toLocaleString('en-US', { timeZone: 'America/New_York' }));
    const day  = et.getDay();   // 0=Sun, 6=Sat
    const h    = et.getHours();
    const m    = et.getMinutes();
    const mins = h * 60 + m;    // minutes since midnight ET

    const isWeekday  = day >= 1 && day <= 5;
    const inSession  = mins >= 9 * 60 + 30 && mins < 16 * 60;   // 9:30–16:00
    const isOpen     = isWeekday && inSession;

    // Time until open / close
    let nextLabel = '';
    if (isOpen) {
      const minsLeft = (16 * 60) - mins;
      nextLabel = `closes in ${Math.floor(minsLeft / 60)}h ${minsLeft % 60}m`;
    } else if (isWeekday && mins < 9 * 60 + 30) {
      const minsLeft = (9 * 60 + 30) - mins;
      nextLabel = `opens in ${Math.floor(minsLeft / 60)}h ${minsLeft % 60}m`;
    } else {
      // Weekend or after close — find next Monday (or next day)
      const daysUntilMon = day === 0 ? 1 : (day === 6 ? 2 : 1);
      nextLabel = `opens ${daysUntilMon === 1 ? 'tomorrow' : 'Monday'} 9:30 AM ET`;
    }

    return { isOpen, nextLabel };
  }

  function injectMarketBadge(navLinks) {
    const badge = document.createElement('div');
    badge.id = 'marketBadge';
    badge.title = 'US Stock Market — 9:30 AM – 4:00 PM ET, Mon–Fri';
    badge.style.cssText = `
      display:flex; align-items:center; gap:.35rem;
      font-size:.72rem; font-weight:700; letter-spacing:.04em;
      padding:.25rem .65rem; border-radius:99px; cursor:default;
      border:1px solid; white-space:nowrap; user-select:none;
    `;

    function refresh() {
      const { isOpen, nextLabel } = marketStatus();
      badge.style.background   = isOpen ? 'var(--accent-dim)' : 'rgba(255,255,255,.05)';
      badge.style.borderColor  = isOpen ? 'var(--accent-brd)' : 'rgba(255,255,255,.12)';
      badge.style.color        = isOpen ? 'var(--accent)'        : 'var(--muted)';
      badge.innerHTML = `
        <span style="width:7px;height:7px;border-radius:50%;background:${isOpen ? 'var(--accent)' : 'var(--muted)'};
          ${isOpen ? 'box-shadow:0 0 6px var(--accent);animation:mktPulse 1.8s ease-in-out infinite;' : ''}
          display:inline-block;flex-shrink:0;"></span>
        ${isOpen ? 'MARKET OPEN' : 'MARKET CLOSED'}
        <span style="font-weight:400;opacity:.7;font-size:.68rem;">${nextLabel}</span>
      `;
    }

    // Add pulse keyframe once
    if (!document.getElementById('mktStyle')) {
      const s = document.createElement('style');
      s.id = 'mktStyle';
      s.textContent = `@keyframes mktPulse { 0%,100%{opacity:1} 50%{opacity:.4} }`;
      document.head.appendChild(s);
    }

    refresh();
    setInterval(refresh, 30_000); // update every 30s
    navLinks.insertBefore(badge, navLinks.firstChild);
  }

  // ── mobile bottom nav ────────────────────────────────────────────────────────
  function injectMobileNav() {
    // Already injected (e.g. navigating via SPA)
    if (document.getElementById('mobileNav')) return;

    const path = window.location.pathname.replace(/\/$/, '') || '/';

    // Only show the app nav on app pages — not on the landing/auth pages.
    const PUBLIC = ['/', '/login', '/register', '/forgot-password', '/reset-password'];
    if (PUBLIC.some(p => path === p || (p !== '/' && path.startsWith(p)))) return;

    const PRIMARY = [
      { label: 'Home',      icon: 'icon-home',      href: '/dashboard' },
      { label: 'Trade',     icon: 'icon-trade',     href: '/trade'     },
      { label: 'Signals',   icon: 'icon-signals',   href: '/signals'   },
      { label: 'Autopilot', icon: 'icon-autopilot', href: '/autopilot' },
    ];
    const MORE = [
      { label: 'Options',      icon: 'icon-options',  href: '/options'    },
      { label: 'Market Calls', icon: 'icon-calls',    href: '/calls'      },
      { label: 'Activity Log', icon: 'icon-activity', href: '/activity'   },
      { label: 'AI Tutor',     icon: 'icon-learn',    href: '/learn'      },
      { label: 'Flashcards',   icon: 'icon-cards',    href: '/flashcards' },
    ];

    const isMorePage = MORE.some(m => path === m.href || path.startsWith(m.href + '/'));

    // ── bottom nav bar ──────────────────────────────────────────────────────
    const nav = document.createElement('nav');
    nav.id = 'mobileNav';

    PRIMARY.forEach(item => {
      const a    = document.createElement('a');
      a.href     = item.href;
      const active = path === item.href || path.startsWith(item.href + '/');
      if (active) a.classList.add('active');
      a.innerHTML = `<span class="icon ${item.icon} mnav-icon"></span><span>${item.label}</span>`;
      nav.appendChild(a);
    });

    const moreBtn = document.createElement('button');
    if (isMorePage) moreBtn.classList.add('active');
    moreBtn.innerHTML = `<span class="icon icon-menu mnav-icon"></span><span>More</span>`;
    nav.appendChild(moreBtn);

    // ── overlay ──────────────────────────────────────────────────────────────
    const overlay = document.createElement('div');
    overlay.id = 'mobileMoreOverlay';

    // ── slide-up sheet ───────────────────────────────────────────────────────
    const sheet = document.createElement('div');
    sheet.id = 'mobileMoreSheet';
    const title = document.createElement('div');
    title.className = 'sheet-title';
    title.textContent = 'More';
    sheet.appendChild(title);

    MORE.forEach(item => {
      const a  = document.createElement('a');
      a.href   = item.href;
      if (path === item.href) a.classList.add('active');
      a.innerHTML = `<span class="icon ${item.icon} sheet-icon"></span>${item.label}`;
      sheet.appendChild(a);
    });

    function openSheet()  { sheet.classList.add('open');    overlay.classList.add('open');    }
    function closeSheet() { sheet.classList.remove('open'); overlay.classList.remove('open'); }

    moreBtn.addEventListener('click', () =>
      sheet.classList.contains('open') ? closeSheet() : openSheet()
    );
    overlay.addEventListener('click', closeSheet);

    document.body.appendChild(nav);
    document.body.appendChild(overlay);
    document.body.appendChild(sheet);
  }

  // ── wire everything up ───────────────────────────────────────────────────────
  function init() {
    const navLinks = document.querySelector('.nav-links');
    if (!navLinks) return;

    injectMarketBadge(navLinks);
    injectMobileNav();
  }

  // run after DOM
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
