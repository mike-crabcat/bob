#!/usr/bin/env python3
"""
BOM Weather Fetcher
Fetches current Perth/Mount Lawley weather from BOM FTP XML and formats a compact
WhatsApp-friendly summary.

Standard library only. No requests, no nonsense.
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

BOM_FTP_BASE = "ftp://ftp.bom.gov.au/anon/gen/fwo/"
BOM_OBSERVATIONS_FILE = "IDW60920.xml"  # WA observations
BOM_FORECAST_FILE = "IDW12300.xml"      # Perth metropolitan forecast + Perth precis

# Hourly rain-probability source. BOM publishes NO 3-hourly product on the
# public FTP (verified 2026-10-08: IDW12300/IDW13120 Perth areas are
# day-granularity only; the website's 3-hourly view rides a keyed API), so
# the window layer comes from Open-Meteo's hourly precipitation_probability
# — no key, no deps, labelled by source so replies can attribute honestly.
OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lon}"
    "&hourly=precipitation_probability&forecast_days=2&timezone=Australia%2FPerth"
)

# Default commuter windows (label, start hour, end hour inclusive). The
# rain-jacket routine's spans (8:15–9:30, 17:00–18:00) sit inside these
# hour buckets. Override with --window label=HH-HH (24h, whole hours).
DEFAULT_WINDOWS = (
    ("morning", 8, 9),
    ("afternoon", 17, 18),
)

LOCATIONS = {
    "mount_lawley": {
        "station_id": "009225",        # Perth Metro observation station
        "forecast_area": "WA_PT053",   # Perth location forecast in IDW12300.xml
        "detail_area": "WA_ME001",     # Perth metropolitan text forecast
        "display": "Mt Lawley / Perth",
        "lat": -31.9523,
        "lon": 115.8613,
    },
    "perth": {
        "station_id": "009225",
        "forecast_area": "WA_PT053",
        "detail_area": "WA_ME001",
        "display": "Perth",
        "lat": -31.9523,
        "lon": 115.8613,
    },
    "perth_metro": {
        "station_id": "009225",
        "forecast_area": "WA_PT053",
        "detail_area": "WA_ME001",
        "display": "Perth Metro",
        "lat": -31.9523,
        "lon": 115.8613,
    },
}

ALIASES = {
    "mt lawley": "mount_lawley",
    "mount lawley": "mount_lawley",
    "perth metro": "perth_metro",
    "perth": "perth",
}


def normalise_location(location: str) -> str:
    key = (location or "mount_lawley").strip().lower().replace("-", "_")
    key = ALIASES.get(key, key.replace(" ", "_"))
    return key if key in LOCATIONS else "mount_lawley"


def fetch_xml_data(filename: str, timeout: int = 15) -> Optional[ET.Element]:
    url = f"{BOM_FTP_BASE}{filename}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return ET.fromstring(response.read())
    except (urllib.error.URLError, TimeoutError, ET.ParseError, OSError) as e:
        print(f"Error fetching/parsing BOM {filename}: {e}", file=sys.stderr)
        return None


def element_value(parent: ET.Element, element_type: str) -> Optional[str]:
    for elem in parent.findall("element"):
        if elem.get("type") == element_type:
            return elem.text
    return None


def text_value(parent: ET.Element, text_type: str) -> Optional[str]:
    for elem in parent.findall("text"):
        if elem.get("type") == text_type:
            return elem.text
    return None


def as_float(value: Optional[str]) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def as_int(value: Optional[str]) -> Optional[int]:
    number = as_float(value)
    return int(number) if number is not None else None


def infer_current_condition(humidity: Optional[int], wind_speed: Optional[float], dew_point: Optional[float], temp: Optional[float]) -> str:
    # Observation feed has measurements, not a true descriptive condition. Be honest-ish.
    if wind_speed and wind_speed >= 35:
        return "Windy"
    if humidity is not None and temp is not None and dew_point is not None and humidity >= 80 and (temp - dew_point) <= 2:
        return "Humid / possible fog"
    if humidity is not None and humidity >= 75:
        return "Cloudy-ish"
    if humidity is not None and humidity <= 30:
        return "Dry"
    return "Partly cloudy-ish"


def fetch_current_conditions(location: str = "mount_lawley") -> Optional[Dict[str, Any]]:
    key = normalise_location(location)
    cfg = LOCATIONS[key]
    root = fetch_xml_data(BOM_OBSERVATIONS_FILE)
    if root is None:
        return None

    station = None
    for stn in root.findall(".//station"):
        if stn.get("bom-id") == cfg["station_id"]:
            station = stn
            break
    if station is None:
        print(f"Station {cfg['station_id']} not found", file=sys.stderr)
        return None

    period = station.find(".//period[@index='0']")
    level = period.find(".//level[@type='surface']") if period is not None else None
    if level is None:
        return None

    temp = as_float(element_value(level, "air_temperature"))
    apparent = as_float(element_value(level, "apparent_temp"))
    dew_point = as_float(element_value(level, "dew_point"))
    humidity = as_int(element_value(level, "rel-humidity"))
    wind_speed = as_float(element_value(level, "wind_spd_kmh")) or 0.0
    wind_dir = element_value(level, "wind_dir") or ""
    pressure = as_float(element_value(level, "pres"))

    return {
        "location_name": cfg["display"],
        "station_name": station.get("stn-name", "Perth Metro"),
        "temperature": temp,
        "apparent_temp": apparent,
        "dew_point": dew_point,
        "humidity": humidity,
        "wind_speed": wind_speed,
        "wind_direction": wind_dir,
        "pressure": pressure,
        "condition": infer_current_condition(humidity, wind_speed, dew_point, temp),
        "timestamp": period.get("time-local") if period is not None else None,
    }


def parse_rain_range(text: Optional[str]) -> Dict[str, Optional[float]]:
    if not text:
        return {"rain_amount_min": None, "rain_amount_max": None}
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", text)]
    if not nums:
        return {"rain_amount_min": None, "rain_amount_max": None}
    if len(nums) == 1:
        return {"rain_amount_min": nums[0], "rain_amount_max": nums[0]}
    return {"rain_amount_min": nums[0], "rain_amount_max": nums[1]}


def parse_pop(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else None


def short_date(start_time: Optional[str], index: int) -> str:
    if index == 0:
        return "Today"
    if index == 1:
        return "Tomorrow"
    if start_time:
        try:
            return datetime.fromisoformat(start_time).strftime("%a")
        except ValueError:
            pass
    return f"Day {index + 1}"


def fetch_forecast(location: str = "mount_lawley", days: int = 3) -> Optional[Dict[str, Any]]:
    key = normalise_location(location)
    cfg = LOCATIONS[key]
    root = fetch_xml_data(BOM_FORECAST_FILE)
    if root is None:
        return None

    location_area = None
    detail_area = None
    for area in root.findall(".//area"):
        if area.get("aac") == cfg["forecast_area"]:
            location_area = area
        elif area.get("aac") == cfg["detail_area"]:
            detail_area = area

    if location_area is None:
        print(f"Forecast area {cfg['forecast_area']} not found", file=sys.stderr)
        return None

    detailed_by_index = {}
    if detail_area is not None:
        for fp in detail_area.findall("forecast-period"):
            idx = fp.get("index")
            detailed_by_index[idx] = {
                "forecast_text": text_value(fp, "forecast"),
                "uv_alert": text_value(fp, "uv_alert"),
                "fire_danger": text_value(fp, "fire_danger"),
            }

    forecasts = []
    for fp in location_area.findall("forecast-period")[: max(1, days)]:
        idx_str = fp.get("index", "0")
        idx = int(idx_str) if idx_str.isdigit() else len(forecasts)
        rain_range = parse_rain_range(element_value(fp, "precipitation_range"))
        detail = detailed_by_index.get(idx_str, {})
        forecasts.append({
            "date": fp.get("start-time-local"),
            "label": short_date(fp.get("start-time-local"), idx),
            "min_temp": as_int(element_value(fp, "air_temperature_minimum")),
            "max_temp": as_int(element_value(fp, "air_temperature_maximum")),
            "condition": (text_value(fp, "precis") or "").strip(),
            "rain_chance": parse_pop(text_value(fp, "probability_of_precipitation")),
            **rain_range,
            "forecast_text": detail.get("forecast_text"),
            "uv_alert": detail.get("uv_alert"),
            "fire_danger": detail.get("fire_danger"),
        })

    return {
        "location_name": cfg["display"],
        "source_file": BOM_FORECAST_FILE,
        "forecasts": forecasts,
    }


def fetch_hourly_rain_probability(
    location: str = "mount_lawley",
    windows=DEFAULT_WINDOWS,
) -> Optional[Dict[str, Any]]:
    """Hourly rain probability + per-window maxima (the 2026-10-08 fix).

    The rain-jacket routine demands per-commute-window probabilities, which
    no public BOM product provides; without this layer models fabricated
    plausible-looking numbers and then "corrected" themselves into the
    wrong day. Real hourly data, source-labelled so replies attribute
    honestly. Failure returns None — callers degrade to day-level BOM
    chance and say so, never invent window figures."""
    key = normalise_location(location)
    cfg = LOCATIONS[key]
    url = OPEN_METEO_URL.format(lat=cfg["lat"], lon=cfg["lon"])
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "bob-bom-weather/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
    except Exception as exc:  # noqa: BLE001 - degrade, don't crash the skill
        print(f"hourly probability fetch failed: {exc}", file=sys.stderr)
        return None

    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    pops = hourly.get("precipitation_probability") or []
    if not times or len(times) != len(pops):
        return None

    hours = [
        {"time": t, "pop": p}
        for t, p in zip(times, pops)
        if p is not None
    ]

    today = datetime.now().strftime("%Y-%m-%d")
    window_summary = []
    for label, start_h, end_h in windows:
        todays = [
            h["pop"] for h in hours
            if h["time"].startswith(today)
            and start_h <= int(h["time"][11:13]) <= end_h
        ]
        if todays:
            window_summary.append({
                "label": label,
                "date": today,
                "hours": f"{start_h:02d}:00-{end_h:02d}:59",
                "max_pop": max(todays),
                "hourly_pop": todays,
            })

    return {
        "source": "Open-Meteo hourly precipitation_probability",
        "note": "BOM publishes no 3-hourly product on the public FTP; "
                "window figures are Open-Meteo, not BOM",
        "hours": hours,
        "windows": window_summary,
    }


def get_weather_summary(location: str = "mount_lawley", days: int = 3) -> Dict[str, Any]:
    key = normalise_location(location)
    return {
        "location": key,
        "current": fetch_current_conditions(key),
        "forecast": fetch_forecast(key, days=days),
        "rain_windows": fetch_hourly_rain_probability(key),
        "timestamp": datetime.now().isoformat(),
        "source": "BOM FTP",
    }


def _demo_text(weather: Dict[str, Any]) -> str:
    # Local import keeps bom_fetcher usable even if formatter is edited separately.
    from weather_formatter import format_weather_for_whatsapp
    return format_weather_for_whatsapp(weather)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch BOM weather for Perth/Mount Lawley")
    parser.add_argument("--location", default="mount_lawley", help="mount_lawley, perth, perth_metro")
    parser.add_argument("--days", type=int, default=3, help="forecast days to include")
    parser.add_argument("--format", choices=["whatsapp", "json", "debug"], default="whatsapp")
    args = parser.parse_args()

    weather = get_weather_summary(args.location, days=args.days)
    if args.format == "json":
        print(json.dumps(weather, indent=2, ensure_ascii=False))
    elif args.format == "debug":
        print(json.dumps(weather, indent=2, ensure_ascii=False))
        print("\n--- WhatsApp ---")
        print(_demo_text(weather))
    else:
        print(_demo_text(weather))
    return 0 if weather.get("current") or weather.get("forecast") else 1


if __name__ == "__main__":
    raise SystemExit(main())
