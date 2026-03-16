const { app, BrowserWindow, screen, ipcMain } = require('electron');
const path = require('path');
const fs   = require('fs');

const CFG_PATH = path.join(app.getPath('userData'), 'df-widget.json');

function loadCfg() {
  try { return JSON.parse(fs.readFileSync(CFG_PATH, 'utf8')); } catch { return {}; }
}
function saveCfg(o) {
  fs.writeFileSync(CFG_PATH, JSON.stringify(o));
}

let win;

function createWindow() {
  const { width: sw, height: sh } = screen.getPrimaryDisplay().workAreaSize;
  const c = loadCfg();
  const W = c.w || 460;
  const H = c.h || 780;
  const x = c.x ?? sw - W - 20;
  const y = c.y ?? Math.round((sh - H) / 2);

  win = new BrowserWindow({
    width:  W,
    height: H,
    x, y,
    frame:       false,
    alwaysOnTop: true,
    skipTaskbar: false,
    resizable:   true,
    backgroundColor: '#111111',
    webPreferences: {
      nodeIntegration:  false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js'),
    },
  });

  win.loadURL('http://localhost:8001');

  // retry when server not yet up
  win.webContents.on('did-fail-load', () => {
    setTimeout(() => {
      if (!win.isDestroyed()) win.loadURL('http://localhost:8001');
    }, 3000);
  });

  const saveBounds = () => {
    if (win.isDestroyed()) return;
    const [x, y] = win.getPosition();
    const [w, h] = win.getSize();
    saveCfg({ x, y, w, h });
  };
  win.on('moved',   saveBounds);
  win.on('resized', saveBounds);
}

ipcMain.on('widget-close',    () => win?.close());
ipcMain.on('widget-minimize', () => win?.minimize());
ipcMain.on('widget-reload',   () => win?.webContents.reload());

app.whenReady().then(createWindow);
app.on('window-all-closed', () => app.quit());
