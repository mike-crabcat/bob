---
name: itinerary-pdf
description: Generate a clean one-page itinerary PDF from structured travel/activity details, saving Markdown and PDF outputs.
trigger: when Mike asks for a one-page itinerary PDF, compact travel plan PDF, transfer itinerary, day-plan PDF, or says to make an itinerary using this method/style
---

# Itinerary PDF Skill

Use this skill to generate compact one-page itinerary documents for family travel: transfers, travel legs, check-in plans, short day plans, and other brief logistics summaries.

## Workflow

1. Gather or infer the key details:
   - title and date
   - route/summary
   - pickup/dropoff or start/end points
   - departure/pickup time and target arrival
   - people/luggage
   - booking/reference details
   - timeline
   - checklist / what to bring
   - notes and contacts

2. Create an output folder under the workspace, usually:
   `/home/bob/workspace/trips/<trip-name>/<itinerary-name>/`

3. Run the script using JSON for anything non-trivial:

```
bash("python skills/itinerary-pdf/make_itinerary_pdf.py --input-json /home/bob/workspace/trips/example/itinerary.json --output-dir /home/bob/workspace/trips/example/transfer-itinerary")
```

Or direct CLI for simple itineraries:

```
bash("python skills/itinerary-pdf/make_itinerary_pdf.py --title 'Disney to Gare Montparnasse' --date '7 July 2026' --subtitle 'Private van transfer for Bordeaux train' --pickup '09:30 from Disney Newport Bay Club' --dropoff 'Gare Montparnasse' --departure 'Train departs 12:38' --arrival-target 'Arrive 11:00–11:30' --people '3 adults, 2 children' --luggage '5 suitcases + hand luggage' --transport 'G7 Van / private transfer' --notes 'Do not leave later than 10:00' --checklist 'Passports;Train tickets;Snacks;Water;Child entertainment' --output-dir /home/bob/workspace/trips/trip-france-holiday-june-2026/disney-to-montparnasse")
```

4. The script writes:
   - Markdown source
   - HTML source
   - PDF, if a converter is installed

5. Send the generated PDF via WhatsApp or email as requested. If PDF generation fails, report the Markdown/HTML paths and either convert manually or attach the HTML/Markdown.

## Notes

- The script does not send messages or emails itself.
- Keep itinerary content short. One-page means one page, not a novella wearing travel pants.
- For PDFs, the script tries converters in this order: `weasyprint`, `wkhtmltopdf`, headless Chrome, `pandoc`.
