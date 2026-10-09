// C++ port of voicechanger/chain.py: Settings + VoiceChain.
#pragma once

#include <memory>
#include <string>
#include <vector>

#include "effects.h"
#include "spectral_voice.h"

namespace vc {

enum class Quality { LowLatency = 0, Balanced = 1, HighQuality = 2 };

// Mirrors the Python Settings dataclass field for field. Plain data, so it can
// be copied under a seqlock between the settings thread and the audio thread.
struct Settings {
    double pitch = 0.0, formant = 0.0;
    Mode mode = Mode::Normal;
    double robot_hz = 110.0, mix = 1.0;
    // character (all neutral by default)
    double tremor_hz = 5.5, tremor_depth = 0.0, jitter = 0.0, breath = 0.0;
    double gravel = 0.0, gravel_hz = 50.0;
    double input_gain_db = 0.0;
    bool gate_enabled = true;
    double gate_threshold_db = -50.0;
    double highpass_hz = 80.0, lowpass_hz = 18000.0;
    double low_hz = 200.0, low_db = 0.0, mid_hz = 1500.0, mid_q = 0.9, mid_db = 0.0;
    double high_hz = 5000.0, high_db = 0.0, drive = 0.0;
    bool comp_enabled = true;
    double comp_threshold_db = -20.0, comp_ratio = 3.0, comp_makeup_db = 3.0;
    double reverb_mix = 0.0, reverb_room = 0.6, output_gain_db = 0.0;
    bool bypass = false;
    // APO-only fields
    Quality quality = Quality::Balanced;
};

// Parses "key=value" lines (the format the GUI writes). Unknown keys and
// malformed values are ignored; missing keys keep `base`'s value.
Settings parse_settings(const std::string& text, Settings base = Settings());

struct QualitySpec {
    int fft_size, overlap;
};

// FFT size scales with the sample rate so frequency resolution (and so the
// lowest voice that shifts cleanly) is the same at 44.1, 48 or 96 kHz.
QualitySpec quality_spec(Quality q, int sample_rate);

class VoiceChain {
public:
    // block_size: the largest block process() will see (fixes the shifter's
    // padding; see SpectralVoice). always_pad: blocks may also be shorter,
    // as the audio engine's can be.
    VoiceChain(int sample_rate, Quality quality, int block_size, bool always_pad = false);

    // Mono in -> mono out, n <= block_size. `in` and `out` may alias.
    void process(const float* in, float* out, int n, const Settings& s);
    void reset();

    int latency() const { return voice_.latency(); }
    int block_size() const { return block_; }
    Quality quality() const { return quality_; }
    double input_peak = 0.0, output_peak = 0.0;

private:
    int fs_, block_;
    Quality quality_;
    SpectralVoice voice_;
    DelayLine dry_, bypass_delay_;
    GravelModulator gravel_;
    Biquad rumble_, hp_, lp_, low_, mid_, high_;
    NoiseGate gate_;
    Drive drive_;
    Compressor comp_;
    Reverb reverb_;
    Limiter limiter_;
    double reverb_room_ = -1.0;
    std::vector<double> x_, wet_, dry_buf_;
};

}  // namespace vc
