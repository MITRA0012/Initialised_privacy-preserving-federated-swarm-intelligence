const { app, BrowserWindow, ipcMain, dialog, shell } = require('electron');
const path = require('node:path');
const fs = require('node:fs/promises');
const crypto = require('node:crypto');
const { spawn } = require('node:child_process');
const net = require('node:net');

const DEFAULT_PROFILE = { name: 'My assistant', instructions: 'Be helpful, clear, and honest. Use the owner’s library when relevant. Say when the library does not contain enough information.', model: '', temperature: 0.4 };
let win;
let dataDir;
let profileFile;
let knowledgeFile;
let chatFile;
let federationFile;
let federatedProcesses = {};

if (process.defaultApp) {
  if (process.argv.length >= 2) app.setAsDefaultProtocolClient('kin-agent', process.execPath, [path.resolve(process.argv[1])]);
} else {
  app.setAsDefaultProtocolClient('kin-agent');
}
const hasSingleInstance = app.requestSingleInstanceLock();
if (!hasSingleInstance) app.quit();
else app.on('second-instance', (_event, argv) => {
  const link = argv.find(arg => arg.startsWith('kin-agent://'));
  if (win) { if (win.isMinimized()) win.restore(); win.focus(); if (link) win.webContents.send('app:deep-link', link); }
});

async function readJson(file, fallback) {
  try { return JSON.parse(await fs.readFile(file, 'utf8')); } catch { return fallback; }
}
async function writeJson(file, value) {
  await fs.mkdir(path.dirname(file), { recursive: true });
  const temp = `${file}.tmp`;
  await fs.writeFile(temp, JSON.stringify(value, null, 2), 'utf8');
  await fs.rename(temp, file);
}
function findLocalPort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => { const port = server.address().port; server.close(error => error ? reject(error) : resolve(port)); });
  });
}
function windowOptions() {
  return { width: 1440, height: 900, minWidth: 900, minHeight: 620, backgroundColor: '#f5f4f0', webPreferences: { preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true, nodeIntegration: false, sandbox: true } };
}
function createWindow() {
  win = new BrowserWindow(windowOptions());
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (url === 'https://ollama.com/download/windows') shell.openExternal(url);
    return { action: 'deny' };
  });
  win.webContents.on('will-navigate', (event, url) => {
    const devUrl = process.env.VITE_DEV_SERVER_URL;
    if (devUrl ? !url.startsWith(devUrl) : !url.startsWith('file://')) event.preventDefault();
  });
  if (process.env.VITE_DEV_SERVER_URL) win.loadURL(process.env.VITE_DEV_SERVER_URL);
  else win.loadFile(path.join(__dirname, '..', 'dist', 'index.html'));
}
function tokenize(s) { return new Set((s.toLowerCase().match(/[\p{L}\p{N}]{3,}/gu) || []).filter(x => !['the','and','for','with','that','this','from','are','was','you','your'].includes(x))); }
function findRelevant(query, docs) {
  const terms = tokenize(query);
  return docs.map(doc => {
    const words = tokenize(doc.content);
    const score = [...terms].reduce((n, term) => n + (words.has(term) ? 1 : 0), 0);
    return { ...doc, score };
  }).filter(d => d.score > 0).sort((a, b) => b.score - a.score).slice(0, 4);
}

app.whenReady().then(async () => {
  if (!hasSingleInstance) return;
  dataDir = path.join(app.getPath('userData'), 'instance');
  profileFile = path.join(dataDir, 'profile.json');
  knowledgeFile = path.join(dataDir, 'knowledge.json');
  chatFile = path.join(dataDir, 'conversations.json');
  federationFile = path.join(dataDir, 'federation.json');
  await fs.mkdir(dataDir, { recursive: true });
  createWindow();
  app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
});
app.on('before-quit', () => { for (const child of Object.values(federatedProcesses)) if (!child.killed) child.kill(); });
app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit(); });

ipcMain.handle('app:state', async () => ({ profile: await readJson(profileFile, DEFAULT_PROFILE), documents: (await readJson(knowledgeFile, [])).map(({ id, name, addedAt }) => ({ id, name, addedAt })), messages: await readJson(chatFile, []), dataDir }));
ipcMain.handle('federation:status', async () => {
  const config = await readJson(federationFile, { coordinator: '', caPath: '', privateKeyPath: '', pythonPath: 'python', projectPath: '', inferencePort: 11436 });
  const processes = Object.fromEntries(Object.entries(federatedProcesses).map(([key, child]) => [key, child.exitCode === null ? 'running' : 'stopped']));
  let inference = { running: false, ready: false, round: -1 };
  try { const response = await fetch(`http://127.0.0.1:${config.inferencePort}/health`, { signal: AbortSignal.timeout(700) }); if (response.ok) inference = { running: true, ...(await response.json()) }; } catch {}
  return { config, processes, inference };
});
ipcMain.handle('federation:save', async (_e, raw) => {
  const config = { coordinator: String(raw.coordinator || '').trim(), caPath: String(raw.caPath || '').trim(), privateKeyPath: String(raw.privateKeyPath || '').trim(), pythonPath: String(raw.pythonPath || 'python').trim(), projectPath: String(raw.projectPath || '').trim(), inferencePort: Number(raw.inferencePort || 11436) };
  if (!/^(\[[0-9a-fA-F:]+\]|[A-Za-z0-9.-]+):[1-9][0-9]{0,4}$/.test(config.coordinator) || Number(config.coordinator.slice(config.coordinator.lastIndexOf(':') + 1)) > 65535) throw new Error('Enter the coordinator as host:port.');
  if (!Number.isInteger(config.inferencePort) || config.inferencePort < 1024 || config.inferencePort > 65535) throw new Error('Inference port must be from 1024 to 65535.');
  if (!path.isAbsolute(config.caPath) || !path.isAbsolute(config.privateKeyPath) || !path.isAbsolute(config.projectPath)) throw new Error('Choose absolute paths for the certificate, private key, and federated project directory.');
  for (const file of [config.caPath, config.privateKeyPath, path.join(config.projectPath, 'kin_federated', 'client_app.py')]) await fs.access(file);
  await writeJson(federationFile, config); return config;
});
ipcMain.handle('federation:start', async () => {
  if (Object.values(federatedProcesses).some(child => child.exitCode === null)) throw new Error('Kin federated runtime is already running.');
  const config = await readJson(federationFile, null);
  if (!config) throw new Error('Save federation connection settings first.');
  const flower = process.platform === 'win32' ? 'flower-supernode.exe' : 'flower-supernode';
  const env = { ...process.env, PYTHONPATH: `${config.projectPath}${path.delimiter}${process.env.PYTHONPATH || ''}`, FEDERATED_KIN_DATA_DIR: dataDir, FEDERATED_ARTIFACT_DIR: path.join(dataDir, 'federated-adapter'), FEDERATED_ADAPTER_DIR: path.join(dataDir, 'federated-adapter') };
  await fs.mkdir(env.FEDERATED_ARTIFACT_DIR, { recursive: true });
  const logs = path.join(dataDir, 'logs'); await fs.mkdir(logs, { recursive: true });
  const startChild = (key, command, args) => {
    const log = require('node:fs').createWriteStream(path.join(logs, `federation-${key}.log`), { flags: 'a' });
    const child = spawn(command, args, { cwd: config.projectPath, env, windowsHide: true, stdio: ['ignore', log, log] });
    child.on('error', error => { log.write(`\n${error.message}\n`); }); federatedProcesses[key] = child; return child;
  };
  const inference = startChild('inference', config.pythonPath, ['-m', 'uvicorn', 'kin_federated.inference_service:app', '--host', '127.0.0.1', '--port', String(config.inferencePort)]);
  const flowerPort = await findLocalPort();
  const flowerChild = startChild('flower', flower, ['--superlink', config.coordinator, '--root-certificates', config.caPath, '--auth-supernode-private-key', config.privateKeyPath, '--host', '127.0.0.1', '--port', String(flowerPort)]);
  await new Promise(resolve => setTimeout(resolve, 700));
  if (inference.exitCode !== null || flowerChild.exitCode !== null) { for (const child of Object.values(federatedProcesses)) child.kill(); throw new Error('A Kin federation process exited at startup. Check the federation logs in Kin’s local data folder.'); }
  return { started: true };
});
ipcMain.handle('federation:stop', async () => { for (const child of Object.values(federatedProcesses)) if (child.exitCode === null) child.kill(); federatedProcesses = {}; return { stopped: true }; });
ipcMain.handle('profile:update', async (_e, next) => {
  const safe = { ...DEFAULT_PROFILE, ...(await readJson(profileFile, DEFAULT_PROFILE)), name: String(next.name || '').slice(0, 80), instructions: String(next.instructions || '').slice(0, 4000), temperature: Math.min(1, Math.max(0, Number(next.temperature) || 0.4)) };
  await writeJson(profileFile, safe); return safe;
});
ipcMain.handle('model:list', async () => {
  try { const r = await fetch('http://127.0.0.1:11434/api/tags', { signal: AbortSignal.timeout(2500) }); if (!r.ok) throw new Error('Ollama is not responding'); return { online: true, models: (await r.json()).models || [] }; }
  catch { return { online: false, models: [], error: 'Ollama is not running. Start it and install a model to chat offline.' }; }
});
ipcMain.handle('optimization:evaluate', async (_e, rawUrl, values) => {
  let url;
  try { url = new URL(String(rawUrl)); } catch { throw new Error('Enter a valid local evaluator URL.'); }
  if (url.protocol !== 'http:' || !['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)) throw new Error('For privacy, the evaluator must use HTTP on localhost or 127.0.0.1.');
  if (!values || typeof values !== 'object' || Array.isArray(values)) throw new Error('Candidate parameters were invalid.');
  const response = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ parameters: values }), signal: AbortSignal.timeout(180000) });
  if (!response.ok) throw new Error(`Evaluator returned HTTP ${response.status}.`);
  const result = await response.json();
  if (typeof result.score !== 'number' || !Number.isFinite(result.score)) throw new Error('Evaluator must return JSON with a finite numeric "score" field.');
  return result.score;
});
ipcMain.handle('model:set', async (_e, model) => { const p = await readJson(profileFile, DEFAULT_PROFILE); p.model = String(model || '').slice(0, 160); await writeJson(profileFile, p); return p; });
ipcMain.handle('documents:add', async () => {
  const picked = await dialog.showOpenDialog(win, { title: 'Add knowledge to this instance', properties: ['openFile', 'multiSelections'], filters: [{ name: 'Text documents', extensions: ['txt', 'md', 'markdown'] }] });
  if (picked.canceled) return [];
  const docs = await readJson(knowledgeFile, []);
  for (const file of picked.filePaths) {
    const stat = await fs.stat(file);
    if (stat.size > 2_000_000) continue;
    const content = await fs.readFile(file, 'utf8');
    docs.push({ id: crypto.randomUUID(), name: path.basename(file), content: content.slice(0, 500_000), addedAt: new Date().toISOString() });
  }
  await writeJson(knowledgeFile, docs); return docs.map(({ id, name, addedAt }) => ({ id, name, addedAt }));
});
ipcMain.handle('documents:remove', async (_e, id) => { const docs = (await readJson(knowledgeFile, [])).filter(d => d.id !== id); await writeJson(knowledgeFile, docs); return docs.map(({ id, name, addedAt }) => ({ id, name, addedAt })); });
ipcMain.handle('chat:send', async (_e, text, mode = 'local') => {
  const message = String(text || '').trim().slice(0, 8000);
  if (!message) throw new Error('Enter a message first.');
  const p = await readJson(profileFile, DEFAULT_PROFILE);
  if (mode !== 'general' && !p.model) throw new Error('Choose a local model first. Install one from the Models panel.');
  const docs = await readJson(knowledgeFile, []);
  const relevant = findRelevant(message, docs);
  const context = relevant.map((d, i) => `[Source ${i + 1}: ${d.name}]\n${d.content.slice(0, 6000)}`).join('\n\n');
  const history = await readJson(chatFile, []);
  const messages = [{ role: 'system', content: `${p.instructions}\n\nYou are ${p.name}. The owner’s knowledge library has ${docs.length} document(s). Use the source excerpts below when relevant. Never claim a source says something it does not. Cite used sources as [Source: filename]. If a question depends on information not found in these excerpts, be transparent.\n\n${context ? `Relevant library excerpts:\n${context}` : 'No matching library excerpts were found for this message.'}` }, ...history.slice(-16), { role: 'user', content: message }];
  let response;
  try {
    const r = mode === 'general'
      ? await fetch(`http://127.0.0.1:${(await readJson(federationFile, { inferencePort: 11436 })).inferencePort}/answer`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ query: message, context: relevant.map((d, i) => `[Source ${i + 1}: ${d.name}]\n${d.content.slice(0, 6000)}`), instructions: p.instructions }), signal: AbortSignal.timeout(180000) })
      : await fetch('http://127.0.0.1:11434/api/chat', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ model: p.model, messages, stream: false, options: { temperature: p.temperature } }), signal: AbortSignal.timeout(180000) });
    if (!r.ok) throw new Error(`${mode === 'general' ? 'Federated inference' : 'Local model'} returned HTTP ${r.status}.`);
    const body = await r.json(); response = (mode === 'general' ? body.answer : body.message?.content)?.trim() || 'The model returned an empty answer.';
  } catch (e) { throw new Error(e.name === 'TimeoutError' ? 'The model took too long. Try a smaller model or a shorter question.' : `Could not reach the local model: ${e.message}`); }
  const next = [...history, { role: 'user', content: message, at: new Date().toISOString() }, { role: 'assistant', content: response, at: new Date().toISOString(), sources: relevant.map(d => d.name) }].slice(-100);
  await writeJson(chatFile, next); return { answer: response, sources: relevant.map(d => d.name) };
});
ipcMain.handle('chat:clear', async () => { await writeJson(chatFile, []); return true; });
ipcMain.handle('knowledge:export', async () => {
  const result = await dialog.showSaveDialog(win, { defaultPath: 'kin-shared-knowledge.json', filters: [{ name: 'Kin knowledge bundle', extensions: ['json'] }] });
  if (result.canceled || !result.filePath) return false;
  const docs = await readJson(knowledgeFile, []);
  await fs.writeFile(result.filePath, JSON.stringify({ format: 'kin-knowledge-v1', exportedAt: new Date().toISOString(), documents: docs.map(({ name, content }) => ({ name, content })) }, null, 2), 'utf8'); return true;
});
ipcMain.handle('knowledge:import', async () => {
  const result = await dialog.showOpenDialog(win, { properties: ['openFile'], filters: [{ name: 'Kin knowledge bundle', extensions: ['json'] }] });
  if (result.canceled || !result.filePaths[0]) return false;
  const bundle = JSON.parse(await fs.readFile(result.filePaths[0], 'utf8'));
  if (bundle.format !== 'kin-knowledge-v1' || !Array.isArray(bundle.documents)) throw new Error('This is not a supported Kin knowledge bundle.');
  const docs = await readJson(knowledgeFile, []);
  for (const d of bundle.documents.slice(0, 100)) if (typeof d.name === 'string' && typeof d.content === 'string') docs.push({ id: crypto.randomUUID(), name: d.name.slice(0, 200), content: d.content.slice(0, 500_000), addedAt: new Date().toISOString() });
  await writeJson(knowledgeFile, docs); return true;
});
