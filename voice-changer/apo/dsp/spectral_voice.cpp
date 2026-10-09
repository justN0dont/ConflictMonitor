#include "spectral_voice.h"

#include <algorithm>
#include <cmath>
#include <stdexcept>

#include "rng.h"

namespace vc {

namespace {
constexpr double kPi = 3.14159265358979323846;
constexpr double kTwoPi = 2.0 * kPi;
constexpr int kKMax = 6;    // kernel table spans +/- this many bins
constexpr int kKRes = 256;  // table points per bin
constexpr int kLobe = 3;    // bins drawn either side of each resynthesised peak
// A sinusoid continues the previous frame's nearest one within kTrack bins at
// balanced quality at 48 kHz (hop 256, FFT 2048): a glide of up to kTrackRate
// Hz per second. The window scales to that rate at other hops and sample
// rates, but never drops below kTrack bins (see the constructor).
constexpr double kTrack = 2.0;
constexpr double kTrackRate = kTrack * 48000.0 * 48000.0 / (256 * 2048);
constexpr int kMaxBlockDefault = 8192;

// numpy's round() is round-half-to-even; nearbyint matches under the default
// rounding mode.
inline double round_even(double x) { return std::nearbyint(x); }

inline double wrap(double p) { return p - kTwoPi * round_even(p / kTwoPi); }

// Per-frame decay of jitter's 25 ms glide toward each new random target.
inline double glide_coef(int hop, int sample_rate) { return std::exp(-hop / (0.025 * sample_rate)); }

std::complex<double> dirichlet(double x, int n) {
    const double num = std::sin(kPi * x);
    const double den = std::sin(kPi * x / n);
    const double mag = std::fabs(den) < 1e-12 ? (double)n : num / den;
    return std::polar(1.0, kPi * x * (n - 1) / n) * mag;
}
}  // namespace

SpectralVoice::SpectralVoice(int sample_rate, int fft_size, int overlap, int block_size, bool always_pad,
                             double lifter_seconds, bool track_peaks)
    : sr_(sample_rate), n_(fft_size), overlap_(overlap), track_peaks_(track_peaks), fft_(fft_size) {
    if (fft_size % overlap) throw std::invalid_argument("fft_size must be divisible by overlap");
    hop_ = n_ / overlap_;
    half_ = n_ / 2 + 1;
    pad_ = (block_size > 0 && block_size % hop_ == 0 && !always_pad) ? 0 : hop_ - 1;
    latency_ = n_ - hop_ + pad_;
    scale_ = 1.0 / (0.375 * overlap_);
    expct_ = kTwoPi * hop_ / n_;
    // Jitter glides to each new random target with a 25 ms time constant.
    // That smoothing leaves (1-c)/(1+c) of the target's variance, which grows
    // with the hop, so the target is scaled to give the pitch spread of the
    // reference hop (256 at 48 kHz: balanced) at every quality and rate.
    // (Products of the same two factors, so it is exactly 1 there.)
    const double c = glide_coef(hop_, sample_rate), c_ref = glide_coef(256, 48000);
    jitter_coef_ = c;
    jitter_norm_ = std::sqrt(((1.0 + c) * (1.0 - c_ref)) / ((1.0 - c) * (1.0 + c_ref)));
    // A partial gliding at a given rate moves hop/sr * rate Hz per hop, which is
    // hop * n / sr^2 * rate bins: four times as many at high-quality as at
    // balanced, nine times at 16 kHz. The window scales to follow the same
    // glide rate everywhere (2 bins at balanced and low-latency at 48 kHz, 8 at
    // high-quality, 18 at 16 kHz balanced). Same operation order as the Python.
    track_ = std::max(kTrack, kTrackRate * hop_ * n_ / ((double)sample_rate * sample_rate));
    // Noise frames are mutually incoherent, so overlap-add sums their power,
    // not their amplitude: coherent 0.375*o vs incoherent sqrt(35/128*o).
    noise_comp_ = 0.375 * overlap_ / std::sqrt(35.0 / 128.0 * overlap_);

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
    // At most one sinusoid per bin: shift_peaks' peaks are local maxima, and
    // robot keeps only the harmonic drawn in each bin.
    prev_f_.assign(half_, 0.0);
    prev_drawn_.assign(half_, 0.0);
    prev_ph_.assign(half_, 0.0);
    re_.assign(n_, 0.0);
    im_.assign(n_, 0.0);
    mag_.assign(half_, 0.0);
    phase_.assign(half_, 0.0);
    true_freq_.assign(half_, 0.0);
    log_env_.assign(half_, 0.0);
    flat_.assign(half_, 0.0);
    new_phase_.assign(half_, 0.0);
    // Breath noise is tilted toward the highs like real aspiration
    // (turbulence at the glottis is weak below ~1.5 kHz).
    breath_tilt_.assign(half_, 0.0);
    for (int k = 0; k < half_; k++) {
        const double fk = (double)k * sample_rate / n_;
        breath_tilt_[k] = fk * fk / (fk * fk + 1500.0 * 1500.0);
    }
    breath_mag_.assign(half_, 0.0);
    spec_out_.assign(half_, cd(0, 0));
    peaks_.reserve(half_);
    peak_amp_.reserve(half_);
    peak_fo_.reserve(half_);
    peak_ph_.reserve(half_);  // shift_peaks and robot keep at most one entry per bin
    peak_dest_.reserve(half_);
    reset();
}

void SpectralVoice::set_pitch(double semitones) { pitch_ratio_ = std::pow(2.0, semitones / 12.0); }
void SpectralVoice::set_formant(double semitones) { formant_ratio_ = std::pow(2.0, semitones / 12.0); }

void SpectralVoice::set_tremor(double hz, double depth_st) {
    tremor_hz_ = std::min(std::max(hz, 0.5), 15.0);
    tremor_depth_ = std::min(std::max(depth_st, 0.0), 3.0);
}
void SpectralVoice::set_jitter(double amount) { jitter_ = std::min(std::max(amount, 0.0), 1.0); }
void SpectralVoice::set_breath(double amount) { breath_ = std::min(std::max(amount, 0.0), 1.0); }

void SpectralVoice::reset() {
    std::fill(frame_.begin(), frame_.end(), 0.0);
    std::fill(accum_.begin(), accum_.end(), 0.0);
    std::fill(last_phase_.begin(), last_phase_.end(), 0.0);
    std::fill(sum_phase_.begin(), sum_phase_.end(), 0.0);
    prev_count_ = 0;
    std::fill(out_ring_.begin(), out_ring_.end(), 0.0);
    pending_count_ = 0;
    ring_read_ = 0;
    ring_count_ = pad_;  // start with `pad_` zeros queued
    frame_index_ = 0;
    tremor_phase_ = 0.0;
    tremor_cycle_ = 0;
    jitter_state_ = 0.0;
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

double SpectralVoice::character(double& gain) {
    // Depends only on the frame count, never on the audio. At neutral settings
    // the factors are 2**0 and 10**0, both exactly 1.0, so the output is
    // bit-identical to running without them.
    const uint64_t f = frame_index_;
    // Each tremor cycle runs at its own rate, up to +/-10% off (about 6% RMS),
    // so it doesn't sound like a metronome: natural tremor varies by several
    // percent from cycle to cycle. Drawn per cycle, not per frame, where the
    // draws would average out over the cycle. The phase runs even at zero
    // depth, so the cycles depend only on elapsed frames, not on when tremor
    // was turned up.
    const double rate = tremor_hz_ * (1.0 + 0.2 * (hash_uniform(1, tremor_cycle_) - 0.5));
    tremor_phase_ += kTwoPi * rate * hop_ / sr_;
    while (tremor_phase_ >= kTwoPi) {
        tremor_phase_ -= kTwoPi;
        tremor_cycle_++;
    }
    const double s = std::sin(tremor_phase_);
    const double tremor_st = tremor_depth_ * s;
    gain = std::pow(10.0, 1.5 * tremor_depth_ * s / 20.0);  // real vibrato swings loudness too
    const double target = jitter_ * 0.6 * jitter_norm_ * (2.0 * hash_uniform(2, f) - 1.0);
    jitter_state_ = target + (jitter_state_ - target) * jitter_coef_;
    return pitch_ratio_ * std::pow(2.0, (tremor_st + jitter_state_) / 12.0);
}

void SpectralVoice::process_frame(double* out_hop) {
    const int n = n_, half = half_, osamp = overlap_;
    const uint64_t f = frame_index_;
    double gain = 1.0;
    const double r = character(gain);

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
    if (mode_ == Mode::Normal && std::fabs(r - 1.0) >= 1e-9) {
        shift_peaks(r);  // sets sum_phase_
    } else {
        if (mode_ == Mode::Normal) {
            for (int k = 0; k < half; k++) spec_out_[k] = std::polar(flat_[k], phase_[k]);
            prev_count_ = 0;  // no sinusoids to continue: per-bin phases only
        } else if (mode_ == Mode::Robot) {
            std::fill(spec_out_.begin(), spec_out_.end(), cd(0, 0));
            const double f0 = robot_hz_ * r * n / sr_;
            const int count = (int)((half - 1) / f0);
            // Phases come from the previous frame's sum_phase_ and prev_*, which
            // are only rewritten below. Where harmonics share a bin the last
            // one wins, as in numpy's fancy assignment, and only it is kept
            // for the next frame (so a low f0, with more harmonics than bins,
            // cannot outgrow the lists). Each harmonic is a single-bin line:
            // drawn at its bin.
            peak_fo_.clear();
            peak_dest_.clear();
            peak_ph_.clear();
            int cursor = 0;
            for (int h = 1; h <= count; h++) {
                const double fh = h * f0;
                const int idx = (int)round_even(fh);
                const double ph = next_phase(fh, idx, idx, cursor);
                spec_out_[idx] = std::polar(1.0, ph);
                if (!peak_dest_.empty() && peak_dest_.back() == idx) {
                    peak_fo_.back() = fh;
                    peak_ph_.back() = ph;
                } else {
                    peak_fo_.push_back(fh);
                    peak_dest_.push_back(idx);
                    peak_ph_.push_back(ph);
                }
            }
            keep_sinusoids(true);
        } else {
            const uint64_t first = f * (uint64_t)half;
            for (int k = 0; k < half; k++) {
                const double ph = kTwoPi * hash_uniform(4, first + k);
                spec_out_[k] = cd(std::cos(ph), std::sin(ph));
            }
            prev_count_ = 0;
        }

        // ---- re-apply the (optionally shifted) envelope ----
        for (int k = 0; k < half; k++) spec_out_[k] *= std::exp(interp_env(k / formant_ratio_));
        if (mode_ != Mode::Normal) {
            // Replaced excitations carry no loudness of their own; match frame energy.
            double out_e = 0.0, in_e = 0.0;
            for (int k = 0; k < half; k++) {
                out_e += std::norm(spec_out_[k]);
                in_e += mag_[k] * mag_[k];
            }
            double g = out_e > 0 ? std::sqrt(in_e / out_e) : 1.0;
            if (mode_ == Mode::Whisper) g *= noise_comp_;
            for (int k = 0; k < half; k++) spec_out_[k] *= g;
        }
        // Empty bins get phase 0, not the arg of a signed zero (see spectral.py):
        // silence scales bins to +-0, and arg(-0-0i) = -pi would be carried
        // into every later frame, differently from numpy.
        for (int k = 0; k < half; k++) sum_phase_[k] = spec_out_[k] == cd(0, 0) ? 0.0 : std::arg(spec_out_[k]);
    }

    // Breath goes in after the phase memory is updated: noise phases must
    // never be propagated into the next frame's partials.
    if (breath_ > 0 && mode_ != Mode::Whisper) add_breath();
    for (int k = 0; k < half; k++) spec_out_[k] *= gain;
    frame_index_++;
    overlap_add(out_hop);
}

void SpectralVoice::add_breath() {
    // Trades `breath` of the frame's energy for noise under the (formant-
    // shifted) envelope, so it whispers the same vowel. Splitting the energy
    // rather than adding to it changes the texture more than the loudness.
    const int half = half_;
    double e_h = 0.0, e_n = 0.0;
    for (int k = 0; k < half; k++) {
        const double m = std::exp(interp_env(k / formant_ratio_)) * breath_tilt_[k];
        breath_mag_[k] = m;
        e_h += std::norm(spec_out_[k]);
        e_n += m * m;
    }
    if (e_h <= 0 || e_n <= 0) return;
    const double g = std::sqrt(breath_ * e_h / e_n) * noise_comp_;
    const double keep = std::sqrt(1.0 - breath_);
    const uint64_t first = frame_index_ * (uint64_t)half;
    for (int k = 0; k < half; k++) {
        const double ph = kTwoPi * hash_uniform(3, first + k);
        spec_out_[k] = keep * spec_out_[k] + breath_mag_[k] * g * cd(std::cos(ph), std::sin(ph));
    }
}

void SpectralVoice::shift_peaks(double r) {
    const int half = half_;
    std::fill(spec_out_.begin(), spec_out_.end(), cd(0, 0));

    double mag_max = 0.0;
    for (int k = 0; k < half; k++) mag_max = std::max(mag_max, mag_[k]);
    const double floor = mag_max * 1e-4;

    peaks_.clear();
    peak_amp_.clear();
    peak_fo_.clear();
    peak_dest_.clear();
    peak_ph_.clear();
    int cursor = 0;
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
                peak_ph_.push_back(next_phase(fo, fo, dest, cursor));  // each lobe is drawn at its exact frequency
            }
        }
    }
    if (peaks_.empty()) return;  // silence; phase memory left as is

    std::fill(new_phase_.begin(), new_phase_.end(), 0.0);
    const size_t np = peaks_.size();
    for (size_t i = 0; i < np; i++) new_phase_[peak_dest_[i]] = peak_ph_[i];
    std::copy(new_phase_.begin(), new_phase_.end(), sum_phase_.begin());
    keep_sinusoids(false);

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

double SpectralVoice::next_phase(double f, double drawn, int dest, int& cursor) const {
    // Continue the previous frame's nearest sinusoid within track_ bins (the
    // lower one on a tie), else take the per-bin memory, as before. Per-bin
    // memory alone loses a partial whenever its bin changes (tremor, jitter,
    // the speaker's own glides): the frame is then drawn out of phase with
    // the frames it overlaps and the partial cancels for about a frame. The
    // continued phase is matched midway between the frames' centres, where
    // they overlap most; a frame-start match leaves them pi x (frequency
    // change) apart there. `drawn` is the frequency the frame contains: the
    // sinusoid's own for a window lobe, its bin for robot's single-bin lines.
    const double per_bin = wrap(sum_phase_[dest] + kTwoPi * f / overlap_);
    const int m = prev_count_;
    if (!track_peaks_ || m == 0) return per_bin;
    while (cursor < m && prev_f_[cursor] < f) cursor++;  // np.searchsorted, side="left"
    const int lo = cursor > 0 ? cursor - 1 : 0, hi = cursor < m ? cursor : m - 1;
    const double d_lo = std::fabs(f - prev_f_[lo]), d_hi = std::fabs(prev_f_[hi] - f);
    const int near = d_hi < d_lo ? hi : lo;
    if (std::min(d_lo, d_hi) > track_) return per_bin;
    return wrap(prev_ph_[near] + kPi * (prev_f_[near] + f) / overlap_ + kPi * (prev_drawn_[near] - drawn));
}

void SpectralVoice::keep_sinusoids(bool drawn_at_bin) {
    if (!track_peaks_) return;
    const int count = (int)peak_fo_.size();
    for (int i = 0; i < count; i++) {
        prev_f_[i] = peak_fo_[i];
        prev_drawn_[i] = drawn_at_bin ? (double)peak_dest_[i] : peak_fo_[i];
        prev_ph_[i] = peak_ph_[i];
    }
    prev_count_ = count;
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
