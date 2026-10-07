// Streams a raw float32 mono file through the C++ VoiceChain in fixed blocks.
// Used by tests/test_cpp_golden.py to hold the C++ port to the Python reference.
//
//   dsp_cli <sample_rate> <block> <settings.ini> <in.f32> <out.f32>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <sstream>
#include <vector>

#include "../dsp/chain.h"

int main(int argc, char** argv) {
    if (argc != 6) {
        std::cerr << "usage: dsp_cli <sample_rate> <block> <settings.ini> <in.f32> <out.f32>\n";
        return 2;
    }
    const int sr = std::atoi(argv[1]);
    const int block = std::atoi(argv[2]);
    std::ifstream sf(argv[3]);
    std::stringstream ss;
    ss << sf.rdbuf();
    const vc::Settings s = vc::parse_settings(ss.str());

    std::ifstream in(argv[4], std::ios::binary);
    std::vector<float> x;
    in.seekg(0, std::ios::end);
    const size_t bytes = (size_t)in.tellg();
    in.seekg(0);
    x.assign(bytes / sizeof(float), 0.0f);
    in.read(reinterpret_cast<char*>(x.data()), (std::streamsize)(x.size() * sizeof(float)));

    vc::VoiceChain chain(sr, s.quality, block);
    std::vector<float> y(x.size(), 0.0f);
    for (size_t i = 0; i + block <= x.size(); i += block) chain.process(&x[i], &y[i], block, s);

    std::ofstream out(argv[5], std::ios::binary);
    out.write(reinterpret_cast<const char*>(y.data()), (std::streamsize)(y.size() * sizeof(float)));
    std::cout << "latency " << chain.latency() << "\n";
    return 0;
}
