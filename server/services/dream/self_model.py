"""Nightly self-curation → curated self-model (docs/prompt-layout-plan.md
Phase 2b, 2026-10-11).

The raw self-brief rendered self-bob claims newest-first under a 4k cap:
~5 of 147 practices and none of 16 limits ever reached the prompt, states
ate the budget, and stale claims ("no tool lists groups", "GLM is the
default") stayed in force. Once per night (the first dream run at/after
02:00 local) one call on a strong model reads EVERYTHING — all active self
claims, the persona (so nothing is restated), and ground truth (live tool
names, skills, model roles) — and writes a short structured self-model:

  state (≤6) · practices (≤12) · limits (≤6) · watch-outs (≤4)
  + housekeeping: stale/contradicted self claims retired (reversible,
    reason recorded); tool how-tos proposed for their skill docs
  + optional per-GROUP social read (≤3, calibration not feelings) and a
    fenced one-line "tone today"

The self-brief renders the newest ready version (< 48h) plus an in-day
delta of key claims written since — behind BOB_SELF_MODEL_BRIEF (off until
the eval A/B), social read behind BOB_SELF_MODEL_SOCIAL (off). Curation
itself runs whenever the dream runs (BOB_SELF_CURATION, default on) so
versions accumulate for the A/B. A one-line digest reaches Mike's DM with
the first dream run at/after 08:00.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CURATION_HOUR = 2          # local: first dream run at/after this curates
DIGEST_HOUR = 8            # local: first dream run at/after this sends the digest
MAX_AGE = timedelta(hours=48)
CAPS = {"state": 6, "practices": 12, "limits": 6, "watchouts": 4, "social": 3}
RENDER_MAX = 3400
ITEM_MAX = 230            # chars per bullet — a rule, not a paragraph
SECTION_BUDGET = {"state": 800, "practices": 1500, "limits": 600, "watchouts": 500}
DELTA_TYPES = ("practice", "limit", "self_state", "feedback")
DELTA_MAX_ITEMS, DELTA_MAX_CHARS = 8, 800


def _flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() not in ("off", "0", "false", "no")


def curation_enabled() -> bool:
    return _flag("BOB_SELF_CURATION", "on")


def brief_enabled() -> bool:
    return _flag("BOB_SELF_MODEL_BRIEF", "off")


def social_enabled() -> bool:
    return _flag("BOB_SELF_MODEL_SOCIAL", "off")


def curation_model(ctx: Any) -> str:
    return os.environ.get("BOB_SELF_CURATION_MODEL", "z-ai/glm-5.3")


def _now() -> datetime:
    return datetime.now()  # local wall clock — memory claims use the same


# ---------------------------------------------------------------- inputs

async def _ground_truth(ctx: Any) -> dict[str, Any]:
    """What is actually true about Bob's setup right now — the curator uses
    it to retire claims the code has outgrown."""
    out: dict[str, Any] = {}
    try:
        from server.services.tool_registry import build_common_tools
        tools = build_common_tools(ctx, session_key="agent:main:whatsapp:dm:0",
                                   is_trusted=True, contact_id=None)
        out["tools"] = sorted({t.name for t in tools})
    except Exception:
        logger.warning("self-curation: tool list unavailable", exc_info=True)
    try:
        skills_dir = Path(ctx.settings.harness.workspace_dir).expanduser() / "skills"
        skills = []
        for d in sorted(skills_dir.iterdir()):
            md = d / "skill.md" if (d / "skill.md").is_file() else d / "SKILL.md"
            if md.is_file():
                desc = ""
                for line in md.read_text(errors="replace").splitlines()[:8]:
                    if line.startswith("description:"):
                        desc = line.split(":", 1)[1].strip()[:140]
                skills.append(f"{d.name}: {desc}")
        out["skills"] = skills
    except Exception:
        logger.warning("self-curation: skills list unavailable", exc_info=True)
    try:
        from server.repositories.bg_jobs import BgJobsRepository
        running = await BgJobsRepository(ctx.db).running_rows()
        out["running_services"] = sorted(
            f"{r['name']}: {(r.get('description') or '')[:100]}" for r in running)
    except Exception:
        logger.warning("self-curation: running services unavailable", exc_info=True)
    try:
        import yaml
        cfg = yaml.safe_load((Path(ctx.settings.config_dir).expanduser()
                              / "models.yaml").read_text()) or {}
        out["model_aliases"] = cfg.get("aliases") or {}
        out["model_roles"] = cfg.get("roles") or {}
        out["default_model"] = ctx.settings.openai.default_model
    except Exception:
        pass
    return out


async def _social_signals(ctx: Any, *, hours: int = 48, max_chars: int = 9000) -> str:
    """Per group, last ``hours``: reactions to Bob's messages and the next
    few human messages after each Bob message. Collection only — the
    curator interprets. Names stay out (group-level read, never per person)."""
    from server.repositories.groups import GroupRepository
    from server.repositories.history import HistoryRepository
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    hist = HistoryRepository(ctx.db)
    lines: list[str] = []
    for g in await GroupRepository(ctx.db).actively_bound_groups():
        key = f"agent:main:whatsapp:group:{g['whatsapp_jid'].split('@')[0]}"
        try:
            rows = await hist.social_rows(key, since_utc=since)
        except Exception:
            continue
        reactions: dict[str, int] = {}
        replies: list[str] = []
        after_bob = 0
        for r in rows:
            prov = r.get("provenance") or ""
            if r["role"] == "assistant":
                if prov not in ("extraction_marker",):
                    after_bob = 3
                continue
            if prov == "wa_reaction":
                try:
                    meta = json.loads(r.get("metadata") or "{}").get("reaction") or {}
                except (TypeError, ValueError):
                    meta = {}
                if meta.get("target_role") == "assistant" and meta.get("emoji"):
                    reactions[meta["emoji"]] = reactions.get(meta["emoji"], 0) + 1
                continue
            if not prov and after_bob:
                replies.append(" ".join(str(r.get("content") or "").split())[:160])
                after_bob -= 1
        if reactions or replies:
            lines.append(f"[{g['name']}] last {hours}h — reactions to Bob: "
                         + (", ".join(f"{e}×{n}" for e, n in reactions.items()) or "none"))
            lines.extend(f"  reply: {t}" for t in replies[-20:])
    return "\n".join(lines)[-max_chars:]


# ---------------------------------------------------------------- curate

_SYSTEM = """You curate the SELF-MODEL of Bob, an AI agent who lives in WhatsApp
groups and DMs. Each night you rewrite it from everything Bob has learned
about himself. Bob reads the result at the top of every turn, so it must be
short, current, true, and ranked by what matters most.

You get: (1) every active self claim (id, type, date, text); (2) Bob's
persona (do NOT restate anything it already says); (3) GROUND TRUTH about his
current setup (live tool names, skills, model roles). Ground truth beats
claims: a claim that says a tool/skill/model doesn't exist when it does, or
that a setup fact holds when the ground truth says otherwise, is STALE.

Write:
Every bullet is ONE short sentence (≤ 30 words): a rule or a fact, never a
paragraph or a list of sub-rules.
Scope: a nickname, in-joke, word rule or arrangement that belongs to ONE
group or person is NOT global state — keep it out of state (it lives in that
group's memory); only a rule Bob must apply everywhere belongs here.
- state: ≤6 bullets — what is true about Bob's current setup/situation now.
  Only facts consistent with the ground truth. Date-stamp time-sensitive ones.
- practices: ≤12 standing rules, MERGED (combine near-duplicates), ranked by
  importance × how often they apply. Rules, not stories. Generalise one-off
  incidents into the rule they teach; drop pure tool how-tos (see below).
- limits: ≤6 real, current limits. CHECK EACH against the ground truth before
  keeping it: "no tool does X" is stale when a tool for X is in the tool list
  (e.g. list_groups lists WhatsApp groups); "Y isn't deployed/running" is
  stale when Y is in running_services. Stale limits go in retire, not here.
- watchouts: ≤4 things to be careful about THIS WEEK, from recent incidents
  and corrections (feedback/incident claims of the last ~7 days).
- retire: ONLY claims that are (a) STALE — contradicted by the ground truth
  or by a newer claim; reason starts "stale:" — or (b) an exact DUPLICATE
  of another active claim saying the same thing; reason "duplicate of
  <that claim's id>". Merging or generalising a claim into the self-model is
  NOT a reason to retire it — the raw claims stay for recall. Never retire
  self_image claims. Expect a handful per night, not dozens.
- move_to_skill: claims that are tool/skill how-tos rather than standing
  practice — [{"id", "skill", "note"}] (proposed only; not applied).
{social_spec}
Return ONLY JSON:
{"state": [str], "practices": [str], "limits": [str], "watchouts": [str],
 "retire": [{"id": str, "reason": str}],
 "move_to_skill": [{"id": str, "skill": str, "note": str}]{social_keys}}"""

_SOCIAL_SPEC = """- social: ≤3 bullets, one per GROUP (never per person), from the social
  signals provided: how Bob's contributions are landing there, citing the
  observed signals (reactions, mockery/praise, corrections, ignored posts)
  and ENDING in a calibration for Bob's behaviour ("show photo evidence
  before verdicts"), never a feeling. Omit groups with too little signal.
- tone_today: one short line of playful flavour for Bob's tone (e.g. "dry, a
  little smug after the re-judge landed", "a bit stung after the roast") or ""
  if nothing stands out. Negative is fine when the signals say so; it colours
  tone only.
"""


def _user_payload(claims: list[dict], persona: str, truth: dict, social: str) -> str:
    claim_lines = [f"{c['id']} | {c['claim_type_key']} | {str(c['created_at'])[:10]} | "
                   f"{' '.join(str(c['value']).split())[:600]}" for c in claims]
    parts = [
        "## Active self claims (id | type | date | text)", *claim_lines, "",
        "## Ground truth (current setup)", json.dumps(truth, indent=1)[:12000], "",
        "## Persona (already in Bob's prompt — do not restate)", persona[:14000],
    ]
    if social:
        parts += ["", "## Social signals (per group)", social]
    return "\n".join(parts)


MAX_RETIRE = 10   # per night — 2026-10-11 first live run retired 56 as "merged"


def _valid_retire_reason(reason: str, cid: str, claim_ids: set[str]) -> bool:
    """Stale (with a reason) or a duplicate of ANOTHER active claim, named.
    'Merged into practice' is not a retirement reason (lossy for recall)."""
    r = reason.strip().lower()
    if r.startswith("stale") and len(r) > len("stale:") + 5:
        return True
    if r.startswith("duplicate of "):
        other = reason.strip()[len("duplicate of "):].split()[0].rstrip(".,;:")
        return other in claim_ids and other != cid
    return False


def validate(raw: Any, claim_ids: set[str], *, social: bool) -> dict[str, Any]:
    """Clamp the curator's JSON to the contract; drop unknown claim ids."""
    if not isinstance(raw, dict):
        raise ValueError("curation output is not a JSON object")

    def strs(key: str) -> list[str]:
        vals = raw.get(key) or []
        if not isinstance(vals, list):
            return []
        out_: list[str] = []
        for v in vals:
            t = " ".join(str(v).split())
            if not t:
                continue
            if len(t) > ITEM_MAX:
                t = t[:ITEM_MAX].rsplit(" ", 1)[0].rstrip(",;:") + " …"
            out_.append(t)
        return out_[:CAPS[key]]

    out: dict[str, Any] = {k: strs(k) for k in ("state", "practices", "limits", "watchouts")}
    out["retire"] = [{"id": str(r["id"]), "reason": str(r.get("reason") or "")[:300]}
                     for r in (raw.get("retire") or [])
                     if isinstance(r, dict) and str(r.get("id")) in claim_ids
                     and _valid_retire_reason(str(r.get("reason") or ""),
                                              str(r["id"]), claim_ids)][:MAX_RETIRE]
    out["move_to_skill"] = [{"id": str(r["id"]), "skill": str(r.get("skill") or "?"),
                             "note": str(r.get("note") or "")[:200]}
                            for r in (raw.get("move_to_skill") or [])
                            if isinstance(r, dict) and str(r.get("id")) in claim_ids]
    if social:
        out["social"] = strs("social")
        out["tone_today"] = " ".join(str(raw.get("tone_today") or "").split())[:160]
    if not out["practices"] and not out["state"]:
        raise ValueError("curation output has no state and no practices")
    return out


def render(curated: dict[str, Any]) -> str:
    sections = [("Current state", "state"), ("How I work", "practices"),
                ("Known limits", "limits"), ("Watch-outs this week", "watchouts")]
    lines: list[str] = []
    for title, key in sections:
        if not curated.get(key):
            continue
        lines.append(f"{title}:")
        used = 0
        for v in curated[key]:
            if used + len(v) > SECTION_BUDGET.get(key, 600):
                break  # per-section budget: no section can starve the next
            lines.append(f"- {v}")
            used += len(v)
    text = "\n".join(lines)
    return text if len(text) <= RENDER_MAX else text[:RENDER_MAX].rsplit("\n", 1)[0]


def render_social(curated: dict[str, Any]) -> str:
    if not curated.get("social") and not curated.get("tone_today"):
        return ""
    lines = ["Social read (how I'm landing — calibration, not feelings):"]
    lines += [f"- {v}" for v in curated.get("social") or []]
    if curated.get("tone_today"):
        lines.append(f"- Tone today: {curated['tone_today']} (colours tone only — "
                     "never effort, helpfulness or accuracy; don't mention it "
                     "unless asked how I am)")
    return "\n".join(lines)


def digest_line(version_no: int, curated: dict, retired: int) -> str:
    return (f"Self-model v{version_no} curated overnight: {len(curated.get('practices', []))} "
            f"practices, {len(curated.get('limits', []))} limits, "
            f"{len(curated.get('watchouts', []))} watch-outs; retired {retired} stale self "
            f"claim(s); {len(curated.get('move_to_skill', []))} how-to(s) proposed for skill "
            f"docs. (bob dream self-model to inspect)")


async def curate(ctx: Any, *, apply: bool = True) -> dict[str, Any]:
    """Run one curation now. Returns the stored row (or the would-be row
    when ``apply`` is False — nothing written, nothing retired)."""
    from server.services.llm_dispatch import LLMDispatchService
    from server.services.llm_json import parse_llm_json
    from server.services.memory.admin import retire_self_claims, self_claims_for_curation
    from server.services.persona import render_persona

    claims = await self_claims_for_curation(ctx.db)
    claim_ids = {c["id"] for c in claims}
    persona = render_persona(ctx.settings.harness.workspace_dir)
    truth = await _ground_truth(ctx)
    # The social read is always CURATED (so versions carry it for the A/B);
    # whether Bob SEES it is BOB_SELF_MODEL_SOCIAL at render time.
    social = await _social_signals(ctx) if _flag("BOB_SELF_MODEL_SOCIAL_CURATE", "on") else ""
    system = _SYSTEM.replace("{social_spec}", _SOCIAL_SPEC if social else "").replace(
        "{social_keys}", ',\n "social": [str], "tone_today": str' if social else "")
    model = curation_model(ctx)
    now = _now()
    row: dict[str, Any] = {"id": f"sm-{uuid.uuid4().hex[:12]}",
                           "created_at": now.isoformat(), "local_date": now.date().isoformat(),
                           "model": model, "source_counts_json": json.dumps(
                               {"claims": len(claims), "social_chars": len(social)})}
    try:
        text = await LLMDispatchService(ctx).prompt(
            [{"role": "system", "content": system},
             {"role": "user", "content": _user_payload(claims, persona, truth, social)}],
            model=model, temperature=0.0, max_tokens=8000,
            reasoning_effort="medium", call_category="dream_self_curation")
        curated = validate(parse_llm_json(text), claim_ids, social=bool(social))
    except Exception as exc:
        row.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:500])
        if apply:
            await _store(ctx, row)
        logger.warning("self-curation failed: %s", row["error"])
        return row

    retired = 0
    if apply and curated["retire"]:
        retired = await retire_self_claims(
            ctx.db, [(r["id"], r["reason"]) for r in curated["retire"]],
            ref=f"dream_curation:{row['id']}")
    version_no = await _version_count(ctx) + 1
    row.update(status="ready", curated_json=json.dumps(curated, ensure_ascii=False),
               rendered=render(curated), social_rendered=render_social(curated),
               housekeeping_json=json.dumps({"retired": curated["retire"],
                                             "retired_applied": retired,
                                             "move_to_skill": curated["move_to_skill"]},
                                            ensure_ascii=False),
               digest=digest_line(version_no, curated, retired))
    if apply:
        await _store(ctx, row)
        from server.services.memory.self_brief import reset_cache
        reset_cache()
    logger.info("self-curation %s: %s", row["id"], row["digest"])
    return row


# ---------------------------------------------------------------- store

_COLS = ("id", "created_at", "local_date", "model", "status", "curated_json",
         "rendered", "social_rendered", "housekeeping_json", "source_counts_json",
         "digest", "digest_sent_at", "error")


async def _store(ctx: Any, row: dict[str, Any]) -> None:
    await ctx.db.execute(
        f"INSERT INTO dream_self_model ({', '.join(_COLS)}) VALUES "
        f"({', '.join('?' * len(_COLS))})", tuple(row.get(c) for c in _COLS))


def export_latest_readonly(conn: Any) -> dict[str, Any] | None:
    """Newest ready version from an operator-opened read-only sqlite3
    connection (bob eval snapshot). Table missing → None."""
    import sqlite3
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM dream_self_model WHERE status = 'ready' "
            "ORDER BY created_at DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(row) if row else None


async def seed_version(db: Any, row: dict[str, Any]) -> None:
    """Insert a snapshot version into a (world) DB, stamped NOW so the
    in-day delta starts empty."""
    now = _now()
    vals = {**row, "created_at": now.isoformat(), "local_date": now.date().isoformat(),
            "digest_sent_at": now.isoformat()}
    await db.execute(
        f"INSERT INTO dream_self_model ({', '.join(_COLS)}) VALUES "
        f"({', '.join('?' * len(_COLS))})", tuple(vals.get(c) for c in _COLS))


async def _version_count(ctx: Any) -> int:
    row = await ctx.db.fetch_one(
        "SELECT COUNT(*) AS n FROM dream_self_model WHERE status = 'ready'")
    return int(row["n"]) if row else 0


async def latest_ready(db: Any) -> dict[str, Any] | None:
    row = await db.fetch_one(
        "SELECT * FROM dream_self_model WHERE status = 'ready' "
        "ORDER BY created_at DESC LIMIT 1")
    return dict(row) if row else None


async def latest_any_today(db: Any, local_date: str) -> dict[str, Any] | None:
    row = await db.fetch_one(
        "SELECT * FROM dream_self_model WHERE local_date = ? "
        "ORDER BY created_at DESC LIMIT 1", (local_date,))
    return dict(row) if row else None


async def failures_today(db: Any, local_date: str) -> int:
    row = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM dream_self_model WHERE local_date = ? "
        "AND status = 'failed'", (local_date,))
    return int(row["n"]) if row else 0


# ---------------------------------------------------------------- dream hooks

async def run_if_due(ctx: Any) -> dict[str, Any] | None:
    """Called from every dream run: curate once per local day, at/after
    CURATION_HOUR (a failed attempt retries on the next run, max 3/day)."""
    if not curation_enabled():
        return None
    now = _now()
    if now.hour < CURATION_HOUR:
        return None
    today = now.date().isoformat()
    existing = await latest_any_today(ctx.db, today)
    if existing and existing["status"] == "ready":
        return None
    if await failures_today(ctx.db, today) >= 3:
        return None
    return await curate(ctx)


async def deliver_digest_if_due(ctx: Any) -> bool:
    """Send today's one-line digest to the owner's DM with the first dream
    run at/after DIGEST_HOUR."""
    if not _flag("BOB_SELF_MODEL_DIGEST", "on"):
        return False
    now = _now()
    if now.hour < DIGEST_HOUR:
        return False
    row = await latest_any_today(ctx.db, now.date().isoformat())
    if not row or row["status"] != "ready" or row.get("digest_sent_at") or not row.get("digest"):
        return False
    from server.services.effects import emit_and_deliver
    from server.services.steering import owner_contact
    owner = await owner_contact(ctx)
    phone = "".join(ch for ch in str((owner or {}).get("phone_number") or "") if ch.isdigit())
    if not phone:
        return False
    result = await emit_and_deliver(
        ctx, kind="whatsapp_send", idempotency_key=f"self_model_digest:{row['id']}",
        payload={"chat_id": f"{phone}@s.whatsapp.net", "text": row["digest"]})
    if result.get("ok"):
        await ctx.db.execute(
            "UPDATE dream_self_model SET digest_sent_at = ? WHERE id = ?",
            (now.isoformat(), row["id"]))
        return True
    return False


# ---------------------------------------------------------------- self-brief

async def curated_brief(db: Any, *, now: datetime | None = None) -> str | None:
    """The curated self-brief (None → caller falls back to the raw
    renderer): newest ready version < 48h old + in-day delta + optional
    social read."""
    from server.services.memory.admin import self_claims_since
    from server.services.memory.self_brief import _first_sentence
    row = await latest_ready(db)
    if not row or not row.get("rendered"):
        return None
    now = now or _now()
    try:
        made = datetime.fromisoformat(row["created_at"])
    except ValueError:
        return None
    if now - made > MAX_AGE:
        return None
    parts = ["## Self-Brief — your current self-model (curated nightly from memory)",
             row["rendered"]]
    if social_enabled() and row.get("social_rendered"):
        parts.append(row["social_rendered"])
    delta = await self_claims_since(db, row["created_at"], DELTA_TYPES,
                                    limit=DELTA_MAX_ITEMS)
    if delta:
        lines, used = ["Since last night (new, not yet curated):"], 0
        for c in delta:
            line = f"- [{c['claim_type_key']}] {_first_sentence(c['value'])}"
            if used + len(line) > DELTA_MAX_CHARS:
                break
            lines.append(line)
            used += len(line)
        if len(lines) > 1:
            parts.append("\n".join(lines))
    return "\n".join(parts)
