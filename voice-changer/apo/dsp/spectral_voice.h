// C++ port of voicechanger/dsp/spectral.py. Same algorithm, same numbers: the
// golden test (tests/test_cpp_golden.py) holds the two implementations to
// within floating-point noise of each other. That includes the character
// controls (tremor, jitter, breath) and whisper noise: their randomness comes
// from the counter-based hash_uniform (rng.h) keyed on the frame count, so
// both languages draw the same numbers.
//
// Real-time safe: every buffer is allocated in the constructor.
#pragma once

#include <complex>
#include <cstdint>
#include <vector>

#include "fft.h"

namespace vc {

enum class Mode { Normal = 0, Robot = 1, Whisper = 2 };

class SpectralVoice {
public:
    // block_size: the block length process() will be called with. When it is a
    // multiple of the hop no padding is needed; otherwise hop-1 samples are
    // added so output is always available (see latency()). always_pad adds
    // them regardless, for callers whose blocks may also be shorter.
    SpectralVoice(int sample_rate, int fft_size = 2048, int overlap = 8, int block_size = 0, bool always_pad = false,
                  double lifter_seconds = 0.0012);

    void set_pitch(double semitones);
    void set_formant(double semitones);
    void set_mode(Mode m) { mode_ = m; }
    void set_robot_pitch(double hz) { robot_hz_ = hz < 20.0 ? 20.0 : hz; }
    // Character controls, clamped to the settings parser's ranges like the Python.
    void set_tremor(double hz, double depth_st);  // vibrato rate, peak swing in semitones (0 = off)
    void set_jitter(double amount);               // 0..1, up to +/-0.6 semitone of random pitch drift
    void set_breath(double amount);               // 0..1, share of frame energy turned into aspiration
    void reset();

    // Process m samples. m must not exceed the capacity given by
    // max_block (block_size, or 8192 if 0).
    void process(const double* in, double* out, int m);

    int latency() const { return latency_; }
    int hop() const { return hop_; }
    int fft_size() const { return n_; }

private:
    using cd = std::complex<double>;

    void process_frame(double* out_hop);
    double character(double& gain);  // this frame's pitch ratio (and gain) after tremor and jitter
    void shift_peaks(double r);      // fills spec_out_ from analysis buffers
    void add_breath();               // mixes aspiration noise into spec_out_
    void overlap_add(double* out_hop);
    cd kernel(double d) const;
    double interp_env(double x) const;  // np.interp(x, k, log_env), clamped

    int sr_, n_, overlap_, hop_, half_, latency_, pad_, lifter_;
    double scale_, expct_, jitter_coef_, noise_comp_;
    double pitch_ratio_ = 1.0, formant_ratio_ = 1.0, robot_hz_ = 110.0;
    double tremor_hz_ = 5.5, tremor_depth_ = 0.0, jitter_ = 0.0, breath_ = 0.0;
    Mode mode_ = Mode::Normal;

    FFT fft_;
    std::vector<double> window_, lifter_win_;
    std::vector<cd> ktab_;

    // streaming state
    std::vector<double> frame_, pending_, hop_out_, accum_, out_ring_;
    int pending_count_ = 0;
    int ring_read_ = 0, ring_count_ = 0;
    std::vector<double> last_phase_, sum_phase_;
    uint64_t frame_index_ = 0;  // frames since reset: the counter for every random draw
    double tremor_phase_ = 0.0, jitter_state_ = 0.0;

    // per-frame scratch
    std::vector<double> re_, im_, mag_, phase_, true_freq_, log_env_, flat_, new_phase_;
    std::vector<double> breath_tilt_, breath_mag_;
    std::vector<cd> spec_out_;
    std::vector<int> peaks_;
    std::vector<double> peak_amp_, peak_fo_, peak_ph_;
    std::vector<int> peak_dest_;
};

}  // namespace vc
