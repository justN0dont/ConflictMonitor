// Drives VoiceChangerAPO.dll the way audiodg.exe does: class factory ->
// Initialize -> format negotiation -> LockForProcess -> APOProcess per period
// -> UnlockForProcess. Checks the audio that comes out.
//
//   apo_harness <path\to\VoiceChangerAPO.dll>
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <string>
#include <vector>

#include "../src/apo_headers.h"
#include "../src/media_type.h"

namespace {

int g_failures = 0;
#define CHECK(cond, ...)                                  \
    do {                                                  \
        if (cond) {                                       \
            printf("  ok    ");                           \
        } else {                                          \
            printf("  FAIL  ");                           \
            g_failures++;                                 \
        }                                                 \
        printf(__VA_ARGS__);                              \
        printf("\n");                                     \
    } while (0)

const double kPi = 3.14159265358979323846;
const UINT32 kRate = 48000, kChannels = 2, kPeriod = 480;

std::wstring g_dir;

void WriteSettings(const char* text) {
    const std::wstring tmp = g_dir + L"\\apo-settings.ini.tmp";
    FILE* f = _wfopen(tmp.c_str(), L"wb");
    fputs(text, f);
    fclose(f);
    MoveFileExW(tmp.c_str(), (g_dir + L"\\apo-settings.ini").c_str(), MOVEFILE_REPLACE_EXISTING);
}

// Harmonic-rich "voice" with a formant-like resonance near 700 Hz.
std::vector<float> Voice(double f0, double seconds, double& phase_state) {
    const size_t n = (size_t)(seconds * kRate);
    std::vector<float> x(n);
    for (size_t i = 0; i < n; i++) {
        const double t = phase_state + (double)i / kRate;
        double v = 0.0;
        for (int h = 1; h < 40 && f0 * h < kRate / 2.2; h++) {
            const double f = f0 * h;
            v += std::sin(2 * kPi * f * t) / (1 + ((f - 700) / 300) * ((f - 700) / 300));
        }
        x[i] = (float)(0.05 * v);
    }
    phase_state += (double)n / kRate;
    return x;
}

// Fundamental by autocorrelation over the second half of the signal.
double F0(const std::vector<float>& y) {
    const size_t start = y.size() / 2, n = y.size() - start;
    const int lo = kRate / 1000, hi = kRate / 50;
    double best = -1e300;
    int lag = lo;
    for (int L = lo; L <= hi; L++) {
        double s = 0.0;
        for (size_t i = 0; i + L < n; i += 2) s += (double)y[start + i] * y[start + i + L];
        if (s > best) { best = s; lag = L; }
    }
    return (double)kRate / lag;
}

// f0 in semitones re the median, every 10 ms over 40 ms windows, from 0.5 s
// in (past the chain's delay and settling). The autocorrelation sums only
// within the window, so longer lags have fewer terms: that tips the argmax to
// the true period rather than a multiple of it.
std::vector<double> F0Track(const std::vector<float>& y) {
    const size_t win = kRate / 25, hop = kRate / 100;
    const int lo = kRate / 400, hi = kRate / 70;
    std::vector<double> hz;
    for (size_t s = kRate / 2; s + win < y.size(); s += hop) {
        double best = -1e300;
        int lag = lo;
        for (int L = lo; L <= hi; L++) {
            double acc = 0.0;
            for (size_t i = 0; i + L < win; i++) acc += (double)y[s + i] * y[s + i + L];
            if (acc > best) { best = acc; lag = L; }
        }
        hz.push_back((double)kRate / lag);
    }
    std::vector<double> sorted = hz;
    std::sort(sorted.begin(), sorted.end());
    const double median = sorted.empty() ? 1.0 : sorted[sorted.size() / 2];
    for (double& v : hz) v = 12.0 * std::log2(v / median);
    return hz;
}

// Peak-to-peak swing of a semitone track, ignoring the outer 5% either side.
double Swing(std::vector<double> st) {
    if (st.empty()) return 0.0;
    std::sort(st.begin(), st.end());
    return st[st.size() * 95 / 100] - st[st.size() * 5 / 100];
}

// Rising crossings per second, with hysteresis so tracking noise near zero
// doesn't count. Track points are 10 ms apart.
double ModulationHz(const std::vector<double>& st, double hysteresis) {
    int crossings = 0;
    bool low = false;
    for (double v : st) {
        if (v < -hysteresis) low = true;
        else if (v > hysteresis && low) { crossings++; low = false; }
    }
    return st.empty() ? 0.0 : crossings / (st.size() * 0.01);
}

double Rms(const std::vector<float>& y) {
    double s = 0.0;
    for (float v : y) s += (double)v * v;
    return std::sqrt(s / (y.empty() ? 1 : y.size()));
}

struct Apo {
    IAudioProcessingObject* apo = nullptr;
    IAudioProcessingObjectRT* rt = nullptr;
    IAudioProcessingObjectConfiguration* cfg = nullptr;
};

// Runs mono signal x (duplicated to stereo) through the APO. Returns the left
// channel; checks that every channel carries the same signal.
std::vector<float> Run(Apo& a, const std::vector<float>& x, bool* channels_equal, UINT32* last_flags,
                       bool in_place = false, bool silent = false) {
    std::vector<float> out;
    std::vector<float> ib(kPeriod * kChannels), ob(kPeriod * kChannels);
    bool equal = true;
    for (size_t pos = 0; pos + kPeriod <= x.size(); pos += kPeriod) {
        for (UINT32 i = 0; i < kPeriod; i++)
            for (UINT32 c = 0; c < kChannels; c++) ib[i * kChannels + c] = silent ? 123.0f : x[pos + i];
        float* obuf = in_place ? ib.data() : ob.data();
        APO_CONNECTION_PROPERTY in{(UINT_PTR)ib.data(), kPeriod, silent ? BUFFER_SILENT : BUFFER_VALID, 0};
        APO_CONNECTION_PROPERTY o{(UINT_PTR)obuf, 0, BUFFER_INVALID, 0};
        APO_CONNECTION_PROPERTY* pin = &in;
        APO_CONNECTION_PROPERTY* pout = &o;
        a.rt->APOProcess(1, &pin, 1, &pout);
        if (last_flags) *last_flags = o.u32BufferFlags;
        for (UINT32 i = 0; i < kPeriod; i++) {
            out.push_back(obuf[i * kChannels]);
            for (UINT32 c = 1; c < kChannels; c++)
                if (obuf[i * kChannels + c] != obuf[i * kChannels]) equal = false;
        }
        if (o.u32ValidFrameCount != kPeriod) equal = false;
    }
    if (channels_equal) *channels_equal = equal;
    return out;
}

}  // namespace

int wmain(int argc, wchar_t** argv) {
    if (argc != 2) {
        fprintf(stderr, "usage: apo_harness <VoiceChangerAPO.dll>\n");
        return 2;
    }
    // Private data dir so the test never touches a real installation.
    wchar_t tmp[MAX_PATH];
    GetTempPathW(MAX_PATH, tmp);
    g_dir = std::wstring(tmp) + L"vc_apo_harness";
    CreateDirectoryW(g_dir.c_str(), nullptr);
    DeleteFileW((g_dir + L"\\apo-settings.ini").c_str());
    SetEnvironmentVariableW(L"VOICECHANGER_DATA_DIR", g_dir.c_str());

    CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    HMODULE dll = LoadLibraryW(argv[1]);
    CHECK(dll != nullptr, "load %ls", argv[1]);
    if (!dll) return 1;
    auto get_class = (HRESULT(STDAPICALLTYPE*)(REFCLSID, REFIID, void**))GetProcAddress(dll, "DllGetClassObject");
    auto can_unload = (HRESULT(STDAPICALLTYPE*)())GetProcAddress(dll, "DllCanUnloadNow");
    CHECK(get_class && can_unload, "exports DllGetClassObject / DllCanUnloadNow");
    if (!get_class || !can_unload) return 1;

    IClassFactory* factory = nullptr;
    CHECK(SUCCEEDED(get_class(CLSID_VoiceChangerAPO, __uuidof(IClassFactory), (void**)&factory)), "class factory");
    IClassFactory* none = nullptr;
    CHECK(get_class(GUID_NULL, __uuidof(IClassFactory), (void**)&none) == CLASS_E_CLASSNOTAVAILABLE, "rejects foreign CLSID");

    Apo a;
    CHECK(SUCCEEDED(factory->CreateInstance(nullptr, __uuidof(IAudioProcessingObject), (void**)&a.apo)), "create instance");
    a.apo->QueryInterface(__uuidof(IAudioProcessingObjectRT), (void**)&a.rt);
    a.apo->QueryInterface(__uuidof(IAudioProcessingObjectConfiguration), (void**)&a.cfg);
    IAudioSystemEffects* sfx = nullptr;
    a.apo->QueryInterface(__uuidof(IAudioSystemEffects), (void**)&sfx);
    IAudioSystemEffects2* sfx2 = nullptr;
    a.apo->QueryInterface(__uuidof(IAudioSystemEffects2), (void**)&sfx2);
    CHECK(a.rt && a.cfg && sfx && sfx2, "exposes RT, Configuration, IAudioSystemEffects, IAudioSystemEffects2");
    if (!a.rt || !a.cfg) return 1;

    APO_REG_PROPERTIES* reg = nullptr;
    CHECK(SUCCEEDED(a.apo->GetRegistrationProperties(&reg)) && reg && reg->clsid == CLSID_VoiceChangerAPO &&
              reg->u32NumAPOInterfaces == 1 && reg->iidAPOInterfaceList[0] == __uuidof(IAudioSystemEffects),
          "registration properties");
    CoTaskMemFree(reg);

    APOInitBaseStruct init{sizeof(APOInitBaseStruct), CLSID_VoiceChangerAPO};
    CHECK(SUCCEEDED(a.apo->Initialize(sizeof(init), (BYTE*)&init)), "Initialize");
    GUID* fx = (GUID*)1;
    UINT nfx = 99;
    CHECK(sfx2 && SUCCEEDED(sfx2->GetEffectsList(&fx, &nfx, nullptr)) && nfx == 0 && fx == nullptr, "GetEffectsList (none)");

    // ---- format negotiation ----
    FloatMediaType* f48 = FloatMediaType::Create(kRate, kChannels);
    FloatMediaType* f44 = FloatMediaType::Create(44100, kChannels);
    IAudioMediaType* sup = nullptr;
    CHECK(a.apo->IsInputFormatSupported(nullptr, f48, &sup) == S_OK && sup == f48, "accepts float32 stereo 48 kHz");
    if (sup) sup->Release();
    sup = nullptr;
    HRESULT hr = a.apo->IsOutputFormatSupported(f48, f44, &sup);
    UNCOMPRESSEDAUDIOFORMAT uf{};
    CHECK(hr == S_FALSE && sup && ReadFormat(sup, &uf) && uf.fFramesPerSecond == (float)kRate,
          "proposes the input's rate when output asks for another");
    if (sup) sup->Release();

    // ---- no settings file: must be an exact pass-through ----
    APO_CONNECTION_DESCRIPTOR din{APO_CONNECTION_BUFFER_TYPE_EXTERNAL, 0, kPeriod, f48, 0};
    APO_CONNECTION_DESCRIPTOR dout{APO_CONNECTION_BUFFER_TYPE_EXTERNAL, 0, kPeriod, f48, 0};
    APO_CONNECTION_DESCRIPTOR* pdin = &din;
    APO_CONNECTION_DESCRIPTOR* pdout = &dout;
    CHECK(SUCCEEDED(a.cfg->LockForProcess(1, &pdin, 1, &pdout)), "LockForProcess");
    CHECK(FAILED(a.cfg->LockForProcess(1, &pdin, 1, &pdout)), "second LockForProcess refused");

    double phase = 0.0;
    std::vector<float> x = Voice(150, 1.0, phase);
    bool eq = false;
    UINT32 flags = 0;
    std::vector<float> y = Run(a, x, &eq, &flags);
    double maxdiff = 0.0;
    for (size_t i = 0; i < y.size(); i++) maxdiff = std::fmax(maxdiff, std::fabs(y[i] - x[i]));
    CHECK(maxdiff == 0.0, "no settings file -> bit-exact pass-through (max diff %g)", maxdiff);
    HNSTIME lat = -1;
    a.apo->GetLatency(&lat);
    CHECK(lat == 0, "pass-through reports zero latency");

    // ---- settings appear: +12 semitones ----
    WriteSettings("quality=balanced\npitch=12\nformant=0\ngate_enabled=0\ncomp_enabled=0\n");
    Sleep(400);
    x = Voice(150, 3.0, phase);
    y = Run(a, x, &eq, &flags);
    double f0 = F0(y);
    CHECK(std::fabs(f0 - 300.0) < 6.0, "+12 st: 150 Hz -> %.1f Hz (want 300)", f0);
    CHECK(eq, "all channels carry the processed voice");
    CHECK(flags == BUFFER_VALID, "output marked valid");
    a.apo->GetLatency(&lat);
    CHECK(lat > 300000 && lat < 600000, "latency reported: %.1f ms", lat / 10000.0);
    CHECK(Rms(y) > 0.3 * Rms(x), "output level sane (in %.3f, out %.3f)", Rms(x), Rms(y));

    // ---- live update: -12 semitones, in place ----
    WriteSettings("quality=balanced\npitch=-12\nformant=0\ngate_enabled=0\ncomp_enabled=0\n");
    Sleep(400);
    x = Voice(150, 3.0, phase);
    y = Run(a, x, &eq, &flags, /*in_place=*/true);
    f0 = F0(y);
    CHECK(std::fabs(f0 - 75.0) < 3.0, "live change to -12 st, in-place buffers: %.1f Hz (want 75)", f0);

    // ---- quality change swaps the chain without stopping ----
    HNSTIME before = 0, after = 0;
    a.apo->GetLatency(&before);
    WriteSettings("quality=low-latency\npitch=7\ngate_enabled=0\ncomp_enabled=0\n");
    Sleep(400);
    Run(a, Voice(200, 0.2, phase), nullptr, nullptr);  // picks up the new chain
    Sleep(300);
    x = Voice(200, 3.0, phase);
    y = Run(a, x, &eq, &flags);
    a.apo->GetLatency(&after);
    f0 = F0(y);
    CHECK(after < before, "quality change: latency %.1f -> %.1f ms", before / 10000.0, after / 10000.0);
    CHECK(std::fabs(f0 - 200.0 * std::pow(2.0, 7.0 / 12)) < 6.0, "new chain shifts correctly: %.1f Hz (want %.1f)", f0,
          200.0 * std::pow(2.0, 7.0 / 12));

    // ---- character controls ----
    // All on at once (with a pitch shift): output finite, level sane.
    WriteSettings("quality=low-latency\npitch=-3\ntremor_depth=2\ntremor_hz=4\njitter=0.5\nbreath=0.5\n"
                  "gravel=0.6\ngravel_hz=60\ngate_enabled=0\ncomp_enabled=0\n");
    Sleep(400);
    x = Voice(200, 3.0, phase);
    y = Run(a, x, &eq, &flags);
    bool all_finite = true;
    double peak = 0.0;
    for (float v : y) {
        all_finite = all_finite && std::isfinite(v);
        peak = std::fmax(peak, std::fabs(v));
    }
    CHECK(all_finite && eq && flags == BUFFER_VALID, "tremor+jitter+breath+gravel: output finite, channels equal");
    CHECK(Rms(y) > 0.3 * Rms(x) && Rms(y) < 3.0 * Rms(x) && peak < 0.9,
          "tremor+jitter+breath+gravel: level sane (in %.3f, out %.3f rms, peak %.3f)", Rms(x), Rms(y), peak);

    // Tremor alone: f0 holds steady without it and swings at its rate with it.
    WriteSettings("quality=low-latency\ntremor_depth=0\ntremor_hz=4\ngate_enabled=0\ncomp_enabled=0\n");
    Sleep(400);
    y = Run(a, Voice(200, 3.0, phase), &eq, &flags);
    const double steady = Swing(F0Track(y));
    WriteSettings("quality=low-latency\ntremor_depth=2\ntremor_hz=4\ngate_enabled=0\ncomp_enabled=0\n");
    Sleep(400);
    y = Run(a, Voice(200, 3.0, phase), &eq, &flags);
    const std::vector<double> track = F0Track(y);
    const double swing = Swing(track), rate = ModulationHz(track, 0.5);
    CHECK(steady < 0.3, "tremor off: f0 steady (swing %.2f st)", steady);
    CHECK(swing > 2.5 && swing < 5.0, "tremor 2 st: f0 swings %.2f st peak to peak (want ~4)", swing);
    CHECK(rate > 3.0 && rate < 5.0, "tremor 4 Hz: f0 modulated at %.1f Hz", rate);

    // ---- silent buffers: contents ignored, output valid and finite ----
    y = Run(a, std::vector<float>(kPeriod * 20, 0.0f), &eq, &flags, false, /*silent=*/true);
    bool finite = true;
    for (float v : y) finite = finite && std::isfinite(v) && std::fabs(v) < 1.0f;
    CHECK(finite && flags == BUFFER_VALID, "BUFFER_SILENT input treated as silence");

    // ---- status heartbeat for the GUI ----
    WIN32_FIND_DATAW fd;
    HANDLE fh = FindFirstFileW((g_dir + L"\\apo-status-*.ini").c_str(), &fd);
    CHECK(fh != INVALID_HANDLE_VALUE, "status heartbeat file written");
    if (fh != INVALID_HANDLE_VALUE) FindClose(fh);

    // ---- settings removed: back to exact pass-through ----
    DeleteFileW((g_dir + L"\\apo-settings.ini").c_str());
    Sleep(400);
    x = Voice(150, 0.5, phase);
    y = Run(a, x, &eq, &flags);
    maxdiff = 0.0;
    for (size_t i = 0; i < y.size(); i++) maxdiff = std::fmax(maxdiff, std::fabs(y[i] - x[i]));
    CHECK(maxdiff == 0.0, "settings removed -> pass-through again (max diff %g)", maxdiff);

    // ---- teardown ----
    CHECK(SUCCEEDED(a.cfg->UnlockForProcess()), "UnlockForProcess");
    CHECK(FAILED(a.cfg->UnlockForProcess()), "second UnlockForProcess refused");
    fh = FindFirstFileW((g_dir + L"\\apo-status-*.ini").c_str(), &fd);
    CHECK(fh == INVALID_HANDLE_VALUE, "status file removed on unlock");
    if (fh != INVALID_HANDLE_VALUE) FindClose(fh);

    sfx->Release();
    sfx2->Release();
    a.cfg->Release();
    a.rt->Release();
    CHECK(can_unload() == S_FALSE, "DLL stays loaded while an object is alive");
    a.apo->Release();
    factory->Release();
    f48->Release();
    f44->Release();
    CHECK(can_unload() == S_OK, "DllCanUnloadNow once everything is released");

    printf(g_failures ? "\n%d FAILED\n" : "\nALL PASSED\n", g_failures);
    return g_failures ? 1 : 0;
}
