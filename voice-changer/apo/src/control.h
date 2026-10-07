// Everything the APO does off the audio thread: watching the settings file,
// publishing settings to the audio thread without locks, building a new DSP
// chain when the quality changes, and writing a status heartbeat for the GUI.
//
// Data directory: %ProgramData%\VoiceChanger (override with the
// VOICECHANGER_DATA_DIR environment variable, used by the test harness).
//   apo-settings.ini        written by the GUI, read here
//   apo-status-<id>.ini     written here once a second while audio flows
//   apo.log                 short diagnostic log
#pragma once

#include <atomic>
#include <cstdint>
#include <string>
#include <thread>

#include "../dsp/chain.h"

namespace vcapo {

std::wstring DataDir();
void Log(const char* fmt, ...);

// Single-writer seqlock around a plain-data Settings.
class SettingsSlot {
public:
    void Publish(const vc::Settings& s, bool active);
    // Returns true and fills `out` if a newer, consistent value is available.
    bool ReadIfNewer(uint32_t& seen, vc::Settings& out, bool& active) const;

private:
    std::atomic<uint32_t> seq_{0};
    vc::Settings value_{};
    bool active_ = false;
};

class Controller {
public:
    Controller() = default;
    ~Controller() { Stop(); }

    // Reads the settings file synchronously, so the first audio block already
    // uses them. Returns false if the file is absent (pass-through mode).
    bool LoadInitial(vc::Settings& out);

    void Start(int sample_rate, int channels, int max_block, vc::Quality current_quality);
    void Stop();

    // ---- audio-thread side (wait-free) ----
    bool PollSettings(vc::Settings& out, bool& active) { return slot_.ReadIfNewer(seen_seq_, out, active); }
    vc::VoiceChain* TakePendingChain() { return pending_.exchange(nullptr, std::memory_order_acq_rel); }
    void RetireChain(vc::VoiceChain* old) {
        vc::VoiceChain* prev = retired_.exchange(old, std::memory_order_acq_rel);
        // The settings thread only builds a new chain once `retired_` is empty,
        // so `prev` is always null here. Never free on the audio thread.
        (void)prev;
    }
    void ReportBlock(uint32_t frames, float in_peak, float out_peak, int latency);

private:
    void Run();
    bool ReadSettingsFile(std::string& text, uint64_t& stamp);
    void WriteStatus();

    SettingsSlot slot_;
    uint32_t seen_seq_ = 0;  // audio thread only
    std::atomic<vc::VoiceChain*> pending_{nullptr};
    std::atomic<vc::VoiceChain*> retired_{nullptr};

    std::thread thread_;
    void* stop_event_ = nullptr;
    int sample_rate_ = 0, channels_ = 0, max_block_ = 0;
    vc::Quality built_quality_ = vc::Quality::Balanced, desired_quality_ = vc::Quality::Balanced;
    uint64_t file_stamp_ = 0;

    std::atomic<uint64_t> frames_{0};
    std::atomic<float> in_peak_{0.0f}, out_peak_{0.0f};
    std::atomic<int> latency_{0};
    std::wstring status_path_;
};

}  // namespace vcapo
