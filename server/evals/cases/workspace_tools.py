"""Workspace tools in a real turn: bash over the world's real workspace,
and fetch-on-demand media (history photos replay as read_image stubs —
production's media policy — so describing one requires loading it)."""

from __future__ import annotations

from datetime import timedelta

from server.evals.case import Check
from server.evals.cases._kit import noise_png
from server.evals.registry import eval_case


@eval_case(
    id="workspace_bash",
    category="workspace_tools",
    description="Asked what's in the workspace, Bob looks (bash) and answers "
                "from what he saw.",
    checks=[Check("tool_call_made", {"tool_name": "bash"}),
            Check("min_length", {"min_length": 10})],
    rubric=["Bob's answer names files or folders that his bash output actually "
            "listed (e.g. notes/, recipes/, generated-images/) and invents none."],
)
async def workspace_bash(w):
    w.files({"notes/todo.md": "- fix gutter\n", "recipes/brisket.md": "# Brisket\n",
             "generated-images/poster.png": noise_png(32)})
    await w.inbound("Mike", "What files are in my workspace?")


@eval_case(
    id="media_stub_fetch_on_reference",
    category="workspace_tools",
    description="An older photo replays as a read_image stub; asked about a "
                "detail in it, Bob loads it instead of answering blind.",
    checks=[Check("tool_call_made", {"tool_name": "read_image"})],
    rubric=["Bob does not describe the washer as if seen without loading the "
            "photo; after loading it he answers honestly from what it shows "
            "(saying he can't make the washer out is fine)."],
    context="The photo is an abstract noise image — there is no visible washer "
            "in it. The fetch behaviour is what matters.",
)
async def media_stub_fetch_on_reference(w):
    media = w.workspace / "whatsapp-media" / "EVALTAP01.jpg"
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(noise_png(128, seed=7))
    await w.history("Mike", [
        ("Mike", "that tap looks wrecked, how do I fix it?",
         {"image_path": str(media), "image_mime_type": "image/png"}),
        ("Bob", "Limescale on the chrome ring, seat looks intact. Unscrew the "
                "collar, then the headgear — the washer sits at the bottom."),
    ], ago=timedelta(minutes=50))
    await w.inbound("Mike", "Before I start — is the washer in that photo "
                    "sitting flat or bent?")


def _garden_log() -> str:
    lines = [f"- day {i}: watered beds {i % 7 + 1}, checked drippers" for i in range(1, 3001)]
    lines[1799] = "- day 1800: tomatos staked, recieve compost delivery tomorrow"
    return "\n".join(lines) + "\n"


@eval_case(
    id="edit_large_file_surgical",
    category="workspace_tools",
    description="A typo deep in a 3,000-line file: Bob finds it and fixes that "
                "one line — outcome-gated (the rest of the file byte-identical).",
    checks=[Check("flag", {"key": "fixed"}),
            Check("flag", {"key": "surgical"})],
    rubric=["Bob fixes the typo(s) on the day-1800 entry and says so briefly; "
            "he does not claim changes he didn't make."],
)
async def edit_large_file_surgical(w):
    original = _garden_log()
    w.files({"notes/garden-log.md": original})
    await w.inbound("Mike", "There's a typo in notes/garden-log.md somewhere "
                    "around day 1800 — 'recieve'. Fix it please.")
    after = (w.workspace / "notes/garden-log.md").read_text()
    w.flag("fixed", "recieve" not in after and "receive compost" in after)
    a, b = original.splitlines(), after.splitlines()
    w.flag("surgical", len(a) == len(b)
           and sum(x != y for x, y in zip(a, b)) == 1)


_CSV_TOOL = '''import csv, sys


def parse_row(row):
    return {"name": row[0].strip(), "qty": int(row[1])}


def load(path):
    with open(path) as f:
        return [parse_row(r) for r in csv.reader(f)]


def total(rows):
    # parse_row already coerced qty
    return sum(r["qty"] for r in rows)


if __name__ == "__main__":
    items = load(sys.argv[1])
    print(total(items), parse_row(["x", "0"]))
'''


@eval_case(
    id="rename_function_multi_site",
    category="workspace_tools",
    description="Rename a function used in four places in one scratch script — "
                "every use renamed, nothing else touched (any exact method: "
                "multi_edit, replace_all, or a sed rename).",
    checks=[Check("flag", {"key": "renamed"}),
            Check("flag", {"key": "rest_unchanged"})],
    rubric=["Bob renames every use of parse_row to parse_line and confirms "
            "briefly; no other edits."],
)
async def rename_function_multi_site(w):
    w.files({"scratch/csv_tool.py": _CSV_TOOL})
    await w.inbound("Mike", "In scratch/csv_tool.py rename parse_row to "
                    "parse_line everywhere.")
    after = (w.workspace / "scratch/csv_tool.py").read_text()
    w.flag("renamed", "parse_row" not in after and after.count("parse_line") == 4)
    w.flag("rest_unchanged", after == _CSV_TOOL.replace("parse_row", "parse_line"))


_DEPLOY_SH = r"""#!/bin/bash
set -euo pipefail

notify() {
    local msg="$1"
    curl -s -X POST "$HOOK_URL" -d "{\"text\": \"${msg}\"}" > /dev/null
}

backup() {
    tar czf "/backups/site-$(date +%F).tgz" ./site
    echo 'Backup done — don\'t forget to rotate'
}

backup
notify "deployed"
"""

_BACKUP_NEW = r"""backup() {
    local stamp
    stamp="$(date +%F-%H%M)"
    tar czf "/backups/site-${stamp}.tgz" ./site
    find /backups -name 'site-*.tgz' -mtime +14 -delete
    echo "Backup ${stamp} done; pruned >14d"
}"""


def _old_backup_fn() -> str:
    start = _DEPLOY_SH.index("backup() {")
    return _DEPLOY_SH[start:_DEPLOY_SH.index("\n}\n", start) + 2]


@eval_case(
    id="edit_quote_heavy_block",
    category="workspace_tools",
    description="Replace a multi-line shell function full of quotes, $vars "
                "and backslashes — the shape that breaks sed and heredoc "
                "rewrites. Outcome-gated: the function replaced verbatim, "
                "everything else byte-identical.",
    checks=[Check("flag", {"key": "replaced_verbatim"}),
            Check("flag", {"key": "rest_unchanged"})],
    rubric=["Bob replaces the backup() function with exactly the provided "
            "text and confirms briefly; he does not alter notify() or the "
            "rest of the script."],
)
async def edit_quote_heavy_block(w):
    w.files({"scratch/deploy.sh": _DEPLOY_SH})
    await w.inbound(
        "Mike",
        "In scratch/deploy.sh replace the whole backup() function with exactly "
        "this, nothing else changed:\n\n" + _BACKUP_NEW)
    after = (w.workspace / "scratch/deploy.sh").read_text()
    expected = _DEPLOY_SH.replace(_old_backup_fn(), _BACKUP_NEW)
    w.flag("replaced_verbatim", _BACKUP_NEW in after and "don\\'t forget" not in after)
    w.flag("rest_unchanged", after == expected)
