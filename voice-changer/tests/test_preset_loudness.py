"""Character presets keep their loudness relative to "natural" at any mic level.

Several presets boost the EQ or drive and let the compressor pull the level
back down. Below the compressor's threshold nothing pulls it down, so tuning
at one speaking level can leave a preset many dB louder than natural on a
quiet mic and quieter on a loud one. Each preset is therefore measured at a
quiet and a loud level, with a low and a high voice.
"""

import numpy as np
import pytest
from scipy.signal import lfilter

from voicechanger.chain import VoiceChain
from voicechanger.presets import make_settings

SR = 48000
CHARACTERS = ["old_lady", "old_man", "kid", "tough_guy", "pirate", "valley_girl", "announcer", "villain",
              "raspy", "ghost", "ogre", "alien", "megaphone", "vibrato"]
LEVELS_DBFS = (-38, -22)  # speech RMS: a quiet laptop mic, and a close headset
VOICES_HZ = (110, 210)
TOLERANCE_DB = 3.0


def k_weighted(y):
    """BS.1770 K-weighting at 48 kHz: the head-effect high shelf, then the RLB high-pass."""
    y = lfilter([1.53512485958697, -2.69169618940638, 1.19839281085285],
                [1.0, -1.69065929318241, 0.73248077421585], y)
    return lfilter([1.0, -2.0, 1.0], [1.0, -1.99004745483398, 0.99007225036621], y)


def speech_like():
    """One phrase per voice: syllables and pauses, a drifting pitch, three formants.

    Returns the signal at unit RMS per phrase and one mask per voice marking
    its syllables. A held tone would keep the compressor at one operating
    point; syllables and pauses make it attack and release the way speech does.
    """
    lead, phrase, gap = int(0.15 * SR), SR, int(0.2 * SR)
    t = np.arange(phrase) / SR
    env = np.clip(1.3 * np.sin(2 * np.pi * 3.0 * t), 0.0, 1.0) ** 0.7  # 3 syllables
    parts, masks, pos = [np.zeros(lead)], [], lead
    for f0 in VOICES_HZ:
        f = f0 * (1 + 0.08 * np.sin(2 * np.pi * 0.7 * t) + 0.03 * np.sin(2 * np.pi * 2.3 * t))
        phase = 2 * np.pi * np.cumsum(f) / SR
        x = np.zeros(phrase)
        for h in range(1, int(10000 / f0)):  # the formants leave nothing that matters above 10 kHz
            fh = h * f
            amp = (1 / (1 + ((fh - 650) / 140) ** 2) + 0.6 / (1 + ((fh - 1150) / 180) ** 2)
                   + 0.25 / (1 + ((fh - 2600) / 300) ** 2))
            x += amp * np.sin(h * phase)
        x *= env
        active = env > 0.05
        parts += [x / np.sqrt(np.mean(x[active] ** 2)), np.zeros(gap)]
        mask = np.zeros(lead + len(VOICES_HZ) * (phrase + gap), dtype=bool)
        mask[pos:pos + phrase] = active
        masks.append(mask)
        pos += phrase + gap
    return np.concatenate(parts), masks


@pytest.fixture(scope="module")
def loudness():
    """loudness(preset) -> {(level, voice Hz): K-weighted dB over active speech}, cached."""
    x, masks = speech_like()
    cache = {}

    def measure(preset):
        if preset not in cache:
            cache[preset] = {}
            for level in LEVELS_DBFS:
                y = VoiceChain(SR, "balanced", make_settings(preset)).process_buffer(x * 10 ** (level / 20))
                k = k_weighted(y)
                for f0, m in zip(VOICES_HZ, masks):
                    cache[preset][(level, f0)] = 10 * np.log10(np.mean(k[m] ** 2))
        return cache[preset]
    return measure


@pytest.mark.parametrize("preset", CHARACTERS)
def test_character_preset_stays_near_natural_loudness_at_any_mic_level(loudness, preset):
    ref, got = loudness("natural"), loudness(preset)
    diffs = {key: got[key] - ref[key] for key in ref}
    report = ", ".join(f"{hz} Hz at {lvl} dBFS: {d:+.1f} dB" for (lvl, hz), d in diffs.items())
    assert all(abs(d) <= TOLERANCE_DB for d in diffs.values()), report
