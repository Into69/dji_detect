"""
External SDR IQ capture, triggered by ANTsdr DroneID detections.

When a drone is detected, the worker tunes an external SDR (e.g. Ettus B206mini
via UHD, or anything SoapySDR supports) to the frequency the ANTsdr reported and
records a short burst of raw IQ to disk in SigMF format:

    sdr_captures/<stamp>_<serial>_<freq>MHz.sigmf-data   interleaved int16 I/Q (ci16_le)
    sdr_captures/<stamp>_<serial>_<freq>MHz.sigmf-meta   JSON metadata (rate, freq, gain, trigger drone)

These files are the input for offline decoding work.

Backends (both optional, loaded lazily):
  - UHD python bindings (`import uhd`) — native path for Ettus devices
  - SoapySDR python bindings (`import SoapySDR`) — generic path for other SDRs
numpy is required for either.
"""

import datetime
import json
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

CAPTURES_DIR = Path(__file__).parent / "sdr_captures"

DEFAULTS = {
    "enabled":        False,
    "device":         "",          # "uhd:type=b200,serial=..." or "soapy:driver=..."
    "sample_rate":    15.36e6,     # DJI DroneID OFDM is natively sampled at 15.36 Msps
    "bandwidth":      0.0,         # 0 = match sample rate
    "gain":           50.0,        # dB
    "antenna":        "",          # "" = device default (B2xx: "RX2" or "TX/RX")
    "duration_s":     2.0,         # seconds of IQ per capture
    "cooldown_s":     60,          # min seconds between captures of the same drone
    "max_storage_gb": 10.0,        # stop capturing when the capture dir exceeds this
}

LIMITS = {
    "sample_rate": (1e6, 61.44e6),
    "bandwidth":   (0.0, 61.44e6),
    "gain":        (0.0, 90.0),
    "duration_s":  (0.05, 10.0),
    "cooldown_s":  (0, 86400),
    "max_storage_gb": (0.1, 10000.0),
}

# Upper bound on samples per capture. UHD rejects num_samps above 0x0fffffff in a
# single stream command, and the capture buffer is held in RAM (4 bytes/sample),
# so 2**27 samples = 512 MiB. duration_s is shortened to fit at high rates.
MAX_CAPTURE_SAMPLES = 2 ** 27

# USB receive frames requested for B2xx devices (see _uhd_open_args).
UHD_B200_RECV_FRAMES = 512

# Self-test, run after the device opens and whenever an RF setting changes.
SELF_TEST_FREQS_MHZ = (2437.0, 5800.0)   # centre of the DroneID 2.4 / 5.8 GHz bands
SELF_TEST_STREAM_S = 0.5                 # measured stream length (after a short priming stream)
SELF_TEST_PRIME_S = 0.05                 # first stream after open can hiccup; not measured
CLIP_LEVEL = 32000                       # |I| or |Q| at/above this counts as clipped (int16)

# States in which detections are accepted. While the device is still opening or
# self-testing (a B2xx loads firmware + FPGA on first open, which takes seconds),
# the first detection is held in the queue and recorded as soon as it is ready.
ACCEPTING_STATES = ("opening", "testing", "ready", "capturing")

FREQ_MIN_MHZ = 50.0
FREQ_MAX_MHZ = 6000.0


def sanitize_config(raw) -> dict:
    cfg = dict(DEFAULTS)
    if not isinstance(raw, dict):
        return cfg
    for key, default in DEFAULTS.items():
        if key not in raw or raw[key] is None:
            continue
        v = raw[key]
        if isinstance(default, bool):
            if isinstance(v, bool):
                val = v
            elif isinstance(v, str) and v.strip().lower() in ("true", "1", "yes", "on"):
                val = True
            elif isinstance(v, str) and v.strip().lower() in ("false", "0", "no", "off", ""):
                val = False
            elif isinstance(v, (int, float)):
                val = bool(v)
            else:
                continue
        else:
            try:
                val = type(default)(v)
            except (TypeError, ValueError):
                continue
        if key in LIMITS:
            lo, hi = LIMITS[key]
            val = type(default)(max(lo, min(hi, val)))
        if isinstance(val, str):
            val = val.strip()
        cfg[key] = val
    max_dur = MAX_CAPTURE_SAMPLES / cfg["sample_rate"]
    if cfg["duration_s"] > max_dur:
        cfg["duration_s"] = round(int(max_dur * 1000) / 1000, 3)
    return cfg


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", s or "")[:64] or "unknown"


def safe_capture_base(name: str) -> str:
    """Normalise a user-supplied capture name to its base (no extension)."""
    name = (name or "").strip()
    for ext in (".sigmf-data", ".sigmf-meta"):
        if name.endswith(ext):
            name = name[: -len(ext)]
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


# ── Backend loading ──────────────────────────────────────────────────────────

_np = None
_uhd = None
_soapy = None
_backend_errors: dict = {}
_backends_loaded = False
_backends_lock = threading.Lock()


def _load_backends():
    global _np, _uhd, _soapy, _backends_loaded
    with _backends_lock:
        if _backends_loaded:
            return
        _backends_loaded = True
        try:
            import numpy
            _np = numpy
        except Exception as e:
            _backend_errors["numpy"] = str(e)
        try:
            import uhd
            _uhd = uhd
        except Exception as e:
            _backend_errors["uhd"] = str(e)
        try:
            import SoapySDR
            _soapy = SoapySDR
        except Exception as e:
            _backend_errors["soapy"] = str(e)


def backend_info() -> dict:
    _load_backends()
    return {
        "numpy": _np is not None,
        "uhd":   _uhd is not None,
        "soapy": _soapy is not None,
        "uhd_cli": shutil.which("uhd_find_devices") is not None,
        "errors": dict(_backend_errors),
    }


def _parse_kv(s: str) -> dict:
    out = {}
    for part in s.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _kv_to_str(d: dict) -> str:
    return ",".join(f"{k}={v}" for k, v in d.items() if v != "")


def _find_uhd_devices() -> list:
    devs = []
    # Prefer the CLI: its output format is stable across UHD versions.
    exe = shutil.which("uhd_find_devices")
    if exe:
        try:
            out = subprocess.run([exe], capture_output=True, text=True, timeout=20).stdout
            cur = None
            for line in out.splitlines():
                if "-- UHD Device" in line:
                    cur = {}
                    devs.append(cur)
                    continue
                m = re.match(r"^\s+(\w+):\s*(.*)$", line)
                if cur is not None and m:
                    cur[m.group(1)] = m.group(2).strip()
        except Exception as e:
            _backend_errors["uhd_find_devices"] = str(e)
    elif _uhd is not None:
        try:
            for addr in _uhd.find(""):
                devs.append(_parse_kv(addr.to_string() if hasattr(addr, "to_string") else str(addr)))
        except Exception as e:
            _backend_errors["uhd_find"] = str(e)
    result = []
    for d in devs:
        args = {k: d[k] for k in ("type", "serial") if d.get(k)}
        if not args:
            continue
        label = d.get("product") or d.get("name") or d.get("type", "UHD")
        if d.get("serial"):
            label += f"  (S/N {d['serial']})"
        result.append({"id": "uhd:" + _kv_to_str(args), "label": f"UHD · {label}", "backend": "uhd"})
    return result


def _find_soapy_devices() -> list:
    if _soapy is None:
        return []
    result = []
    try:
        for kw in _soapy.Device.enumerate():
            try:
                d = dict(kw)
            except Exception:
                d = _parse_kv(str(kw))
            # Skip Soapy's UHD shim when native UHD is available — same hardware, slower path.
            if d.get("driver") == "uhd" and _uhd is not None:
                continue
            args = {k: d[k] for k in ("driver", "serial", "device_id") if d.get(k)}
            if not args:
                continue
            label = d.get("label") or d.get("product") or d.get("driver", "Soapy")
            result.append({"id": "soapy:" + _kv_to_str(args), "label": f"Soapy · {label}", "backend": "soapy"})
    except Exception as e:
        _backend_errors["soapy_enumerate"] = str(e)
    return result


# ── Device wrappers ──────────────────────────────────────────────────────────

def _uhd_open_args(args: str) -> str:
    """Add a larger USB receive buffer for B2xx devices unless the user set one.

    UHD's B2xx default (16 frames, ~256 KB) holds only ~4 ms at 15.36 Msps sc16.
    The capture thread shares the GIL with the web server, so a few ms between
    recv() calls is enough to overflow; 512 frames (~8 MB) gives >100 ms of slack.
    """
    kv = _parse_kv(args)
    if kv.get("type") == "b200" and "num_recv_frames" not in kv:
        kv["num_recv_frames"] = str(UHD_B200_RECV_FRAMES)
        return _kv_to_str(kv)
    return args


class _UhdDevice:
    def __init__(self, args: str):
        open_args = _uhd_open_args(args)
        try:
            self.usrp = _uhd.usrp.MultiUSRP(open_args)
        except Exception as e:
            if open_args == args:
                raise
            print(f"[SDR] Open with '{open_args}' failed ({e}); retrying with '{args}'")
            open_args = args
            self.usrp = _uhd.usrp.MultiUSRP(args)
        self.open_args = open_args
        st_args = _uhd.usrp.StreamArgs("sc16", "sc16")
        st_args.channels = [0]
        self.streamer = self.usrp.get_rx_stream(st_args)
        self.max_samps = self.streamer.get_max_num_samps()
        # Scratch buffer for draining; one uint32 per sc16 I/Q sample.
        self.buf = _np.zeros((1, self.max_samps), dtype=_np.uint32)
        self.name = self.usrp.get_mboard_name()
        self._applied = {}

    def configure(self, cfg: dict):
        rate = cfg["sample_rate"]
        if self._applied.get("rate") != rate:
            self.usrp.set_rx_rate(rate, 0)
        bw = cfg["bandwidth"] or rate
        if self._applied.get("bw") != bw:
            try:
                self.usrp.set_rx_bandwidth(bw, 0)
            except Exception:
                pass
        if cfg["antenna"] and self._applied.get("ant") != cfg["antenna"]:
            self.usrp.set_rx_antenna(cfg["antenna"], 0)
        if self._applied.get("gain") != cfg["gain"]:
            self.usrp.set_rx_gain(cfg["gain"], 0)
        self._applied = {"rate": rate, "bw": bw, "ant": cfg["antenna"], "gain": cfg["gain"]}

    def actual(self) -> dict:
        return {
            "sample_rate": self.usrp.get_rx_rate(0),
            "gain":        self.usrp.get_rx_gain(0),
            "antenna":     self.usrp.get_rx_antenna(0),
            "bandwidth":   self.usrp.get_rx_bandwidth(0),
        }

    def tune(self, freq_hz: float) -> float:
        """Tune and wait (≤0.5 s) for LO lock. Sets self.last_lock: True/False, or None if unknown."""
        self.usrp.set_rx_freq(_uhd.types.TuneRequest(freq_hz), 0)
        self.last_lock = False
        deadline = time.time() + 0.5
        while time.time() < deadline:
            try:
                if self.usrp.get_rx_sensor("lo_locked", 0).to_bool():
                    self.last_lock = True
                    break
            except Exception:
                self.last_lock = None        # device has no lo_locked sensor
                break
            time.sleep(0.005)
        return self.usrp.get_rx_freq(0)

    def capture(self, num_samps: int, out) -> tuple:
        """Fill `out` (int16, len 2*num_samps). Returns (samples_received, overflows)."""
        md = _uhd.types.RXMetadata()
        cmd = _uhd.types.StreamCMD(_uhd.types.StreamMode.num_done)
        cmd.num_samps = num_samps
        cmd.stream_now = True
        self.streamer.issue_stream_cmd(cmd)
        # View the int16 I/Q buffer as one uint32 per complex sample so UHD's
        # sc16 recv writes straight into it -- no per-packet Python conversion.
        out32 = out[: 2 * num_samps].view(_np.uint32)
        chunk = max(self.max_samps, 1 << 18)
        got, overflows = 0, 0
        timeout = 1.0
        while got < num_samps:
            want = min(chunk, num_samps - got)
            n = self.streamer.recv(out32[got: got + want].reshape(1, want), md, timeout)
            timeout = 0.5
            got += n
            err = md.error_code
            if err == _uhd.types.RXMetadataErrorCode.overflow:
                overflows += 1
                break        # num_done bursts end on overflow; report a short capture
            elif err == _uhd.types.RXMetadataErrorCode.timeout:
                break
            elif err != _uhd.types.RXMetadataErrorCode.none:
                raise RuntimeError(f"UHD recv error: {err}")
        # Drain anything left so the next capture starts clean.
        stop = _uhd.types.StreamCMD(_uhd.types.StreamMode.stop_cont)
        self.streamer.issue_stream_cmd(stop)
        while self.streamer.recv(self.buf, md, 0.05):
            pass
        return got, overflows

    def close(self):
        self.streamer = None
        self.usrp = None


class _SoapyDevice:
    def __init__(self, args: str):
        from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CS16
        self.RX, self.CS16 = SOAPY_SDR_RX, SOAPY_SDR_CS16
        self.dev = _soapy.Device(args)
        self.name = self.dev.getHardwareKey()
        self._applied = {}

    def configure(self, cfg: dict):
        rate = cfg["sample_rate"]
        if self._applied.get("rate") != rate:
            self.dev.setSampleRate(self.RX, 0, rate)
        bw = cfg["bandwidth"] or rate
        if self._applied.get("bw") != bw:
            try:
                self.dev.setBandwidth(self.RX, 0, bw)
            except Exception:
                pass
        if cfg["antenna"] and self._applied.get("ant") != cfg["antenna"]:
            self.dev.setAntenna(self.RX, 0, cfg["antenna"])
        if self._applied.get("gain") != cfg["gain"]:
            try:
                self.dev.setGainMode(self.RX, 0, False)
            except Exception:
                pass
            self.dev.setGain(self.RX, 0, cfg["gain"])
        self._applied = {"rate": rate, "bw": bw, "ant": cfg["antenna"], "gain": cfg["gain"]}

    def actual(self) -> dict:
        return {
            "sample_rate": self.dev.getSampleRate(self.RX, 0),
            "gain":        self.dev.getGain(self.RX, 0),
            "antenna":     self.dev.getAntenna(self.RX, 0),
            "bandwidth":   self.dev.getBandwidth(self.RX, 0),
        }

    def tune(self, freq_hz: float) -> float:
        self.dev.setFrequency(self.RX, 0, freq_hz)
        time.sleep(0.01)
        self.last_lock = None                # Soapy has no portable lock sensor
        return self.dev.getFrequency(self.RX, 0)

    def capture(self, num_samps: int, out) -> tuple:
        stream = self.dev.setupStream(self.RX, self.CS16, [0])
        self.dev.activateStream(stream)
        got, overflows, failures = 0, 0, 0
        try:
            while got < num_samps:
                want = min(65536, num_samps - got)
                sr = self.dev.readStream(stream, [out[2 * got:]], want, timeoutUs=1_000_000)
                if sr.ret > 0:
                    got += sr.ret
                    failures = 0
                elif sr.ret == -4:          # SOAPY_SDR_OVERFLOW
                    overflows += 1
                else:
                    failures += 1
                    if failures > 5:
                        break
        finally:
            self.dev.deactivateStream(stream)
            self.dev.closeStream(stream)
        return got, overflows

    def close(self):
        self.dev = None


# ── Manager ──────────────────────────────────────────────────────────────────

class SdrCaptureManager:
    """Owns the SDR on a single worker thread; detections enqueue capture requests."""

    def __init__(self, config: dict, broadcast=None, error=None, version: str = ""):
        self.cfg = sanitize_config(config)
        self._broadcast = broadcast or (lambda payload: None)
        self._error = error or (lambda src, msg: None)
        self._version = version
        self._q: queue.Queue = queue.Queue(maxsize=1)
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._dev = None
        self._dev_id = ""
        self._buf = None                  # reusable int16 I/Q capture buffer (worker thread only)
        self._tested_key = None           # RF settings the last self-test ran with
        self._selftest_req = threading.Event()
        self._reported_fail = ""          # last self-test failure sent to error(), to avoid repeats
        self._last_capture: dict = {}     # serial -> wall time
        self._dropped_sns: set = set()     # serials already counted as dropped while busy
        self._state = {
            "state":        "disabled",   # disabled | opening | testing | ready | capturing | error
            "device_name":  "",
            "last_error":   "",
            "captures":     0,
            "dropped":      0,
            "last_capture": None,
            "self_test":    None,         # {ok, ran_at, elapsed_s, warnings, checks:[{name, level, detail}]}
        }
        CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
        threading.Thread(target=self._worker, daemon=True, name="sdr-capture").start()

    # -- public API (any thread) --

    def update_config(self, raw: dict) -> dict:
        with self._lock:
            self.cfg = sanitize_config({**self.cfg, **(raw or {})})
            cfg = dict(self.cfg)
        self._wake.set()
        return cfg

    def config(self) -> dict:
        with self._lock:
            return dict(self.cfg)

    def status(self) -> dict:
        with self._lock:
            st = dict(self._state)
            st["enabled"] = self.cfg["enabled"]
            st["device"] = self.cfg["device"]
        st["storage_bytes"] = self._storage_bytes()
        return st

    def devices(self) -> dict:
        _load_backends()
        devs = _find_uhd_devices() + _find_soapy_devices()
        # A device we already hold open may not enumerate — keep it listed.
        with self._lock:
            cur_id, cur_name = self._dev_id, self._state.get("device_name")
        if cur_id and not any(d["id"] == cur_id for d in devs):
            devs.insert(0, {"id": cur_id, "label": f"{cur_name or cur_id} (in use)",
                            "backend": cur_id.split(":", 1)[0]})
        return {"devices": devs, "backends": backend_info()}

    def on_detection(self, drone: dict, sensor: dict | None = None):
        """Called for every parsed detection. Cheap; never blocks."""
        with self._lock:
            if not self.cfg["enabled"] or self._state["state"] not in ACCEPTING_STATES:
                return
            sn = drone.get("serial_number") or "unknown"
            now = time.time()
            if now - self._last_capture.get(sn, 0) < self.cfg["cooldown_s"]:
                return
            freq = float(drone.get("freq") or 0)
            if not (FREQ_MIN_MHZ <= freq <= FREQ_MAX_MHZ):
                return
            try:
                self._q.put_nowait({"drone": dict(drone), "sensor": dict(sensor or {}),
                                    "freq_mhz": freq, "reason": "detection"})
                self._last_capture[sn] = now
            except queue.Full:
                if sn not in self._dropped_sns:
                    self._dropped_sns.add(sn)
                    self._state["dropped"] += 1

    def test_capture(self, freq_mhz: float) -> dict:
        if not (FREQ_MIN_MHZ <= freq_mhz <= FREQ_MAX_MHZ):
            return {"ok": False, "error": f"frequency must be {FREQ_MIN_MHZ:.0f}-{FREQ_MAX_MHZ:.0f} MHz"}
        with self._lock:
            if self._state["state"] not in ACCEPTING_STATES:
                return {"ok": False, "error": f"SDR not ready (state: {self._state['state']})"}
        try:
            self._q.put_nowait({"drone": {}, "sensor": {}, "freq_mhz": freq_mhz, "reason": "manual test"})
        except queue.Full:
            return {"ok": False, "error": "a capture is already queued"}
        return {"ok": True}

    def request_self_test(self) -> dict:
        """Ask the worker to re-run the self-test (and reopen the device if it is in error)."""
        with self._lock:
            if not self.cfg["enabled"] or not self.cfg["device"]:
                return {"ok": False, "error": "SDR is disabled or no device is selected"}
        self._selftest_req.set()
        self._wake.set()
        return {"ok": True}

    def list_captures(self) -> list:
        items = []
        for meta in sorted(CAPTURES_DIR.glob("*.sigmf-meta"), reverse=True):
            base = meta.name[: -len(".sigmf-meta")]
            data = CAPTURES_DIR / (base + ".sigmf-data")
            try:
                m = json.loads(meta.read_text())
            except Exception:
                m = {}
            g = m.get("global", {})
            cap = (m.get("captures") or [{}])[0]
            try:
                size = data.stat().st_size
            except OSError:
                size = 0
            items.append({
                "name":        base,
                "size":        size,
                "sample_rate": g.get("core:sample_rate"),
                "freq_hz":     cap.get("core:frequency"),
                "datetime":    cap.get("core:datetime"),
                "trigger":     g.get("dji_detect:trigger", {}),
                "capture":     g.get("dji_detect:capture", {}),
            })
        return items

    def capture_path(self, name: str, part: str) -> Path | None:
        base = safe_capture_base(name)
        if not base or part not in ("data", "meta"):
            return None
        p = CAPTURES_DIR / f"{base}.sigmf-{part}"
        return p if p.is_file() else None

    def delete_capture(self, name: str) -> bool:
        base = safe_capture_base(name)
        found = False
        for part in ("data", "meta"):
            p = CAPTURES_DIR / f"{base}.sigmf-{part}"
            if p.is_file():
                p.unlink()
                found = True
        return found

    # -- worker --

    def _set_state(self, **kw):
        with self._lock:
            if all(self._state.get(k) == v for k, v in kw.items()):
                return
            self._state.update(kw)
            st = dict(self._state)
            st["enabled"] = self.cfg["enabled"]
        self._broadcast({"type": "sdr_status", "status": st})

    def _storage_bytes(self) -> int:
        total = 0
        for p in CAPTURES_DIR.glob("*.sigmf-data"):
            try:
                total += p.stat().st_size
            except OSError:
                pass
        return total

    def _close(self):
        if self._dev is not None:
            try:
                self._dev.close()
            except Exception:
                pass
        self._dev = None
        self._dev_id = ""
        self._buf = None                  # release the capture buffer with the device
        self._tested_key = None           # a reopened device is always re-tested

    @staticmethod
    def _capture_samples(cfg: dict) -> int:
        return min(int(cfg["sample_rate"] * cfg["duration_s"]), MAX_CAPTURE_SAMPLES)

    def _ensure_buffer(self, cfg: dict):
        """Return an int16 buffer of 2*samples for this config, reusing the last one.

        A new buffer is written once (fill) so the OS maps its pages now, on the
        idle worker, rather than page-faulting while samples are streaming in.
        """
        need = 2 * self._capture_samples(cfg)
        if self._buf is None or self._buf.size != need:
            self._buf = None              # free the old one before allocating
            buf = _np.empty(need, dtype=_np.int16)
            buf.fill(0)
            self._buf = buf
        return self._buf

    def _ensure_device(self, cfg: dict) -> bool:
        """Open/close the device to match config. Returns True if ready to capture."""
        if not cfg["enabled"] or not cfg["device"]:
            if self._dev is not None:
                print("[SDR] Closing device")
            self._close()
            self._set_state(state="disabled", device_name="",
                            last_error="" if cfg["enabled"] is False else "no device selected")
            return False
        if self._dev is not None and self._dev_id == cfg["device"]:
            return True
        self._close()
        _load_backends()
        if _np is None:
            self._set_state(state="error", last_error="numpy is not installed")
            return False
        backend, _, args = cfg["device"].partition(":")
        self._set_state(state="opening", last_error="")
        print(f"[SDR] Opening {backend} device '{args}'")
        t_open = time.time()
        try:
            if backend == "uhd":
                if _uhd is None:
                    raise RuntimeError("UHD python module not installed: " + _backend_errors.get("uhd", ""))
                self._dev = _UhdDevice(args)
            elif backend == "soapy":
                if _soapy is None:
                    raise RuntimeError("SoapySDR python module not installed: " + _backend_errors.get("soapy", ""))
                self._dev = _SoapyDevice(args)
            else:
                raise RuntimeError(f"unknown backend '{backend}'")
            self._dev_id = cfg["device"]
            self._dev.configure(cfg)
            opened = getattr(self._dev, "open_args", args)
            print(f"[SDR] Opened: {self._dev.name} (args '{opened}') — {time.time() - t_open:.1f}s")
            # "ready" is set by the self-test the worker runs next.
            self._set_state(state="testing", device_name=self._dev.name)
            return True
        except Exception as e:
            self._close()
            msg = f"open failed: {e}"
            print(f"[SDR] {msg}")
            self._error("SDR", msg)
            self._set_state(state="error", last_error=msg)
            return False

    @staticmethod
    def _rf_key(cfg: dict) -> tuple:
        return (cfg["device"], cfg["sample_rate"], cfg["bandwidth"], cfg["gain"], cfg["antenna"])

    def _self_test(self, cfg: dict, cfg_err: str = "") -> bool:
        """Verify the open device took the settings, tunes, and streams cleanly.

        Levels: ok / warn (captures still run, but check this) / fail (device is
        closed and reopened later). Result goes into status()["self_test"].
        """
        t0 = time.time()
        self._set_state(state="testing")
        checks: list = []

        def add(name, level, detail):
            checks.append({"name": name, "level": level, "detail": detail})

        try:
            if cfg_err:
                add("Configure", "fail", f"device rejected the settings: {cfg_err}")
            act = self._dev.actual()

            rate, got_rate = cfg["sample_rate"], float(act["sample_rate"])
            if abs(got_rate - rate) > rate * 1e-3:
                add("Sample rate", "warn", f"requested {rate / 1e6:.3f} Msps, device set "
                    f"{got_rate / 1e6:.3f} Msps (captures record the actual rate)")
            else:
                add("Sample rate", "ok", f"{got_rate / 1e6:.3f} Msps")

            got_gain = float(act["gain"])
            if abs(got_gain - cfg["gain"]) > 1.0:
                add("Gain", "warn", f"requested {cfg['gain']:.0f} dB, device set {got_gain:.1f} dB "
                    "(outside the device's range?)")
            else:
                add("Gain", "ok", f"{got_gain:.1f} dB")

            want_bw = cfg["bandwidth"] or rate
            try:
                got_bw = float(act["bandwidth"])
            except (TypeError, ValueError):
                got_bw = 0.0
            if got_bw and abs(got_bw - want_bw) > want_bw * 0.05:
                add("Bandwidth", "warn", f"requested {want_bw / 1e6:.2f} MHz, device set {got_bw / 1e6:.2f} MHz")
            else:
                add("Bandwidth", "ok", f"{got_bw / 1e6:.2f} MHz" if got_bw else "device default")

            if cfg["antenna"] and act["antenna"] != cfg["antenna"]:
                add("Antenna", "fail", f"requested '{cfg['antenna']}', device is on '{act['antenna']}'")
            else:
                add("Antenna", "ok", str(act["antenna"]) or "default")

            # Tuning: both DroneID bands. One band failing is a warning; both is a failure.
            tuned_ok = 0
            for f_mhz in SELF_TEST_FREQS_MHZ:
                if not (FREQ_MIN_MHZ <= f_mhz <= FREQ_MAX_MHZ):
                    continue
                got_f = float(self._dev.tune(f_mhz * 1e6))
                lock = getattr(self._dev, "last_lock", None)
                name = f"Tune {f_mhz / 1000:.1f} GHz"
                if abs(got_f - f_mhz * 1e6) > 1e6:
                    add(name, "warn", f"device tuned to {got_f / 1e6:.1f} MHz instead — "
                        f"detections in this band can't be captured")
                elif lock is False:
                    add(name, "warn", "LO did not lock within 0.5 s")
                else:
                    tuned_ok += 1
                    add(name, "ok", "locked" if lock else "tuned (lock not reported)")
            if tuned_ok == 0:
                add("Tuning", "fail", "could not tune either DroneID band (2.4 / 5.8 GHz)")

            # Streaming: short priming stream (first one after open may hiccup), then measure.
            n = max(1, min(self._capture_samples(cfg), int(rate * SELF_TEST_STREAM_S)))
            if self._buf is not None and self._buf.size >= 2 * n:
                buf = self._buf[: 2 * n]
            else:
                buf = _np.empty(2 * n, dtype=_np.int16)
            self._dev.capture(max(1, min(n, int(rate * SELF_TEST_PRIME_S))), buf)
            ts = time.time()
            got, overflows = self._dev.capture(n, buf)
            elapsed = time.time() - ts
            if got == 0:
                add("Stream", "fail", "no samples received")
            elif overflows or got < n:
                add("Stream", "warn", f"{got}/{n} samples, {overflows} overflow(s) in a "
                    f"{n / rate:.2f} s test — check the SDR is on a USB 3 port and the host isn't overloaded")
            else:
                add("Stream", "ok", f"{got} samples in {elapsed:.2f} s, no overflows")

            if got:
                s = buf[: 2 * min(got, 1 << 20)].astype(_np.int32)   # ≤1M samples is plenty
                peak = int(_np.abs(s).max())
                clipped = float(_np.count_nonzero(_np.abs(s) >= CLIP_LEVEL)) / s.size
                rms = float(_np.sqrt(_np.mean(s.astype(_np.float64) ** 2)))
                if peak == 0:
                    add("Signal", "fail", "samples are all zero — check the antenna port and gain")
                elif clipped > 0.001:
                    add("Signal", "warn", f"{clipped * 100:.1f}% of samples clipped (peak {peak}) — lower the gain")
                else:
                    add("Signal", "ok", f"peak {peak}, RMS {rms:.0f} (int16 full scale 32767)")
        except Exception as e:
            add("Self-test", "fail", f"{type(e).__name__}: {e}")

        fails = [c for c in checks if c["level"] == "fail"]
        warns = [c for c in checks if c["level"] == "warn"]
        ok = not fails
        result = {"ok": ok, "ran_at": time.time(), "elapsed_s": round(time.time() - t0, 2),
                  "warnings": len(warns), "checks": checks}
        summary = "; ".join(f"{c['name']}: {c['detail']}" for c in (fails or warns))
        print(f"[SDR] Self-test {'passed' if ok else 'FAILED'} in {result['elapsed_s']}s"
              + (f" — {summary}" if summary else ""))
        if ok:
            self._reported_fail = ""
            self._set_state(state="ready", self_test=result,
                            last_error=f"self-test: {summary}" if warns else "")
        else:
            msg = f"self-test failed — {summary}"
            if msg != self._reported_fail:       # don't repeat the same alert every retry
                self._reported_fail = msg
                self._error("SDR", msg)
            self._set_state(state="error", self_test=result, last_error=msg)
        return ok

    def _worker(self):
        retry_at = 0.0
        buf_failed_size = 0               # buffer size whose allocation last failed
        while True:
            cfg = self.config()
            if time.time() >= retry_at:
                ready = self._ensure_device(cfg)
                if not ready and cfg["enabled"] and cfg["device"]:
                    retry_at = time.time() + 15     # back off after a failed open
            else:
                ready = self._dev is not None
            if ready:
                cfg_err = ""
                try:
                    self._dev.configure(cfg)
                except Exception as e:
                    cfg_err = str(e) or type(e).__name__
                # Allocate/resize the capture buffer here, off the trigger path.
                need = 2 * self._capture_samples(cfg)
                if need != buf_failed_size:
                    try:
                        self._ensure_buffer(cfg)
                        buf_failed_size = 0
                    except MemoryError:
                        self._buf = None
                        buf_failed_size = need    # report once per size, not every loop
                        msg = (f"cannot allocate {need * 2 / 2**20:.0f} MiB capture buffer "
                               "— lower sample rate or duration")
                        self._error("SDR", msg)
                        self._set_state(last_error=msg)
                # Self-test after open, after any RF setting change, or on request.
                key = self._rf_key(cfg)
                if key != self._tested_key or self._selftest_req.is_set():
                    self._selftest_req.clear()
                    self._tested_key = key
                    if not self._self_test(cfg, cfg_err):
                        self._close()            # also clears _tested_key → re-test on reopen
                        retry_at = time.time() + 15
                        continue
            try:
                req = self._q.get(timeout=1.0)
            except queue.Empty:
                if self._wake.is_set():
                    self._wake.clear()
                    retry_at = 0.0
                continue
            with self._lock:
                self._dropped_sns.clear()
            if self._dev is None:
                continue
            try:
                self._capture(req, self.config())
            except Exception as e:
                msg = f"capture failed: {e}"
                print(f"[SDR] {msg}")
                self._error("SDR", msg)
                self._close()                    # force a reopen on the next loop
                self._set_state(state="error", last_error=msg)
                retry_at = time.time() + 5

    def _capture(self, req: dict, cfg: dict):
        max_bytes = cfg["max_storage_gb"] * 1e9
        if self._storage_bytes() >= max_bytes:
            self._set_state(last_error=f"storage limit reached ({cfg['max_storage_gb']} GB) — capture skipped")
            return
        drone = req["drone"]
        freq_hz = req["freq_mhz"] * 1e6
        num = self._capture_samples(cfg)
        out = self._ensure_buffer(cfg)    # normally already allocated by the worker loop

        self._set_state(state="capturing")
        t0 = time.time()
        actual_freq = self._dev.tune(freq_hz)
        t_start = time.time()
        got, overflows = self._dev.capture(num, out)
        actual = self._dev.actual()

        stamp = datetime.datetime.fromtimestamp(t_start).strftime("%Y%m%d-%H%M%S")
        sn = drone.get("serial_number") or "test"
        base = f"{stamp}_{_safe(sn)}_{req['freq_mhz']:.1f}MHz"
        data_path = CAPTURES_DIR / f"{base}.sigmf-data"
        out[: 2 * got].tofile(data_path)

        meta = {
            "global": {
                "core:datatype":    "ci16_le",
                "core:sample_rate": actual["sample_rate"],
                "core:version":     "1.0.0",
                "core:num_channels": 1,
                "core:hw":          self._dev.name,
                "core:recorder":    f"dji-detect {self._version}".strip(),
                "core:description": f"IQ capture triggered by {req['reason']}"
                                    + (f" of {drone.get('device_type', '')} {sn}" if drone else ""),
                "core:extensions":  [{"name": "dji_detect", "version": "0.1.0", "optional": True}],
                "dji_detect:capture": {
                    "requested_samples": num,
                    "captured_samples":  got,
                    "complete":          got >= num,
                    "overflows":         overflows,
                    "gain_db":           actual["gain"],
                    "antenna":           actual["antenna"],
                    "bandwidth_hz":      actual["bandwidth"],
                    "requested_freq_hz": freq_hz,
                    "trigger_latency_s": round(t_start - drone.get("last_seen", t0), 4) if drone else None,
                    "device":            cfg["device"],
                },
                "dji_detect:trigger": {k: drone.get(k) for k in (
                    "serial_number", "device_type", "protocol", "freq", "rssi",
                    "drone_lat", "drone_lon", "geodetic_altitude", "height_agl",
                    "pilot_lat", "pilot_lon", "home_lat", "home_lon",
                    "horizontal_speed", "vertical_speed", "course", "last_seen",
                ) if k in drone},
            },
            "captures": [{
                "core:sample_start": 0,
                "core:frequency":    actual_freq,
                "core:datetime":     datetime.datetime.fromtimestamp(
                    t_start, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            }],
            "annotations": [],
        }
        s = req.get("sensor") or {}
        if s.get("lat") is not None and s.get("lon") is not None:
            coords = [s["lon"], s["lat"]] + ([s["alt"]] if s.get("alt") is not None else [])
            meta["global"]["core:geolocation"] = {"type": "Point", "coordinates": coords}
        (CAPTURES_DIR / f"{base}.sigmf-meta").write_text(json.dumps(meta, indent=2))

        short = got < num
        info = {"name": base, "samples": got, "requested": num, "complete": not short,
                "overflows": overflows,
                "freq_mhz": req["freq_mhz"], "serial": sn, "bytes": got * 4,
                "elapsed_s": round(time.time() - t0, 2)}
        print(f"[SDR] Captured {got} samples @ {req['freq_mhz']:.1f} MHz -> {base} "
              f"({overflows} overflows, {info['elapsed_s']}s)")
        with self._lock:
            self._state["captures"] += 1
        warn = ""
        if short:
            warn = (f"short capture: {got}/{num} samples ({100 * got / max(num, 1):.0f}%)"
                    + (f", {overflows} overflow(s)" if overflows else ""))
            self._error("SDR", warn)
        elif overflows:
            warn = f"{overflows} overflow(s) during last capture"
        self._set_state(state="ready", last_capture=info, last_error=warn)
        self._broadcast({"type": "sdr_capture", "capture": info})
