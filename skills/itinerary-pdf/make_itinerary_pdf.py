#!/usr/bin/env python3
"""Generate compact one-page itinerary Markdown/HTML/PDF files."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "itinerary"


def as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        if ";" in value:
            return [x.strip() for x in value.split(";") if x.strip()]
        return [value.strip()] if value.strip() else []
    return [str(value).strip()] if str(value).strip() else []


def read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("JSON root must be an object")
        return data
    except Exception as exc:
        raise RuntimeError(f"Could not read JSON input: {exc}") from exc


def merge_data(args: argparse.Namespace) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if args.input_json:
        data.update(read_json(Path(args.input_json)))

    direct = {
        "title": args.title,
        "subtitle": args.subtitle,
        "date": args.date,
        "transport": args.transport,
        "booking": args.booking,
        "people": args.people,
        "luggage": args.luggage,
        "source_note": args.source_note,
    }
    for k, v in direct.items():
        if v:
            data[k] = v

    if args.pickup or args.dropoff or args.departure or args.arrival_target:
        timing = dict(data.get("timing") or {})
        if args.pickup:
            timing["pickup"] = args.pickup
        if args.dropoff:
            timing["dropoff"] = args.dropoff
        if args.departure:
            timing["departure"] = args.departure
        if args.arrival_target:
            timing["arrival_target"] = args.arrival_target
        data["timing"] = timing

    if args.address_from or args.address_to:
        addresses = dict(data.get("addresses") or {})
        if args.address_from:
            addresses["from"] = args.address_from
        if args.address_to:
            addresses["to"] = args.address_to
        data["addresses"] = addresses

    if args.timeline:
        data["timeline"] = as_list(args.timeline)
    if args.checklist:
        data["checklist"] = as_list(args.checklist)
    if args.notes:
        data["notes"] = as_list(args.notes)
    if args.contacts:
        data["contacts"] = as_list(args.contacts)

    if not data.get("title"):
        raise RuntimeError("Missing required field: title")
    if not data.get("date"):
        raise RuntimeError("Missing required field: date")

    return data


def md_escape(text: Any) -> str:
    return str(text).strip()


def build_markdown(data: dict[str, Any]) -> str:
    title = md_escape(data.get("title"))
    subtitle = md_escape(data.get("subtitle", ""))
    date = md_escape(data.get("date"))
    lines: list[str] = [f"# {title}", ""]
    if subtitle:
        lines += [f"**{subtitle}**", ""]
    lines += [f"**Date:** {date}", ""]

    for key, label in [("route", "Route"), ("summary", "Summary"), ("transport", "Transport"), ("booking", "Booking"), ("people", "People"), ("luggage", "Luggage")]:
        if data.get(key):
            lines.append(f"**{label}:** {md_escape(data[key])}")
    lines.append("")

    timing = data.get("timing") or {}
    if isinstance(timing, dict) and timing:
        lines += ["## Key timing"]
        for key, label in [("pickup", "Pickup/start"), ("arrival_target", "Target arrival"), ("departure", "Departure"), ("dropoff", "Drop-off/end")]:
            if timing.get(key):
                lines.append(f"- **{label}:** {md_escape(timing[key])}")
        lines.append("")

    addresses = data.get("addresses") or {}
    if isinstance(addresses, dict) and addresses:
        lines += ["## Addresses"]
        if addresses.get("from"):
            lines.append(f"- **From:** {md_escape(addresses['from'])}")
        if addresses.get("to"):
            lines.append(f"- **To:** {md_escape(addresses['to'])}")
        lines.append("")

    sections = [("timeline", "Timeline"), ("checklist", "Checklist"), ("notes", "Notes"), ("contacts", "Contacts")]
    for key, label in sections:
        items = as_list(data.get(key))
        if items:
            lines += [f"## {label}"]
            lines += [f"- {md_escape(x)}" for x in items]
            lines.append("")

    if data.get("source_note"):
        lines += ["---", md_escape(data["source_note"]), ""]
    return "\n".join(lines).strip() + "\n"


def card(label: str, value: Any) -> str:
    if not value:
        return ""
    return f'<div class="card"><div class="label">{html.escape(label)}</div><div class="value">{html.escape(str(value))}</div></div>'


def list_section(label: str, items: list[str], klass: str = "") -> str:
    if not items:
        return ""
    lis = "".join(f"<li>{html.escape(x)}</li>" for x in items)
    return f'<section class="{klass}"><h2>{html.escape(label)}</h2><ul>{lis}</ul></section>'


def build_html(data: dict[str, Any]) -> str:
    title = str(data.get("title", "Itinerary"))
    subtitle = str(data.get("subtitle", ""))
    timing = data.get("timing") or {}
    addresses = data.get("addresses") or {}

    key_cards = "".join([
        card("Date", data.get("date")),
        card("Route", data.get("route")),
        card("Transport", data.get("transport")),
        card("Booking", data.get("booking")),
        card("People", data.get("people")),
        card("Luggage", data.get("luggage")),
    ])

    timing_cards = ""
    if isinstance(timing, dict):
        timing_cards = "".join([
            card("Pickup / start", timing.get("pickup")),
            card("Target arrival", timing.get("arrival_target")),
            card("Departure", timing.get("departure")),
            card("Drop-off / end", timing.get("dropoff")),
        ])

    address_html = ""
    if isinstance(addresses, dict) and (addresses.get("from") or addresses.get("to")):
        address_html = '<section><h2>Addresses</h2><div class="twocol">' + card("From", addresses.get("from")) + card("To", addresses.get("to")) + '</div></section>'

    footer = html.escape(str(data.get("source_note") or "Generated itinerary."))
    summary = data.get("summary")
    summary_html = f'<section class="summary">{html.escape(str(summary))}</section>' if summary else ""

    return f'''<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
@page {{ size: A4; margin: 12mm; }}
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif; color: #18212b; margin: 0; font-size: 10.5pt; line-height: 1.28; }}
header {{ border-bottom: 3px solid #1f4e79; padding-bottom: 7px; margin-bottom: 9px; }}
h1 {{ margin: 0; font-size: 23pt; line-height: 1.05; color: #163b5c; }}
.subtitle {{ margin-top: 4px; font-size: 11.5pt; color: #53616f; font-weight: 600; }}
.grid {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 6px; margin: 8px 0; }}
.twocol {{ display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }}
.card {{ border: 1px solid #d8e0e8; border-radius: 7px; padding: 5px 7px; background: #f8fafc; min-height: 34px; }}
.label {{ color: #53616f; font-size: 7.5pt; text-transform: uppercase; letter-spacing: .04em; font-weight: 700; margin-bottom: 2px; }}
.value {{ font-size: 10pt; font-weight: 650; }}
section {{ margin: 8px 0; }}
.summary {{ background: #fff7df; border-left: 4px solid #d9a514; padding: 7px 9px; border-radius: 5px; font-weight: 600; }}
h2 {{ font-size: 12pt; margin: 7px 0 4px; color: #163b5c; }}
ul {{ margin: 0; padding-left: 18px; }}
li {{ margin: 2px 0; }}
.columns {{ display: grid; grid-template-columns: 1.1fr .9fr; gap: 10px; }}
footer {{ position: fixed; bottom: 5mm; left: 12mm; right: 12mm; font-size: 7.5pt; color: #6b7785; border-top: 1px solid #d8e0e8; padding-top: 3px; }}
.compact li {{ margin: 1px 0; }}
</style>
</head>
<body>
<header>
  <h1>{html.escape(title)}</h1>
  {f'<div class="subtitle">{html.escape(subtitle)}</div>' if subtitle else ''}
</header>
{summary_html}
<div class="grid">{key_cards}</div>
<section><h2>Key timing</h2><div class="grid">{timing_cards}</div></section>
{address_html}
<div class="columns">
  <div>
    {list_section('Timeline', as_list(data.get('timeline')))}
    {list_section('Notes', as_list(data.get('notes')))}
  </div>
  <div>
    {list_section('Checklist', as_list(data.get('checklist')), 'compact')}
    {list_section('Contacts', as_list(data.get('contacts')), 'compact')}
  </div>
</div>
<footer>{footer}</footer>
</body>
</html>'''


def run_converter(html_path: Path, md_path: Path, pdf_path: Path) -> tuple[bool, str]:
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    tools = [
        ("weasyprint", ["weasyprint", str(html_path), str(pdf_path)]),
        ("wkhtmltopdf", ["wkhtmltopdf", "--quiet", str(html_path), str(pdf_path)]),
    ]
    if chrome:
        tools.append(("chrome", [chrome, "--headless", "--disable-gpu", "--no-sandbox", f"--print-to-pdf={pdf_path}", html_path.as_uri()]))
    tools.append(("pandoc", ["pandoc", str(md_path), "-o", str(pdf_path)]))
    for name, cmd in tools:
        if name != "chrome" and not shutil.which(name):
            continue
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
            if pdf_path.exists() and pdf_path.stat().st_size > 0:
                return True, f"PDF created using {name}"
        except Exception as exc:
            last = f"{name} failed: {exc}"
            continue
    return False, "No working PDF converter found. Created Markdown and HTML only."


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a compact one-page itinerary document.")
    parser.add_argument("--input-json")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--title")
    parser.add_argument("--subtitle")
    parser.add_argument("--date")
    parser.add_argument("--pickup")
    parser.add_argument("--dropoff")
    parser.add_argument("--departure")
    parser.add_argument("--arrival-target")
    parser.add_argument("--transport")
    parser.add_argument("--booking")
    parser.add_argument("--people")
    parser.add_argument("--luggage")
    parser.add_argument("--address-from")
    parser.add_argument("--address-to")
    parser.add_argument("--notes", help="Semicolon-separated notes")
    parser.add_argument("--checklist", help="Semicolon-separated checklist items")
    parser.add_argument("--timeline", help="Semicolon-separated timeline items")
    parser.add_argument("--contacts", help="Semicolon-separated contacts")
    parser.add_argument("--source-note")

    try:
        args = parser.parse_args()
        data = merge_data(args)
        out_dir = Path(args.output_dir).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        slug = slugify(str(data["title"]))
        md_path = out_dir / f"{slug}.md"
        html_path = out_dir / f"{slug}.html"
        pdf_path = out_dir / f"{slug}.pdf"

        md_path.write_text(build_markdown(data), encoding="utf-8")
        html_path.write_text(build_html(data), encoding="utf-8")
        pdf_ok, pdf_msg = run_converter(html_path, md_path, pdf_path)

        print("Itinerary generated.")
        print(f"Markdown: {md_path}")
        print(f"HTML: {html_path}")
        if pdf_ok:
            print(f"PDF: {pdf_path}")
        print(pdf_msg)
        return 0
    except Exception as exc:
        print(f"Could not generate itinerary: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
