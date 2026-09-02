#!/usr/bin/env python3
"""Geocode location string to lat/lng using Google Places API (New).

Usage:
    python skills/google-places/geocoder.py --location "Federation Square, Melbourne"

Environment:
    GOOGLE_PLACES_API_KEY must be set.
"""

import argparse
import json
import os
import sys

import requests


PLACES_TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"


def geocode_location(location: str, api_key: str | None = None) -> dict | None:
    api_key = api_key or os.environ.get("GOOGLE_PLACES_API_KEY")
    if not api_key:
        print(json.dumps({"error": "API key not provided"}), file=sys.stderr)
        return None

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "places.location,places.formattedAddress,places.id",
    }

    body = {
        "textQuery": location,
        "pageSize": 1,
    }

    try:
        response = requests.post(
            PLACES_TEXT_SEARCH_URL,
            headers=headers,
            json=body,
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()

        places = data.get("places", [])
        if not places:
            print(json.dumps({"error": "Location not found"}), file=sys.stderr)
            return None

        place = places[0]
        loc = place.get("location", {})
        lat = loc.get("latitude")
        lng = loc.get("longitude")

        if lat is None or lng is None:
            print(json.dumps({"error": "No coordinates in response"}), file=sys.stderr)
            return None

        return {
            "lat": lat,
            "lng": lng,
            "formatted_address": place.get("formattedAddress", ""),
            "place_id": place.get("id"),
        }

    except requests.exceptions.Timeout:
        print(json.dumps({"error": "Geocoding request timed out"}), file=sys.stderr)
        return None
    except requests.exceptions.RequestException as e:
        print(json.dumps({"error": f"Geocoding request failed: {e}"}), file=sys.stderr)
        return None
    except (KeyError, json.JSONDecodeError) as e:
        print(json.dumps({"error": f"Invalid response from API: {e}"}), file=sys.stderr)
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--location", required=True, help="Location to geocode")
    parser.add_argument("--api-key", help="Google API key (optional, uses env var)")
    args = parser.parse_args()

    result = geocode_location(args.location, args.api_key)
    if result is None:
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
