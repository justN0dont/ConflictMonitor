#include "control.h"

#include <windows.h>
#include <shlobj.h>

#include <cstdarg>
#include <cstdio>
#include <vector>

namespace vcapo {

namespace {
uint64_t FileStamp(const WIN32_FILE_ATTRIBUTE_DATA& a) {
    return ((uint64_t)a.ftLastWriteTime.dwHighDateTime << 32) ^ a.ftLastWriteTime.dwLowDateTime ^
           ((uint64_t)a.nFileSizeLow << 20);
}

const char* QualityName(vc::Quality q) {
    return q == vc::Quality::LowLatency ? "low-latency" : (q == vc::Quality::Balanced ? "balanced" : "high-quality");
}
}  // namespace

std::wstring DataDir() {
    wchar_t buf[MAX_PATH];
    const DWORD n = GetEnvironmentVariableW(L"VOICECHANGER_DATA_DIR", buf, MAX_PATH);
    if (n > 0 && n < MAX_PATH) return buf;
    PWSTR pd = nullptr;
    std::wstring dir = L"C:\\ProgramData";
    if (SUCCEEDED(SHGetKnownFolderPath(FOLDERID_ProgramData, 0, nullptr, &pd))) dir = pd;
    if (pd) CoTaskMemFree(pd);
    return dir + L"\\VoiceChanger";
}

void Log(const char* fmt, ...) {
    // Never called from the audio thread. Never throws either: it runs in
    // catch blocks whose whole point is that nothing escapes.
    std::wstring path;
    try {
        path = DataDir() + L"\\apo.log";
    } catch (...) {
        return;
    }
    WIN32_FILE_ATTRIBUTE_DATA a;
    if (GetFileAttributesExW(path.c_str(), GetFileExInfoStandard, &a) && a.nFileSizeLow > 256 * 1024) {
        DeleteFileW(path.c_str());  // keep it small
    }
    FILE* f = _wfopen(path.c_str(), L"a");
    if (!f) return;
    SYSTEMTIME t;
    GetLocalTime(&t);
    fprintf(f, "%04d-%02d-%02d %02d:%02d:%02d [pid %lu] ", t.wYear, t.wMonth, t.wDay, t.wHour, t.wMinute, t.wSecond,
            GetCurrentProcessId());
    va_list ap;
    va_start(ap, fmt);
    vfprintf(f, fmt, ap);
    va_end(ap);
    fputc('\n', f);
    fclose(f);
}

// ---- SettingsSlot ----------------------------------------------------------

void SettingsSlot::Publish(const vc::Settings& s, bool active) {
    const uint32_t q = seq_.load(std::memory_order_relaxed);
    seq_.store(q + 1, std::memory_order_relaxed);  // odd: write in progress
    std::atomic_thread_fence(std::memory_order_release);
    value_ = s;
    active_ = active;
    std::atomic_thread_fence(std::memory_order_release);
    seq_.store(q + 2, std::memory_order_release);
}

bool SettingsSlot::ReadIfNewer(uint32_t& seen, vc::Settings& out, bool& active) const {
    const uint32_t a = seq_.load(std::memory_order_acquire);
    if (a == seen || (a & 1)) return false;
    vc::Settings copy = value_;
    const bool act = active_;
    std::atomic_thread_fence(std::memory_order_acquire);
    if (seq_.load(std::memory_order_relaxed) != a) return false;  // torn; try next block
    out = copy;
    active = act;
    seen = a;
    return true;
}

// ---- Controller ------------------------------------------------------------

bool Controller::ReadSettingsFile(std::string& text, uint64_t& stamp) {
    const std::wstring path = DataDir() + L"\\apo-settings.ini";
    WIN32_FILE_ATTRIBUTE_DATA a;
    if (!GetFileAttributesExW(path.c_str(), GetFileExInfoStandard, &a)) return false;
    stamp = FileStamp(a);
    // The GUI replaces the file atomically, but allow for a reader racing an
    // editor: share everything, retry briefly.
    for (int attempt = 0; attempt < 3; attempt++) {
        HANDLE h = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr,
                               OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (h == INVALID_HANDLE_VALUE) {
            Sleep(5);
            continue;
        }
        std::vector<char> buf(64 * 1024);
        DWORD got = 0;
        const BOOL ok = ReadFile(h, buf.data(), (DWORD)buf.size() - 1, &got, nullptr);
        CloseHandle(h);
        if (!ok) continue;
        text.assign(buf.data(), got);
        return true;
    }
    return false;
}

bool Controller::LoadInitial(vc::Settings& out) {
    std::string text;
    uint64_t stamp = 0;
    if (!ReadSettingsFile(text, stamp)) {
        file_stamp_ = 0;
        out = vc::Settings();
        slot_.Publish(out, false);
        return false;
    }
    file_stamp_ = stamp;
    out = vc::parse_settings(text);
    slot_.Publish(out, true);
    return true;
}

void Controller::Start(int sample_rate, int channels, int max_block, vc::Quality current_quality) {
    Stop();
    sample_rate_ = sample_rate;
    channels_ = channels;
    max_block_ = max_block;
    built_quality_ = desired_quality_ = current_quality;
    wchar_t name[96];
    swprintf(name, 96, L"\\apo-status-%lu-%p.ini", GetCurrentProcessId(), (void*)this);
    status_path_ = DataDir() + name;
    stop_event_ = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (!stop_event_) {
        Log("CreateEvent failed (%lu); live settings updates disabled", GetLastError());
        return;
    }
    thread_ = std::thread([this] { Run(); });
}

void Controller::Stop() {
    if (stop_event_) {
        SetEvent(stop_event_);
        if (thread_.joinable()) thread_.join();
        CloseHandle(stop_event_);
        stop_event_ = nullptr;
    }
    if (!status_path_.empty()) {
        DeleteFileW(status_path_.c_str());
        status_path_.clear();
    }
    delete pending_.exchange(nullptr);
    delete retired_.exchange(nullptr);
}

void Controller::ReportBlock(uint32_t frames, float in_peak, float out_peak, int latency) {
    frames_.fetch_add(frames, std::memory_order_relaxed);
    // Keep the loudest peak since the last status write.
    float cur = in_peak_.load(std::memory_order_relaxed);
    while (in_peak > cur && !in_peak_.compare_exchange_weak(cur, in_peak, std::memory_order_relaxed)) {
    }
    cur = out_peak_.load(std::memory_order_relaxed);
    while (out_peak > cur && !out_peak_.compare_exchange_weak(cur, out_peak, std::memory_order_relaxed)) {
    }
    latency_.store(latency, std::memory_order_relaxed);
}

void Controller::WriteStatus() {
    if (status_path_.empty()) return;
    const uint64_t frames = frames_.load(std::memory_order_relaxed);
    const float inp = in_peak_.exchange(0.0f, std::memory_order_relaxed);
    const float outp = out_peak_.exchange(0.0f, std::memory_order_relaxed);
    FILETIME ft;
    GetSystemTimeAsFileTime(&ft);
    const uint64_t unix_ms = ((((uint64_t)ft.dwHighDateTime << 32) | ft.dwLowDateTime) - 116444736000000000ull) / 10000ull;
    char exe[MAX_PATH] = "";
    GetModuleFileNameA(nullptr, exe, MAX_PATH);

    char text[1024];
    const int len = snprintf(text, sizeof(text),
                             "updated_ms=%llu\npid=%lu\nhost=%s\nsample_rate=%d\nchannels=%d\nquality=%s\n"
                             "latency_ms=%.1f\nframes=%llu\nin_peak=%.5f\nout_peak=%.5f\n",
                             (unsigned long long)unix_ms, GetCurrentProcessId(), exe, sample_rate_, channels_,
                             QualityName(built_quality_),
                             sample_rate_ ? 1000.0 * latency_.load(std::memory_order_relaxed) / sample_rate_ : 0.0,
                             (unsigned long long)frames, inp, outp);
    const std::wstring tmp = status_path_ + L".tmp";
    HANDLE h = CreateFileW(tmp.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) return;  // no write access: status is optional
    DWORD wrote = 0;
    WriteFile(h, text, (DWORD)len, &wrote, nullptr);
    CloseHandle(h);
    MoveFileExW(tmp.c_str(), status_path_.c_str(), MOVEFILE_REPLACE_EXISTING);
}

void Controller::Run() {
    uint32_t ticks = 0;
    bool failing = false;
    while (WaitForSingleObject(stop_event_, 100) == WAIT_TIMEOUT) {
        // Nothing may escape this thread: std::terminate would take down
        // audiodg.exe and all system audio. What can throw here (strings,
        // file buffers, parsing) only fails when memory runs out, so skip the
        // tick and try again on the next.
        try {
            // Free whatever the audio thread swapped out.
            delete retired_.exchange(nullptr, std::memory_order_acq_rel);

            std::string text;
            uint64_t stamp = 0;
            const bool present = ReadSettingsFile(text, stamp);
            if (!present && file_stamp_ != 0) {
                file_stamp_ = 0;
                slot_.Publish(vc::Settings(), false);
                Log("settings file removed: pass-through");
            } else if (present && stamp != file_stamp_) {
                const vc::Settings s = vc::parse_settings(text);
                desired_quality_ = s.quality;
                slot_.Publish(s, true);
                file_stamp_ = stamp;  // last, so a tick that throws reads the file again
            }
            // A quality change needs a differently sized chain. Build it here,
            // never on the audio thread, and only once both handoff slots are
            // empty (see AdoptPendingChain), so nothing is ever freed in real
            // time. Retried each tick.
            if (desired_quality_ != built_quality_ && pending_.load() == nullptr && retired_.load() == nullptr) {
                try {
                    pending_.store(new vc::VoiceChain(sample_rate_, desired_quality_, max_block_, /*always_pad=*/true),
                                   std::memory_order_release);
                    built_quality_ = desired_quality_;
                } catch (...) {
                    Log("could not build chain for new quality");
                    desired_quality_ = built_quality_;
                }
            }
            if (++ticks % 10 == 0) WriteStatus();
            failing = false;
        } catch (...) {
            if (!failing) Log("settings thread: tick failed (out of memory?); retrying");
            failing = true;
        }
    }
}

}  // namespace vcapo
