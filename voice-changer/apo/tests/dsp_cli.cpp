// Streams a raw float32 mono file through the C++ VoiceChain in fixed blocks.
// Used by tests/test_cpp_golden.py to hold the C++ port to the Python reference.
//
//   dsp_cli <sample_rate> <block> <settings.ini> <in.f32> <out.f32> [<alt.ini> <period>]
//       With alt.ini, blocks alternate between the two settings every `period`
//       blocks (first `period` blocks use settings.ini), as a UI changing
//       settings live would. The chain's quality comes from settings.ini.
//       Prints the chain's latency and the number of heap allocations made
//       inside VoiceChain::process, which must be zero: the APO calls it on
//       the real-time audio thread.
//   dsp_cli --hash <stream> <counter>...
//       Prints hash_uniform(stream, counter) for each counter, one per line,
//       with enough digits to round-trip the double exactly.
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <new>
#include <sstream>
#include <string>
#include <vector>

#include "../dsp/chain.h"
#include "../dsp/rng.h"

// Counting replacements for the global allocation functions.
static long g_allocations = 0;
void* operator new(std::size_t n) {
    g_allocations++;
    if (void* p = std::malloc(n ? n : 1)) return p;
    throw std::bad_alloc();
}
void* operator new[](std::size_t n) { return operator new(n); }
void operator delete(void* p) noexcept { std::free(p); }
void operator delete[](void* p) noexcept { std::free(p); }
void operator delete(void* p, std::size_t) noexcept { std::free(p); }
void operator delete[](void* p, std::size_t) noexcept { std::free(p); }

namespace {
vc::Settings load_settings(const char* path) {
    std::ifstream sf(path);
    std::stringstream ss;
    ss << sf.rdbuf();
    return vc::parse_settings(ss.str());
}
}  // namespace

int main(int argc, char** argv) {
    if (argc >= 3 && std::string(argv[1]) == "--hash") {
        const uint64_t stream = std::strtoull(argv[2], nullptr, 10);
        for (int i = 3; i < argc; i++)
            std::printf("%.17g\n", vc::hash_uniform(stream, std::strtoull(argv[i], nullptr, 10)));
        return 0;
    }
    if (argc != 6 && argc != 8) {
        std::cerr << "usage: dsp_cli <sample_rate> <block> <settings.ini> <in.f32> <out.f32> [<alt.ini> <period>]\n"
                     "       dsp_cli --hash <stream> <counter>...\n";
        return 2;
    }
    const int sr = std::atoi(argv[1]);
    const int block = std::atoi(argv[2]);
    const vc::Settings s = load_settings(argv[3]);
    const vc::Settings alt = argc == 8 ? load_settings(argv[6]) : s;
    const size_t period = argc == 8 ? (size_t)std::atoi(argv[7]) : 0;

    std::ifstream in(argv[4], std::ios::binary);
    std::vector<float> x;
    in.seekg(0, std::ios::end);
    const size_t bytes = (size_t)in.tellg();
    in.seekg(0);
    x.assign(bytes / sizeof(float), 0.0f);
    in.read(reinterpret_cast<char*>(x.data()), (std::streamsize)(x.size() * sizeof(float)));

    vc::VoiceChain chain(sr, s.quality, block);
    std::vector<float> y(x.size(), 0.0f);
    const long allocations_before = g_allocations;
    for (size_t i = 0, b = 0; i + block <= x.size(); i += block, b++) {
        const bool use_alt = period > 0 && (b / period) % 2 == 1;
        chain.process(&x[i], &y[i], block, use_alt ? alt : s);
    }
    const long allocations = g_allocations - allocations_before;

    std::ofstream out(argv[5], std::ios::binary);
    out.write(reinterpret_cast<const char*>(y.data()), (std::streamsize)(y.size() * sizeof(float)));
    std::cout << "latency " << chain.latency() << "\nallocations " << allocations << "\n";
    return 0;
}
