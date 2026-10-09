// In-place iterative radix-2 complex FFT. Allocation-free after construction,
// so it is safe on the audio thread.
#pragma once

#include <cmath>
#include <cstdint>
#include <stdexcept>
#include <vector>

namespace vc {

class FFT {
public:
    explicit FFT(int n) : n_(n), cos_(n / 2), sin_(n / 2), rev_(n) {
        if (n < 2 || (n & (n - 1)) != 0) throw std::invalid_argument("FFT size must be a power of two");
        const double two_pi = 2.0 * 3.14159265358979323846;
        for (int i = 0; i < n / 2; i++) {
            cos_[i] = std::cos(two_pi * i / n);
            sin_[i] = std::sin(two_pi * i / n);
        }
        int bits = 0;
        while ((1 << bits) < n) bits++;
        for (int i = 0; i < n; i++) {
            uint32_t r = 0;
            for (int b = 0; b < bits; b++) r |= ((i >> b) & 1u) << (bits - 1 - b);
            rev_[i] = r;
        }
    }

    int size() const { return n_; }

    // Forward transform, e^{-i...}, unscaled.
    void forward(double* re, double* im) const { transform(re, im, -1.0); }

    // Inverse transform, scaled by 1/N.
    void inverse(double* re, double* im) const {
        transform(re, im, 1.0);
        const double s = 1.0 / n_;
        for (int i = 0; i < n_; i++) {
            re[i] *= s;
            im[i] *= s;
        }
    }

private:
    void transform(double* re, double* im, double sign) const {
        const int n = n_;
        for (int i = 0; i < n; i++) {
            const int j = (int)rev_[i];
            if (j > i) {
                double t = re[i]; re[i] = re[j]; re[j] = t;
                t = im[i]; im[i] = im[j]; im[j] = t;
            }
        }
        for (int len = 2; len <= n; len <<= 1) {
            const int half = len >> 1;
            const int step = n / len;
            for (int start = 0; start < n; start += len) {
                for (int k = 0; k < half; k++) {
                    const double wr = cos_[k * step];
                    const double wi = sign * sin_[k * step];
                    const int a = start + k, b = a + half;
                    const double tr = re[b] * wr - im[b] * wi;
                    const double ti = re[b] * wi + im[b] * wr;
                    re[b] = re[a] - tr;
                    im[b] = im[a] - ti;
                    re[a] += tr;
                    im[a] += ti;
                }
            }
        }
    }

    int n_;
    std::vector<double> cos_, sin_;
    std::vector<uint32_t> rev_;
};

}  // namespace vc
