// C++ port of voicechanger/dsp/effects.py. Block semantics match the Python
// exactly (dynamics compute one gain per block and ramp across it), so both
// produce the same output for the same block size.
#pragma once

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <vector>

#include "rng.h"

namespace vc {

inline double db_to_lin(double db) { return std::pow(10.0, db / 20.0); }
inline double lin_to_db(double x) { return 20.0 * std::log10(std::max(x, 1e-12)); }

// One-pole smoothing coefficient for a per-block update.
inline double block_coef(double time_s, int block, double fs) {
    return std::exp(-block / std::max(time_s * fs, 1e-9));
}

// np.linspace(start, end, n, endpoint=False), or [end] when n == 1.
inline double ramp_at(double start, double end, int i, int n) {
    return n > 1 ? start + (end - start) * i / n : end;
}

inline double block_rms(const double* x, int n) {
    double s = 0.0;
    for (int i = 0; i < n; i++) s += x[i] * x[i];
    return std::sqrt(s / n);
}

// ---------------------------------------------------------------------------
// Filters
// ---------------------------------------------------------------------------

enum class BiquadKind { Lowpass, Highpass, Peaking, LowShelf, HighShelf };

// RBJ audio-EQ-cookbook biquad in transposed direct form II (same as
// scipy.signal.lfilter), with state carried between blocks.
class Biquad {
public:
    Biquad(double fs, BiquadKind kind, double f0, double q = 0.707, double gain_db = 0.0) : fs_(fs), kind_(kind) {
        set(f0, q, gain_db);
    }

    void set(double f0, double q = 0.707, double gain_db = 0.0) {
        if (f0 == f0_ && q == q_ && gain_db == g_) return;
        f0_ = f0; q_ = q; g_ = gain_db;
        const double pi = 3.14159265358979323846;
        const double f = std::min(std::max(f0, 1.0), fs_ * 0.49);
        const double w0 = 2 * pi * f / fs_;
        const double cw = std::cos(w0), sw = std::sin(w0);
        const double alpha = sw / (2 * q);
        const double A = std::pow(10.0, gain_db / 40.0);
        double b[3] = {1, 0, 0}, a[3] = {1, 0, 0};
        switch (kind_) {
            case BiquadKind::Lowpass:
                b[0] = (1 - cw) / 2; b[1] = 1 - cw; b[2] = (1 - cw) / 2;
                a[0] = 1 + alpha; a[1] = -2 * cw; a[2] = 1 - alpha;
                break;
            case BiquadKind::Highpass:
                b[0] = (1 + cw) / 2; b[1] = -(1 + cw); b[2] = (1 + cw) / 2;
                a[0] = 1 + alpha; a[1] = -2 * cw; a[2] = 1 - alpha;
                break;
            case BiquadKind::Peaking:
                b[0] = 1 + alpha * A; b[1] = -2 * cw; b[2] = 1 - alpha * A;
                a[0] = 1 + alpha / A; a[1] = -2 * cw; a[2] = 1 - alpha / A;
                break;
            case BiquadKind::LowShelf: {
                const double sq = 2 * std::sqrt(A) * alpha;
                b[0] = A * ((A + 1) - (A - 1) * cw + sq);
                b[1] = 2 * A * ((A - 1) - (A + 1) * cw);
                b[2] = A * ((A + 1) - (A - 1) * cw - sq);
                a[0] = (A + 1) + (A - 1) * cw + sq;
                a[1] = -2 * ((A - 1) + (A + 1) * cw);
                a[2] = (A + 1) + (A - 1) * cw - sq;
                break;
            }
            case BiquadKind::HighShelf: {
                const double sq = 2 * std::sqrt(A) * alpha;
                b[0] = A * ((A + 1) + (A - 1) * cw + sq);
                b[1] = -2 * A * ((A - 1) + (A + 1) * cw);
                b[2] = A * ((A + 1) + (A - 1) * cw - sq);
                a[0] = (A + 1) - (A - 1) * cw + sq;
                a[1] = 2 * ((A - 1) - (A + 1) * cw);
                a[2] = (A + 1) - (A - 1) * cw - sq;
                break;
            }
        }
        b0_ = b[0] / a[0]; b1_ = b[1] / a[0]; b2_ = b[2] / a[0];
        a1_ = a[1] / a[0]; a2_ = a[2] / a[0];
    }

    void process(double* x, int n) {
        for (int i = 0; i < n; i++) {
            const double in = x[i];
            const double y = b0_ * in + z0_;
            z0_ = b1_ * in - a1_ * y + z1_;
            z1_ = b2_ * in - a2_ * y;
            x[i] = y;
        }
    }

    void reset() { z0_ = z1_ = 0.0; }

private:
    double fs_;
    BiquadKind kind_;
    double f0_ = -1, q_ = -1, g_ = -1e9;
    double b0_ = 1, b1_ = 0, b2_ = 0, a1_ = 0, a2_ = 0;
    double z0_ = 0, z1_ = 0;
};

// Fixed integer delay; time-aligns the dry signal with the shifter.
class DelayLine {
public:
    explicit DelayLine(int samples) : buf_(std::max(samples, 0), 0.0) {}
    void process(const double* in, double* out, int n) {
        if (buf_.empty()) {
            if (out != in) std::copy(in, in + n, out);
            return;
        }
        const int d = (int)buf_.size();
        for (int i = 0; i < n; i++) {
            const double v = in[i];
            out[i] = buf_[pos_];
            buf_[pos_] = v;
            pos_ = (pos_ + 1) % d;
        }
    }
    void reset() { std::fill(buf_.begin(), buf_.end(), 0.0); pos_ = 0; }

private:
    std::vector<double> buf_;
    int pos_ = 0;
};

// ---------------------------------------------------------------------------
// Dynamics
// ---------------------------------------------------------------------------

class NoiseGate {
public:
    explicit NoiseGate(double fs, double threshold_db = -50.0, double attack = 0.002, double release = 0.12,
                       double hold = 0.08, double floor_db = -80.0)
        : fs_(fs), attack_(attack), release_(release), hold_(hold), floor_(db_to_lin(floor_db)),
          threshold_db(threshold_db) {}

    void process(double* x, int n) {
        if (!enabled || n == 0) {
            gain_ = 1.0;
            return;
        }
        const double level = lin_to_db(block_rms(x, n));
        if (level >= threshold_db) {
            open_ = true;
            held_ = 0.0;
        } else if (level < threshold_db - 6.0) {
            held_ += n / fs_;
            if (held_ >= hold_) open_ = false;
        }
        const double floor_db = lin_to_db(floor_);
        const double target_db = open_ ? 0.0 : floor_db;
        const double cur_db = lin_to_db(gain_);
        const double rate = -floor_db / (target_db > cur_db ? attack_ : release_);
        const double step = rate * n / fs_;
        const double new_db = target_db > cur_db ? std::min(cur_db + step, target_db) : std::max(cur_db - step, target_db);
        const double nw = db_to_lin(new_db);
        for (int i = 0; i < n; i++) x[i] *= ramp_at(gain_, nw, i, n);
        gain_ = nw;
    }

    bool is_open() const { return open_; }
    void reset() { gain_ = 1.0; open_ = true; held_ = 0.0; }

private:
    double fs_, attack_, release_, hold_, floor_;
    double gain_ = 1.0, held_ = 0.0;
    bool open_ = true;

public:
    double threshold_db;
    bool enabled = true;
};

class Compressor {
public:
    explicit Compressor(double fs) : fs_(fs) {}

    void process(double* x, int n) {
        if (!enabled || n == 0) {
            gain_reduction_db = 0.0;
            return;
        }
        const double level = lin_to_db(block_rms(x, n));
        const double c = block_coef(level > env_db_ ? attack : release, n, fs_);
        env_db_ = level + (env_db_ - level) * c;
        gain_reduction_db = curve(env_db_);
        const double nw = db_to_lin(gain_reduction_db + makeup_db);
        for (int i = 0; i < n; i++) x[i] *= ramp_at(gain_, nw, i, n);
        gain_ = nw;
    }

    void reset() { env_db_ = -120.0; gain_ = 1.0; gain_reduction_db = 0.0; }

    double threshold_db = -18.0, ratio = 3.0, attack = 0.005, release = 0.1, knee_db = 6.0, makeup_db = 0.0;
    bool enabled = true;
    double gain_reduction_db = 0.0;

private:
    double curve(double level_db) const {
        const double over = level_db - threshold_db;
        const double k = knee_db;
        const double slope = 1.0 - 1.0 / std::max(ratio, 1.0);
        if (2 * over < -k) return 0.0;
        if (2 * std::fabs(over) <= k && k > 0) return -slope * (over + k / 2) * (over + k / 2) / (2 * k);
        return -slope * over;
    }

    double fs_;
    double env_db_ = -120.0, gain_ = 1.0;
};

// Peak limiter: instant attack per block, smooth release, then a soft-knee
// clip so nothing above the ceiling ever leaves the chain.
class Limiter {
public:
    explicit Limiter(double fs, double ceiling_db = -1.0, double release = 0.08)
        : fs_(fs), ceiling_(db_to_lin(ceiling_db)), release_(release) {}

    void process(double* x, int n) {
        if (n == 0) return;
        double peak = 0.0;
        for (int i = 0; i < n; i++) peak = std::max(peak, std::fabs(x[i]));
        const double target = peak > 0 ? std::min(1.0, ceiling_ / peak) : 1.0;
        if (target < gain_) {
            for (int i = 0; i < n; i++) x[i] *= target;
            gain_ = target;
        } else {
            const double nw = target + (gain_ - target) * block_coef(release_, n, fs_);
            for (int i = 0; i < n; i++) x[i] *= ramp_at(gain_, nw, i, n);
            gain_ = nw;
        }
        const double c = ceiling_, k = 0.8 * c;
        for (int i = 0; i < n; i++) {
            const double a = std::fabs(x[i]);
            if (a > k) x[i] = (x[i] < 0 ? -1.0 : 1.0) * (k + (c - k) * std::tanh((a - k) / (c - k)));
        }
    }

    double ceiling() const { return ceiling_; }
    void reset() { gain_ = 1.0; }

private:
    double fs_, ceiling_, release_;
    double gain_ = 1.0;
};

// ---------------------------------------------------------------------------
// Colour
// ---------------------------------------------------------------------------

inline void drive(double* x, int n, double amount) {
    if (amount <= 0) return;
    const double k = 1.0 + 19.0 * amount;
    const double norm = std::tanh(k);
    for (int i = 0; i < n; i++) x[i] = std::tanh(k * x[i]) / norm;
}

// Vocal fry / gravel: a fast, slightly irregular train of raised-cosine dips
// in level at `hz`, like vocal folds slapping shut in uneven pulses; `amount`
// sets the dip depth. The rate wanders +/-25% every 256 samples, keyed on the
// absolute sample index through hash_uniform, so the result is the same for any
// block size and matches the Python. The phase advances even at amount 0, so
// the pulse train depends only on elapsed time, never on when it was switched on.
class GravelModulator {
public:
    static constexpr int kTick = 256;  // samples per random rate step

    explicit GravelModulator(double fs) : fs_(fs) {}

    void process(double* x, int n, double amount, double hz) {
        // Same ranges as the settings parser (and the Python), whatever the caller passes.
        amount = std::min(std::max(amount, 0.0), 1.0);
        hz = std::min(std::max(hz, 10.0), 200.0);
        const double two_pi = 2.0 * 3.14159265358979323846;
        for (int i = 0; i < n; i++, count_++) {
            const uint64_t tick = count_ / kTick;
            if (tick != tick_) {  // one draw per tick, not per sample
                tick_ = tick;
                wobble_ = hash_uniform(5, tick);
            }
            const double f = hz * (1.0 + 0.5 * (wobble_ - 0.5));
            // Python accumulates the block with cumsum and wraps once per
            // block; wrapping per sample instead only moves the phase by whole
            // turns, which cos cannot see.
            phase_ += two_pi * f / fs_;
            if (phase_ >= two_pi) phase_ -= two_pi;
            if (amount > 0) {
                const double m = 0.5 - 0.5 * std::cos(phase_);
                x[i] *= 1.0 - amount * m * m;
            }
        }
    }

    void reset() { phase_ = 0.0; count_ = 0; tick_ = UINT64_MAX; }

private:
    double fs_;
    double phase_ = 0.0;
    uint64_t count_ = 0;          // absolute sample index
    uint64_t tick_ = UINT64_MAX;  // tick whose draw is cached in wobble_ (none yet)
    double wobble_ = 0.0;
};

// Freeverb-style: 8 damped combs in parallel into 4 allpasses in series.
class Reverb {
public:
    explicit Reverb(double fs, double room = 0.6, double damp = 0.4) {
        static const int combs[8] = {1116, 1188, 1277, 1356, 1422, 1491, 1557, 1617};
        static const int aps[4] = {556, 441, 341, 225};
        const double s = fs / 44100.0;
        for (int d : combs) combs_.push_back(Line((int)(d * s)));
        for (int d : aps) aps_.push_back(Line((int)(d * s)));
        set(room, damp);
    }

    void set(double room, double damp) {
        room_ = room;
        damp_ = damp;
        fb_ = 0.7 + 0.28 * room;
    }

    double room() const { return room_; }

    void process(double* x, int n) {
        if (mix <= 0) return;
        for (int i = 0; i < n; i++) {
            const double in = x[i];
            double wet = 0.0;
            for (Line& c : combs_) {
                // y[n] = x[n] + fb * lp(y[n-D]),  lp[n] = (1-damp) d[n] + damp lp[n-1]
                const double delayed = c.y[c.pos];
                c.lp = (1.0 - damp_) * delayed + damp_ * c.lp;
                const double y = in + fb_ * c.lp;
                c.y[c.pos] = y;
                c.pos = (c.pos + 1) % (int)c.y.size();
                wet += y;
            }
            wet *= 0.12;
            for (Line& a : aps_) {
                // y[n] = -g x[n] + x[n-D] + g y[n-D]
                const double y = -0.5 * wet + a.x[a.pos] + 0.5 * a.y[a.pos];
                a.x[a.pos] = wet;
                a.y[a.pos] = y;
                a.pos = (a.pos + 1) % (int)a.y.size();
                wet = y;
            }
            x[i] = in * (1 - 0.5 * mix) + wet * mix;
        }
    }

    void reset() {
        for (Line& c : combs_) c.clear();
        for (Line& a : aps_) a.clear();
    }

    double mix = 0.0;

private:
    struct Line {
        explicit Line(int d) : x(d, 0.0), y(d, 0.0) {}
        void clear() { std::fill(x.begin(), x.end(), 0.0); std::fill(y.begin(), y.end(), 0.0); pos = 0; lp = 0.0; }
        std::vector<double> x, y;
        int pos = 0;
        double lp = 0.0;
    };
    std::vector<Line> combs_, aps_;
    double room_ = 0.6, damp_ = 0.4, fb_ = 0.868;
};

}  // namespace vc
