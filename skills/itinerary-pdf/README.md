# itinerary-pdf

Generates compact Markdown, HTML, and PDF itinerary documents.

## JSON example

```json
{
  "title": "Disney to Gare Montparnasse",
  "subtitle": "Private van transfer for Bordeaux train",
  "date": "7 July 2026",
  "route": "Disney Newport Bay Club → Gare Montparnasse",
  "summary": "Book a G7 Van for 09:30. Aim to arrive 11:00–11:30 for the 12:38 train.",
  "timing": {
    "pickup": "09:30",
    "arrival_target": "11:00–11:30",
    "departure": "12:38 train"
  },
  "people": "3 adults, 2 children",
  "luggage": "5 suitcases + hand luggage",
  "booking": "G7 Van, request large van and child boosters",
  "addresses": {
    "from": "Disney Newport Bay Club, Disneyland Paris",
    "to": "Gare Montparnasse, Paris"
  },
  "timeline": ["09:15 bags ready", "09:30 van pickup", "11:00–11:30 arrive Montparnasse"],
  "checklist": ["Train tickets", "Passports", "Snacks", "Water"],
  "notes": ["Do not rely on Uber roulette", "Allow time for toilets and platform finding"],
  "contacts": ["G7 booking/app"],
  "source_note": "Prepared from trip logistics discussed in WhatsApp."
}
```

## Direct CLI example

```bash
python /home/bob/.config/cyborg/harness/skills/itinerary-pdf/make_itinerary_pdf.py \
  --title "Disney to Gare Montparnasse" \
  --date "7 July 2026" \
  --pickup "09:30 from Disney Newport Bay Club" \
  --dropoff "Gare Montparnasse" \
  --people "3 adults, 2 children" \
  --luggage "5 suitcases" \
  --output-dir /home/bob/workspace/trips/example-itinerary
```
