"""Goal templates — event plans as data (bob-events-plan.md §3.5).

"Organise X for group Y" instantiates a default child DAG with decision
rules in strategy data; the LLM adapts via the normal goal tools afterwards.
Templates ship as data (this dict), and ``{config_dir}/goal_templates/*.json``
adds or overrides — so the playbook is editable without code changes.

String values are ``str.format``-ed with the instantiation params. Derived
params are computed here (slugs, defaults) so callers pass only the obvious
four: event_name, group_name, group_session_key, decide_by.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from bob_server.context import AppContext

logger = logging.getLogger(__name__)

DEFAULT_TEMPLATES: dict[str, dict[str, Any]] = {
    "team-event": {
        "description": (
            "Plan a group event end to end: negotiate a time with the roster, "
            "shortlist venues against dietary needs, poll the group, book by "
            "phone, remind attendees (T-24h / T-2h), and produce merch behind "
            "the human-approved payment gate."),
        "params": ["event_name", "group_name", "group_session_key", "decide_by"],
        "root": {
            "objective": "Organise {event_name} for {group_name}",
            "kind": "event_plan",
            "strategy": {
                "v": 2,
                "plan": (
                    "1) Fan out availability DMs (parent each outreach under "
                    "the negotiate child; also collect t-shirt sizes). "
                    "2) Shortlist venues against dietary claims; poll "
                    "finalists in the group. 3) Book the winner by phone "
                    "(voice subagent, goal_parent_id = the book child); on "
                    "success write the event entity + attendance claims. "
                    "4) Remind at T-24h and T-2h via schedule_goal_wakeup. "
                    "5) Designs to the group; order merch ONLY after an "
                    "approved purchase approval."),
                "known": [],
                "open_questions": [],
                "next_actions": [],
                "refs": {"entities": ["group-{group_slug}", "event-{event_slug}"],
                         "claims": []},
            },
        },
        "children": [
            {
                "key": "negotiate",
                "objective": "Negotiate a time for {event_name} "
                             "(quorum of invitees by {decide_by})",
                "kind": "negotiate",
                "strategy": {
                    "decision": {"quorum": "{quorum}", "of": "invitees",
                                 "decide_by": "{decide_by}"},
                },
            },
            {
                "key": "venue",
                "objective": "Shortlist venues for {event_name} against "
                             "dietary requirements",
                "kind": "task",
                "strategy": {},
            },
            {
                "key": "book",
                "objective": "Book the chosen venue for the decided slot "
                             "(phone; landline echo is a known failure mode)",
                "kind": "call",
                "strategy": {},
            },
            {
                "key": "remind",
                "objective": "Remind attendees at T-24h and T-2h before "
                             "{event_name}",
                "kind": "task",
                "strategy": {},
            },
            {
                "key": "merch",
                "objective": "Design, get approval, and order {event_name} "
                             "merch",
                "kind": "task",
                "strategy": {
                    "payment_gate": (
                        "the order requires an approved `purchase` approval "
                        "(respond_approval in the origin conversation); "
                        "never spend without it"),
                },
            },
        ],
        "holders": ["{group_session_key}"],
    },
}


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or "x"


_SINGLE_PLACEHOLDER = re.compile(r"^\{([a-z_][a-z0-9_]*)\}$")


def _walk_format(value: Any, params: dict[str, Any], missing: set[str]) -> Any:
    if isinstance(value, str):
        # A string that IS a single placeholder keeps the param's type
        # (numbers stay numbers — e.g. decision-rule quorum).
        single = _SINGLE_PLACEHOLDER.match(value)
        if single:
            name = single.group(1)
            if name in params:
                return params[name]
            missing.add(name)
            return value
        try:
            return value.format(**params)
        except KeyError as exc:
            missing.add(str(exc).strip("'"))
            return value
    if isinstance(value, list):
        return [_walk_format(v, params, missing) for v in value]
    if isinstance(value, dict):
        return {k: _walk_format(v, params, missing) for k, v in value.items()}
    return value


def load_templates(config_dir: Any) -> dict[str, dict[str, Any]]:
    """Defaults + any JSON templates in ``{config_dir}/goal_templates/``."""
    templates = {name: dict(tpl) for name, tpl in DEFAULT_TEMPLATES.items()}
    tdir = config_dir / "goal_templates"
    try:
        for path in sorted(tdir.glob("*.json")):
            try:
                tpl = json.loads(path.read_text())
                name = tpl.get("name") or path.stem
                templates[name] = tpl
            except (OSError, ValueError):
                logger.warning("skipping unreadable goal template %s", path)
    except Exception:
        pass
    return templates


async def instantiate_template(
    ctx: AppContext, *, template_name: str, session_key: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Create the template's goal tree under ``session_key`` (the working
    conversation). Registers holder conversations (e.g. the group chat) so
    Phase-2 routing and §2.0 seeding cover replies given there."""
    from bob_server.repositories.conversations import ConversationRepository
    from bob_server.repositories.goals import GoalRepository
    from bob_server.services import goal_service

    templates = load_templates(ctx.settings.config_dir)
    tpl = templates.get(template_name)
    if tpl is None:
        raise ValueError(
            f"unknown goal template {template_name!r}; available: "
            f"{sorted(templates)}")

    merged = dict(params)
    merged.setdefault("quorum", 0.75)
    merged.setdefault("group_slug", _slugify(str(params.get("group_name", ""))))
    merged.setdefault("event_slug", _slugify(str(params.get("event_name", ""))))

    missing: set[str] = set()
    spec = _walk_format(tpl, merged, missing)
    if missing:
        raise ValueError(
            f"template {template_name!r} missing required params: {sorted(missing)} "
            f"(needs {tpl.get('params', [])})")

    root_spec = spec["root"]
    root = await goal_service.create_goal(
        ctx, conversation_id=session_key,
        objective=root_spec["objective"],
        kind=root_spec.get("kind", "task"),
        strategy=root_spec.get("strategy") or None)

    repo = GoalRepository(ctx.db)
    children: dict[str, str] = {}
    root_refs = (root_spec.get("strategy") or {}).get("refs")
    for child_spec in spec.get("children", []):
        child_strategy = dict(child_spec.get("strategy") or {})
        # Children inherit the root's entity refs so the claim router's ref
        # match reaches the child that accumulates the information (e.g.
        # group-chat attendance folds into the negotiate child, not just the
        # root) — otherwise replies fragment across the tree.
        if root_refs and "refs" not in child_strategy:
            child_strategy["refs"] = dict(root_refs)
        # v2 envelope so the state stays a first-class strategy (decision
        # rules ride as extra keys), not legacy-wrapped residue.
        child_strategy.setdefault("v", 2)
        child = await goal_service.create_goal(
            ctx, conversation_id=session_key,
            objective=child_spec["objective"],
            kind=child_spec.get("kind", "task"),
            strategy=child_strategy or None,
            parent_goal_id=root["id"])
        children[child_spec["key"]] = child["id"]

    conv_repo = ConversationRepository(ctx.db)
    for holder_key in spec.get("holders", []):
        if not isinstance(holder_key, str) or not holder_key:
            continue
        cid = await conv_repo.resolve_cid(holder_key)
        await repo.add_holder(root["id"], cid, role="holder")

    return {"template": template_name, "root_goal_id": root["id"],
            "children": children, "holders": spec.get("holders", [])}
