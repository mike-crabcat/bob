# BOM Weather Skill

Fetches live weather from the Bureau of Meteorology for Mount Lawley / Perth and returns a compact WhatsApp-friendly summary.

## Usage

```bash
python skills/bom-weather/bom_fetcher.py --location mount_lawley --days 3 --format whatsapp
```

Other useful modes:

```bash
python skills/bom-weather/bom_fetcher.py --location mount_lawley --days 3 --format json
python skills/bom-weather/bom_fetcher.py --location mount_lawley --days 3 --format debug
```

## Supported locations

- `mount_lawley` — default, uses Perth Metro observation station and Perth forecast
- `perth`
- `perth_metro`

## Data sources

Official BOM FTP XML:

- Observations: `IDW60920.xml`
- Perth metropolitan/location forecast: `IDW12300.xml`

## Output includes

- Current temperature and apparent temperature
- Wind and humidity
- Today/tomorrow/next-day forecast
- Min/max temperature where BOM provides it
- Rain chance and rain range where BOM provides it
- Today detail and UV alert when present

## Requirements

- Python 3
- Standard library only
- Internet access to BOM FTP

No `requests`. No mystery dependencies. A blessedly small pile of machinery.
