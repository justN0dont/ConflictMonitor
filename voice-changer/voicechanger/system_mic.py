"""Install the voice changer into a Windows microphone, for every app at once.

The DSP runs as a Windows audio *system effect* (an APO,
``apo/VoiceChangerAPO.dll``) attached to a real capture endpoint. Every
program that opens that microphone (Discord, Zoom, games, OBS, browsers) then
receives the transformed voice. No virtual cable is involved.

What installing does (administrator rights needed):

1. Copies the DLL to ``%ProgramFiles%\\VoiceChanger`` under a versioned name, so
   an update never fights a copy the audio engine still has loaded.
2. Registers it as a COM class and as an audio processing object.
3. On the chosen microphone's ``FxProperties``, sets it as the stream effect
   (SFX) for the default and communications modes, and makes sure "audio
   enhancements" are enabled. Every original value is saved first, and
   uninstalling puts each one back exactly (including deleting values that
   did not exist before).
4. Creates ``%ProgramData%\\VoiceChanger``, where the GUI writes
   ``apo-settings.ini`` (no admin needed) and the APO reports status.
5. Restarts the Windows Audio service so the engine picks it up.

The microphone endpoints' FxProperties keys are owned by the system, so writes
go through ``REG_OPTION_BACKUP_RESTORE`` with the backup/restore privileges
an elevated administrator holds.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

APO_CLSID = "{157de9ec-65c3-4865-90df-5b3c35fe2f93}"
IID_IAUDIOSYSTEMEFFECTS = "{5FA00F27-ADD6-499a-8A9D-6B98521FA75B}"

CAPTURE_ROOT = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"
COM_KEY = rf"SOFTWARE\Classes\CLSID\{APO_CLSID}"
APO_KEY = rf"SOFTWARE\Classes\AudioEngine\AudioProcessingObjects\{APO_CLSID}"
OUR_KEY = r"SOFTWARE\VoiceChanger"

# Property keys stored as "{fmtid},pid" value names.
FX = "{d04e05a6-594b-4fb6-a80d-01af5eed7d1d}"
PKEY_FX_STREAM = FX + ",5"          # SFX: per-stream effect
PKEY_FX_COMPOSITE_STREAM = FX + ",13"  # Windows 11 composite SFX list (if the driver uses one)
PKEY_SFX_MODES = "{d3993a3f-99c2-4402-b5ec-a92a0367664b},5"
PKEY_DISABLE_SYSFX = "{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},5"
PKEY_DEVICE_DESC = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"
PKEY_INTERFACE_NAME = "{b3f8fa53-0004-438e-9003-51a46e139bfc},6"

MODE_DEFAULT = "{C18E2F7E-933D-4965-B7D1-1EEF228D2AF3}"
MODE_COMMUNICATIONS = "{98951333-B9CD-48B1-A0A3-FF40682D73F7}"

REG_SZ, REG_BINARY, REG_DWORD, REG_MULTI_SZ = 1, 3, 4, 7
DEVICE_STATE_ACTIVE = 1

TOUCHED_FX = (PKEY_FX_STREAM, PKEY_FX_COMPOSITE_STREAM, PKEY_SFX_MODES)
TOUCHED_PROPS = (PKEY_DISABLE_SYSFX,)


# ---------------------------------------------------------------------------
# Registry access (HKLM only). Real backend below; tests use an in-memory one.
# ---------------------------------------------------------------------------

class Registry:
    def values(self, path: str) -> dict[str, tuple[int, object]]:
        raise NotImplementedError

    def subkeys(self, path: str) -> list[str]:
        raise NotImplementedError

    def exists(self, path: str) -> bool:
        raise NotImplementedError

    def set(self, path: str, name: str, typ: int, data) -> None:
        raise NotImplementedError

    def delete_value(self, path: str, name: str) -> None:
        raise NotImplementedError

    def delete_tree(self, path: str) -> None:
        raise NotImplementedError

    # helpers
    def get(self, path: str, name: str):
        """(type, data) for a value, matching the name case-insensitively, or None."""
        for k, v in self.values(path).items():
            if k.lower() == name.lower():
                return v
        return None

    def actual_name(self, path: str, name: str) -> str:
        for k in self.values(path):
            if k.lower() == name.lower():
                return k
        return name


class WinRegistry(Registry):  # pragma: no cover - needs Windows
    """HKLM via winreg, with writes that bypass ACLs using backup/restore privileges."""

    def __init__(self) -> None:
        import winreg

        self.w = winreg
        _enable_privileges(["SeBackupPrivilege", "SeRestorePrivilege"])

    def _open(self, path: str, write: bool):
        if not write:
            return self.w.OpenKey(self.w.HKEY_LOCAL_MACHINE, path, 0, self.w.KEY_READ | self.w.KEY_WOW64_64KEY)
        return _WriteKey(path)

    def values(self, path):
        out = {}
        try:
            with self._open(path, False) as k:
                i = 0
                while True:
                    try:
                        name, data, typ = self.w.EnumValue(k, i)
                    except OSError:
                        break
                    out[name] = (typ, data)
                    i += 1
        except FileNotFoundError:
            pass
        return out

    def subkeys(self, path):
        out = []
        try:
            with self._open(path, False) as k:
                i = 0
                while True:
                    try:
                        out.append(self.w.EnumKey(k, i))
                    except OSError:
                        break
                    i += 1
        except FileNotFoundError:
            pass
        return out

    def exists(self, path):
        try:
            self._open(path, False).Close()
            return True
        except FileNotFoundError:
            return False

    def set(self, path, name, typ, data):
        with self._open(path, True) as k:
            self.w.SetValueEx(k.handle, name, 0, typ, data)

    def delete_value(self, path, name):
        with self._open(path, True) as k:
            try:
                self.w.DeleteValue(k.handle, name)
            except FileNotFoundError:
                pass

    def delete_tree(self, path):
        for sub in self.subkeys(path):
            self.delete_tree(path + "\\" + sub)
        try:
            self.w.DeleteKeyEx(self.w.HKEY_LOCAL_MACHINE, path, self.w.KEY_WOW64_64KEY, 0)
        except FileNotFoundError:
            pass


class _WriteKey:  # pragma: no cover - needs Windows
    """HKLM key opened with REG_OPTION_BACKUP_RESTORE, which (with the backup
    and restore privileges enabled) ignores the key's ACL. That is needed for
    the system-owned MMDevices endpoint keys. winreg accepts the raw handle."""

    def __init__(self, path: str) -> None:
        import ctypes
        from ctypes import wintypes

        self._advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        hkey = wintypes.HKEY()
        disp = wintypes.DWORD()
        REG_OPTION_BACKUP_RESTORE, KEY_ALL_ACCESS, KEY_WOW64_64KEY = 0x4, 0xF003F, 0x100
        HKLM = wintypes.HKEY(0x80000002)
        rc = self._advapi.RegCreateKeyExW(HKLM, path, 0, None, REG_OPTION_BACKUP_RESTORE,
                                          KEY_ALL_ACCESS | KEY_WOW64_64KEY, None, ctypes.byref(hkey),
                                          ctypes.byref(disp))
        if rc != 0:
            raise OSError(rc, f"cannot open HKLM\\{path} for writing (error {rc})")
        self.handle = hkey.value

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._advapi.RegCloseKey(self.handle)


class MemoryRegistry(Registry):
    """In-memory HKLM for tests. Paths are case-insensitive like the real thing."""

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, tuple[int, object]]] = {}

    @staticmethod
    def _k(path):
        return path.lower().strip("\\")

    def values(self, path):
        return dict(self.keys.get(self._k(path), {}))

    def subkeys(self, path):
        p = self._k(path) + "\\"
        seen = []
        for k in self.keys:
            if k.startswith(p):
                child = k[len(p):].split("\\")[0]
                if child not in seen:
                    seen.append(child)
        return seen

    def exists(self, path):
        p = self._k(path)
        return p in self.keys or any(k.startswith(p + "\\") for k in self.keys)

    def set(self, path, name, typ, data):
        vals = self.keys.setdefault(self._k(path), {})
        for existing in list(vals):
            if existing.lower() == name.lower():
                name = existing
        vals[name] = (typ, list(data) if isinstance(data, list) else data)

    def delete_value(self, path, name):
        vals = self.keys.get(self._k(path), {})
        for existing in list(vals):
            if existing.lower() == name.lower():
                del vals[existing]

    def delete_tree(self, path):
        p = self._k(path)
        for k in list(self.keys):
            if k == p or k.startswith(p + "\\"):
                del self.keys[k]


# ---------------------------------------------------------------------------
# System operations (files, ACLs, service restart). Swappable for tests.
# ---------------------------------------------------------------------------

@dataclass
class System:
    program_files: Path = field(default_factory=lambda: Path(os.environ.get("ProgramFiles", r"C:\Program Files")))
    data_dir: Path = field(default_factory=lambda: data_dir())
    dry_run: bool = False
    log: list[str] = field(default_factory=list)

    def install_dll(self, src: Path) -> Path:
        digest = hashlib.sha256(src.read_bytes()).hexdigest()[:10]
        dest_dir = self.program_files / "VoiceChanger"
        dest = dest_dir / f"VoiceChangerAPO-{digest}.dll"
        self.log.append(f"copy {src} -> {dest}")
        if not self.dry_run:
            dest_dir.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                shutil.copy2(src, dest)
        return dest

    def remove_old_dlls(self, keep: Path | None) -> None:
        d = self.program_files / "VoiceChanger"
        if self.dry_run or not d.exists():
            return
        for f in d.glob("VoiceChangerAPO-*.dll"):
            if keep is None or f.resolve() != keep.resolve():
                try:
                    f.unlink()
                except OSError:
                    pass  # still loaded by the audio engine; removed next time

    def prepare_data_dir(self) -> None:
        self.log.append(f"prepare {self.data_dir}")
        if self.dry_run:
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":  # pragma: no cover
            # Users: write settings without admin. LocalService (+ the write-
            # restricted SID audiodg's token carries): read settings, write status.
            subprocess.run(["icacls", str(self.data_dir),
                            "/grant", "*S-1-5-32-545:(OI)(CI)M",
                            "/grant", "*S-1-5-19:(OI)(CI)M",
                            "/grant", "*S-1-5-33:(OI)(CI)M"], check=False, capture_output=True)

    def restart_audio(self) -> None:
        self.log.append("restart Audiosrv")
        if self.dry_run or sys.platform != "win32":
            return
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",  # pragma: no cover
                        "Restart-Service -Name Audiosrv -Force"], check=False, capture_output=True)
        time.sleep(1.0)


def data_dir() -> Path:
    if os.environ.get("VOICECHANGER_DATA_DIR"):
        return Path(os.environ["VOICECHANGER_DATA_DIR"])
    return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "VoiceChanger"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@dataclass
class Endpoint:
    id: str          # "{guid}" under MMDevices\Audio\Capture
    name: str        # e.g. "Microphone (Realtek(R) Audio)"
    active: bool
    installed: bool

    @property
    def label(self) -> str:
        return self.name + ("" if self.active else " [disabled/unplugged]")


def _endpoint_path(eid: str, sub: str = "") -> str:
    return CAPTURE_ROOT + "\\" + eid + ("\\" + sub if sub else "")


def is_installed_on(reg: Registry, eid: str) -> bool:
    fx = _endpoint_path(eid, "FxProperties")
    v = reg.get(fx, PKEY_FX_STREAM)
    if v and str(v[1]).lower() == APO_CLSID:
        return True
    comp = reg.get(fx, PKEY_FX_COMPOSITE_STREAM)
    return bool(comp and APO_CLSID in [str(x).lower() for x in (comp[1] or [])])


def list_endpoints(reg: Registry) -> list[Endpoint]:
    out = []
    for eid in reg.subkeys(CAPTURE_ROOT):
        state = reg.get(_endpoint_path(eid), "DeviceState")
        props = _endpoint_path(eid, "Properties")
        desc = reg.get(props, PKEY_DEVICE_DESC)
        iface = reg.get(props, PKEY_INTERFACE_NAME)
        name = (desc[1] if desc else "Microphone") + (f" ({iface[1]})" if iface else "")
        out.append(Endpoint(eid, name, bool(state and state[1] == DEVICE_STATE_ACTIVE), is_installed_on(reg, eid)))
    out.sort(key=lambda e: (not e.active, e.name.lower()))
    return out


# ---------------------------------------------------------------------------
# Install / uninstall
# ---------------------------------------------------------------------------

def _backup_path(eid: str) -> str:
    return OUR_KEY + r"\Endpoints" + "\\" + eid


def _snapshot(reg: Registry, path: str, names) -> dict:
    snap = {}
    for n in names:
        v = reg.get(path, n)
        snap[n] = None if v is None else [v[0], _to_json(v[1])]
    return snap


def _to_json(data):
    return {"hex": data.hex()} if isinstance(data, (bytes, bytearray)) else data


def _from_json(data):
    return bytes.fromhex(data["hex"]) if isinstance(data, dict) and "hex" in data else data


def register_apo(reg: Registry, dll: Path) -> None:
    reg.set(COM_KEY, "", REG_SZ, "Voice Changer APO")
    reg.set(COM_KEY + r"\InprocServer32", "", REG_SZ, str(dll))
    reg.set(COM_KEY + r"\InprocServer32", "ThreadingModel", REG_SZ, "Both")
    for name, typ, val in [
        ("FriendlyName", REG_SZ, "Voice Changer"),
        ("Copyright", REG_SZ, "Voice Changer contributors"),
        ("MajorVersion", REG_DWORD, 1),
        ("MinorVersion", REG_DWORD, 0),
        ("Flags", REG_DWORD, 0x0E),  # APO_FLAG_DEFAULT
        ("MinInputConnections", REG_DWORD, 1),
        ("MaxInputConnections", REG_DWORD, 1),
        ("MinOutputConnections", REG_DWORD, 1),
        ("MaxOutputConnections", REG_DWORD, 1),
        ("MaxInstances", REG_DWORD, 0xFFFFFFFF),
        ("NumAPOInterfaces", REG_DWORD, 1),
        ("APOInterface0", REG_SZ, IID_IAUDIOSYSTEMEFFECTS),
    ]:
        reg.set(APO_KEY, name, typ, val)


def installed_endpoints(reg: Registry) -> list[str]:
    return reg.subkeys(OUR_KEY + r"\Endpoints")


def attach(reg: Registry, eid: str) -> None:
    """Point one capture endpoint's stream effect at the APO, saving originals first."""
    if not reg.exists(_endpoint_path(eid)):
        raise ValueError(f"no capture endpoint {eid}")
    fx = _endpoint_path(eid, "FxProperties")
    props = _endpoint_path(eid, "Properties")
    bpath = _backup_path(eid)
    if reg.get(bpath, "Backup") is None:  # first install on this mic: remember the originals
        backup = {"fx": _snapshot(reg, fx, TOUCHED_FX), "props": _snapshot(reg, props, TOUCHED_PROPS),
                  "fx_key_existed": reg.exists(fx)}
        reg.set(bpath, "Backup", REG_SZ, json.dumps(backup))

    reg.set(fx, reg.actual_name(fx, PKEY_FX_STREAM), REG_SZ, APO_CLSID)

    # Drivers using a Windows 11 composite effect list: put ours first, keep theirs.
    comp = reg.get(fx, PKEY_FX_COMPOSITE_STREAM)
    if comp is not None and comp[0] == REG_MULTI_SZ:
        rest = [c for c in (comp[1] or []) if str(c).lower() != APO_CLSID]
        reg.set(fx, reg.actual_name(fx, PKEY_FX_COMPOSITE_STREAM), REG_MULTI_SZ, [APO_CLSID] + rest)

    # The stream effect must be declared for the modes apps open the mic in.
    modes = reg.get(fx, PKEY_SFX_MODES)
    current = list(modes[1]) if modes and modes[0] == REG_MULTI_SZ and modes[1] else []
    have = {m.upper() for m in current}
    for m in (MODE_DEFAULT, MODE_COMMUNICATIONS):
        if m.upper() not in have:
            current.append(m)
    reg.set(fx, reg.actual_name(fx, PKEY_SFX_MODES), REG_MULTI_SZ, current)

    # "Audio enhancements" on, or Windows skips every effect on this mic.
    reg.set(props, reg.actual_name(props, PKEY_DISABLE_SYSFX), REG_DWORD, 0)


def detach(reg: Registry, eid: str) -> bool:
    """Restore one endpoint from its backup. Returns False if we never touched it."""
    bpath = _backup_path(eid)
    saved = reg.get(bpath, "Backup")
    if saved is None:
        return False
    backup = json.loads(saved[1])
    for sub, snap in (("FxProperties", backup["fx"]), ("Properties", backup["props"])):
        path = _endpoint_path(eid, sub)
        for name, val in snap.items():
            if val is None:
                reg.delete_value(path, name)
            else:
                reg.set(path, reg.actual_name(path, name), val[0], _from_json(val[1]))
    fx = _endpoint_path(eid, "FxProperties")
    if not backup.get("fx_key_existed", True) and not reg.values(fx) and not reg.subkeys(fx):
        reg.delete_tree(fx)  # we created it; leave no trace
    reg.delete_tree(bpath)
    return True


def install(endpoint_ids: list[str], dll_src: Path, reg: Registry, system: System) -> Path:
    dll = system.install_dll(Path(dll_src))
    register_apo(reg, dll)
    for eid in endpoint_ids:
        attach(reg, eid)
    reg.set(OUR_KEY, "DllPath", REG_SZ, str(dll))
    system.prepare_data_dir()
    system.restart_audio()
    system.remove_old_dlls(keep=dll)
    return dll


def uninstall(endpoint_ids: list[str] | None, reg: Registry, system: System) -> list[str]:
    """Detach from the given endpoints (all if None). Unregisters the APO once
    no endpoint uses it."""
    targets = installed_endpoints(reg) if endpoint_ids is None else endpoint_ids
    done = [eid for eid in targets if detach(reg, eid)]
    if not installed_endpoints(reg):
        reg.delete_tree(APO_KEY)
        reg.delete_tree(COM_KEY)
        reg.delete_tree(OUR_KEY)
    system.restart_audio()
    if not installed_endpoints(reg):
        system.remove_old_dlls(keep=None)
    return done


# ---------------------------------------------------------------------------
# Settings + status (no admin needed)
# ---------------------------------------------------------------------------

def settings_text(settings, quality: str = "balanced") -> str:
    lines = [f"quality={quality}"]
    for k, v in settings.to_dict().items():
        lines.append(f"{k}={int(v) if isinstance(v, bool) else v}")
    return "\n".join(lines) + "\n"


def write_settings(settings, quality: str = "balanced", directory: Path | None = None) -> Path:
    """Atomically replace the settings file the APO watches (picked up within ~0.1 s)."""
    d = directory or data_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / "apo-settings.ini"
    tmp = d / "apo-settings.ini.tmp"
    tmp.write_text(settings_text(settings, quality))
    os.replace(tmp, path)
    return path


def read_status(directory: Path | None = None, max_age_s: float = 3.0) -> list[dict]:
    """Live APO instances (one per app stream using the mic), newest first."""
    d = directory or data_dir()
    out = []
    now_ms = time.time() * 1000
    for f in d.glob("apo-status-*.ini") if d.exists() else []:
        try:
            info = dict(line.split("=", 1) for line in f.read_text().splitlines() if "=" in line)
        except OSError:
            continue
        if now_ms - float(info.get("updated_ms", 0)) <= max_age_s * 1000:
            out.append(info)
    out.sort(key=lambda i: -float(i.get("updated_ms", 0)))
    return out


def find_dll() -> Path | None:
    here = Path(__file__).resolve().parent
    candidates = [
        os.environ.get("VOICECHANGER_APO_DLL"),
        here / "bin" / "VoiceChangerAPO.dll",
        here.parent / "apo" / "build" / "Release" / "VoiceChangerAPO.dll",
        here.parent / "apo" / "build" / "VoiceChangerAPO.dll",
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    return None


# ---------------------------------------------------------------------------
# Windows plumbing
# ---------------------------------------------------------------------------

def is_admin() -> bool:  # pragma: no cover - Windows only
    if sys.platform != "win32":
        return False
    import ctypes

    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def _enable_privileges(names: list[str]) -> None:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class LUID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]

    class TOKEN_PRIVILEGES(ctypes.Structure):
        _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]

    TOKEN_ADJUST_PRIVILEGES, TOKEN_QUERY, SE_PRIVILEGE_ENABLED = 0x20, 0x8, 0x2
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                                   ctypes.byref(token)):
        raise OSError(ctypes.get_last_error(), "OpenProcessToken failed")
    try:
        for name in names:
            luid = LUID()
            if not advapi.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
                raise OSError(ctypes.get_last_error(), f"LookupPrivilegeValue({name}) failed")
            tp = TOKEN_PRIVILEGES(1, (LUID_AND_ATTRIBUTES * 1)(LUID_AND_ATTRIBUTES(luid, SE_PRIVILEGE_ENABLED)))
            advapi.AdjustTokenPrivileges(token, False, ctypes.byref(tp), 0, None, None)
            if ctypes.get_last_error() != 0:
                raise PermissionError(f"could not enable {name}; run as administrator")
    finally:
        kernel.CloseHandle(token)


def run_elevated(args: list[str], timeout_s: float = 120.0) -> int:  # pragma: no cover - Windows only
    """Run `python -m voicechanger <args>` as administrator (UAC prompt) and wait."""
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
                    ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
                    ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
                    ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]

    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):
        exe = exe[:-len("pythonw.exe")] + "python.exe"
    params = subprocess.list2cmdline(["-m", "voicechanger", *args])
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x40  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = exe
    info.lpParameters = params
    info.lpDirectory = str(Path(__file__).resolve().parent.parent)
    info.nShow = 0  # hidden
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        raise PermissionError("administrator permission was declined")
    kernel = ctypes.windll.kernel32
    kernel.WaitForSingleObject(info.hProcess, int(timeout_s * 1000))
    code = wintypes.DWORD()
    kernel.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    kernel.CloseHandle(info.hProcess)
    return int(code.value)


# ---------------------------------------------------------------------------
# CLI: python -m voicechanger apo ...
# ---------------------------------------------------------------------------

def cli(args) -> int:  # pragma: no cover - exercised on Windows
    if sys.platform != "win32" and not args.dry_run:
        print("The system microphone effect is Windows-only.")
        return 1
    if args.action == "list":
        for e in list_endpoints(WinRegistry() if sys.platform == "win32" else MemoryRegistry()):
            print(f"{'*' if e.installed else ' '} {e.id}  {e.label}")
        print("\n* = voice changer installed")
        return 0
    if args.action == "status":
        inst = read_status()
        if not inst:
            print("No app is using a voice-changed microphone right now.")
        for i in inst:
            print(f"{Path(i.get('host', '?')).name}: {i.get('sample_rate')} Hz, {i.get('channels')} ch, "
                  f"{i.get('quality')}, latency {i.get('latency_ms')} ms, in {i.get('in_peak')} out {i.get('out_peak')}")
        return 0

    if not args.dry_run and not is_admin():
        print("Asking Windows for administrator permission...")
        forwarded = ["apo", args.action] + sum((["--endpoint", e] for e in args.endpoint or []), [])
        if getattr(args, "dll", None):
            forwarded += ["--dll", args.dll]
        return run_elevated(forwarded)

    log_path = data_dir() / "install.log"
    try:
        reg = WinRegistry()
        system = System(dry_run=args.dry_run)
        if args.action == "install":
            if not args.endpoint:
                print("--endpoint is required (see `apo list`)")
                return 2
            dll = Path(args.dll) if args.dll else find_dll()
            if dll is None:
                print("VoiceChangerAPO.dll not found. Download the Windows build (see README) or pass --dll.")
                return 2
            path = install(args.endpoint, dll, reg, system)
            msg = f"installed {path} on {', '.join(args.endpoint)}"
        else:
            done = uninstall(args.endpoint or None, reg, system)
            msg = f"removed from {', '.join(done) or 'nothing'}"
        print(msg)
        _append_log(log_path, msg)
        return 0
    except Exception as exc:
        _append_log(log_path, f"{args.action} failed: {exc!r}")
        print(f"{args.action} failed: {exc}")
        return 1


def _append_log(path: Path, line: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
    except OSError:
        pass
