import json
import sys
from pathlib import Path

import numpy as np

from voicechanger.cli import main
from voicechanger.fileio import read_wav, write_wav
from voicechanger.presets import make_settings

sys.path.insert(0, str(Path(__file__).parent))
from test_dsp import SR, voice_like  # noqa: E402


def test_file_ignores_a_saved_bypass(tmp_path):
    """Settings saved while A/B-ing must not turn 'file' into a copy of the input."""
    src, dst, saved = tmp_path / "in.wav", tmp_path / "out.wav", tmp_path / "saved.json"
    write_wav(str(src), voice_like(seconds=1.0), SR)
    saved.write_text(json.dumps(make_settings("deeper").update(bypass=True).to_dict()))
    assert main(["file", str(src), str(dst), "--settings", str(saved), "--quality", "low-latency"]) == 0
    x, _ = read_wav(str(src))
    y, sr = read_wav(str(dst))
    assert sr == SR and len(y) == len(x)
    assert np.max(np.abs(y - x)) > 0.05
