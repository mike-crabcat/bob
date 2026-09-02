#!/usr/bin/env python3
"""Format Google Places search results for WhatsApp-friendly output.

Usage:
    python skills/google-places/places_formatter.py --results-json '{"places": [...]}'

Input can also be provided via stdin for script chaining.
"""

import argparse
import json
import sys
from typing import Any


TYPE_EMOJIS = {
    "restaurant": "🍽️",
    "cafe": "☕",
    "bar": "🍺",
    "pub": "🍺",
    "park": "🌳",
    "beach": "🏖️",
    "museum": "🏛️",
    "attraction": "🎯",
    "store": "🛍️",
    "shopping_mall": "🛒",
    "pharmacy": "💊",
    "hospital": "🏥",
    "gas_station": "⛽",
    "supermarket": "🛒",
    "bakery": "🥐",
    "night_club": "🎉",
    "gym": "💪",
    "spa": "💆",
    "hotel": "🏨",
    "tourist_attraction": "🎯",
    "art_gallery": "🖼️",
    "library": "📚",
    "movie_theater": "🎬",
}


def get_type_emoji(place_type: str) -> str:
    """Get emoji for place type.

    Args:
        place_type: Place type string

    Returns:
        Emoji character
    """
    # Normalize type string (lowercase, strip whitespace)
    normalized = place_type.lower().strip()

    # Direct match
    if normalized in TYPE_EMOJIS:
        return TYPE_EMOJIS[normalized]

    # Partial match (e.g., "coffee_shop" → "cafe")
    for type_key, emoji in TYPE_EMOJIS.items():
        if type_key in normalized or normalized in type_key:
            return emoji

    # Default emoji
    return "📍"


def format_distance(distance_m: int | None) -> str:
    """Format distance for display.

    Args:
        distance_m: Distance in meters

    Returns:
        Formatted distance string
    """
    if distance_m is None:
        return ""

    if distance_m < 1000:
        return f"{distance_m}m"
    else:
        return f"{distance_m / 1000:.1f}km"


def format_rating(rating: float | None, review_count: int | None) -> str:
    """Format rating for display.

    Args:
        rating: Place rating (out of 5)
        review_count: Number of reviews

    Returns:
        Formatted rating string
    """
    if rating is None:
        return ""

    result = f"⭐ {rating:.1f}"
    if review_count:
        result += f" ({review_count})"

    return result


def format_place(place: dict[str, Any], index: int) -> str:
    """Format a single place for WhatsApp.

    Args:
        place: Place data dictionary
        index: Place index (1-based)

    Returns:
        Formatted place string
    """
    lines = []

    # Index and name
    emoji = get_type_emoji(place.get("type", "place"))
    lines.append(f"{index}. {emoji} {place.get('name', 'Unknown')}")

    # Rating
    rating_str = format_rating(place.get("rating"), place.get("review_count"))
    if rating_str:
        lines.append(f"   {rating_str}")

    # Distance
    distance_str = format_distance(place.get("distance_m"))
    if distance_str:
        lines.append(f"   {distance_str}")

    # Address
    address = place.get("address")
    if address:
        lines.append(f"   {address}")

    return "\n".join(lines)


def format_results(results: dict[str, Any]) -> str:
    """Format search results for WhatsApp.

    Args:
        results: Results dictionary with places array

    Returns:
        WhatsApp-formatted string
    """
    places = results.get("places", [])

    if not places:
        return "No places found."

    # Determine search context from first place
    first_place = places[0]
    place_type = first_place.get("type", "places")

    # Build header
    emoji = get_type_emoji(place_type)
    header = f"{emoji} {place_type.title()} near your location"

    # Format each place
    formatted_places = []
    for i, place in enumerate(places[:8], 1):  # Limit to 8 places
        formatted_place = format_place(place, i)
        formatted_places.append(formatted_place)

    # Combine everything
    output = f"{header}\n\n"
    output += "\n\n".join(formatted_places)

    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-json", help="JSON string of results")
    parser.add_argument("--results-file", help="Path to JSON file with results")
    args = parser.parse_args()

    # Load results from various sources
    if args.results_json:
        try:
            results = json.loads(args.results_json)
        except json.JSONDecodeError as e:
            print(f"Error parsing JSON: {e}", file=sys.stderr)
            return 1
    elif args.results_file:
        try:
            with open(args.results_file, "r") as f:
                results = json.load(f)
        except (IOError, json.JSONDecodeError) as e:
            print(f"Error reading results file: {e}", file=sys.stderr)
            return 1
    else:
        # Try reading from stdin (for script chaining)
        try:
            stdin_data = sys.stdin.read()
            if stdin_data.strip():
                results = json.loads(stdin_data)
            else:
                print("Error: No results provided. Use --results-json, --results-file, or pipe via stdin.", file=sys.stderr)
                return 1
        except json.JSONDecodeError as e:
            print(f"Error parsing stdin JSON: {e}", file=sys.stderr)
            return 1

    # Validate results
    if not isinstance(results, dict):
        print("Error: Results must be a dictionary", file=sys.stderr)
        return 1

    if "error" in results:
        print(f"Error: {results['error']}", file=sys.stderr)
        return 1

    # Format and print
    formatted = format_results(results)
    print(formatted)
    return 0


if __name__ == "__main__":
    sys.exit(main())