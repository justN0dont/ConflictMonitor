"""Install / uninstall logic for the system-wide microphone effect, run against
an in-memory registry shaped like a real Windows 11 machine."""

import copy
import json

import pytest

from voicechanger import system_mic as sm
from voicechanger.chain import Settings
from voicechanger.presets import make_settings

REALTEK = "{0a1b2c3d-0000-0000-0000-000000000001}"
USB = "{0a1b2c3d-0000-0000-0000-000000000002}"
COMPOSITE = "{0a1b2c3d-0000-0000-0000-000000000003}"
UNPLUGGED = "{0a1b2c3d-0000-0000-0000-000000000004}"
VENDOR_SFX = "{aaaaaaaa-1111-2222-3333-444444444444}"
VENDOR_SFX2 = "{bbbbbbbb-1111-2222-3333-444444444444}"


def ep(eid, sub=""):
    return sm.CAPTURE_ROOT + "\\" + eid + ("\\" + sub if sub else "")


@pytest.fixture
def reg():
    r = sm.MemoryRegistry()

    def add(eid, desc, iface, state=1):
        r.set(ep(eid), "DeviceState", sm.REG_DWORD, state)
        r.set(ep(eid, "Properties"), sm.PKEY_DEVICE_DESC, sm.REG_SZ, desc)
        r.set(ep(eid, "Properties"), sm.PKEY_INTERFACE_NAME, sm.REG_SZ, iface)

    # Onboard mic whose driver ships its own stream effect (upper-case names, as some drivers write).
    add(REALTEK, "Microphone", "Realtek(R) Audio")
    r.set(ep(REALTEK, "FxProperties"), sm.PKEY_FX_STREAM.upper(), sm.REG_SZ, VENDOR_SFX)
    r.set(ep(REALTEK, "FxProperties"), sm.PKEY_SFX_MODES, sm.REG_MULTI_SZ, [sm.MODE_DEFAULT])
    r.set(ep(REALTEK, "FxProperties"), "{unrelated},1", sm.REG_BINARY, b"\x01\x02")
    # USB mic: no effects at all, enhancements switched off by the user.
    add(USB, "Microphone", "Blue Yeti")
    r.set(ep(USB, "Properties"), sm.PKEY_DISABLE_SYSFX, sm.REG_DWORD, 1)
    # Windows 11 driver using a composite effect list.
    add(COMPOSITE, "Headset Microphone", "Arctis 7")
    r.set(ep(COMPOSITE, "FxProperties"), sm.PKEY_FX_COMPOSITE_STREAM, sm.REG_MULTI_SZ, [VENDOR_SFX, VENDOR_SFX2])
    add(UNPLUGGED, "Microphone", "Old Webcam", state=4)
    return r


@pytest.fixture
def system(tmp_path):
    dll = tmp_path / "VoiceChangerAPO.dll"
    dll.write_bytes(b"MZ fake dll")
    s = sm.System(program_files=tmp_path / "pf", data_dir=tmp_path / "data")
    s.dll = dll
    return s


def test_lists_endpoints_with_names_and_state(reg):
    eps = {e.id: e for e in sm.list_endpoints(reg)}
    assert eps[REALTEK].name == "Microphone (Realtek(R) Audio)"
    assert eps[USB].active and not eps[UNPLUGGED].active
    assert not any(e.installed for e in eps.values())
    assert sm.list_endpoints(reg)[-1].id == UNPLUGGED  # inactive sorted last


def test_install_attaches_registers_and_copies(reg, system):
    dll = sm.install([REALTEK], system.dll, reg, system)
    assert dll.exists() and dll.parent == system.program_files / "VoiceChanger"
    assert dll.name.startswith("VoiceChangerAPO-")
    fx = ep(REALTEK, "FxProperties")
    # replaced the vendor SFX in place, keeping the driver's spelling of the name
    assert reg.values(fx)[sm.PKEY_FX_STREAM.upper()] == (sm.REG_SZ, sm.APO_CLSID)
    assert sm.PKEY_FX_STREAM not in reg.values(fx)
    modes = reg.get(fx, sm.PKEY_SFX_MODES)[1]
    assert modes == [sm.MODE_DEFAULT, sm.MODE_COMMUNICATIONS]
    assert reg.get(ep(REALTEK, "Properties"), sm.PKEY_DISABLE_SYSFX) == (sm.REG_DWORD, 0)
    # COM + APO registration point at the copied DLL
    assert reg.get(sm.COM_KEY + r"\InprocServer32", "")[1] == str(dll)
    assert reg.get(sm.COM_KEY + r"\InprocServer32", "ThreadingModel")[1] == "Both"
    assert reg.get(sm.APO_KEY, "APOInterface0")[1] == sm.IID_IAUDIOSYSTEMEFFECTS
    assert reg.get(sm.APO_KEY, "Flags")[1] == 0x0E
    assert {e.id for e in sm.list_endpoints(reg) if e.installed} == {REALTEK}
    assert any("Audiosrv" in line for line in system.log)


def test_uninstall_restores_every_original_value_exactly(reg, system):
    before = copy.deepcopy(reg.keys)
    sm.install([REALTEK, USB, COMPOSITE], system.dll, reg, system)
    assert reg.keys != before
    done = sm.uninstall(None, reg, system)
    assert set(done) == {REALTEK, USB, COMPOSITE}
    assert reg.keys == before  # byte-for-byte, including values that didn't exist
    assert not list((system.program_files / "VoiceChanger").glob("*.dll"))


def test_reinstall_keeps_the_true_originals(reg, system):
    before = copy.deepcopy(reg.keys)
    sm.install([REALTEK], system.dll, reg, system)
    sm.install([REALTEK], system.dll, reg, system)  # e.g. an update
    backup = json.loads(reg.get(sm.OUR_KEY + r"\Endpoints" + "\\" + REALTEK, "Backup")[1])
    assert backup["fx"][sm.PKEY_FX_STREAM] == [sm.REG_SZ, VENDOR_SFX]
    sm.uninstall([REALTEK], reg, system)
    assert reg.keys == before


def test_composite_list_puts_ours_first_and_keeps_theirs(reg, system):
    sm.install([COMPOSITE], system.dll, reg, system)
    comp = reg.get(ep(COMPOSITE, "FxProperties"), sm.PKEY_FX_COMPOSITE_STREAM)[1]
    assert comp == [sm.APO_CLSID, VENDOR_SFX, VENDOR_SFX2]
    sm.install([COMPOSITE], system.dll, reg, system)  # no duplicates on repeat
    assert reg.get(ep(COMPOSITE, "FxProperties"), sm.PKEY_FX_COMPOSITE_STREAM)[1].count(sm.APO_CLSID) == 1


def test_enhancements_forced_on_and_restored(reg, system):
    props = ep(USB, "Properties")
    sm.install([USB], system.dll, reg, system)
    assert reg.get(props, sm.PKEY_DISABLE_SYSFX)[1] == 0
    sm.uninstall([USB], reg, system)
    assert reg.get(props, sm.PKEY_DISABLE_SYSFX)[1] == 1


def test_partial_uninstall_keeps_registration_until_last(reg, system):
    sm.install([REALTEK, USB], system.dll, reg, system)
    sm.uninstall([REALTEK], reg, system)
    assert reg.exists(sm.APO_KEY) and reg.exists(sm.COM_KEY)
    assert {e.id for e in sm.list_endpoints(reg) if e.installed} == {USB}
    sm.uninstall([USB], reg, system)
    assert not reg.exists(sm.APO_KEY) and not reg.exists(sm.COM_KEY) and not reg.exists(sm.OUR_KEY)


def test_uninstall_of_untouched_endpoint_changes_nothing(reg, system):
    before = copy.deepcopy(reg.keys)
    assert sm.uninstall([USB], reg, system) == []
    assert reg.keys == before


def test_unknown_endpoint_rejected(reg, system):
    with pytest.raises(ValueError):
        sm.install(["{not-a-mic}"], system.dll, reg, system)


def test_binary_values_survive_backup(reg, system):
    reg.set(ep(USB, "FxProperties"), sm.PKEY_SFX_MODES, sm.REG_BINARY, b"\x00\xff")
    before = copy.deepcopy(reg.keys)
    sm.install([USB], system.dll, reg, system)
    sm.uninstall([USB], reg, system)
    assert reg.keys == before


def test_settings_file_round_trip(tmp_path):
    s = make_settings("demon")
    path = sm.write_settings(s, "low-latency", tmp_path)
    text = path.read_text()
    assert "quality=low-latency" in text and "pitch=-12" in text and "gate_enabled=1" in text
    assert not (tmp_path / "apo-settings.ini.tmp").exists()
    # every Settings field is written
    for name in Settings().to_dict():
        assert f"\n{name}=" in "\n" + text


def test_status_reader_ignores_stale_instances(tmp_path):
    import time

    now = int(time.time() * 1000)
    (tmp_path / "apo-status-1-a.ini").write_text(f"updated_ms={now}\nhost=C:\\x\\Discord.exe\nsample_rate=48000\n")
    (tmp_path / "apo-status-2-b.ini").write_text(f"updated_ms={now - 60000}\nhost=old.exe\n")
    live = sm.read_status(tmp_path)
    assert [i["host"] for i in live] == ["C:\\x\\Discord.exe"]
