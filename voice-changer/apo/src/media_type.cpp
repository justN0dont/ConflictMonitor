#include "media_type.h"

#include <new>

FloatMediaType* FloatMediaType::Create(UINT32 sample_rate, UINT32 channels, DWORD channel_mask) {
    FloatMediaType* mt = new (std::nothrow) FloatMediaType();
    if (!mt) return nullptr;
    WAVEFORMATEXTENSIBLE& w = mt->wfx_;
    w.Format.wFormatTag = WAVE_FORMAT_EXTENSIBLE;
    w.Format.nChannels = (WORD)channels;
    w.Format.nSamplesPerSec = sample_rate;
    w.Format.wBitsPerSample = 32;
    w.Format.nBlockAlign = (WORD)(channels * 4);
    w.Format.nAvgBytesPerSec = sample_rate * channels * 4;
    w.Format.cbSize = sizeof(WAVEFORMATEXTENSIBLE) - sizeof(WAVEFORMATEX);
    w.Samples.wValidBitsPerSample = 32;
    w.dwChannelMask = channel_mask;
    w.SubFormat = kSubtypeIeeeFloat;
    return mt;
}

STDMETHODIMP FloatMediaType::QueryInterface(REFIID riid, void** ppv) {
    if (!ppv) return E_POINTER;
    if (riid == __uuidof(IUnknown) || riid == __uuidof(IAudioMediaType)) {
        *ppv = static_cast<IAudioMediaType*>(this);
        AddRef();
        return S_OK;
    }
    *ppv = nullptr;
    return E_NOINTERFACE;
}

STDMETHODIMP_(ULONG) FloatMediaType::AddRef() { return (ULONG)InterlockedIncrement(&refs_); }

STDMETHODIMP_(ULONG) FloatMediaType::Release() {
    const LONG r = InterlockedDecrement(&refs_);
    if (r == 0) delete this;
    return (ULONG)r;
}

STDMETHODIMP FloatMediaType::IsCompressedFormat(BOOL* pfCompressed) {
    if (!pfCompressed) return E_POINTER;
    *pfCompressed = FALSE;
    return S_OK;
}

STDMETHODIMP FloatMediaType::IsEqual(IAudioMediaType* other, DWORD* pdwFlags) {
    if (!other || !pdwFlags) return E_POINTER;
    *pdwFlags = 0;
    UNCOMPRESSEDAUDIOFORMAT a{}, b{};
    GetUncompressedAudioFormat(&a);
    if (!ReadFormat(other, &b)) return S_FALSE;
    if (a.guidFormatType == b.guidFormatType) *pdwFlags |= AUDIOMEDIATYPE_EQUAL_FORMAT_TYPES;
    if (a.guidFormatType == b.guidFormatType && a.dwSamplesPerFrame == b.dwSamplesPerFrame &&
        a.dwBytesPerSampleContainer == b.dwBytesPerSampleContainer && a.dwValidBitsPerSample == b.dwValidBitsPerSample &&
        a.fFramesPerSecond == b.fFramesPerSecond) {
        *pdwFlags |= AUDIOMEDIATYPE_EQUAL_FORMAT_DATA | AUDIOMEDIATYPE_EQUAL_FORMAT_USER_DATA;
        return S_OK;
    }
    return S_FALSE;
}

STDMETHODIMP_(const WAVEFORMATEX*) FloatMediaType::GetAudioFormat() { return &wfx_.Format; }

STDMETHODIMP FloatMediaType::GetUncompressedAudioFormat(UNCOMPRESSEDAUDIOFORMAT* f) {
    if (!f) return E_POINTER;
    f->guidFormatType = kSubtypeIeeeFloat;
    f->dwSamplesPerFrame = wfx_.Format.nChannels;
    f->dwBytesPerSampleContainer = 4;
    f->dwValidBitsPerSample = 32;
    f->fFramesPerSecond = (FLOAT)wfx_.Format.nSamplesPerSec;
    f->dwChannelMask = wfx_.dwChannelMask;
    return S_OK;
}

bool ReadFormat(IAudioMediaType* mt, UNCOMPRESSEDAUDIOFORMAT* out) {
    if (!mt || !out) return false;
    BOOL compressed = TRUE;
    if (FAILED(mt->IsCompressedFormat(&compressed)) || compressed) return false;
    return SUCCEEDED(mt->GetUncompressedAudioFormat(out));
}

bool IsFloat32(const UNCOMPRESSEDAUDIOFORMAT& f) {
    return f.guidFormatType == kSubtypeIeeeFloat && f.dwBytesPerSampleContainer == 4 && f.dwValidBitsPerSample == 32 &&
           f.dwSamplesPerFrame >= 1 && f.dwSamplesPerFrame <= 32 && f.fFramesPerSecond >= 8000.0f &&
           f.fFramesPerSecond <= 384000.0f;
}
