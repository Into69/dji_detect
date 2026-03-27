# DJI Drone Detector

A web-based DJI drone detection and tracking application. Receives drone telemetry from an [ANTsdr](https://www.crowdsupply.com/microphase/antsdr-e200) device, displays live positions on an interactive map, and sends alerts to Discord and/or a TAK server.

![Version](https://img.shields.io/badge/version-0.1.4-blue)

## Features

- **Live map tracking** — drone, pilot, and home/takeoff positions on a Leaflet map
- **Multiple map styles** — OpenStreetMap, CartoDB Dark, Google Hybrid, and more
- **Offline tile caching** — server-side tile proxy caches tiles for offline/low-bandwidth use
- **Detection history** — persists last-known data for every drone seen; browse in History tab
- **Discord alerts** — webhook embed per drone, rate-limited to one alert per 30 seconds
- **TAK/CoT output** — sends Cursor-on-Target XML events for drone and sensor position
- **Sensor location** — manual lat/lon or live GPS via gpsd
- **Configurable UI** — sensor name, icon, map style, auto-fit, auto-track, flight path lines

## Requirements

- Python 3.9+
- ANTsdr device running DJI drone detection firmware (TCP output mode)
- Optional: gpsd for live sensor GPS position

```
pip install -r requirements.txt
```

## Quick Start

1. Run the app:
   ```
   python dji-detect.py
   ```

2. Open `http://<host>:5000` in a browser.

The ANTsdr device should be configured to connect to this machine on the port specified in `dd-config.json` (default `52002`).

## Configuration

All settings are saved to `dd-config.json` and can also be changed live from the **Settings** panel in the web UI.

| Field | Default | Description |
|---|---|---|
| `antsdr_host` | `0.0.0.0` | Interface to listen on for ANTsdr TCP connection |
| `antsdr_port` | `52002` | TCP port the ANTsdr device connects to |
| `connection_type` | `tcp` | `tcp` or `serial` |
| `serial_port` | `/dev/ttyUSB0` | Serial device path (serial mode only) |
| `serial_baud` | `115200` | Serial baud rate |
| `map_style` | `osm` | Map tile style |
| `auto_fit` | `true` | Auto-fit map to show all markers |
| `auto_track` | `true` | Auto-centre on new drone positions |
| `show_lines` | `true` | Show flight path lines |
| `sensor_name` | `Sensor` | Display name shown in header and page title |
| `sensor_icon` | `📡` | Icon shown next to sensor name |
| `sensor_location_source` | `manual` | `manual` or `gpsd` |
| `sensor_lat` / `sensor_lon` | `0.0` | Sensor position (manual mode) |
| `discord_webhook` | *(empty)* | Discord webhook URL for alerts |
| `tak_enabled` | `false` | Enable TAK/CoT output |
| `tak_protocol` | `udp` | `udp` or `tcp` |
| `tak_host` | *(empty)* | TAK server hostname or IP |
| `tak_port` | `8087` | TAK server port |

## Project Structure

```
dji-detect.py       # Main application (WSGI, ANTsdr listener, TAK, Discord)
web/index.html      # Single-file frontend (Leaflet, SSE, settings UI)
requirements.txt    # Python dependencies
dd-config.example.json  # Config template (copy to dd-config.json)
maps/               # Tile cache (auto-created, gitignored)
history.txt         # Drone detection history (auto-created, gitignored)
```

## License

MIT
