#include "apo.h"

#include <cmath>
#include <cstring>
#include <new>

#include "media_type.h"

VoiceChangerAPO::VoiceChangerAPO() { InterlockedIncrement(&g_objects); }

VoiceChangerAPO::~VoiceChangerAPO() {
    control_.Stop();
    delete chain_;
    InterlockedDecrement(&g_objects);
}

// ---- IUnknown --------------------------------------------------------------

STDMETHODIMP VoiceChangerAPO::QueryInterface(REFIID riid, void** ppv) {
    if (!ppv) return E_POINTER;
    if (riid == __uuidof(IUnknown) || riid == __uuidof(IAudioProcessingObject)) {
        *ppv = static_cast<IAudioProcessingObject*>(this);
    } else if (riid == __uuidof(IAudioProcessingObjectRT)) {
        *ppv = static_cast<IAudioProcessingObjectRT*>(this);
    } else if (riid == __uuidof(IAudioProcessingObjectConfiguration)) {
        *ppv = static_cast<IAudioProcessingObjectConfiguration*>(this);
    } else if (riid == __uuidof(IAudioSystemEffects)) {
        *ppv = static_cast<IAudioSystemEffects*>(this);
    } else if (riid == __uuidof(IAudioSystemEffects2)) {
        *ppv = static_cast<IAudioSystemEffects2*>(this);
    } else {
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    AddRef();
    return S_OK;
}

STDMETHODIMP_(ULONG) VoiceChangerAPO::AddRef() { return (ULONG)InterlockedIncrement(&refs_); }

STDMETHODIMP_(ULONG) VoiceChangerAPO::Release() {
    const LONG r = InterlockedDecrement(&refs_);
    if (r == 0) delete this;
    return (ULONG)r;
}

// ---- IAudioProcessingObject -----------------------------------------------

STDMETHODIMP VoiceChangerAPO::Reset() {
    if (chain_) chain_->reset();
    return S_OK;
}

STDMETHODIMP VoiceChangerAPO::GetLatency(HNSTIME* pTime) {
    if (!pTime) return E_POINTER;
    *pTime = (chain_ && sample_rate_ && active_) ? (HNSTIME)(10000000.0 * chain_->latency() / sample_rate_) : 0;
    return S_OK;
}

STDMETHODIMP VoiceChangerAPO::GetRegistrationProperties(APO_REG_PROPERTIES** ppRegProps) {
    if (!ppRegProps) return E_POINTER;
    auto* p = static_cast<APO_REG_PROPERTIES*>(CoTaskMemAlloc(sizeof(APO_REG_PROPERTIES)));
    if (!p) return E_OUTOFMEMORY;
    std::memset(p, 0, sizeof(*p));
    p->clsid = CLSID_VoiceChangerAPO;
    p->Flags = APO_FLAG_DEFAULT;
    wcscpy_s(p->szFriendlyName, L"Voice Changer");
    wcscpy_s(p->szCopyrightInfo, L"Voice Changer contributors");
    p->u32MajorVersion = 1;
    p->u32MinorVersion = 0;
    p->u32MinInputConnections = 1;
    p->u32MaxInputConnections = 1;
    p->u32MinOutputConnections = 1;
    p->u32MaxOutputConnections = 1;
    p->u32MaxInstances = 0xffffffff;
    p->u32NumAPOInterfaces = 1;
    p->iidAPOInterfaceList[0] = __uuidof(IAudioSystemEffects);
    *ppRegProps = p;
    return S_OK;
}

STDMETHODIMP VoiceChangerAPO::Initialize(UINT32 cbDataSize, BYTE* pbyData) {
    // The engine passes an APOInitSystemEffects{,2,3} block. Nothing in it
    // changes what this APO does, so only sanity-check it.
    if (cbDataSize && !pbyData) return E_POINTER;
    if (pbyData && cbDataSize >= sizeof(APOInitBaseStruct)) {
        const auto* base = reinterpret_cast<const APOInitBaseStruct*>(pbyData);
        if (base->clsid != CLSID_VoiceChangerAPO && base->clsid != GUID_NULL) return APOERR_INVALID_APO_CLSID;
    }
    return S_OK;
}

HRESULT VoiceChangerAPO::CheckFormat(IAudioMediaType* opposite, IAudioMediaType* requested, IAudioMediaType** supported) {
    if (!requested || !supported) return E_POINTER;
    *supported = nullptr;
    UNCOMPRESSEDAUDIOFORMAT req{};
    if (!ReadFormat(requested, &req)) return APOERR_FORMAT_NOT_SUPPORTED;

    // Input and output must share rate and channel count (the APO converts
    // nothing); when the other side is already fixed, match it.
    UNCOMPRESSEDAUDIOFORMAT want = req;
    UNCOMPRESSEDAUDIOFORMAT opp{};
    if (opposite && ReadFormat(opposite, &opp)) {
        want.dwSamplesPerFrame = opp.dwSamplesPerFrame;
        want.fFramesPerSecond = opp.fFramesPerSecond;
        want.dwChannelMask = opp.dwChannelMask;
    }
    const bool same_shape = want.dwSamplesPerFrame == req.dwSamplesPerFrame && want.fFramesPerSecond == req.fFramesPerSecond;
    if (IsFloat32(req) && same_shape) {
        requested->AddRef();
        *supported = requested;
        return S_OK;
    }
    if (want.dwSamplesPerFrame < 1 || want.dwSamplesPerFrame > 32 || want.fFramesPerSecond < 8000.0f ||
        want.fFramesPerSecond > 384000.0f) {
        return APOERR_FORMAT_NOT_SUPPORTED;
    }
    FloatMediaType* mt = FloatMediaType::Create((UINT32)want.fFramesPerSecond, want.dwSamplesPerFrame, want.dwChannelMask);
    if (!mt) return E_OUTOFMEMORY;
    *supported = mt;
    return S_FALSE;  // "not that one, but this one"
}

STDMETHODIMP VoiceChangerAPO::IsInputFormatSupported(IAudioMediaType* pOppositeFormat, IAudioMediaType* pRequestedInputFormat,
                                                     IAudioMediaType** ppSupportedInputFormat) {
    return CheckFormat(pOppositeFormat, pRequestedInputFormat, ppSupportedInputFormat);
}

STDMETHODIMP VoiceChangerAPO::IsOutputFormatSupported(IAudioMediaType* pOppositeFormat, IAudioMediaType* pRequestedOutputFormat,
                                                      IAudioMediaType** ppSupportedOutputFormat) {
    return CheckFormat(pOppositeFormat, pRequestedOutputFormat, ppSupportedOutputFormat);
}

STDMETHODIMP VoiceChangerAPO::GetInputChannelCount(UINT32* pu32ChannelCount) {
    if (!pu32ChannelCount) return E_POINTER;
    *pu32ChannelCount = channels_;
    return S_OK;
}

// ---- IAudioProcessingObjectConfiguration -----------------------------------

STDMETHODIMP VoiceChangerAPO::LockForProcess(UINT32 nIn, APO_CONNECTION_DESCRIPTOR** ppIn, UINT32 nOut,
                                             APO_CONNECTION_DESCRIPTOR** ppOut) {
    if (locked_) return APOERR_APO_LOCKED;
    if (nIn != 1 || nOut != 1 || !ppIn || !ppOut || !ppIn[0] || !ppOut[0]) return APOERR_NUM_CONNECTIONS_INVALID;
    UNCOMPRESSEDAUDIOFORMAT fin{}, fout{};
    if (!ReadFormat(ppIn[0]->pFormat, &fin) || !ReadFormat(ppOut[0]->pFormat, &fout)) return APOERR_INVALID_CONNECTION_FORMAT;
    if (!IsFloat32(fin) || !IsFloat32(fout) || fin.dwSamplesPerFrame != fout.dwSamplesPerFrame ||
        fin.fFramesPerSecond != fout.fFramesPerSecond) {
        return APOERR_INVALID_CONNECTION_FORMAT;
    }
    if (ppIn[0]->u32MaxFrameCount == 0 || ppOut[0]->u32MaxFrameCount < ppIn[0]->u32MaxFrameCount) {
        return APOERR_INVALID_OUTPUT_MAXFRAMECOUNT;
    }

    channels_ = fin.dwSamplesPerFrame;
    sample_rate_ = (UINT32)(fin.fFramesPerSecond + 0.5f);
    max_frames_ = ppIn[0]->u32MaxFrameCount;

    try {
        active_ = control_.LoadInitial(settings_);
        delete chain_;
        chain_ = new vc::VoiceChain((int)sample_rate_, settings_.quality, (int)max_frames_);
        mono_in_.assign(max_frames_, 0.0f);
        mono_out_.assign(max_frames_, 0.0f);
    } catch (...) {
        vcapo::Log("LockForProcess: out of memory (rate %u, frames %u)", sample_rate_, max_frames_);
        return E_OUTOFMEMORY;
    }
    control_.Start((int)sample_rate_, (int)channels_, (int)max_frames_, settings_.quality);
    vcapo::Log("locked: %u Hz, %u ch, %u frames/period, latency %d samples, %s", sample_rate_, channels_, max_frames_,
               chain_->latency(), active_ ? "active" : "pass-through (no settings file)");
    locked_ = true;
    return S_OK;
}

STDMETHODIMP VoiceChangerAPO::UnlockForProcess() {
    if (!locked_) return APOERR_ALREADY_UNLOCKED;
    control_.Stop();
    delete chain_;
    chain_ = nullptr;
    locked_ = false;
    return S_OK;
}

// ---- IAudioProcessingObjectRT (audio thread: no locks, no allocation) --------

STDMETHODIMP_(void) VoiceChangerAPO::APOProcess(UINT32 nIn, APO_CONNECTION_PROPERTY** ppIn, UINT32 nOut,
                                                APO_CONNECTION_PROPERTY** ppOut) {
    if (!locked_ || nIn != 1 || nOut != 1 || !ppIn || !ppOut || !ppIn[0] || !ppOut[0]) return;
    APO_CONNECTION_PROPERTY* in = ppIn[0];
    APO_CONNECTION_PROPERTY* out = ppOut[0];
    const UINT32 frames = in->u32ValidFrameCount;
    const float* src = reinterpret_cast<const float*>(in->pBuffer);
    float* dst = reinterpret_cast<float*>(out->pBuffer);
    const UINT32 ch = channels_;
    const bool silent = in->u32BufferFlags == BUFFER_SILENT;

    if (vc::VoiceChain* fresh = control_.TakePendingChain()) {
        control_.RetireChain(chain_);
        chain_ = fresh;
    }
    control_.PollSettings(settings_, active_);

    out->u32ValidFrameCount = frames;
    if (!active_ || !chain_) {
        // No settings yet: leave the microphone exactly as it was.
        if (dst != src) {
            if (silent) std::memset(dst, 0, sizeof(float) * frames * ch);
            else std::memcpy(dst, src, sizeof(float) * frames * ch);
        }
        out->u32BufferFlags = in->u32BufferFlags;
        return;
    }

    float in_peak = 0.0f, out_peak = 0.0f;
    for (UINT32 done = 0; done < frames;) {
        const UINT32 n = (frames - done) < max_frames_ ? (frames - done) : max_frames_;
        // Downmix all channels (array mics deliver several) to mono.
        const float inv = 1.0f / (float)ch;
        for (UINT32 i = 0; i < n; i++) {
            float acc = 0.0f;
            if (!silent) {
                const float* f = src + (size_t)(done + i) * ch;
                for (UINT32 c = 0; c < ch; c++) acc += f[c];
            }
            mono_in_[i] = acc * inv;
        }
        chain_->process(mono_in_.data(), mono_out_.data(), (int)n, settings_);

        // Fail safe: anything non-finite resets the chain and passes the dry
        // signal for this block instead of putting noise on the call.
        bool finite = true;
        for (UINT32 i = 0; i < n; i++) {
            if (!std::isfinite(mono_out_[i])) { finite = false; break; }
        }
        if (!finite) {
            chain_->reset();
            for (UINT32 i = 0; i < n; i++) mono_out_[i] = mono_in_[i];
        }
        for (UINT32 i = 0; i < n; i++) {
            float* f = dst + (size_t)(done + i) * ch;
            const float v = mono_out_[i];
            for (UINT32 c = 0; c < ch; c++) f[c] = v;
        }
        if ((float)chain_->input_peak > in_peak) in_peak = (float)chain_->input_peak;
        if ((float)chain_->output_peak > out_peak) out_peak = (float)chain_->output_peak;
        done += n;
    }
    // The chain has delay and a reverb tail, so even silent input can produce
    // sound: always mark the output valid.
    out->u32BufferFlags = BUFFER_VALID;
    control_.ReportBlock(frames, in_peak, out_peak, chain_->latency());
}

STDMETHODIMP_(UINT32) VoiceChangerAPO::CalcInputFrames(UINT32 u32OutputFrameCount) { return u32OutputFrameCount; }
STDMETHODIMP_(UINT32) VoiceChangerAPO::CalcOutputFrames(UINT32 u32InputFrameCount) { return u32InputFrameCount; }

// ---- IAudioSystemEffects2 ---------------------------------------------------

STDMETHODIMP VoiceChangerAPO::GetEffectsList(LPGUID* ppEffectsIds, UINT* pcEffects, HANDLE) {
    if (!ppEffectsIds || !pcEffects) return E_POINTER;
    *ppEffectsIds = nullptr;
    *pcEffects = 0;
    return S_OK;
}
