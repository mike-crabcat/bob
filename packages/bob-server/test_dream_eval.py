"""Run dream eval cases directly without the full CLI."""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "packages/bob-server")

from bob_server.config import Settings
from bob_server.context import AppContext
from bob_server.database import Database

# Load .env manually
env_file = Path.home() / "config" / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


async def main():
    settings = Settings.from_env()
    schema_dir = Path("packages/bob-server/bob_server/schemas")
    db = Database(settings.data_dir / "bob.db", schema_dir)
    await db.connect()
    await db.apply_migrations()
    ctx = AppContext(settings=settings, db=db)

    # Discover dream eval cases
    from bob_server.evals.registry import get_cases_by_category
    cases = get_cases_by_category("dream")

    if not cases:
        print("No dream eval cases found")
        return

    print(f"Running {len(cases)} dream eval cases...\n")

    for case in cases:
        print(f"  {case.id}: ", end="", flush=True)
        try:
            start = time.monotonic()
            result = await asyncio.wait_for(case.run(ctx), timeout=case.timeout_seconds)
            latency = time.monotonic() - start
            response = result.get("response", "")

            # Parse the response
            text = response.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

            try:
                ops = json.loads(text)
                if not isinstance(ops, list):
                    ops = []
            except (json.JSONDecodeError, ValueError):
                ops = []

            # Run structural checks
            passed = True
            details = []

            for check in case.structural_checks:
                if check.kind == "json_valid":
                    try:
                        json.loads(text)
                        details.append(f"  {check.kind}: PASS")
                    except (json.JSONDecodeError, ValueError):
                        details.append(f"  {check.kind}: FAIL - not valid JSON")
                        passed = False

            # Summary
            op_count = len(ops)
            categories = set()
            for op in ops:
                if isinstance(op, dict):
                    categories.add(op.get("category", ""))

            if op_count == 0:
                print(f"FAIL ({latency:.1f}s) — returned empty array []")
                passed = False
            else:
                print(f"{'PASS' if passed else 'FAIL'} ({latency:.1f}s) — {op_count} ops: {', '.join(sorted(categories))}")

            for d in details:
                print(d)

            # Show first operation content
            if ops:
                first = ops[0]
                if isinstance(first, dict):
                    cat = first.get("category", "?")
                    slug = first.get("slug", "?")
                    title = first.get("title", "?")
                    content_preview = first.get("content", "")[:200].replace("\n", " ")
                    print(f"  first op: [{cat}/{slug}] {title}")
                    print(f"    {content_preview}...")

            if op_count == 0:
                print(f"  raw response: {response[:300]}")

        except Exception as e:
            print(f"ERROR: {e}")

        print()

    await db.close()


asyncio.run(main())
