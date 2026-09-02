#!/usr/bin/env python3
"""Search for nearby places using Google Places API (New).

Uses Text Search for freeform queries (e.g. "pizza", "coffee") and
Nearby Search for type-based lookups (e.g. "restaurant", "cafe").

Usage:
    python skills/google-places/places_search.py --lat -37.817 --lng 144.968 --search-term "pizza"

Environment:
    GOOGLE_PLACES_API_KEY must be set.
"""

import argparse
import json
import math
import os
import sys
import time

import requests


FIELD_MASK = "places.displayName,places.types,places.rating,places.userRatingCount,places.location,places.formattedAddress"

# Place types accepted by the Nearby Search API's includedTypes field.
NEARBY_TYPES = {
    "accounting", "airport", "amusement_center", "aquarium",
    "art_gallery", "atm", "auto_parts_store", "bakery", "bank",
    "bar", "beauty_salon", "bicycle_store", "book_store", "bowling_alley",
    "bus_stop", "cafe", "campground", "car_dealer", "car_rental",
    "car_repair", "car_wash", "casino", "cemetery", "church",
    "city_hall", "clothing_store", "convenience_store", "courthouse",
    "dentist", "department_store", "doctor", "drugstore", "electrician",
    "electronics_store", "embassy", "fire_station", "florist", "funeral_home",
    "furniture_store", "gas_station", "golf_course", "grocery_store",
    "gym", "hair_care", "hardware_store", "hindu_temple", "home_goods_store",
    "hospital", "insurance_agency", "jewelry_store", "laundry", "lawyer",
    "library", "light_rail_station", "liquor_store", "local_government_office",
    "locksmith", "lodging", "meal_delivery", "meal_takeaway", "mosque",
    "movie_rental", "movie_theater", "moving_company", "museum",
    "night_club", "optician", "park", "parking", "pet_store",
    "pharmacy", "physiotherapist", "plumber", "police", "post_office",
    "primary_school", "real_estate_agency", "restaurant", "roofing_contractor",
    "rv_park", "school", "secondary_school", "shoe_store", "shopping_mall",
    "spa", "stadium", "storage", "store", "subway_station", "supermarket",
    "synagogue", "taxi_stand", "tourist_attraction", "train_station",
    "transit_station", "travel_agency", "university", "veterinary_care", "zoo",
}


def calculate_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000
    lat1_rad, lon1_rad = math.radians(lat1), math.radians(lon1)
    lat2_rad, lon2_rad = math.radians(lat2), math.radians(lon2)
    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def _parse_places(data: dict, origin_lat: float, origin_lng: float) -> list[dict]:
    places = []
    for place in data.get("places", []):
        location = place.get("location", {})
        place_lat = location.get("latitude")
        place_lng = location.get("longitude")

        distance_m = None
        if place_lat is not None and place_lng is not None:
            distance_m = calculate_distance(origin_lat, origin_lng, place_lat, place_lng)

        display_name = place.get("displayName", {})
        name = display_name.get("text", "Unknown") if isinstance(display_name, dict) else display_name

        types = place.get("types", [])
        primary_type = types[0] if types else "place"

        places.append({
            "name": name,
            "type": primary_type,
            "types": types,
            "rating": place.get("rating"),
            "review_count": place.get("userRatingCount"),
            "distance_m": round(distance_m) if distance_m else None,
            "address": place.get("formattedAddress", ""),
            "lat": place_lat,
            "lng": place_lng,
        })

    places.sort(key=lambda p: p.get("distance_m") or float("inf"))
    return places


def search_nearby(
    lat: float,
    lng: float,
    search_term: str | None = None,
    radius: int = 1500,
    max_results: int = 10,
    api_key: str | None = None,
) -> dict:
    api_key = api_key or os.environ.get("GOOGLE_PLACES_API_KEY")
    if not api_key:
        print(json.dumps({"error": "API key not provided"}), file=sys.stderr)
        return {"error": "API key not provided"}

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": FIELD_MASK,
    }

    # Use Text Search for freeform queries, Nearby Search for known types.
    use_text_search = search_term and search_term.lower() not in NEARBY_TYPES

    if use_text_search:
        url = "https://places.googleapis.com/v1/places:searchText"
        body: dict = {
            "textQuery": search_term,
            "pageSize": max_results,
            "locationBias": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": radius,
                },
            },
        }
    else:
        url = "https://places.googleapis.com/v1/places:searchNearby"
        body = {
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": radius,
                },
            },
            "maxResultCount": max_results,
        }
        if search_term:
            body["includedTypes"] = [search_term.lower()]

    max_retries = 3
    base_delay = 1.0

    for attempt in range(max_retries):
        try:
            response = requests.post(url, headers=headers, json=body, timeout=10)

            if response.status_code == 429:
                if attempt < max_retries - 1:
                    time.sleep(base_delay * (2 ** attempt))
                    continue
                print(json.dumps({"error": "Rate limit exceeded"}), file=sys.stderr)
                return {"error": "Rate limit exceeded"}

            response.raise_for_status()
            data = response.json()

            # Text Search may paginate via nextPageToken; ignore for now.
            places = _parse_places(data, lat, lng)
            return {"places": places}

        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                time.sleep(base_delay * (2 ** attempt))
                continue
            print(json.dumps({"error": "Search request timed out"}), file=sys.stderr)
            return {"error": "Search request timed out"}
        except requests.exceptions.RequestException as e:
            print(json.dumps({"error": f"Search request failed: {e}"}), file=sys.stderr)
            return {"error": f"Search request failed: {e}"}
        except (KeyError, json.JSONDecodeError) as e:
            print(json.dumps({"error": f"Invalid response from API: {e}"}), file=sys.stderr)
            return {"error": f"Invalid response from API: {e}"}

    return {"error": "Search failed after retries"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lat", required=True, type=float, help="Latitude")
    parser.add_argument("--lng", required=True, type=float, help="Longitude")
    parser.add_argument("--search-term", help="Search term or place type")
    parser.add_argument("--radius", type=int, default=1500, help="Search radius in meters (default: 1500)")
    parser.add_argument("--max-results", type=int, default=10, help="Maximum results (default: 10)")
    parser.add_argument("--api-key", help="Google API key (optional, uses env var)")
    args = parser.parse_args()

    result = search_nearby(
        args.lat,
        args.lng,
        args.search_term,
        args.radius,
        args.max_results,
        args.api_key,
    )

    if "error" in result:
        print(json.dumps(result, indent=2), file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
