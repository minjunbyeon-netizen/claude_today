const { ipcRenderer } = require('electron');

window.addEventListener('DOMContentLoaded', () => {
  // ── Drag bar ──────────────────────────────────────────────
  const bar = document.createElement('div');
  bar.id = '__wbar';
  bar.style.cssText = [
    'position:fixed',
    'top:0','left:0','right:0',
    'height:30px',
    'background:#0d0d0d',
    'border-bottom:1px solid #222',
    'display:flex',
    'align-items:center',
    'justify-content:space-between',
    'padding:0 12px',
    'z-index:2147483647',
    '-webkit-app-region:drag',
    'user-select:none',
    'box-sizing:border-box',
  ].join(';');

  bar.innerHTML = `
    <span style="font-size:11px;color:#444;font-family:-apple-system,Helvetica Neue,sans-serif;letter-spacing:.04em">daily-focus</span>
    <div style="-webkit-app-region:no-drag;display:flex;align-items:center;gap:4px">
      <button id="__wreload"  title="새로고침" style="${btnStyle('#444')}">↻</button>
      <button id="__wmin"     title="최소화"   style="${btnStyle('#444')}">−</button>
      <button id="__wclose"   title="닫기"     style="${btnStyle('#555')}">✕</button>
    </div>
  `;

  document.body.prepend(bar);

  // push page content below bar
  const spacer = document.createElement('div');
  spacer.style.cssText = 'height:30px;flex-shrink:0';
  document.body.insertBefore(spacer, bar.nextSibling);

  document.getElementById('__wreload').onclick = () => ipcRenderer.send('widget-reload');
  document.getElementById('__wmin').onclick    = () => ipcRenderer.send('widget-minimize');
  document.getElementById('__wclose').onclick  = () => ipcRenderer.send('widget-close');

  // hover effect
  ['__wreload','__wmin','__wclose'].forEach(id => {
    const btn = document.getElementById(id);
    btn.addEventListener('mouseenter', () => { btn.style.color = '#fff'; });
    btn.addEventListener('mouseleave', () => { btn.style.color = '#444'; });
  });
});

function btnStyle(color) {
  return [
    'background:none',
    'border:none',
    `color:${color}`,
    'cursor:pointer',
    'font-size:14px',
    'padding:0 5px',
    'line-height:1',
    'transition:color .15s',
  ].join(';');
}
