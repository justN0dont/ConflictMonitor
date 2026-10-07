#include "chain.h"

#include <cstdlib>
#include <sstream>

namespace vc {

namespace {
std::string trim(const std::string& s) {
    size_t a = s.find_first_not_of(" \t\r\n");
    if (a == std::string::npos) return "";
    size_t b = s.find_last_not_of(" \t\r\n");
    return s.substr(a, b - a + 1);
}

bool parse_double(const std::string& v, double& out) {
    char* end = nullptr;
    const double d = std::strtod(v.c_str(), &end);
    if (end == v.c_str() || *end != '\0' || d != d) return false;  // empty, trailing junk, NaN
    out = d;
    return true;
}

bool parse_bool(const std::string& v, bool& out) {
    if (v == "1" || v == "true" || v == "True") { out = true; return true; }
    if (v == "0" || v == "false" || v == "False") { out = false; return true; }
    return false;
}

double clampd(double v, double lo, double hi) { return v < lo ? lo : (v > hi ? hi : v); }
}  // namespace

Settings parse_settings(const std::string& text, Settings s) {
    std::istringstream in(text);
    std::string line;
    while (std::getline(in, line)) {
        line = trim(line);
        if (line.empty() || line[0] == '#' || line[0] == ';' || line[0] == '[') continue;
        const size_t eq = line.find('=');
        if (eq == std::string::npos) continue;
        const std::string key = trim(line.substr(0, eq));
        const std::string val = trim(line.substr(eq + 1));
        double d;
        bool b;
        // Ranges are clamped generously: a hand-edited file must never be
        // able to push the audio engine into something pathological.
        if (key == "mode") {
            if (val == "normal") s.mode = Mode::Normal;
            else if (val == "robot") s.mode = Mode::Robot;
            else if (val == "whisper") s.mode = Mode::Whisper;
        } else if (key == "quality") {
            if (val == "low-latency") s.quality = Quality::LowLatency;
            else if (val == "balanced") s.quality = Quality::Balanced;
            else if (val == "high-quality") s.quality = Quality::HighQuality;
        } else if (key == "gate_enabled") { if (parse_bool(val, b)) s.gate_enabled = b; }
        else if (key == "comp_enabled") { if (parse_bool(val, b)) s.comp_enabled = b; }
        else if (key == "bypass") { if (parse_bool(val, b)) s.bypass = b; }
        else if (!parse_double(val, d)) continue;
        else if (key == "pitch") s.pitch = clampd(d, -36, 36);
        else if (key == "formant") s.formant = clampd(d, -24, 24);
        else if (key == "robot_hz") s.robot_hz = clampd(d, 20, 2000);
        else if (key == "mix") s.mix = clampd(d, 0, 1);
        else if (key == "tremor_hz") s.tremor_hz = clampd(d, 0.5, 15);
        else if (key == "tremor_depth") s.tremor_depth = clampd(d, 0, 3);
        else if (key == "jitter") s.jitter = clampd(d, 0, 1);
        else if (key == "breath") s.breath = clampd(d, 0, 1);
        else if (key == "gravel") s.gravel = clampd(d, 0, 1);
        else if (key == "gravel_hz") s.gravel_hz = clampd(d, 10, 200);
        else if (key == "input_gain_db") s.input_gain_db = clampd(d, -60, 40);
        else if (key == "gate_threshold_db") s.gate_threshold_db = clampd(d, -120, 0);
        else if (key == "highpass_hz") s.highpass_hz = clampd(d, 10, 20000);
        else if (key == "lowpass_hz") s.lowpass_hz = clampd(d, 100, 24000);
        else if (key == "low_db") s.low_db = clampd(d, -24, 24);
        else if (key == "mid_db") s.mid_db = clampd(d, -24, 24);
        else if (key == "high_db") s.high_db = clampd(d, -24, 24);
        else if (key == "drive") s.drive = clampd(d, 0, 1);
        else if (key == "comp_threshold_db") s.comp_threshold_db = clampd(d, -80, 0);
        else if (key == "comp_ratio") s.comp_ratio = clampd(d, 1, 50);
        else if (key == "comp_makeup_db") s.comp_makeup_db = clampd(d, 0, 40);
        else if (key == "reverb_mix") s.reverb_mix = clampd(d, 0, 1);
        else if (key == "reverb_room") s.reverb_room = clampd(d, 0, 1);
        else if (key == "output_gain_db") s.output_gain_db = clampd(d, -60, 24);
    }
    return s;
}

QualitySpec quality_spec(Quality q, int sample_rate) {
    int base = q == Quality::LowLatency ? 1024 : (q == Quality::Balanced ? 2048 : 4096);
    // Keep ~the same bin width as at 48 kHz.
    while (sample_rate > 72000 && base < 16384) {
        base *= 2;
        sample_rate /= 2;
    }
    return {base, 8};
}

VoiceChain::VoiceChain(int sample_rate, Quality quality, int block_size)
    : fs_(sample_rate),
      block_(block_size),
      quality_(quality),
      voice_(sample_rate, quality_spec(quality, sample_rate).fft_size, quality_spec(quality, sample_rate).overlap,
             block_size),
      dry_(voice_.latency()),
      bypass_delay_(voice_.latency()),
      gravel_(sample_rate),
      rumble_(sample_rate, BiquadKind::Highpass, 80.0),
      hp_(sample_rate, BiquadKind::Highpass, 80.0),
      lp_(sample_rate, BiquadKind::Lowpass, 18000.0),
      low_(sample_rate, BiquadKind::LowShelf, 200.0),
      mid_(sample_rate, BiquadKind::Peaking, 1500.0, 0.9),
      high_(sample_rate, BiquadKind::HighShelf, 5000.0),
      gate_(sample_rate),
      comp_(sample_rate),
      reverb_(sample_rate),
      limiter_(sample_rate),
      x_(block_size),
      wet_(block_size),
      dry_buf_(block_size) {}

void VoiceChain::reset() {
    voice_.reset();
    dry_.reset();
    bypass_delay_.reset();
    gravel_.reset();
    rumble_.reset(); hp_.reset(); lp_.reset(); low_.reset(); mid_.reset(); high_.reset();
    gate_.reset();
    comp_.reset();
    reverb_.reset();
    limiter_.reset();
}

void VoiceChain::process(const float* in, float* out, int n, const Settings& s) {
    double* x = x_.data();
    double* wet = wet_.data();
    double* dry = dry_buf_.data();

    const double in_gain = db_to_lin(s.input_gain_db);
    double peak = 0.0;
    for (int i = 0; i < n; i++) {
        x[i] = in[i] * in_gain;
        peak = std::max(peak, std::fabs(x[i]));
    }
    input_peak = peak;

    // Bypass runs through a matching delay so toggling it is an honest A/B.
    if (s.bypass) {
        // Keep the shifter and gravel state running (output discarded), so
        // toggling bypass never changes their phase history.
        voice_.process(x, wet, n);
        gravel_.process(wet, n, s.gravel, s.gravel_hz);
        dry_.process(x, dry, n);
        bypass_delay_.process(x, wet, n);
        peak = 0.0;
        for (int i = 0; i < n; i++) {
            out[i] = (float)wet[i];
            peak = std::max(peak, std::fabs(wet[i]));
        }
        output_peak = peak;
        return;
    }
    bypass_delay_.process(x, dry, n);  // discard; keeps the line's contents current

    rumble_.process(x, n);
    gate_.enabled = s.gate_enabled;
    gate_.threshold_db = s.gate_threshold_db;
    gate_.process(x, n);

    voice_.set_pitch(s.pitch);
    voice_.set_formant(s.formant);
    voice_.set_mode(s.mode);
    voice_.set_robot_pitch(s.robot_hz);
    voice_.set_tremor(s.tremor_hz, s.tremor_depth);
    voice_.set_jitter(s.jitter);
    voice_.set_breath(s.breath);
    voice_.process(x, wet, n);
    gravel_.process(wet, n, s.gravel, s.gravel_hz);
    dry_.process(x, dry, n);
    for (int i = 0; i < n; i++) x[i] = wet[i] * s.mix + dry[i] * (1.0 - s.mix);

    hp_.set(s.highpass_hz);
    lp_.set(s.lowpass_hz);
    low_.set(200.0, 0.707, s.low_db);
    mid_.set(1500.0, 0.9, s.mid_db);
    high_.set(5000.0, 0.707, s.high_db);
    hp_.process(x, n);
    lp_.process(x, n);
    low_.process(x, n);
    mid_.process(x, n);
    high_.process(x, n);
    drive(x, n, s.drive);

    comp_.enabled = s.comp_enabled;
    comp_.threshold_db = s.comp_threshold_db;
    comp_.ratio = s.comp_ratio;
    comp_.makeup_db = s.comp_makeup_db;
    comp_.process(x, n);

    if (reverb_room_ != s.reverb_room) {
        reverb_.set(s.reverb_room, 0.4);
        reverb_room_ = s.reverb_room;
    }
    reverb_.mix = s.reverb_mix;
    reverb_.process(x, n);

    const double out_gain = db_to_lin(s.output_gain_db);
    for (int i = 0; i < n; i++) x[i] *= out_gain;
    limiter_.process(x, n);
    peak = 0.0;
    for (int i = 0; i < n; i++) {
        out[i] = (float)x[i];
        peak = std::max(peak, std::fabs(x[i]));
    }
    output_peak = peak;
}

}  // namespace vc
