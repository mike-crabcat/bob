"""Base world: the fictional cast, owner profile, and background memory
every eval world starts from (docs/evals.md → Worlds).

Hermetic by design: no real names, numbers or history — with the live
memory store a fan-out case once spent 86 rounds mining real figurine
history. The background memory population exists so recall is never
trivially empty: a recall that "finds nothing" in an empty store proves
nothing about retrieval. All dates are relative to the world's ``now``
(one captured instant per world), so nothing drifts as the calendar moves.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Person:
    name: str                  # display name, "First Last"
    phone: str                 # digits, no +
    trusted: bool = False
    owner: bool = False
    email: str | None = None
    contact_id: str = ""       # filled in by the template build

    @property
    def first(self) -> str:
        return self.name.split()[0]

    @property
    def slug(self) -> str:
        return "person-" + self.name.lower().replace(" ", "-")

    @property
    def jid(self) -> str:
        return f"{self.phone}@s.whatsapp.net"

    @property
    def dm_key(self) -> str:
        return f"agent:main:whatsapp:dm:{self.phone}"


@dataclass
class Group:
    name: str
    gid: str                   # raw group id (digits)
    members: list[str]         # Person.first names
    policy: dict = field(default_factory=dict)

    @property
    def jid(self) -> str:
        return f"{self.gid}@g.us"

    @property
    def key(self) -> str:
        return f"agent:main:whatsapp:group:{self.gid}"

    @property
    def slug(self) -> str:
        return "group-" + self.name.lower().replace(" ", "-")


CAST: list[Person] = [
    Person("Mike Cleaver", "61400000001", trusted=True, owner=True,
           email="mike@example.org"),
    Person("Helen Cleaver", "61400000002", trusted=True),
    Person("Sarah Cleaver", "61400000003"),
    Person("Ryan Ortiz", "61400000011"),
    Person("Blair Novak", "61400000012"),
    Person("David Kerr", "61400000013"),
    Person("Sylvain Marchetti", "61400000014"),
    Person("Rupert Hale", "61400000015"),
    Person("Simon Tran", "61400000016"),
    Person("Chris Doyle", "61400000017"),
    Person("Priya Sharma", "61400000021"),
    Person("Tomasz Nowak", "61400000022"),
    Person("Grace Liu", "61400000023"),
    Person("Hamish Reid", "61400000024"),
    Person("Leilani Kahale", "61400000025"),
    Person("Oskar Berg", "61400000026"),
    Person("Marcus Bell", "61400000031"),
]

GROUPS: list[Group] = [
    Group("Doom Club", "120363000000000001",
          ["Mike", "David", "Rupert", "Sylvain", "Chris", "Blair"],
          policy={"group_outbound_enabled": True}),
    Group("Weeming Boys", "120363000000000002",
          ["Mike", "Ryan", "Blair", "David", "Simon"]),
    Group("Quiz Night", "120363000000000003",
          ["Mike", "Priya", "Tomasz", "Grace", "Hamish", "Leilani", "Oskar"]),
    Group("Crypto Bob", "120363000000000004", ["Mike", "Simon"]),
]

USER_MD = """\
- **Name:** Mike Cleaver
- **What to call them:** Mike
- **Pronouns:** he/him
- **Timezone:** Australia/Perth (GMT+8)
- **Email:** mike@example.org
- **Notes:** Creator of Bob Jnr

## Family

*Scope: Mike's own household — context for his requests and family
conversations only.*

- **Wife:** Helen (lawyer, coeliac/gluten-free)
- **Sister:** Sarah (lives in Fremantle)
"""


def base_memory(now: datetime) -> list[tuple[str, str, str, list[tuple[str, str]]]]:
    """(entity_id, type, display_name, claims) — background population."""
    def day(n: int) -> str:
        return (now + timedelta(days=n)).strftime("%Y-%m-%d")

    return [
        ("person-helen-cleaver", "person", "Helen Cleaver", [
            ("job", "Lawyer"),
            ("dietary_restriction", "Coeliac — strictly gluten-free")]),
        ("person-sarah-cleaver", "person", "Sarah Cleaver", [
            ("home_address", "Fremantle")]),
        ("person-ryan-ortiz", "person", "Ryan Ortiz", [
            ("job", "Electrician"),
            ("interest", "Fremantle Dockers supporter")]),
        ("person-blair-novak", "person", "Blair Novak", [
            ("interest", "Barbecue and smoking meat")]),
        ("person-david-kerr", "person", "David Kerr", [
            ("job", "High-school science teacher")]),
        ("person-sylvain-marchetti", "person", "Sylvain Marchetti", [
            ("work_schedule", "Works from home Thursdays")]),
        ("person-simon-tran", "person", "Simon Tran", [
            ("interest", "Runs the club's fixture list")]),
        ("person-priya-sharma", "person", "Priya Sharma", [
            ("work_schedule", "Works from home Thursdays only (her own message)"),
            ("workplace", "Woodside, Perth CBD")]),
        ("person-hamish-reid", "person", "Hamish Reid", [
            ("interest", "Organises the Quiz Night lunches")]),
        ("person-tomasz-nowak", "person", "Tomasz Nowak", [
            ("dietary_restriction", "Vegetarian")]),
        ("group-doom-club", "group", "Doom Club", [
            ("tradition", "Monthly Friday lunch, usually somewhere in Northbridge")]),
        ("group-weeming-boys", "group", "Weeming Boys", [
            ("tradition", "Watch the Dockers games together")]),
        ("group-quiz-night", "group", "Quiz Night", [
            ("tradition", "Quiz on Wednesday nights at The Brass Monkey")]),
        ("location-the-brass-monkey", "location", "The Brass Monkey", [
            ("description", "Pub in Northbridge, Perth")]),
        ("location-little-crema", "location", "Little Crema", [
            ("description", "Café in Leederville")]),
        ("dayplan-" + day(-3), "dayplan", f"Day plan {day(-3)}", [
            ("date", day(-3)),
            ("description", "Dentist 9:00; Audit call with Marcus 14:00")]),
        ("dayplan-" + day(-10), "dayplan", f"Day plan {day(-10)}", [
            ("date", day(-10)),
            ("description", "Quiz Night at The Brass Monkey 19:30")]),
    ]


# --------------------------------------------------------------------------
# snapshot (operator-captured production context, out of the repo)
# --------------------------------------------------------------------------

def snapshot_dir(data_dir: Path) -> Path:
    return data_dir / "evals" / "snapshot"


def load_snapshot(data_dir: Path) -> dict:
    """{'self_claims': [...], 'skills_dir': Path|None, 'mcp_servers': [...]|None}.

    The snapshot (bob eval snapshot) carries production context a world
    can't invent: Bob's learned self-practices (they ride every
    production prompt via the self-brief) and the live skills index
    (SKILL.md only — no scripts). Missing snapshot → repo defaults:
    no self-brief, the repo's core skills bundle."""
    snap = snapshot_dir(data_dir)
    claims: list[dict] = []
    p = snap / "self_claims.json"
    if p.is_file():
        claims = json.loads(p.read_text())
    skills = snap / "skills" if (snap / "skills").is_dir() else None
    mcp = None
    if (snap / "mcp_servers.json").is_file():
        mcp = [s for s in json.loads((snap / "mcp_servers.json").read_text())
               if s.get("tools")]
    sm = None
    if (snap / "self_model.json").is_file():
        sm = json.loads((snap / "self_model.json").read_text())
    return {"self_claims": claims, "skills_dir": skills, "mcp_servers": mcp,
            "self_model": sm}


def skill_sources(snapshot: dict) -> Path:
    return snapshot["skills_dir"] or (REPO_ROOT / "skills")


def fingerprint(snapshot: dict) -> str:
    """persona_hash: everything that shapes the prompt and isn't the case —
    persona bundle, fixture user.md, skill docs, self-claims, base cast."""
    h = hashlib.sha256()
    for p in sorted((REPO_ROOT / "self").rglob("*.md")):
        h.update(p.relative_to(REPO_ROOT).as_posix().encode())
        h.update(p.read_bytes())
    h.update(USER_MD.encode())
    src = skill_sources(snapshot)
    for p in sorted(src.glob("*/SKILL.md")) + sorted(src.glob("*/skill.md")):
        h.update(p.parent.name.encode())
        h.update(p.read_bytes())
    h.update(json.dumps(snapshot["self_claims"], sort_keys=True).encode())
    h.update(json.dumps(snapshot.get("mcp_servers"), sort_keys=True).encode())
    # The curated self-model shapes the prompt only when the brief renders
    # it — a flag-off run stays comparable with pre-Phase-2b baselines.
    from server.services.dream.self_model import brief_enabled, social_enabled
    if brief_enabled() and snapshot.get("self_model"):
        sm = snapshot["self_model"]
        h.update((sm.get("rendered") or "").encode())
        if social_enabled():
            h.update((sm.get("social_rendered") or "").encode())
    h.update(json.dumps([(p.name, p.phone, p.trusted) for p in CAST]).encode())
    return h.hexdigest()[:12]
