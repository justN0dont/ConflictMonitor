// C++ port of voicechanger/dsp/rng.py: counter-based random numbers.
//
// Value `counter` of stream `stream` is a pure function of the pair (a
// SplitMix64 finaliser over a mix of both), so it never depends on block size,
// on what earlier frames drew, or on the language. uint64_t arithmetic wraps
// mod 2**64 by definition, which is exactly what the Python masks emulate.
//
// Streams in use: 1 tremor-rate wobble, 2 jitter, 3 breath noise phase,
// 4 whisper noise phase, 5 gravel rate wobble.
#pragma once

#include <cstdint>

namespace vc {

// Uniform in [0, 1), determined entirely by (stream, counter).
inline double hash_uniform(uint64_t stream, uint64_t counter) {
    uint64_t z = stream * 0xD1B54A32D192ED03ull + counter * 0x9E3779B97F4A7C15ull + 0x632BE59BD9B4E019ull;
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBull;
    z ^= z >> 31;
    // 53 random bits -> a double in [0, 1), exactly representable.
    return (double)(z >> 11) * (1.0 / 9007199254740992.0);
}

}  // namespace vc
