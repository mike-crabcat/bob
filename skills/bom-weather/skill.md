---
name: bom-weather
description: Fetches live BOM weather data for Mount Lawley/Perth and returns concise WhatsApp-friendly summaries with current conditions plus 3-day forecast.
trigger: when Bob asks for weather, wants a weather update, mentions checking weather conditions, or requests current/forecast weather for Mount Lawley or Perth metro area
---

## Instructions

When this skill activates:

1. **Default location**
   - Use `mount_lawley` unless Mike asks for broader Perth.
   - Supported aliases: `mount_lawley`, `perth`, `perth_metro`.

2. **Fetch and format in one command**
   - Run:
     ```bash
     python skills/bom-weather/bom_fetcher.py --location mount_lawley --days 3 --format whatsapp
     ```
   - This prints a ready-to-send WhatsApp summary.

3. **Debug / raw data if needed**
   - Raw JSON:
     ```bash
     python skills/bom-weather/bom_fetcher.py --location mount_lawley --days 3 --format json
     ```
   - Debug with raw data + WhatsApp output:
     ```bash
     python skills/bom-weather/bom_fetcher.py --location mount_lawley --days 3 --format debug
     ```

4. **Send result**
   - In the current chat, use `send_whatsapp_message` with the command output.
   - Do **not** use `send_whatsapp_to_contact` unless Mike explicitly asks you to send the weather to another contact.

## What the skill returns

- Current BOM observations from Perth Metro station:
  - temperature
  - apparent/feels-like temperature
  - inferred condition
  - wind
  - humidity
- Forecast from BOM Perth metropolitan/location forecast:
  - today/tomorrow/next day min/max where provided
  - precis condition
  - rain probability and rain range where provided
  - useful detail text for today
  - UV alert when present

## Rain windows (2026-10-08)

`rain_windows` in every output: per-commute-window maxima (default morning
08:00-09:59, afternoon 17:00-18:59) plus the hourly series. BOM publishes
NO 3-hourly product on the public FTP (the website's 3-hourly view rides a
keyed API), so window figures come from Open-Meteo hourly
precipitation_probability. Attribute window numbers to Open-Meteo and
day-level chances to BOM. If the hourly fetch fails, `rain_windows` is
null — say so and reason from the BOM day-level chance; never invent
per-window figures.

## Data sources

Official BOM FTP XML:
- Observations: `ftp://ftp.bom.gov.au/anon/gen/fwo/IDW60920.xml`
- Perth forecast: `ftp://ftp.bom.gov.au/anon/gen/fwo/IDW12300.xml`
- Hourly rain probability (window layer): `https://api.open-meteo.com/v1/forecast`

## Notes

- Observation XML does not provide a real text condition, so the current condition is an honest inference from humidity/wind/dew point. Forecast conditions come directly from BOM.
- Uses Python standard library only: `urllib`, `xml.etree.ElementTree`, `json`, `argparse`.
- Keep output mobile-friendly. No tables. Tables are how weather becomes homework.
