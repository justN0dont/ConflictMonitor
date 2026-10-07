"""Holds the C++ DSP (the code that runs inside Windows' audio engine) to the
Python reference implementation, sample for sample.

Needs the `dsp_cli` binary: `cmake -S apo -B apo/build && cmake --build apo/build`.
Skipped when it hasn't been built.
"""

import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from voicechanger.chain import QUALITY, VoiceChain
from voicechanger.presets import PRESETS, make_settings

sys.path.insert(0, str(Path(__file__).parent))
from test_dsp import voice_like  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = [ROOT / "apo/build/dsp_cli", ROOT / "apo/build/Release/dsp_cli.exe", ROOT / "apo/build/dsp_cli.exe"]
CLI = next((p for p in CANDIDATES if p.exists()), None)
if os.environ.get("VC_DSP_CLI"):
    CLI = Path(os.environ["VC_DSP_CLI"])

pytestmark = pytest.mark.skipif(CLI is None, reason="dsp_cli not built")
SR = 48000


def settings_ini(settings, quality: str) -> str:
    lines = [f"quality={quality}"]
    for k, v in settings.to_dict().items():
        lines.append(f"{k}={int(v) if isinstance(v, bool) else v}")
    return "\n".join(lines) + "\n"


def run_cpp(tmp_path, settings, quality, x, block):
    (tmp_path / "s.ini").write_text(settings_ini(settings, quality))
    x.astype("<f4").tofile(tmp_path / "in.f32")
    subprocess.run([str(CLI), str(SR), str(block), str(tmp_path / "s.ini"), str(tmp_path / "in.f32"),
                    str(tmp_path / "out.f32")], check=True, capture_output=True)
    return np.fromfile(tmp_path / "out.f32", "<f4").astype(np.float64)


def run_py(settings, quality, x, block):
    chain = VoiceChain(SR, quality, settings)
    assert chain.block_size == block
    return np.concatenate([chain.process(x[i:i + block]) for i in range(0, len(x) - block + 1, block)])


# Whisper uses a random generator, so it can only be compared statistically (below).
DETERMINISTIC = [p for p in PRESETS if PRESETS[p].get("mode") != "whisper"]


@pytest.mark.parametrize("preset", DETERMINISTIC)
def test_cpp_matches_python(tmp_path, preset):
    quality = "balanced"
    block = QUALITY[quality][2]
    x = voice_like(f0=140, seconds=1.5).astype(np.float32).astype(np.float64)
    s = make_settings(preset)
    py = run_py(s, quality, x, block)
    cpp = run_cpp(tmp_path, s, quality, x, block)[:len(py)]
    err = np.max(np.abs(py - cpp))
    assert err < 1e-4, f"{preset}: max abs difference {err:.2e}"


@pytest.mark.parametrize("quality", list(QUALITY))
def test_cpp_matches_python_every_quality(tmp_path, quality):
    block = QUALITY[quality][2]
    x = voice_like(f0=180, seconds=1.0).astype(np.float32).astype(np.float64)
    s = make_settings("feminine")
    py = run_py(s, quality, x, block)
    cpp = run_cpp(tmp_path, s, quality, x, block)[:len(py)]
    assert np.max(np.abs(py - cpp)) < 1e-4


def test_cpp_whisper_level_matches(tmp_path):
    block = QUALITY["balanced"][2]
    x = voice_like(seconds=2.0).astype(np.float32).astype(np.float64)
    s = make_settings("whisper")
    py = run_py(s, "balanced", x, block)[SR:]
    cpp = run_cpp(tmp_path, s, "balanced", x, block)[SR:len(py) + SR]
    assert abs(20 * np.log10(np.std(cpp) / np.std(py))) < 1.0
