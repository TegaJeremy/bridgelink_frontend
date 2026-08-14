const { app, BrowserWindow, shell } = require('electron');
let mainWindow;
function createWindow() {
  mainWindow = new BrowserWindow({ width: 1280, height: 800, minWidth: 900, minHeight: 600, title: 'BridgeLink', webPreferences: { nodeIntegration: false, contextIsolation: true }, autoHideMenuBar: true, backgroundColor: '#07070e', show: false });
  mainWindow.loadURL('https://connect.bridgelin.online');
  mainWindow.once('ready-to-show', () => mainWindow.show());
  mainWindow.webContents.setWindowOpenHandler(({ url }) => { shell.openExternal(url); return { action: 'deny' }; });
  mainWindow.on('closed', () => { mainWindow = null; });
}
app.whenReady().then(createWindow);
app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit(); });
app.on('activate', () => { if (mainWindow === null) createWindow(); });
