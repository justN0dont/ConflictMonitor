// The system-effect APO: inserted on a capture endpoint, it transforms the
// microphone signal for every application that records from it.
#pragma once

#include <memory>
#include <vector>

#include "apo_headers.h"
#include "control.h"

extern volatile LONG g_objects;

// An aggregatable COM object's own IUnknown. Same vtable layout as IUnknown,
// so it is handed out as one; the interfaces' IUnknown methods delegate to
// the controlling (outer) unknown instead.
struct NonDelegatingUnknown {
    virtual HRESULT STDMETHODCALLTYPE NonDelegatingQueryInterface(REFIID riid, void** ppv) = 0;
    virtual ULONG STDMETHODCALLTYPE NonDelegatingAddRef() = 0;
    virtual ULONG STDMETHODCALLTYPE NonDelegatingRelease() = 0;
};

// The audio engine creates APOs aggregated inside an object of its own, so
// this one must support COM aggregation: refusing it (CLASS_E_NOAGGREGATION)
// fails every app's attempt to open the microphone.
class VoiceChangerAPO final : public NonDelegatingUnknown,
                              public IAudioProcessingObject,
                              public IAudioProcessingObjectRT,
                              public IAudioProcessingObjectConfiguration,
                              public IAudioSystemEffects2 {
public:
    // `outer`: the controlling IUnknown when aggregated, else nullptr.
    explicit VoiceChangerAPO(IUnknown* outer);
    ~VoiceChangerAPO();

    // IUnknown, for every interface: delegates to the controlling unknown
    STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override;
    STDMETHODIMP_(ULONG) AddRef() override;
    STDMETHODIMP_(ULONG) Release() override;

    // The object's own IUnknown (the controlling one when not aggregated)
    STDMETHODIMP NonDelegatingQueryInterface(REFIID riid, void** ppv) override;
    STDMETHODIMP_(ULONG) NonDelegatingAddRef() override;
    STDMETHODIMP_(ULONG) NonDelegatingRelease() override;

    // IAudioProcessingObject
    STDMETHODIMP Reset() override;
    STDMETHODIMP GetLatency(HNSTIME* pTime) override;
    STDMETHODIMP GetRegistrationProperties(APO_REG_PROPERTIES** ppRegProps) override;
    STDMETHODIMP Initialize(UINT32 cbDataSize, BYTE* pbyData) override;
    STDMETHODIMP IsInputFormatSupported(IAudioMediaType* pOppositeFormat, IAudioMediaType* pRequestedInputFormat,
                                        IAudioMediaType** ppSupportedInputFormat) override;
    STDMETHODIMP IsOutputFormatSupported(IAudioMediaType* pOppositeFormat, IAudioMediaType* pRequestedOutputFormat,
                                         IAudioMediaType** ppSupportedOutputFormat) override;
    STDMETHODIMP GetInputChannelCount(UINT32* pu32ChannelCount) override;

    // IAudioProcessingObjectRT
    STDMETHODIMP_(void) APOProcess(UINT32 u32NumInputConnections, APO_CONNECTION_PROPERTY** ppInputConnections,
                                   UINT32 u32NumOutputConnections, APO_CONNECTION_PROPERTY** ppOutputConnections) override;
    STDMETHODIMP_(UINT32) CalcInputFrames(UINT32 u32OutputFrameCount) override;
    STDMETHODIMP_(UINT32) CalcOutputFrames(UINT32 u32InputFrameCount) override;

    // IAudioProcessingObjectConfiguration
    STDMETHODIMP LockForProcess(UINT32 u32NumInputConnections, APO_CONNECTION_DESCRIPTOR** ppInputConnections,
                                UINT32 u32NumOutputConnections, APO_CONNECTION_DESCRIPTOR** ppOutputConnections) override;
    STDMETHODIMP UnlockForProcess() override;

    // IAudioSystemEffects2
    STDMETHODIMP GetEffectsList(LPGUID* ppEffectsIds, UINT* pcEffects, HANDLE Event) override;

private:
    HRESULT CheckFormat(IAudioMediaType* opposite, IAudioMediaType* requested, IAudioMediaType** supported);

    IUnknown* outer_;  // not AddRef'd: when aggregated, the outer object owns this one
    LONG refs_ = 1;
    bool locked_ = false;
    UINT32 channels_ = 0, sample_rate_ = 0, max_frames_ = 0;

    vcapo::Controller control_;
    vc::VoiceChain* chain_ = nullptr;  // owned; swapped via the controller
    vc::Settings settings_;            // audio thread's copy
    bool active_ = false;              // false: no settings file, pure pass-through
    std::vector<float> mono_in_, mono_out_;
};
