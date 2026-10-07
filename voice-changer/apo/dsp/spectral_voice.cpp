#include "spectral_voice.h"

#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace vc {

namespace {
constexpr double kPi = 3.14159265358979323846;
constexpr double kTwoPi = 2.0 * kPi;
constexpr int kKMax = 6;    // kernel table spans +/- this many bins
constexpr int kKRes = 256;  // table points per bin
constexpr int kLobe = 3;    // bins drawn either side of each resynthesised peak
constexpr int kMaxBlockDefault = 8192;

// numpy's round() is round-half-to-even; nearbyint matches under the default
// rounding mode.
inline double round_even(double x) { return std::nearbyint(x); }

inline double wrap(double p) { return p - kTwoPi * round_even(p / kTwoPi); }

std::complex<double> dirichlet(double x, int n) {
    const double num = std::sin(kPi * x);
    const double den = std::sin(kPi * x / n);
    const double mag = std::fabs(den) < 1e-12 ? (double)n : num / den;
    return std::polar(1.0, kPi * x * (n - 1) / n) * mag;
}
}  // namespace

SpectralVoice::SpectralVoice(int sample_rate, int fft_size, int overlap, int block_size, double lifter_seconds)
    : sr_(sample_rate), n_(fft_size), overlap_(overlap), fft_(fft_size) {
    if (fft_size % overlap) throw std::invalid_argument("fft_size must be divisible by overlap");
    hop_ = n_ / overlap_;
    half_ = n_ / 2 + 1;
    pad_ = (block_size > 0 && block_size % hop_ == 0) ? 0 : hop_ - 1;
    latency_ = n_ - hop_ + pad_;
    scale_ = 1.0 / (0.375 * overlap_);
    expct_ = kTwoPi * hop_ / n_;

    window_.resize(n_);
    for (int i = 0; i < n_; i++) window_[i] = 0.5 - 0.5 * std::cos(kTwoPi * i / n_);

    const int lifter = (int)round_even(sample_rate * lifter_seconds);
    lifter_ = std::max(8, std::min(n_ / 4, lifter));
    lifter_win_.resize(n_);
    for (int q = 0; q < n_; q++) {
        const int dist = std::min(q, n_ - q);
        lifter_win_[q] = dist < lifter_ ? 1.0 : (dist == lifter_ ? 0.5 : 0.0);
    }

    const int tab = 2 * kKMax * kKRes + 1;
    ktab_.resize(tab);
    for (int i = 0; i < tab; i++) {
        const double d = (double)(i - kKMax * kKRes) / kKRes;
        ktab_[i] = 0.5 * dirichlet(d, n_) - 0.25 * dirichlet(d + 1, n_) - 0.25 * dirichlet(d - 1, n_);
    }

    const int max_block = block_size > 0 ? block_size : kMaxBlockDefault;
    frame_.assign(n_, 0.0);
    pending_.assign(hop_, 0.0);
    hop_out_.assign(hop_, 0.0);
    accum_.assign(n_, 0.0);
    out_ring_.assign(pad_ + max_block + 2 * n_, 0.0);
    last_phase_.assign(half_, 0.0);
    sum_phase_.assign(half_, 0.0);
    re_.assign(n_, 0.0);
    im_.assign(n_, 0.0);
    mag_.assign(half_, 0.0);
    phase_.assign(half_, 0.0);
    true_freq_.assign(half_, 0.0);
    log_env_.assign(half_, 0.0);
    flat_.assign(half_, 0.0);
    new_phase_.assign(half_, 0.0);
    spec_out_.assign(half_, cd(0, 0));
    peaks_.reserve(half_);
    peak_amp_.reserve(half_);
    peak_fo_.reserve(half_);
    peak_ph_.reserve(half_);
    peak_dest_.reserve(half_);
    reset();
}

void SpectralVoice::set_pitch(double semitones) { pitch_ratio_ = std::pow(2.0, semitones / 12.0); }
void SpectralVoice::set_formant(double semitones) { formant_ratio_ = std::pow(2.0, semitones / 12.0); }

void SpectralVoice::reset() {
    std::fill(frame_.begin(), frame_.end(), 0.0);
    std::fill(accum_.begin(), accum_.end(), 0.0);
    std::fill(last_phase_.begin(), last_phase_.end(), 0.0);
    std::fill(sum_phase_.begin(), sum_phase_.end(), 0.0);
    std::fill(out_ring_.begin(), out_ring_.end(), 0.0);
    pending_count_ = 0;
    ring_read_ = 0;
    ring_count_ = pad_;  // start with `pad_` zeros queued
}

void SpectralVoice::process(const double* in, double* out, int m) {
    const int cap = (int)out_ring_.size();
    for (int i = 0; i < m; i++) {
        pending_[pending_count_++] = in[i];
        if (pending_count_ == hop_) {
            pending_count_ = 0;
            std::copy(frame_.begin() + hop_, frame_.end(), frame_.begin());
            std::copy(pending_.begin(), pending_.end(), frame_.end() - hop_);
            process_frame(hop_out_.data());
            int w = (ring_read_ + ring_count_) % cap;
            for (int j = 0; j < hop_; j++) {
                out_ring_[w] = hop_out_[j];
                w = (w + 1) % cap;
            }
            ring_count_ += hop_;
        }
    }
    // A caller that honours block_size always has m samples ready. Anything
    // short (misuse) is padded with silence rather than reading garbage.
    for (int i = 0; i < m; i++) {
        if (ring_count_ > 0) {
            out[i] = out_ring_[ring_read_];
            ring_read_ = (ring_read_ + 1) % cap;
            ring_count_--;
        } else {
            out[i] = 0.0;
        }
    }
}

std::complex<double> SpectralVoice::kernel(double d) const {
    const double c = std::min<double>(std::max<double>(d, -kKMax), kKMax);
    const double pos = (c + kKMax) * kKRes;
    int i0 = (int)pos;
    if (i0 > (int)ktab_.size() - 2) i0 = (int)ktab_.size() - 2;
    const double t = pos - i0;
    return ktab_[i0] * (1.0 - t) + ktab_[i0 + 1] * t;
}

double SpectralVoice::interp_env(double x) const {
    if (x <= 0.0) return log_env_[0];
    if (x >= half_ - 1) return log_env_[half_ - 1];
    const int i0 = (int)x;
    const double t = x - i0;
    return log_env_[i0] * (1.0 - t) + log_env_[i0 + 1] * t;
}

double SpectralVoice::rand_uniform() {
    // xorshift64*
    rng_ ^= rng_ >> 12;
    rng_ ^= rng_ << 25;
    rng_ ^= rng_ >> 27;
    return (double)((rng_ * 2685821657736338717ull) >> 11) * (1.0 / 9007199254740992.0);
}

void SpectralVoice::process_frame(double* out_hop) {
    const int n = n_, half = half_, osamp = overlap_;

    // ---- analysis: magnitude + true per-bin frequency (in bins) ----
    for (int i = 0; i < n; i++) {
        re_[i] = frame_[i] * window_[i];
        im_[i] = 0.0;
    }
    fft_.forward(re_.data(), im_.data());
    double mag_max = 0.0;
    for (int k = 0; k < half; k++) {
        const double mag = std::hypot(re_[k], im_[k]);
        const double ph = std::atan2(im_[k], re_[k]);
        const double delta = wrap(ph - last_phase_[k] - k * expct_);
        last_phase_[k] = ph;
        mag_[k] = mag;
        phase_[k] = ph;
        true_freq_[k] = k + delta * osamp / kTwoPi;
        if (mag > mag_max) mag_max = mag;
    }

    // ---- envelope by cepstral liftering (floored at -80 dB re the peak) ----
    const double floor_add = mag_max * 1e-4 + 1e-12;
    for (int k = 0; k < half; k++) {
        re_[k] = std::log(mag_[k] + floor_add);
        im_[k] = 0.0;
    }
    for (int k = 1; k < half - 1; k++) {
        re_[n - k] = re_[k];
        im_[n - k] = 0.0;
    }
    fft_.inverse(re_.data(), im_.data());
    for (int q = 0; q < n; q++) {
        re_[q] *= lifter_win_[q];
        im_[q] = 0.0;
    }
    fft_.forward(re_.data(), im_.data());
    for (int k = 0; k < half; k++) {
        log_env_[k] = re_[k];
        flat_[k] = mag_[k] / std::exp(log_env_[k]);
    }

    // ---- excitation: shift or replace ----
    if (mode_ == Mode::Normal && std::fabs(pitch_ratio_ - 1.0) >= 1e-9) {
        shift_peaks();
        overlap_add(out_hop);
        return;
    }
    if (mode_ == Mode::Normal) {
        for (int k = 0; k < half; k++) spec_out_[k] = std::polar(flat_[k], phase_[k]);
    } else if (mode_ == Mode::Robot) {
        std::fill(spec_out_.begin(), spec_out_.end(), cd(0, 0));
        const double f0 = robot_hz_ * pitch_ratio_ * n / sr_;
        const int count = (int)((half - 1) / f0);
        // All phases come from the previous frame (numpy evaluates the whole
        // vector before assigning), so read before any write.
        for (int h = 1; h <= count; h++) {
            const double f = h * f0;
            const int idx = (int)round_even(f);
            peak_ph_.push_back(wrap(sum_phase_[idx] + kTwoPi * f / osamp));
        }
        for (int h = 1; h <= count; h++) {
            const int idx = (int)round_even(h * f0);
            spec_out_[idx] = std::polar(1.0, peak_ph_[h - 1]);
        }
        peak_ph_.clear();
    } else {
        for (int k = 0; k < half; k++) spec_out_[k] = std::polar(1.0, rand_uniform() * kTwoPi);
    }

    // ---- re-apply the (optionally shifted) envelope ----
    for (int k = 0; k < half; k++) spec_out_[k] *= std::exp(interp_env(k / formant_ratio_));
    if (mode_ != Mode::Normal) {
        double out_e = 0.0, in_e = 0.0;
        for (int k = 0; k < half; k++) {
            out_e += std::norm(spec_out_[k]);
            in_e += mag_[k] * mag_[k];
        }
        double g = out_e > 0 ? std::sqrt(in_e / out_e) : 1.0;
        if (mode_ == Mode::Whisper) g *= 0.375 * osamp / std::sqrt(35.0 / 128.0 * osamp);
        for (int k = 0; k < half; k++) spec_out_[k] *= g;
    }
    for (int k = 0; k < half; k++) sum_phase_[k] = std::arg(spec_out_[k]);
    overlap_add(out_hop);
}

void SpectralVoice::shift_peaks() {
    const int half = half_;
    const double r = pitch_ratio_;
    std::fill(spec_out_.begin(), spec_out_.end(), cd(0, 0));

    double mag_max = 0.0;
    for (int k = 0; k < half; k++) mag_max = std::max(mag_max, mag_[k]);
    const double floor = mag_max * 1e-4;

    peaks_.clear();
    peak_amp_.clear();
    peak_fo_.clear();
    peak_dest_.clear();
    peak_ph_.clear();
    for (int j = 1; j < half - 1; j++) {
        if (mag_[j] > mag_[j - 1] && mag_[j] >= mag_[j + 1] && mag_[j] > floor) {
            const double fp = true_freq_[j];
            const double fo = fp * r;
            // Sidelobes are local maxima too, but report their parent's
            // frequency, far from their own bin: skip them.
            if (std::fabs(fp - j) <= 0.75 && fo > 0.5 && fo < half - 1.5) {
                peaks_.push_back(j);
                const std::complex<double> h = kernel(fp - j);
                double amp = mag_[j] / std::max(std::abs(h), 1e-12);
                amp *= std::exp(interp_env(fo / formant_ratio_) - interp_env(fp));
                peak_amp_.push_back(amp);
                peak_fo_.push_back(fo);
                const int dest = (int)round_even(fo);
                peak_dest_.push_back(dest);
                peak_ph_.push_back(wrap(sum_phase_[dest] + kTwoPi * fo / overlap_));
            }
        }
    }
    if (peaks_.empty()) return;  // silence; phase memory left as is

    std::fill(new_phase_.begin(), new_phase_.end(), 0.0);
    const size_t np = peaks_.size();
    for (size_t i = 0; i < np; i++) new_phase_[peak_dest_[i]] = peak_ph_[i];
    std::copy(new_phase_.begin(), new_phase_.end(), sum_phase_.begin());

    for (size_t i = 0; i < np; i++) {
        const cd c = std::polar(peak_amp_[i], peak_ph_[i]);
        for (int o = -kLobe; o <= kLobe; o++) {
            const int b = peak_dest_[i] + o;
            if (b < 0 || b >= half) continue;
            spec_out_[b] += c * kernel(peak_fo_[i] - b);
        }
    }

    double out_e = 0.0, in_e = 0.0;
    for (int k = 0; k < half; k++) {
        out_e += std::norm(spec_out_[k]);
        in_e += mag_[k] * mag_[k];
    }
    if (out_e > 0) {
        const double g = std::sqrt(in_e / out_e);
        for (int k = 0; k < half; k++) spec_out_[k] *= g;
    }
}

void SpectralVoice::overlap_add(double* out_hop) {
    const int n = n_, half = half_;
    // irfft: Hermitian extension; imaginary parts of DC and Nyquist are ignored.
    for (int k = 0; k < half; k++) {
        re_[k] = spec_out_[k].real();
        im_[k] = spec_out_[k].imag();
    }
    im_[0] = 0.0;
    im_[half - 1] = 0.0;
    for (int k = 1; k < half - 1; k++) {
        re_[n - k] = re_[k];
        im_[n - k] = -im_[k];
    }
    fft_.inverse(re_.data(), im_.data());
    for (int i = 0; i < n; i++) accum_[i] += re_[i] * window_[i] * scale_;
    for (int i = 0; i < hop_; i++) out_hop[i] = accum_[i];
    std::copy(accum_.begin() + hop_, accum_.end(), accum_.begin());
    std::fill(accum_.end() - hop_, accum_.end(), 0.0);
}

}  // namespace vc
