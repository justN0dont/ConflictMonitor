"""Desktop GUI (tkinter, bundled with Python on Windows and macOS)."""

from __future__ import annotations

import json
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .chain import QUALITY, Settings
from .presets import PRESETS, apply_preset

# (setting, label, from, to, resolution, unit)
SLIDERS = {
    "Voice": [
        ("pitch", "Pitch", -24, 24, 0.5, "st"),
        ("formant", "Formant", -12, 12, 0.5, "st"),
        ("robot_hz", "Robot pitch", 40, 400, 1, "Hz"),
        ("mix", "Mix (dry/wet)", 0, 1, 0.01, ""),
    ],
    "Tone": [
        ("highpass_hz", "High-pass", 20, 1000, 5, "Hz"),
        ("lowpass_hz", "Low-pass", 1000, 20000, 100, "Hz"),
        ("low_db", "Low (200 Hz)", -12, 12, 0.5, "dB"),
        ("mid_db", "Mid (1.5 kHz)", -12, 12, 0.5, "dB"),
        ("high_db", "High (5 kHz)", -12, 12, 0.5, "dB"),
        ("drive", "Drive", 0, 1, 0.01, ""),
    ],
    "Dynamics & space": [
        ("input_gain_db", "Input gain", -24, 24, 0.5, "dB"),
        ("gate_threshold_db", "Gate threshold", -80, -10, 1, "dB"),
        ("comp_threshold_db", "Comp threshold", -50, 0, 1, "dB"),
        ("comp_ratio", "Comp ratio", 1, 20, 0.5, ":1"),
        ("comp_makeup_db", "Comp makeup", 0, 24, 0.5, "dB"),
        ("reverb_mix", "Reverb", 0, 1, 0.01, ""),
        ("reverb_room", "Room size", 0, 1, 0.01, ""),
        ("output_gain_db", "Output gain", -24, 12, 0.5, "dB"),
    ],
}


class Meter(tk.Canvas):
    def __init__(self, master, label: str):
        super().__init__(master, width=220, height=14, bg="#1e1e1e", highlightthickness=0)
        self.label = label
        self._hold = -60.0
        self._hold_t = 0.0

    def set(self, peak: float) -> None:
        import math
        db = 20 * math.log10(max(peak, 1e-6))
        now = time.monotonic()
        if db > self._hold or now - self._hold_t > 1.0:
            self._hold, self._hold_t = db, now
        w = self.winfo_width() or 220

        def x(d):
            return max(0, min(w, (d + 60) / 60 * w))

        self.delete("all")
        color = "#3fb950" if db < -12 else "#d29922" if db < -3 else "#f85149"
        self.create_rectangle(0, 0, x(db), 14, fill=color, width=0)
        self.create_line(x(self._hold), 0, x(self._hold), 14, fill="#ffffff")
        self.create_text(4, 7, anchor="w", text=self.label, fill="#c9d1d9", font=("TkDefaultFont", 8))


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings = Settings()
        self.engine = None
        self.vars: dict[str, tk.Variable] = {}
        self._suspend = False
        root.title("Voice Changer")
        root.minsize(760, 560)

        try:
            from .audio import default_device, list_devices
            self.devices = list_devices()
            self._def_in, self._def_out = default_device("input"), default_device("output")
        except RuntimeError as exc:
            self.devices = []
            self._def_in = self._def_out = None
            messagebox.showwarning("Audio unavailable", str(exc))

        self._build()
        self._load_preset("natural")
        self._tick()
        root.protocol("WM_DELETE_WINDOW", self._close)

    # ---- layout ----------------------------------------------------------
    def _build(self) -> None:
        pad = {"padx": 6, "pady": 3}
        top = ttk.LabelFrame(self.root, text="Devices")
        top.pack(fill="x", **pad)
        ins = [d.label for d in self.devices if d.inputs]
        outs = [d.label for d in self.devices if d.outputs]
        self.in_var = tk.StringVar(value=self._label_for(self._def_in, ins))
        self.out_var = tk.StringVar(value=self._label_for(self._def_out, outs))
        self.mon_var = tk.StringVar(value="(none)")
        self.quality_var = tk.StringVar(value="balanced")
        for col, (text, var, values) in enumerate([
            ("Microphone", self.in_var, ins),
            ("Output (headphones or virtual cable)", self.out_var, outs),
            ("Monitor (hear yourself)", self.mon_var, ["(none)"] + outs),
        ]):
            ttk.Label(top, text=text).grid(row=0, column=col, sticky="w", padx=4)
            ttk.Combobox(top, textvariable=var, values=values, width=34, state="readonly").grid(
                row=1, column=col, padx=4, pady=2, sticky="ew")
            top.columnconfigure(col, weight=1)
        ttk.Label(top, text="Quality").grid(row=0, column=3, sticky="w", padx=4)
        ttk.Combobox(top, textvariable=self.quality_var, values=list(QUALITY), width=12,
                     state="readonly").grid(row=1, column=3, padx=4)
        self.start_btn = ttk.Button(top, text="Start", command=self._toggle)
        self.start_btn.grid(row=1, column=4, padx=6)

        bar = ttk.Frame(self.root)
        bar.pack(fill="x", **pad)
        ttk.Label(bar, text="Preset").pack(side="left")
        self.preset_var = tk.StringVar(value="natural")
        cb = ttk.Combobox(bar, textvariable=self.preset_var, values=list(PRESETS), width=14, state="readonly")
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self._load_preset(self.preset_var.get()))
        ttk.Label(bar, text="Mode").pack(side="left", padx=(12, 2))
        self.vars["mode"] = tk.StringVar(value="normal")
        for m in ("normal", "robot", "whisper"):
            ttk.Radiobutton(bar, text=m, value=m, variable=self.vars["mode"]).pack(side="left")
        self.vars["mode"].trace_add("write", lambda *_: self._push("mode"))
        ttk.Button(bar, text="Save…", command=self._save).pack(side="right")
        ttk.Button(bar, text="Load…", command=self._load).pack(side="right", padx=4)
        ttk.Button(bar, text="Process WAV file…", command=self._process_file).pack(side="right", padx=4)

        body = ttk.Frame(self.root)
        body.pack(fill="x", **pad)
        for col, (group, items) in enumerate(SLIDERS.items()):
            frame = ttk.LabelFrame(body, text=group)
            frame.grid(row=0, column=col, sticky="nsew", padx=4)
            body.columnconfigure(col, weight=1)
            for row, (key, label, lo, hi, res, unit) in enumerate(items):
                self._slider(frame, row, key, label, lo, hi, res, unit)
            if group == "Dynamics & space":
                r = len(items)
                for i, (key, text) in enumerate([("gate_enabled", "Noise gate"), ("comp_enabled", "Compressor")]):
                    self.vars[key] = tk.BooleanVar(value=True)
                    self.vars[key].trace_add("write", lambda *_, k=key: self._push(k))
                    ttk.Checkbutton(frame, text=text, variable=self.vars[key]).grid(
                        row=r, column=i, sticky="w", padx=4)

        foot = ttk.Frame(self.root)
        foot.pack(fill="x", **pad)
        self.vars["bypass"] = tk.BooleanVar(value=False)
        self.vars["bypass"].trace_add("write", lambda *_: self._push("bypass"))
        ttk.Checkbutton(foot, text="Bypass (A/B)", variable=self.vars["bypass"]).pack(side="left")
        self.monitor_on = tk.BooleanVar(value=True)
        self.monitor_on.trace_add("write", self._monitor_toggled)
        ttk.Checkbutton(foot, text="Monitor on", variable=self.monitor_on).pack(side="left", padx=8)
        self.rec_btn = ttk.Button(foot, text="● Record", command=self._toggle_record)
        self.rec_btn.pack(side="left", padx=8)
        meters = ttk.Frame(foot)
        meters.pack(side="right")
        self.in_meter = Meter(meters, "IN")
        self.out_meter = Meter(meters, "OUT")
        self.in_meter.pack(pady=1)
        self.out_meter.pack(pady=1)

        self.status = tk.StringVar(value="Stopped. Use headphones to avoid feedback.")
        ttk.Label(self.root, textvariable=self.status, anchor="w").pack(fill="x", padx=8, pady=(0, 6))

    def _slider(self, frame, row, key, label, lo, hi, res, unit) -> None:
        var = tk.DoubleVar(value=getattr(self.settings, key))
        self.vars[key] = var
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=4)
        val = ttk.Label(frame, width=8, anchor="e")
        val.grid(row=row, column=2, padx=4)

        def changed(*_):
            v = round(var.get() / res) * res
            val.configure(text=(f"{v:g}{unit}" if unit.startswith(":") else f"{v:g} {unit}").strip())
            self._push(key)

        var.trace_add("write", changed)
        ttk.Scale(frame, from_=lo, to=hi, variable=var, orient="horizontal", length=150).grid(
            row=row, column=1, sticky="ew", padx=2)
        frame.columnconfigure(1, weight=1)
        changed()

    @staticmethod
    def _label_for(index, labels):
        for lab in labels:
            if index is not None and lab.startswith(f"[{index}]"):
                return lab
        return labels[0] if labels else ""

    @staticmethod
    def _index(label: str):
        if not label or label == "(none)":
            return None
        return int(label[1:label.index("]")])

    # ---- settings sync ---------------------------------------------------
    def _push(self, key: str) -> None:
        if self._suspend:
            return
        var = self.vars[key]
        current = getattr(self.settings, key)
        value = var.get()
        if isinstance(current, bool):
            value = bool(value)
        elif isinstance(current, float):
            value = float(value)
        setattr(self.settings, key, value)

    def _pull_all(self) -> None:
        self._suspend = True
        try:
            for key, var in self.vars.items():
                var.set(getattr(self.settings, key))
        finally:
            self._suspend = False

    def _load_preset(self, name: str) -> None:
        bypass = self.settings.bypass
        apply_preset(self.settings, name)
        self.settings.bypass = bypass
        self._pull_all()

    def _save(self) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("Voice settings", "*.json")])
        if path:
            with open(path, "w") as f:
                json.dump(self.settings.to_dict(), f, indent=2)

    def _load(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("Voice settings", "*.json")])
        if path:
            with open(path) as f:
                self.settings.update(**json.load(f))
            self._pull_all()

    # ---- engine ----------------------------------------------------------
    def _toggle(self) -> None:
        if self.engine:
            self.engine.stop()
            self.engine = None
            self.start_btn.configure(text="Start")
            self.rec_btn.configure(text="● Record")
            self.status.set("Stopped.")
            return
        from .audio import LiveEngine
        try:
            self.engine = LiveEngine(self._index(self.in_var.get()), self._index(self.out_var.get()),
                                     self._index(self.mon_var.get()), 48000, self.quality_var.get(),
                                     self.settings)
            self.engine.monitor_enabled = self.monitor_on.get() and self.engine.monitor_device is not None
            self.engine.start()
        except Exception as exc:  # device errors surface here
            self.engine = None
            messagebox.showerror("Could not start audio", str(exc))
            return
        self.start_btn.configure(text="Stop")

    def _monitor_toggled(self, *_):
        if self.engine:
            self.engine.monitor_enabled = self.monitor_on.get() and self.engine.monitor_device is not None

    def _toggle_record(self) -> None:
        if not self.engine:
            messagebox.showinfo("Record", "Start the voice changer first.")
            return
        if self.engine._recorder:
            path = self.engine.stop_recording()
            self.rec_btn.configure(text="● Record")
            self.status.set(f"Saved {path}")
            return
        path = filedialog.asksaveasfilename(defaultextension=".wav", filetypes=[("WAV", "*.wav")])
        if path:
            self.engine.start_recording(path)
            self.rec_btn.configure(text="■ Stop recording")

    def _process_file(self) -> None:
        src = filedialog.askopenfilename(filetypes=[("WAV", "*.wav")])
        if not src:
            return
        dst = filedialog.asksaveasfilename(defaultextension=".wav", filetypes=[("WAV", "*.wav")])
        if not dst:
            return
        from .fileio import process_file
        snapshot = Settings().update(**self.settings.to_dict())
        snapshot.bypass = False
        secs = process_file(src, dst, snapshot)
        self.status.set(f"Processed {secs:.1f} s → {dst}")

    def _tick(self) -> None:
        e = self.engine
        if e:
            c = e.chain
            self.in_meter.set(c.input_peak)
            self.out_meter.set(c.output_peak)
            rec = "  ● REC" if e._recorder else ""
            self.status.set(
                f"Running · latency ≈ {e.latency_ms:.0f} ms · CPU {e.cpu_load * 100:.0f}% · "
                f"gate {'open' if c.gate_open else 'closed'} · comp {c.gain_reduction_db:.1f} dB · "
                f"dropouts {e.xruns}{rec}")
        else:
            self.in_meter.set(0)
            self.out_meter.set(0)
        self.root.after(50, self._tick)

    def _close(self) -> None:
        if self.engine:
            self.engine.stop()
        self.root.destroy()


def run_gui() -> int:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if "vista" in ttk.Style().theme_names() else "clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()
    return 0
