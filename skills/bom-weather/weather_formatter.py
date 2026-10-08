#!/usr/bin/env python3
"""
Weather Formatter for WhatsApp.
Formats BOM weather dictionaries into compact, useful phone-sized summaries.
"""

import json
import sys
from datetime import datetime
from typing import Any, Dict, Optional


def fmt_num(value: Any, suffix: str = "") -> str:
    if value is None or value == "":
        return "?"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value}{suffix}"


def get_weather_emoji(condition: Optional[str]) -> str:
    condition = (condition or "").lower()
    if "sunny" in condition or "clear" in condition or "dry" in condition:
        return "☀️"
    if "partly" in condition:
        return "⛅"
    if "cloud" in condition:
        return "☁️"
    if "rain" in condition or "shower" in condition:
        return "🌦️"
    if "storm" in condition or "thunder" in condition:
        return "⛈️"
    if "fog" in condition or "mist" in condition:
        return "🌫️"
    if "wind" in condition:
        return "💨"
    return "🌤️"


def compact_condition(text: Optional[str], max_len: int = 95) -> str:
    if not text:
        return ""
    text = " ".join(text.strip().split())
    if len(text) <= max_len:
        return text
    cut = text[:max_len].rsplit(" ", 1)[0]
    return cut + "…"


def format_rain(day: Dict[str, Any]) -> str:
    pop = day.get("rain_chance")
    mn = day.get("rain_amount_min")
    mx = day.get("rain_amount_max")
    if pop is None:
        return ""
    if mn is not None and mx is not None and (mn or mx):
        return f", rain {pop}% ({fmt_num(mn)}-{fmt_num(mx)}mm)"
    return f", rain {pop}%"


def format_temp_range(day: Dict[str, Any]) -> str:
    mn = day.get("min_temp")
    mx = day.get("max_temp")
    if mn is not None and mx is not None:
        return f"{mn}-{mx}°C"
    if mx is not None:
        return f"max {mx}°C"
    if mn is not None:
        return f"min {mn}°C"
    return "temp ?"


def format_weather_for_whatsapp(weather_data: Dict[str, Any]) -> str:
    if not weather_data:
        return "❌ BOM weather unavailable. Try again later."

    current = weather_data.get("current") or {}
    forecast = (weather_data.get("forecast") or {}).get("forecasts") or []
    location = current.get("location_name") or (weather_data.get("forecast") or {}).get("location_name") or "Perth"
    lines = [f"BOM weather — {location}"]

    if current:
        condition = current.get("condition") or "current conditions"
        emoji = get_weather_emoji(condition)
        temp = fmt_num(current.get("temperature"), "°C")
        feels = current.get("apparent_temp")
        wind_dir = current.get("wind_direction") or ""
        wind_speed = current.get("wind_speed")
        humidity = current.get("humidity")
        now_line = f"{emoji} Now: {temp}, feels {fmt_num(feels, '°C')}, {condition}"
        lines.append(now_line)
        extras = []
        if wind_speed is not None:
            extras.append(f"wind {wind_dir} {fmt_num(wind_speed, 'km/h')}")
        if humidity is not None:
            extras.append(f"humidity {humidity}%")
        if extras:
            lines.append("• " + ", ".join(extras))

    if forecast:
        for day in forecast[:3]:
            label = day.get("label") or "Forecast"
            condition = compact_condition(day.get("condition") or day.get("forecast_text") or "")
            emoji = get_weather_emoji(condition)
            line = f"{emoji} {label}: {format_temp_range(day)}"
            if condition:
                line += f", {condition}"
            line += format_rain(day)
            lines.append(line)

        # Add useful detailed text for today, but do not turn WhatsApp into a wall of weather slurry.
        today_text = compact_condition(forecast[0].get("forecast_text"), max_len=130)
        precis = compact_condition(forecast[0].get("condition"), max_len=130)
        if today_text and today_text != precis:
            lines.append(f"• Today detail: {today_text}")

        uv = forecast[0].get("uv_alert")
        if uv:
            lines.append(f"• UV: {compact_condition(uv, 80)}")

    # Per-window hourly maxima (2026-10-08): real figures for the
    # rain-jacket commute spans, source-labelled — BOM publishes no
    # 3-hourly product on the public FTP, so these come from Open-Meteo.
    rain_windows = weather_data.get("rain_windows") or {}
    for win in rain_windows.get("windows") or []:
        lines.append(
            f"☔ {win['label'].title()} window ({win['hours']}): "
            f"max rain chance {win['max_pop']}% (hourly: "
            + "/".join(f"{p}%" for p in win["hourly_pop"]) + ")"
        )

    if len(lines) == 1:
        lines.append("❌ No current conditions or forecast came back. BOM XML has chosen violence.")

    return "\n".join(lines)


def format_current_only(current_data: Dict[str, Any]) -> str:
    return format_weather_for_whatsapp({"current": current_data, "forecast": None})


def format_forecast_only(forecast_data: Dict[str, Any]) -> str:
    return format_weather_for_whatsapp({"current": None, "forecast": forecast_data})


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        print(f"❌ Could not read weather JSON: {e}")
        return 1
    print(format_weather_for_whatsapp(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
