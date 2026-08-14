const { app, BrowserWindow, shell } = require('electron');
let mainWindow;
let inactivityTimer = null;
const TIMEOUT_MINUTES = 15;
function resetTimer() {
  if (inactivityTimer) clearTimeout(inactivityTimer);
  inactivityTimer = setTimeout(() => {
    if (mainWindow) {
      mainWindow.webContents.reload();
      mainWindow.webContents.once('did-finish-load', () => {
        mainWindow.webContents.executeJavaScript('alert("Session expired due to inactivity. Please log in again.");').catch(() => {});
      });
    }
  }, TIMEOUT_MINUTES * 60 * 1000);
}
function createWindow() {
  mainWindow = new BrowserWindow({ width: 1400, height: 900, minWidth: 1000, minHeight: 600, title: 'BridgeLink Admin', webPreferences: { nodeIntegration: false, contextIsolation: true }, autoHideMenuBar: true, backgroundColor: '#0a0a0f', show: false });
  mainWindow.loadURL('https://connect.bridgelin.online/admin.html');
  mainWindow.once('ready-to-show', () => { mainWindow.show(); resetTimer(); });
  mainWindow.webContents.on('before-input-event', () => resetTimer());
  mainWindow.on('focus', () => resetTimer());
  mainWindow.webContents.on('did-finish-load', () => resetTimer());
  mainWindow.webContents.setWindowOpenHandler(({ url }) => { shell.openExternal(url); return { action: 'deny' }; });
  mainWindow.on('closed', () => { mainWindow = null; if (inactivityTimer) clearTimeout(inactivityTimer); });
}
app.whenReady().then(createWindow);
app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit(); });
app.on('activate', () => { if (mainWindow === null) createWindow(); });
