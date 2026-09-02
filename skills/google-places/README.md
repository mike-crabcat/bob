# Google Places Holiday Research Skill

Find nearby places (restaurants, cafes, parks, attractions, activities) for holiday and travel planning via WhatsApp.

## Setup

### 1. Get a Google Places API Key

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select an existing one
3. Enable the **Places API (New)** and **Geocoding API**
4. Go to **APIs & Services → Credentials**
5. Create credentials → API Key
6. Restrict the API key:
   - Application restrictions: None (or IP addresses if you know them)
   - API restrictions: Places API (New), Geocoding API
7. Copy your API key

### 2. Configure Environment Variable

Add your Google Places API key to your environment:

```bash
export GOOGLE_PLACES_API_KEY="your-api-key-here"
```

For permanent configuration, add to your shell profile (`.bashrc`, `.zshrc`) or use a `.env` file.

### 3. Install Dependencies

```bash
pip install requests
```

This installs into Bob's shared venv at `~/bobenv`.

## Usage Examples

### Via WhatsApp (Primary Workflow)

**Find cafes near a location:**
```
Find cafes near Federation Square Melbourne
```

**Search for attractions:**
```
What attractions are near Bondi Beach?
```

**Find restaurants within a larger radius:**
```
Find restaurants near Times Square, New York within 2km
```

### Direct Script Usage

**Geocode a location:**
```bash
python skills/google-places/geocoder.py --location "Federation Square, Melbourne"
```

Returns:
```json
{
  "lat": -37.817,
  "lng": 144.968,
  "formatted_address": "Federation Square, Melbourne VIC, Australia",
  "place_id": "...",
  "confidence": ["tourist_attraction", "point_of_interest"]
}
```

**Search nearby places:**
```bash
python skills/google-places/places_search.py \
  --lat -37.817 \
  --lng 144.968 \
  --search-term "cafe" \
  --radius 1500 \
  --max-results 10
```

Returns:
```json
{
  "places": [
    {
      "name": "Axil Coffee Roasters",
      "type": "cafe",
      "types": ["cafe", "coffee_shop"],
      "rating": 4.6,
      "review_count": 423,
      "distance_m": 50,
      "address": "345 Flinders Ln, Melbourne VIC 3000, Australia",
      "lat": -37.8173,
      "lng": 144.9678
    }
  ]
}
```

**Format results for WhatsApp:**
```bash
python skills/google-places/places_formatter.py --results-json '{"places": [...]}'
```

Returns:
```
☕ Cafe near your location

1. ☕ Axil Coffee Roasters
   ⭐ 4.6 (423)
   50m
   345 Flinders Ln, Melbourne VIC 3000, Australia
```

## Search Terms

### Food & Drink
- `restaurant`, `cafe`, `bar`, `pub`, `bakery`, `coffee_shop`

### Activities & Attractions
- `tourist_attraction`, `museum`, `park`, `beach`, `art_gallery`, `movie_theater`

### Shopping
- `store`, `shopping_mall`, `supermarket`

### Services
- `pharmacy`, `hospital`, `gas_station`, `bank`, `atm`

## Rate Limits

- **Google Places API (New)**: 1000 requests/day (free tier)
- **Geocoding API**: 40,000 requests/month (free tier)
- Each search uses 1 geocoding request + 1 nearby search request = 2 total

## Error Messages

| Error | Cause | Solution |
|-------|-------|----------|
| "API key not provided" | `GOOGLE_PLACES_API_KEY` not set | Configure environment variable |
| "Location not found" | Location string too vague | Be more specific (e.g., include city/state) |
| "No places found" | No results in search radius | Try larger radius or different search term |
| "Rate limit exceeded" | Too many requests | Wait and try again |

## WhatsApp Output Format

Results are concise and mobile-friendly:

```
☕ Cafes near Federation Square, Melbourne

1. ☕ Axil Coffee Roasters
   ⭐ 4.6 (423)
   50m
   345 Flinders Ln

2. ☕ St Ali South
   ⭐ 4.4 (287)
   120m
   12 Clarke St

[...6 more results...]
```

## Technical Details

- **API Version**: Google Places API (New)
- **Search Type**: Nearby Search
- **Distance Calculation**: Haversine formula (straight-line distance)
- **Coordinate System**: WGS84 (decimal degrees)
- **Output Encoding**: UTF-8

## Troubleshooting

**"API key not provided"**
```bash
echo $GOOGLE_PLACES_API_KEY  # Should show your key
export GOOGLE_PLACES_API_KEY="your-key"
```

**"Location not found"**
Try being more specific:
- "Bondi" → "Bondi Beach, Sydney, Australia"
- "Times Square" → "Times Square, New York, NY"

**"No places found"**
Expand the search radius:
```bash
python skills/google-places/places_search.py --lat ... --lng ... --radius 3000
```

## Dependencies

- `requests` - HTTP client for Google API calls

## License

Part of Bob Jr's skill library. Use responsibly.