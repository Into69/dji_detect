"""
DJI Drone Detection Web App
Receives drone data from ANTsdr via TCP, displays live positions on a map.
"""

VERSION = "0.1.4"

import argparse
import collections
import json
import math
import queue
import re
try:
    import serial as _serial
    _SERIAL_OK = True
except ImportError:
    _SERIAL_OK = False
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import logging

from waitress import serve

# Suppress noisy Waitress socket errors from abrupt SSE client disconnects
logging.getLogger("waitress").setLevel(logging.CRITICAL)

# --- Shared state ---
drone_data: dict = {}       # serial_number -> latest drone dict
sensor_position: dict = {}  # lat, lon, alt, mode — current gpsd fix
antsdr_config: dict = {
    "host":            "0.0.0.0",
    "port":            52002,
    "connection_type": "tcp",       # "tcp" or "serial"
    "serial_port":     "/dev/ttyUSB0",
    "serial_baud":     115200,
    "map_style":       "osm",
    "auto_fit":        True,
    "auto_track":      False,
    "show_lines":      True,
    "sensor_icon":             "📡",
    "sensor_name":             "Sensor",
    "sensor_location_source":  "gpsd",   # "gpsd" or "manual"
    "sensor_lat":              0.0,
    "sensor_lon":              0.0,
    # Discord
    "discord_webhook": "",
    # TAK
    "tak_enabled":     False,
    "tak_protocol":    "udp",       # "udp" or "tcp"
    "tak_host":        "",
    "tak_port":        8087,
}
antsdr_reconnect = threading.Event()  # set to force receiver to reconnect
antsdr_connected: bool = False        # True while TCP connection to ANTsdr is live
raw_lines: collections.deque = collections.deque(maxlen=200)  # recent raw lines (TCP or serial)
drone_history: dict = {}              # serial_number -> last known drone dict
data_lock = threading.Lock()
sse_queues: list = []
_io_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="io")  # reusable pool for discord/TAK/history
_history_last_saved: float = 0.0     # wall time of last history file write
sse_lock = threading.Lock()

STALE_TIMEOUT = 30          # seconds before a drone is considered gone
GPSD_POLL_INTERVAL = 10     # seconds between gpsd broadcasts
TEMPLATE_DIR  = Path(__file__).parent / "web"
CONFIG_FILE   = Path(__file__).parent / "dd-config.json"
HISTORY_FILE  = Path(__file__).parent / "history.txt"
MAPS_DIR      = Path(__file__).parent / "maps"

# Upstream tile URL templates — backend uses {z}/{x}/{y} regardless of provider quirks
TILE_UPSTREAM = {
    "osm":            "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    "carto-dark":     "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
    "carto-light":    "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
    "esri-satellite": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    "esri-topo":      "https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
    "otm":            "https://tile.opentopomap.org/{z}/{x}/{y}.png",
    "google-hybrid":  "https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}",
}


def load_config():
    """Load persisted settings from dd-config.json (if it exists)."""
    if not CONFIG_FILE.exists():
        return
    try:
        saved = json.loads(CONFIG_FILE.read_text())
        mapping = {
            "antsdr_host":      ("host",             str),
            "antsdr_port":      ("port",             int),
            "connection_type":  ("connection_type",  str),
            "serial_port":      ("serial_port",      str),
            "serial_baud":      ("serial_baud",      int),
            "map_style":        ("map_style",         str),
            "auto_fit":         ("auto_fit",          bool),
            "auto_track":       ("auto_track",        bool),
            "show_lines":       ("show_lines",        bool),
            "sensor_icon":             ("sensor_icon",            str),
            "sensor_name":             ("sensor_name",            str),
            "sensor_location_source":  ("sensor_location_source", str),
            "sensor_lat":              ("sensor_lat",              float),
            "sensor_lon":              ("sensor_lon",              float),
            "discord_webhook":  ("discord_webhook",   str),
            "tak_enabled":      ("tak_enabled",       bool),
            "tak_protocol":     ("tak_protocol",      str),
            "tak_host":         ("tak_host",          str),
            "tak_port":         ("tak_port",          int),
        }
        for json_key, (cfg_key, cast) in mapping.items():
            if json_key in saved:
                antsdr_config[cfg_key] = cast(saved[json_key])
        print(f"[Config] Loaded from {CONFIG_FILE}")
    except Exception as e:
        print(f"[Config] Failed to load {CONFIG_FILE}: {e}")


def save_config():
    """Persist current settings to dd-config.json. Raises on failure."""
    CONFIG_FILE.write_text(json.dumps({
        "antsdr_host":     antsdr_config["host"],
        "antsdr_port":     antsdr_config["port"],
        "connection_type": antsdr_config["connection_type"],
        "serial_port":     antsdr_config["serial_port"],
        "serial_baud":     antsdr_config["serial_baud"],
        "map_style":       antsdr_config["map_style"],
        "auto_fit":        antsdr_config["auto_fit"],
        "auto_track":      antsdr_config["auto_track"],
        "show_lines":      antsdr_config["show_lines"],
        "sensor_icon":            antsdr_config["sensor_icon"],
        "sensor_name":            antsdr_config["sensor_name"],
        "sensor_location_source": antsdr_config["sensor_location_source"],
        "sensor_lat":             antsdr_config["sensor_lat"],
        "sensor_lon":             antsdr_config["sensor_lon"],
        "discord_webhook": antsdr_config["discord_webhook"],
        "tak_enabled":     antsdr_config["tak_enabled"],
        "tak_protocol":    antsdr_config["tak_protocol"],
        "tak_host":        antsdr_config["tak_host"],
        "tak_port":        antsdr_config["tak_port"],
    }, indent=2))


def load_history():
    """Load drone history from history.txt into drone_history."""
    global drone_history
    if not HISTORY_FILE.exists():
        return
    try:
        data = json.loads(HISTORY_FILE.read_text())
        if isinstance(data, dict):
            drone_history = data
        print(f"[History] Loaded {len(drone_history)} entries from {HISTORY_FILE}")
    except Exception as e:
        print(f"[History] Failed to load {HISTORY_FILE}: {e}")


def save_history():
    """Persist drone_history to history.txt."""
    try:
        HISTORY_FILE.write_text(json.dumps(drone_history, indent=2))
    except Exception as e:
        print(f"[History] Failed to save {HISTORY_FILE}: {e}")


# --- Broadcast helpers ---

def broadcast(payload: dict):
    msg = json.dumps(payload)
    with sse_lock:
        dead = []
        for q in sse_queues:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            sse_queues.remove(q)


# --- ANTsdr TCP receiver ---

def _parse_antsdr_line(line: str) -> dict | None:
    """
    Parse one ANTsdr CSV line.

    Format:
      dji_O,protocol,freq,rssi,model(code),serial,
      drone_lon,drone_lat,pilot_lon,pilot_lat,home_lon,home_lat,
      geodetic_alt|height_agl,spd_e_cms|spd_n_cms|spd_u_cms;
    """
    line = line.strip().rstrip(";").rstrip(",").rstrip(";")
    if not line.startswith("dji_O"):
        return None

    fields = line.split(",")
    if len(fields) < 14:
        return None

    def flt(s: str) -> float:
        # normalize Unicode minus sign to ASCII hyphen
        return float(s.replace("\u2212", "-").strip())

    def safe_latlon(lat: float, lon: float) -> tuple:
        if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
            return lat, lon
        return 0.0, 0.0

    try:
        protocol  = fields[1].strip()
        freq      = flt(fields[2])
        rssi      = int(flt(fields[3]))
        field4    = fields[4].strip()
        field5    = fields[5].strip()

        drone_lon = flt(fields[6])
        drone_lat = flt(fields[7])
        pilot_lon = flt(fields[8])
        pilot_lat = flt(fields[9])
        home_lon  = flt(fields[10])
        home_lat  = flt(fields[11])

        alt_parts    = fields[12].split("|")
        # firmware sends altitude in decimetres — multiply by 10 for metres
        geodetic_alt = flt(alt_parts[0]) * 10.0
        height_agl   = flt(alt_parts[1]) if len(alt_parts) > 1 else 0.0

        spd_parts        = fields[13].split("|")
        spd_e            = flt(spd_parts[0]) / 100.0
        spd_n            = flt(spd_parts[1]) / 100.0 if len(spd_parts) > 1 else 0.0
        spd_u            = flt(spd_parts[2]) / 100.0 if len(spd_parts) > 2 else 0.0
        horizontal_speed = math.sqrt(spd_e ** 2 + spd_n ** 2)

        # O4 encrypted drone: serial is derived from hash in field4 parentheses
        if protocol == "4":
            m = re.match(r'^(.+?)\((.+)\)$', field4)
            inner = m.group(2) if m else ""
            serial = f"drone-alert-{inner}" if inner else "drone-alert"
            model  = "DJI Encrypted (O4)"
        else:
            # O2/O3: field4 = "Model(code)", field5 = serial
            model  = field4[:field4.rfind("(")].strip() if "(" in field4 else field4
            serial = field5 if len(field5) >= 5 else "unknown"

        pilot_lat, pilot_lon = safe_latlon(pilot_lat, pilot_lon)
        home_lat,  home_lon  = safe_latlon(home_lat,  home_lon)

        return {
            "serial_number":      serial,
            "device_type":        model,
            "freq":               freq,
            "rssi":               rssi,
            "drone_lon":          drone_lon,
            "drone_lat":          drone_lat,
            "pilot_lat":          pilot_lat,
            "pilot_lon":          pilot_lon,
            "home_lat":           home_lat,
            "home_lon":           home_lon,
            "geodetic_altitude":  geodetic_alt,
            "height_agl":         height_agl,
            "horizontal_speed":   horizontal_speed,
            "vertical_speed":     spd_u,
            "last_seen":          time.time(),
        }
    except (ValueError, IndexError) as e:
        print(f"[ANTsdr] Parse error: {e} — line: {line!r}")
        return None


def _set_antsdr_connected(state: bool):
    global antsdr_connected
    antsdr_connected = state
    broadcast({"type": "antsdr_status", "connected": state})


def _map_url(drone: dict) -> str:
    """Build a Google Maps URL with pins for drone, pilot, and home positions."""
    dlat = drone.get("drone_lat", 0)
    dlon = drone.get("drone_lon", 0)
    plat = drone.get("pilot_lat", 0)
    plon = drone.get("pilot_lon", 0)
    hlat = drone.get("home_lat",  0)
    hlon = drone.get("home_lon",  0)

    waypoints = []
    if plat or plon:
        waypoints.append(f"{plat:.6f},{plon:.6f}")
    waypoints.append(f"{dlat:.6f},{dlon:.6f}")
    if hlat or hlon:
        waypoints.append(f"{hlat:.6f},{hlon:.6f}")

    if len(waypoints) > 1:
        return "https://www.google.com/maps/dir/" + "/".join(waypoints)
    return f"https://maps.google.com/?q={dlat:.6f},{dlon:.6f}"


def _send_discord_alert(drone: dict):
    """POST a detection embed to the configured Discord webhook (non-blocking)."""
    webhook = antsdr_config.get("discord_webhook", "").strip()
    if not webhook:
        return
    try:
        import urllib.request
        import urllib.error

        dlat  = drone.get("drone_lat", 0)
        dlon  = drone.get("drone_lon", 0)
        plat  = drone.get("pilot_lat", 0)
        plon  = drone.get("pilot_lon", 0)
        hlat  = drone.get("home_lat",  0)
        hlon  = drone.get("home_lon",  0)

        fields = [
            {"name": "Serial",   "value": drone.get("serial_number", "—"), "inline": True},
            {"name": "Model",    "value": drone.get("device_type",   "—"), "inline": True},
            {"name": "\u200b",   "value": "\u200b",                         "inline": True},
            {"name": "Drone",    "value": f"{dlat:.6f}, {dlon:.6f}",        "inline": True},
        ]
        if plat or plon:
            fields.append({"name": "Pilot", "value": f"{plat:.6f}, {plon:.6f}", "inline": True})
        if hlat or hlon:
            fields.append({"name": "Home",  "value": f"{hlat:.6f}, {hlon:.6f}", "inline": True})
        fields += [
            {"name": "Altitude",
             "value": f"{drone.get('geodetic_altitude', 0):.1f} m MSL"
                      f" / {drone.get('height_agl', 0):.1f} m AGL",
             "inline": True},
            {"name": "Speed",
             "value": f"{drone.get('horizontal_speed', 0):.1f} m/s H"
                      f"  {drone.get('vertical_speed', 0):.1f} m/s V",
             "inline": True},
            {"name": "RSSI / Freq",
             "value": f"{drone.get('rssi', 0)} dBm"
                      f" / {drone.get('freq', 0):.1f} MHz",
             "inline": True},
        ]

        embed = {
            "title":       f"\U0001f6f8 Drone Detected: {drone.get('device_type', 'Unknown')}",
            "description": f"[View on map \U0001f5fa]({_map_url(drone)})",
            "color":       0xf0883e,
            "fields":      fields,
            "footer":      {"text": "DJI Drone Detection"},
            "timestamp":   time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

        payload = json.dumps({"embeds": [embed]}).encode()
        req = urllib.request.Request(
            webhook,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "DJIDetect/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5):
                pass  # 204 No Content on success
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            print(f"[Discord] HTTP {e.code}: {body}")
    except Exception as e:
        print(f"[Discord] Alert failed: {e}")


DISCORD_ALERT_INTERVAL = 30   # seconds between repeated Discord alerts per drone
_discord_last_sent: dict = {} # serial -> last alert wall time


# --- TAK CoT sender ---

def _cot_time(t: float, offset: float = 0.0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t + offset))


def _build_cot_event(uid: str, cot_type: str, lat: float, lon: float, hae: float,
                     callsign: str, remarks: str, stale_secs: int = 60) -> bytes:
    now = time.time()
    xml = (
        "<?xml version='1.0' standalone='yes'?>"
        f"<event version='2.0' uid='{uid}' type='{cot_type}'"
        f" time='{_cot_time(now)}' start='{_cot_time(now)}'"
        f" stale='{_cot_time(now, stale_secs)}' how='m-g'>"
        f"<point lat='{lat:.7f}' lon='{lon:.7f}' hae='{hae:.1f}'"
        f" ce='9999999.0' le='9999999.0'/>"
        f"<detail>"
        f"<contact callsign='{callsign}'/>"
        f"<remarks>{remarks}</remarks>"
        f"</detail>"
        f"</event>"
    )
    return xml.encode()


def _send_tak(drone: dict):
    """Send CoT events for drone, pilot, and home to the TAK server."""
    if not antsdr_config.get("tak_enabled"):
        return
    host  = antsdr_config.get("tak_host", "").strip()
    port  = int(antsdr_config.get("tak_port", 8087))
    proto = antsdr_config.get("tak_protocol", "udp")
    if not host:
        return

    try:
        serial = drone.get("serial_number", "unknown")
        model  = drone.get("device_type", "Unknown")
        dlat   = drone.get("drone_lat", 0)
        dlon   = drone.get("drone_lon", 0)
        plat   = drone.get("pilot_lat", 0)
        plon   = drone.get("pilot_lon", 0)
        hlat   = drone.get("home_lat",  0)
        hlon   = drone.get("home_lon",  0)
        alt    = drone.get("geodetic_altitude", 0)
        tag    = serial[-8:] if len(serial) >= 8 else serial

        remarks = (
            f"Model:{model} Speed:{drone.get('horizontal_speed', 0):.1f}m/s "
            f"Alt:{alt:.0f}m AGL:{drone.get('height_agl', 0):.0f}m "
            f"RSSI:{drone.get('rssi', 0)}dBm Freq:{drone.get('freq', 0):.1f}MHz"
        )

        events = [
            _build_cot_event(
                uid=f"DJI-{serial}", cot_type="a-u-A-M-F-Q",
                lat=dlat, lon=dlon, hae=alt,
                callsign=f"DJI-{tag}", remarks=remarks,
            ),
        ]
        if plat or plon:
            events.append(_build_cot_event(
                uid=f"DJI-{serial}-pilot", cot_type="a-u-G-U-C-I",
                lat=plat, lon=plon, hae=0,
                callsign=f"Pilot-{tag}",
                remarks=f"Pilot of {model} ({serial})",
            ))
        if hlat or hlon:
            events.append(_build_cot_event(
                uid=f"DJI-{serial}-home", cot_type="a-n-G",
                lat=hlat, lon=hlon, hae=0,
                callsign=f"Home-{tag}",
                remarks=f"Home point of {model} ({serial})",
            ))

        if proto == "udp":
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                for ev in events:
                    sock.sendto(ev, (host, port))
            finally:
                sock.close()
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            try:
                sock.connect((host, port))
                for ev in events:
                    sock.sendall(ev)
            finally:
                sock.close()

    except Exception as e:
        print(f"[TAK] Send failed: {e}")


TAK_SENSOR_INTERVAL = 30  # seconds between sensor position beacons to TAK

def _send_tak_sensor():
    """Send the sensor's current GPS position as a CoT event to the TAK server."""
    if not antsdr_config.get("tak_enabled"):
        return
    host  = antsdr_config.get("tak_host", "").strip()
    port  = int(antsdr_config.get("tak_port", 8087))
    proto = antsdr_config.get("tak_protocol", "udp")
    name  = antsdr_config.get("sensor_name", "Sensor").strip() or "Sensor"
    if not host:
        return
    with data_lock:
        pos = sensor_position.copy()
    if not pos.get("lat") or not pos.get("lon"):
        return
    try:
        event = _build_cot_event(
            uid=f"DJI-Detect-Sensor-{name.replace(' ', '_')}",
            cot_type="a-f-G-U-C",
            lat=pos["lat"], lon=pos["lon"],
            hae=pos.get("alt", 0),
            callsign=name,
            remarks=f"DJI Detect sensor — {name}",
            stale_secs=90,
        )
        if proto == "udp":
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.sendto(event, (host, port))
            finally:
                sock.close()
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            try:
                sock.connect((host, port))
                sock.sendall(event)
            finally:
                sock.close()
    except Exception as e:
        print(f"[TAK] Sensor beacon failed: {e}")


def _tak_sensor_beacon():
    """Background thread: send sensor position to TAK every TAK_SENSOR_INTERVAL seconds."""
    while True:
        time.sleep(TAK_SENSOR_INTERVAL)
        _send_tak_sensor()


def _manual_location_broadcaster():
    """Re-broadcast manual sensor position every 30s so auto-fit/auto-track stay active."""
    while True:
        time.sleep(30)
        if antsdr_config.get("sensor_location_source") != "manual":
            continue
        with data_lock:
            if sensor_position.get("lat") and sensor_position.get("lon"):
                sensor_position["last_fix_wall_time"] = time.time()
                pos = sensor_position.copy()
            else:
                continue
        broadcast({"type": "sensor", "position": pos})


def _process_line(line: str):
    """Record raw line, update drone_data, and fire Discord/TAK notifications."""
    global _history_last_saved
    now = time.time()
    with data_lock:
        raw_lines.append({"t": now, "line": line.rstrip()})
    data = _parse_antsdr_line(line)
    if not data or not data.get("serial_number"):
        return
    sn = data["serial_number"]

    send_discord = False
    with data_lock:
        drone_data[sn] = data
        drone_history[sn] = data
        # Check discord throttle under lock to avoid race condition
        if now - _discord_last_sent.get(sn, 0) >= DISCORD_ALERT_INTERVAL:
            _discord_last_sent[sn] = now
            send_discord = True

    # Throttle history saves to at most once every 5 seconds
    if now - _history_last_saved >= 5.0:
        _history_last_saved = now
        _io_executor.submit(save_history)

    snapshot = data.copy()

    if send_discord:
        _io_executor.submit(_send_discord_alert, snapshot)

    # TAK — every detection message
    _io_executor.submit(_send_tak, snapshot)

    broadcast({"type": "update", "drone": data})


def _tcp_server_loop():
    """Bind TCP server and accept one ANTsdr connection at a time."""
    host = antsdr_config["host"]
    port = antsdr_config["port"]
    print(f"[ANTsdr] TCP listening on {host}:{port}")
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((host, port))
        srv.listen(1)
        srv.settimeout(1)

        while not antsdr_reconnect.is_set():
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue

            print(f"[ANTsdr] Connection from {addr[0]}:{addr[1]}")
            _set_antsdr_connected(True)
            try:
                fh = conn.makefile("r", errors="replace")
                while not antsdr_reconnect.is_set():
                    line = fh.readline()
                    if not line:
                        raise ConnectionError("ANTsdr disconnected")
                    _process_line(line)
            except Exception as e:
                print(f"[ANTsdr] Connection lost: {e} — waiting for reconnect")
            finally:
                _set_antsdr_connected(False)
                try:
                    conn.close()
                except Exception:
                    pass
    finally:
        try:
            srv.close()
        except Exception:
            pass


def _serial_loop():
    """Open serial port and read lines from ANTsdr."""
    if not _SERIAL_OK:
        raise RuntimeError("pyserial not installed — run: pip install pyserial")
    port = antsdr_config["serial_port"]
    baud = antsdr_config["serial_baud"]
    print(f"[ANTsdr] Serial {port} @ {baud}")
    with _serial.Serial(port, baud, timeout=1) as ser:
        _set_antsdr_connected(True)
        while not antsdr_reconnect.is_set():
            raw = ser.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace")
            _process_line(line)


def antsdr_receiver():
    """Dispatch to TCP or serial mode; reconnect on error or reconnect event."""
    while True:
        antsdr_reconnect.clear()
        try:
            if antsdr_config["connection_type"] == "serial":
                _serial_loop()
            else:
                _tcp_server_loop()
        except Exception as e:
            _set_antsdr_connected(False)
            print(f"[ANTsdr] Error: {e} — retrying in 5s")
            if not antsdr_reconnect.is_set():
                time.sleep(5)
        finally:
            _set_antsdr_connected(False)


# --- Stale drone cleaner ---

def stale_cleaner():
    """Periodically remove drones that haven't been seen recently."""
    while True:
        time.sleep(5)
        now = time.time()
        with data_lock:
            stale = [s for s, d in drone_data.items()
                     if now - d.get("last_seen", now) > STALE_TIMEOUT]
            for serial in stale:
                del drone_data[serial]
                _discord_last_sent.pop(serial, None)
                broadcast({"type": "remove", "serial": serial})


# --- gpsd poller ---

def gpsd_poller(host: str, port: int):
    """Stream TPV/SKY messages from gpsd and broadcast every GPSD_POLL_INTERVAL seconds."""
    global sensor_position
    last_fix_wall_time: float = 0.0
    print(f"[GPSD] Connecting to {host}:{port}")
    while True:
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((host, port))
            fh = sock.makefile("r")

            # Consume VERSION welcome, then enable streaming
            fh.readline()
            sock.sendall(b'?WATCH={"enable":true,"json":true};\n')

            latest_tpv: dict = {}
            latest_sats: list = []
            last_broadcast: float = 0.0

            while True:
                line = fh.readline()
                if not line:
                    raise ConnectionError("gpsd stream closed")

                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue

                cls = msg.get("class")

                if cls == "TPV":
                    latest_tpv = msg
                    if msg.get("mode", 0) >= 2:
                        last_fix_wall_time = time.time()

                elif cls == "SKY":
                    latest_sats = msg.get("satellites", [])

                # Broadcast on interval using wall time, not sleep
                now = time.time()
                if now - last_broadcast < GPSD_POLL_INTERVAL:
                    continue
                last_broadcast = now

                mode    = latest_tpv.get("mode", 0)
                has_fix = mode >= 2
                # gpsd 3.20+ renamed alt -> altMSL; support both
                alt_val = latest_tpv.get("alt") or latest_tpv.get("altMSL")
                pos = {
                    "mode":               mode,
                    "lat":                latest_tpv.get("lat") if has_fix else None,
                    "lon":                latest_tpv.get("lon") if has_fix else None,
                    "alt":                alt_val               if mode >= 3 else None,
                    "sats_used":          sum(1 for s in latest_sats if s.get("used")),
                    "sats_visible":       len(latest_sats),
                    "gps_time":           latest_tpv.get("time"),
                    "last_fix_wall_time": last_fix_wall_time or None,
                }

                if antsdr_config.get("sensor_location_source", "gpsd") == "gpsd":
                    with data_lock:
                        sensor_position.update(pos)
                    broadcast({"type": "sensor", "position": pos})

        except Exception as e:
            print(f"[GPSD] Error: {e} — retrying in 10s")
            time.sleep(10)
        finally:
            if sock:
                sock.close()


# --- WSGI handlers ---

def handle_index(start_response):
    name = antsdr_config.get("sensor_name", "Sensor").strip() or "Sensor"
    html = (TEMPLATE_DIR / "index.html").read_text(encoding="utf-8") \
               .replace("<title>Sensor</title>", f"<title>{name} - DJI Drone Detector</title>") \
               .encode("utf-8")
    start_response("200 OK", [
        ("Content-Type", "text/html; charset=utf-8"),
        ("Content-Length", str(len(html))),
    ])
    return [html]


def handle_drones(start_response):
    with data_lock:
        body = json.dumps(list(drone_data.values())).encode()
    start_response("200 OK", [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(body))),
    ])
    return [body]


def handle_sensor(start_response):
    with data_lock:
        body = json.dumps(sensor_position).encode()
    start_response("200 OK", [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(body))),
    ])
    return [body]


def handle_stream(start_response):
    start_response("200 OK", [
        ("Content-Type", "text/event-stream"),
        ("Cache-Control", "no-cache"),
        ("X-Accel-Buffering", "no"),
    ])
    return _sse_generator()


def _sse_generator():
    q: queue.Queue = queue.Queue(maxsize=200)
    with sse_lock:
        sse_queues.append(q)

    # Send current state to new client
    payload = json.dumps({"type": "antsdr_status", "connected": antsdr_connected})
    yield f"data: {payload}\n\n".encode()
    with data_lock:
        if sensor_position:
            payload = json.dumps({"type": "sensor", "position": sensor_position})
            yield f"data: {payload}\n\n".encode()
        for drone in drone_data.values():
            payload = json.dumps({"type": "update", "drone": drone})
            yield f"data: {payload}\n\n".encode()

    try:
        while True:
            try:
                msg = q.get(timeout=20)
                yield f"data: {msg}\n\n".encode()
            except queue.Empty:
                yield b'data: {"type":"heartbeat"}\n\n'
    finally:
        with sse_lock:
            try:
                sse_queues.remove(q)
            except ValueError:
                pass


def handle_config_get(start_response):
    with data_lock:
        body = json.dumps({
            "version":         VERSION,
            "antsdr_host":     antsdr_config["host"],
            "antsdr_port":     antsdr_config["port"],
            "connection_type": antsdr_config["connection_type"],
            "serial_port":     antsdr_config["serial_port"],
            "serial_baud":     antsdr_config["serial_baud"],
            "map_style":       antsdr_config["map_style"],
            "auto_fit":        antsdr_config["auto_fit"],
            "auto_track":      antsdr_config["auto_track"],
            "show_lines":      antsdr_config["show_lines"],
            "sensor_icon":            antsdr_config["sensor_icon"],
            "sensor_name":            antsdr_config["sensor_name"],
            "sensor_location_source": antsdr_config["sensor_location_source"],
            "sensor_lat":             antsdr_config["sensor_lat"],
            "sensor_lon":             antsdr_config["sensor_lon"],
            "discord_webhook": antsdr_config["discord_webhook"],
            "tak_enabled":     antsdr_config["tak_enabled"],
            "tak_protocol":    antsdr_config["tak_protocol"],
            "tak_host":        antsdr_config["tak_host"],
            "tak_port":        antsdr_config["tak_port"],
        }).encode()
    start_response("200 OK", [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(body))),
    ])
    return [body]


def handle_config_post(environ, start_response):
    try:
        length  = int(environ.get("CONTENT_LENGTH", 0))
        body    = json.loads(environ["wsgi.input"].read(length))
        conn_type = str(body.get("connection_type", "tcp")).strip()
        if conn_type not in ("tcp", "serial"):
            raise ValueError("connection_type must be 'tcp' or 'serial'")

        if conn_type == "tcp":
            host = str(body["antsdr_host"]).strip()
            port = int(body["antsdr_port"])
            if not host or not (1 <= port <= 65535):
                raise ValueError("invalid host or port")
            antsdr_config["host"] = host
            antsdr_config["port"] = port
        else:
            sp = str(body.get("serial_port", antsdr_config["serial_port"])).strip()
            sb = int(body.get("serial_baud", antsdr_config["serial_baud"]))
            if not sp:
                raise ValueError("serial_port required")
            antsdr_config["serial_port"] = sp
            antsdr_config["serial_baud"] = sb

        antsdr_config["connection_type"] = conn_type
        antsdr_config["map_style"]       = str(body.get("map_style",       antsdr_config["map_style"]))
        antsdr_config["auto_fit"]        = bool(body.get("auto_fit",       antsdr_config["auto_fit"]))
        antsdr_config["auto_track"]      = bool(body.get("auto_track",     antsdr_config["auto_track"]))
        antsdr_config["show_lines"]      = bool(body.get("show_lines",     antsdr_config["show_lines"]))
        antsdr_config["sensor_icon"]            = str(body.get("sensor_icon",            antsdr_config["sensor_icon"]))
        antsdr_config["sensor_name"]            = str(body.get("sensor_name",            antsdr_config["sensor_name"])).strip()
        antsdr_config["sensor_location_source"] = str(body.get("sensor_location_source", antsdr_config["sensor_location_source"]))
        antsdr_config["sensor_lat"]             = float(body.get("sensor_lat",           antsdr_config["sensor_lat"]))
        antsdr_config["sensor_lon"]             = float(body.get("sensor_lon",           antsdr_config["sensor_lon"]))
        # Apply manual position immediately
        if antsdr_config["sensor_location_source"] == "manual":
            slat = antsdr_config["sensor_lat"]
            slon = antsdr_config["sensor_lon"]
            pos = {"mode": 2, "lat": slat, "lon": slon, "alt": None,
                   "sats_used": 0, "sats_visible": 0, "gps_time": None,
                   "last_fix_wall_time": time.time()}
            with data_lock:
                sensor_position.update(pos)
            broadcast({"type": "sensor", "position": pos})
        antsdr_config["discord_webhook"] = str(body.get("discord_webhook", antsdr_config["discord_webhook"])).strip()
        antsdr_config["tak_enabled"]     = bool(body.get("tak_enabled",    antsdr_config["tak_enabled"]))
        antsdr_config["tak_protocol"]    = str(body.get("tak_protocol",    antsdr_config["tak_protocol"])).strip()
        antsdr_config["tak_host"]        = str(body.get("tak_host",        antsdr_config["tak_host"])).strip()
        antsdr_config["tak_port"]        = int(body.get("tak_port",        antsdr_config["tak_port"]))
        save_config()
        if body.get("reconnect", True):
            antsdr_reconnect.set()

        resp = json.dumps({"ok": True}).encode()
        start_response("200 OK", [("Content-Type", "application/json"), ("Content-Length", str(len(resp)))])
        return [resp]
    except Exception as e:
        resp = json.dumps({"ok": False, "error": str(e)}).encode()
        start_response("400 Bad Request", [("Content-Type", "application/json"), ("Content-Length", str(len(resp)))])
        return [resp]


def handle_serial_ports(start_response):
    ports = []
    error = None
    if not _SERIAL_OK:
        error = "pyserial not installed"
    else:
        try:
            from serial.tools import list_ports
            seen = {}
            for p in list_ports.comports():
                seen[p.device] = {"port": p.device, "desc": p.description or p.device}
            # Fallback: glob common Linux/macOS device paths missed by comports()
            import glob
            for pattern in ("/dev/ttyUSB*", "/dev/ttyACM*", "/dev/ttyS[0-9]*",
                            "/dev/tty.usbserial*", "/dev/tty.usbmodem*"):
                for dev in glob.glob(pattern):
                    if dev not in seen:
                        seen[dev] = {"port": dev, "desc": dev}
            ports = sorted(seen.values(), key=lambda p: p["port"])
        except Exception as e:
            error = str(e)
            print(f"[Serial] Port scan error: {e}")
    body = json.dumps({"ports": ports, "error": error}).encode()
    start_response("200 OK", [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(body))),
    ])
    return [body]


def handle_raw(start_response):
    with data_lock:
        body = json.dumps(list(raw_lines)).encode()
    start_response("200 OK", [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(body))),
    ])
    return [body]


def handle_history(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    if method == "GET":
        with data_lock:
            body = json.dumps(drone_history).encode()
        start_response("200 OK", [("Content-Type", "application/json"),
                                   ("Content-Length", str(len(body)))])
        return [body]
    if method == "POST":
        with data_lock:
            drone_history.clear()
        save_history()
        body = json.dumps({"ok": True}).encode()
        start_response("200 OK", [("Content-Type", "application/json"),
                                   ("Content-Length", str(len(body)))])
        return [body]
    start_response("405 Method Not Allowed", [("Content-Type", "text/plain")])
    return [b"Method Not Allowed"]


def handle_not_found(start_response):
    start_response("404 Not Found", [("Content-Type", "text/plain")])
    return [b"Not Found"]


def handle_tile(style, z, x, y, start_response):
    """Proxy and cache a map tile. Serves from disk if cached, fetches upstream otherwise."""
    import urllib.request
    if style not in TILE_UPSTREAM:
        start_response("404 Not Found", [("Content-Type", "text/plain")])
        return [b"Unknown style"]
    cache_path = MAPS_DIR / style / z / x / y
    if cache_path.exists():
        data = cache_path.read_bytes()
    else:
        url = TILE_UPSTREAM[style].format(z=z, x=x, y=y)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "DJIDetect/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(data)
        except Exception as e:
            start_response("502 Bad Gateway", [("Content-Type", "text/plain")])
            return [str(e).encode()]
    ct = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
    start_response("200 OK", [
        ("Content-Type", ct),
        ("Content-Length", str(len(data))),
        ("Cache-Control", "public, max-age=86400"),
    ])
    return [data]


def _maps_dir_bytes():
    if not MAPS_DIR.exists():
        return 0
    return sum(f.stat().st_size for f in MAPS_DIR.rglob("*") if f.is_file())


def handle_map_cache(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    if method == "GET":
        body = json.dumps({"bytes": _maps_dir_bytes()}).encode()
        start_response("200 OK", [("Content-Type", "application/json"),
                                   ("Content-Length", str(len(body)))])
        return [body]
    if method == "POST":
        import shutil
        if MAPS_DIR.exists():
            shutil.rmtree(MAPS_DIR)
        body = json.dumps({"ok": True}).encode()
        start_response("200 OK", [("Content-Type", "application/json"),
                                   ("Content-Length", str(len(body)))])
        return [body]
    start_response("405 Method Not Allowed", [("Content-Type", "text/plain")])
    return [b"Method Not Allowed"]


# --- WSGI app ---

def application(environ, start_response):
    path = environ.get("PATH_INFO", "/")
    method = environ.get("REQUEST_METHOD", "GET")

    if path == "/config":
        if method == "GET":  return handle_config_get(start_response)
        if method == "POST": return handle_config_post(environ, start_response)

    if path == "/map-cache":
        return handle_map_cache(environ, start_response)

    if path == "/history":
        return handle_history(environ, start_response)

    # Tile proxy: /tiles/{style}/{z}/{x}/{y}
    if path.startswith("/tiles/"):
        parts = path.split("/")  # ['', 'tiles', style, z, x, y]
        if len(parts) == 6:
            _, _, style, z, x, y = parts
            return handle_tile(style, z, x, y, start_response)

    if method != "GET":
        start_response("405 Method Not Allowed", [("Content-Type", "text/plain")])
        return [b"Method Not Allowed"]

    if path == "/":       return handle_index(start_response)
    if path == "/drones": return handle_drones(start_response)
    if path == "/sensor": return handle_sensor(start_response)
    if path == "/stream": return handle_stream(start_response)
    if path == "/raw":          return handle_raw(start_response)
    if path == "/serial-ports": return handle_serial_ports(start_response)

    return handle_not_found(start_response)


# --- Entry point ---

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DJI Drone Detection Map Server")
    parser.add_argument("--antsdr-host", default="192.168.1.10",
                        help="ANTsdr TCP host (default: 192.168.1.10)")
    parser.add_argument("--antsdr-port", type=int, default=52002,
                        help="ANTsdr TCP port (default: 52002)")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Web server bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=5000,
                        help="Web server port (default: 5000)")
    parser.add_argument("--stale-timeout", type=int, default=30,
                        help="Seconds before removing unseen drone (default: 30)")
    parser.add_argument("--gpsd-host", default="127.0.0.1",
                        help="gpsd host (default: 127.0.0.1)")
    parser.add_argument("--gpsd-port", type=int, default=2947,
                        help="gpsd port (default: 2947)")
    args = parser.parse_args()

    STALE_TIMEOUT = args.stale_timeout

    # Load saved config first, then let CLI args override if explicitly provided
    load_config()
    load_history()

    # Apply manual sensor location immediately if configured
    if antsdr_config.get("sensor_location_source") == "manual":
        slat = antsdr_config.get("sensor_lat", 0.0)
        slon = antsdr_config.get("sensor_lon", 0.0)
        if slat and slon:
            sensor_position.update({
                "mode": 2, "lat": slat, "lon": slon, "alt": None,
                "sats_used": 0, "sats_visible": 0, "gps_time": None,
                "last_fix_wall_time": time.time(),
            })
            print(f"[Config] Manual sensor location applied: {slat}, {slon}")

    if args.antsdr_host != parser.get_default("antsdr_host"):
        antsdr_config["host"] = args.antsdr_host
    if args.antsdr_port != parser.get_default("antsdr_port"):
        antsdr_config["port"] = args.antsdr_port

    threading.Thread(
        target=antsdr_receiver,
        daemon=True,
        name="antsdr-receiver",
    ).start()

    threading.Thread(
        target=gpsd_poller,
        kwargs={"host": args.gpsd_host, "port": args.gpsd_port},
        daemon=True,
        name="gpsd-poller",
    ).start()

    threading.Thread(
        target=stale_cleaner,
        daemon=True,
        name="stale-cleaner",
    ).start()

    threading.Thread(
        target=_tak_sensor_beacon,
        daemon=True,
        name="tak-sensor-beacon",
    ).start()

    threading.Thread(
        target=_manual_location_broadcaster,
        daemon=True,
        name="manual-location-broadcaster",
    ).start()

    print(f"Starting server at http://{args.host}:{args.port}")
    serve(application, host=args.host, port=args.port, threads=8)
