---
name: google-places
description: Search for nearby places using Google Places API for holiday planning and travel research
trigger: when Bob asks to find, search for, or discover places, attractions, restaurants, cafes, parks, or activities near a location—especially in the context of holiday or travel planning
---

## Instructions

When this skill activates, follow these steps to find places near a location:

1. **Extract Search Parameters**:
   - Location (required): Address, city, place name, or lat/long coordinates
   - Search term or place type (optional): "restaurants", "cafes", "parks", "attractions", "activities", or custom search
   - Radius (optional): Defaults to 1500m for walkable distances
   - Category filter (optional): Defaults to no filter

2. **Geocode Location**:
   - Call `bash("python skills/google-places/geocoder.py --location '<location>'")` to get lat/long coordinates
   - The geocoder returns JSON with lat, lng, and formatted_address
   - If location is ambiguous (multiple matches), present top 2-3 options and ask Bob to clarify

3. **Search Nearby Places**:
   - Call `bash("python skills/google-places/places_search.py --lat <lat> --lng <lng> --search-term '<term>' --radius <radius>")`
   - Uses Google Places API Nearby Search (New)
   - Defaults: radius=1500m, max-results=8, no type filter (let search term drive results)
   - Returns JSON array of places with name, types, rating, vicinity, distance

4. **Format for WhatsApp**:
   - Call `bash("python skills/google-places/places_formatter.py --results-json '<json>'")` for concise, mobile-friendly output
   - Include: name, type, rating, distance (if available), address
   - Limit to top 5-8 results per category
   - Use emojis for readability (🍽️ for restaurants, ☕ for cafes, 🌳 for parks, etc.)

5. **Send Results**:
   - Use `send_whatsapp_message` or `send_whatsapp_to_contact` with formatted results
   - If no results found, suggest expanding radius or different search terms

6. **Handle Errors**:
   - **Missing/invalid API key**: Send message: "Google Places API key not configured. Please set GOOGLE_PLACES_API_KEY environment variable."
   - **Location not found**: Send message: "Couldn't find that location. Try being more specific (e.g., 'Bondi Beach, Sydney' instead of 'Bondi')."
   - **No results found**: Send message: "No places found near there. Try expanding the search radius or different terms."
   - **Rate limiting**: Send message: "Hit rate limit. Try again in a few seconds."

## Key Requirements

- **API**: Google Places API (New) - Nearby Search endpoint
- **Default radius**: 1500m (walkable distance for holiday planning)
- **Default max results**: 8 places per search (concise for WhatsApp)
- **Output format**: WhatsApp-friendly, under 2000 characters, emoji-enhanced
- **Privacy**: Don't cache location data beyond current session
- **Error handling**: Graceful degradation, helpful error messages

## Example WhatsApp Workflow

**Bob's request:** "Find cafes near Federation Square Melbourne"

**Process:**
1. Geocode "Federation Square Melbourne" → lat: -37.817, lng: 144.968
2. Search nearby with "cafes" → 8 places found
3. Format results → WhatsApp message

**WhatsApp output:**
```
☕ Cafes near Federation Square, Melbourne

1. Axil Coffee Roasters
   ⭐ 4.6 | 50m
   345 Flinders Ln

2. St Ali South
   ⭐ 4.4 | 120m
   12 Clarke St

3. Dukes Coffee Roasters
   ⭐ 4.5 | 200m
   247 Flinders Ln

[...5 more results...]
```

## Common Search Terms

- **Food**: "restaurants", "cafes", "bars", "pubs", "brunch"
- **Activities**: "attractions", "museums", "parks", "beaches", "hiking"
- **Shopping**: "shopping", "markets", "malls"
- **Services**: "pharmacies", "supermarkets", "gas stations"

## Rate Limits

Google Places API free tier: 1000 requests/day
Each search uses 1 request (geocoding) + 1 request (nearby search) = 2 total

## Dependencies

This skill requires the `requests` package. Install it once into Bob's venv:
```
bash("pip install requests")
```