const { app, BrowserWindow, Tray, Menu, nativeImage, dialog } = require('electron');
const { spawn, spawnSync } = require('child_process');
const path = require('path');
const http = require('http');

const PORT = 8000;
let mainWindow = null;
let tray = null;
let backendProcess = null;

// â”€â”€ Detect available Python executable â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
function findPython() {
  const candidates = process.platform === 'win32'
    ? [
        path.join(__dirname, '..', '.venv', 'Scripts', 'python.exe'),
        'C:\\Users\\hp\\AppData\\Local\\Programs\\Python\\Python312\\python.exe',
        'python',
        'py',
        'python3'
      ]
    : ['python3', 'python'];

  for (const cmd of candidates) {
    try {
      const result = spawnSync(cmd, ['--version'], { timeout: 3000 });
      if (result.status === 0 || (result.stderr && result.stderr.toString().startsWith('Python'))) {
        console.log(`[backend] Using Python command: ${cmd}`);
        return cmd;
      }
    } catch (e) {
      // not found, try next
    }
  }
  return null;
}

// â”€â”€ Launch FastAPI backend â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
function startBackend() {
  const projectDir = path.join(__dirname, '..');
  const pythonCmd = findPython();

  console.log('PATH=', process.env.PATH);
  console.log('Launching backend with:', pythonCmd);

  if (!pythonCmd) {
    dialog.showErrorBox('DocRAG â€” Python Not Found',
      'Could not find a Python installation.\n\n' +
      'Please install Python 3.8+ from https://www.python.org/downloads/\n' +
      'and make sure it is added to your PATH.\n\n' +
      'Then restart DocRAG.'
    );
    app.quit();
    return;
  }

  backendProcess = spawn(pythonCmd, [
    '-m', 'uvicorn',
    'backend.main:app',
    '--host', '127.0.0.1',
    '--port', String(PORT),
  ], {
    cwd: projectDir,
    env: { ...process.env, PYTHONUNBUFFERED: '1' },
  });

  backendProcess.stdout.on('data', d => console.log('[backend]', d.toString().trim()));
  backendProcess.stderr.on('data', d => console.error('[backend]', d.toString().trim()));
  backendProcess.on('exit', code => console.log(`[backend] exited with code ${code}`));
}

// â”€â”€ Poll until backend responds â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
function waitForBackend(retries = 30, delay = 1000) {
  return new Promise((resolve, reject) => {
    let attempts = 0;
    const check = () => {
      const req = http.get(`http://127.0.0.1:${PORT}/docs`, res => {
        res.statusCode < 500 ? resolve() : retry();
      });
      req.on('error', retry);
      req.end();
    };
    const retry = () => {
      if (++attempts >= retries) reject(new Error('Backend did not start in time.'));
      else setTimeout(check, delay);
    };
    check();
  });
}

// â”€â”€ Main window â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 820,
    minWidth: 800,
    minHeight: 600,
    title: 'DocRAG',
    backgroundColor: '#0b0c0f',
    webPreferences: { contextIsolation: true, nodeIntegration: false },
    titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'default',
    show: false,
  });

  mainWindow.loadURL(`http://127.0.0.1:${PORT}`);
  mainWindow.once('ready-to-show', () => mainWindow.show());
  mainWindow.on('closed', () => { mainWindow = null; });

  mainWindow.on('close', e => {
    if (!app.isQuiting) { e.preventDefault(); mainWindow.hide(); }
  });
}

// â”€â”€ System tray â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
function createTray() {
  const icon = nativeImage.createEmpty();
  tray = new Tray(icon);
  tray.setToolTip('DocRAG â€” Document Search');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Open DocRAG', click: () => mainWindow && mainWindow.show() },
    { type: 'separator' },
    { label: 'Quit', click: () => { app.isQuiting = true; app.quit(); } },
  ]));
  tray.on('double-click', () => mainWindow && mainWindow.show());
}

// â”€â”€ Lifecycle â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
app.whenReady().then(async () => {
  startBackend();
  createTray();

  try {
    await waitForBackend();
  } catch (err) {
    dialog.showErrorBox('DocRAG â€” Startup Failed',
      `Could not connect to the backend.\n\n` +
      `Make sure Python packages are installed:\n` +
      `  python -m pip install -r requirements.txt\n\n` +
      `And Ollama is running:\n` +
      `  ollama serve\n\n${err.message}`
    );
    app.quit();
    return;
  }

  createWindow();
});

app.on('activate', () => {
  if (!mainWindow) createWindow();
  else mainWindow.show();
});

app.on('before-quit', () => {
  app.isQuiting = true;
  if (backendProcess) backendProcess.kill();
});
