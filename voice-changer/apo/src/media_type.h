// Minimal IAudioMediaType for 32-bit float PCM, used to propose a supported
// format back to the audio engine (and by the test harness to describe one).
#pragma once

#include "apo_headers.h"

class FloatMediaType final : public IAudioMediaType {
public:
    static FloatMediaType* Create(UINT32 sample_rate, UINT32 channels, DWORD channel_mask = 0);

    // IUnknown
    STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override;
    STDMETHODIMP_(ULONG) AddRef() override;
    STDMETHODIMP_(ULONG) Release() override;

    // IAudioMediaType
    STDMETHODIMP IsCompressedFormat(BOOL* pfCompressed) override;
    STDMETHODIMP IsEqual(IAudioMediaType* pIAudioType, DWORD* pdwFlags) override;
    STDMETHODIMP_(const WAVEFORMATEX*) GetAudioFormat() override;
    STDMETHODIMP GetUncompressedAudioFormat(UNCOMPRESSEDAUDIOFORMAT* pUncompressedAudioFormat) override;

private:
    FloatMediaType() = default;
    LONG refs_ = 1;
    WAVEFORMATEXTENSIBLE wfx_{};
};

// Reads the format of any IAudioMediaType. Returns false if it is not
// uncompressed PCM/float.
bool ReadFormat(IAudioMediaType* mt, UNCOMPRESSEDAUDIOFORMAT* out);

// True for interleaved 32-bit IEEE float.
bool IsFloat32(const UNCOMPRESSEDAUDIOFORMAT& f);
