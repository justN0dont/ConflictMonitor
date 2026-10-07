// C++ port of voicechanger/dsp/spectral.py. Same algorithm, same numbers: the
// golden test (tests/test_cpp_golden.py) holds the two implementations to
// within floating-point noise of each other.
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
    // added so output is always available (see latency()).
    SpectralVoice(int sample_rate, int fft_size = 2048, int overlap = 8, int block_size = 0,
                  double lifter_seconds = 0.0012);

    void set_pitch(double semitones);
    void set_formant(double semitones);
    void set_mode(Mode m) { mode_ = m; }
    void set_robot_pitch(double hz) { robot_hz_ = hz < 20.0 ? 20.0 : hz; }
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
    void shift_peaks();  // fills spec_out_ from analysis buffers
    void overlap_add(double* out_hop);
    cd kernel(double d) const;
    double interp_env(double x) const;  // np.interp(x, k, log_env), clamped
    double rand_uniform();               // [0, 1)

    int sr_, n_, overlap_, hop_, half_, latency_, pad_, lifter_;
    double scale_, expct_;
    double pitch_ratio_ = 1.0, formant_ratio_ = 1.0, robot_hz_ = 110.0;
    Mode mode_ = Mode::Normal;
    uint64_t rng_ = 0x9E3779B97F4A7C15ull;

    FFT fft_;
    std::vector<double> window_, lifter_win_;
    std::vector<cd> ktab_;

    // streaming state
    std::vector<double> frame_, pending_, hop_out_, accum_, out_ring_;
    int pending_count_ = 0;
    int ring_read_ = 0, ring_count_ = 0;
    std::vector<double> last_phase_, sum_phase_;

    // per-frame scratch
    std::vector<double> re_, im_, mag_, phase_, true_freq_, log_env_, flat_, new_phase_;
    std::vector<cd> spec_out_;
    std::vector<int> peaks_;
    std::vector<double> peak_amp_, peak_fo_, peak_ph_;
    std::vector<int> peak_dest_;
};

}  // namespace vc
