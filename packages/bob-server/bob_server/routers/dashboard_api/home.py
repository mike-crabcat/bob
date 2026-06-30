"""Dashboard API: Home / activity feed."""

from __future__ import annotations

from fastapi import APIRouter

from bob_server.routers.dashboard_api._common import *  # noqa: F403,F405


router = APIRouter()


@router.get("/api/home")
async def get_home(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)

    # Active sessions
    active_sessions: list[dict[str, Any]] = []
    log_exists = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_call_log'"
    )
    msgs_exists_home = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='session_messages'"
    )
    if log_exists:
        rows = await db.fetch_all(
            """SELECT session_key,
                      COUNT(*) as call_count,
                      MAX(created_at) || 'Z' as last_activity,
                      SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed,
                      SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed,
                      ROUND(AVG(CASE WHEN latency_seconds IS NOT NULL THEN latency_seconds END), 2) as avg_latency
               FROM llm_call_log
               WHERE session_key IS NOT NULL
               GROUP BY session_key
               ORDER BY last_activity DESC
               LIMIT 50"""
        )
        for row in rows:
            key = row["session_key"]
            active_sessions.append({
                "session_key": key,
                "channel": _parse_channel(key),
                "call_count": row["call_count"],
                "completed": row["completed"],
                "failed": row["failed"],
                "avg_latency": row["avg_latency"] or 0.0,
                "last_activity": row["last_activity"],
            })
    if msgs_exists_home:
        seen = {s["session_key"] for s in active_sessions}
        msg_rows = await db.fetch_all(
            """SELECT session_key,
                      COUNT(*) as msg_count,
                      MAX(created_at) || 'Z' as last_activity
               FROM session_messages
               WHERE session_key IS NOT NULL
               GROUP BY session_key
               ORDER BY last_activity DESC
               LIMIT 50"""
        )
        for row in msg_rows:
            key = row["session_key"]
            if key not in seen:
                active_sessions.append({
                    "session_key": key,
                    "channel": _parse_channel(key),
                    "call_count": 0,
                    "completed": 0,
                    "failed": 0,
                    "avg_latency": 0.0,
                    "last_activity": row["last_activity"],
                    "msg_count": row["msg_count"],
                })
        active_sessions.sort(key=lambda s: s.get("last_activity") or "", reverse=True)

    # LLM calls chart: 24h by 15min buckets, stacked by call_category
    chart_buckets: list[dict[str, Any]] = []
    chart_categories: list[str] = []
    if log_exists:
        chart_rows = await db.fetch_all(
            """SELECT
                  strftime('%Y-%m-%dT%H:%M',
                      datetime(strftime('%s', created_at) - strftime('%s', created_at) % 900, 'unixepoch')
                  ) as interval_start,
                  call_category,
                  COUNT(*) as count
               FROM llm_call_log
               WHERE created_at >= datetime('now', '-24 hours')
               GROUP BY interval_start, call_category
               ORDER BY interval_start"""
        )
        bucket_map: dict[str, dict[str, int]] = {}
        categories: set[str] = set()
        for row in chart_rows:
            iv = row["interval_start"]
            cat = row["call_category"] or "other"
            categories.add(cat)
            bucket_map.setdefault(iv, {})[cat] = row["count"]
        if categories:
            import datetime as _dt
            now = _dt.datetime.now(_dt.timezone.utc)
            epoch = int(now.timestamp())
            start_epoch = ((epoch - 86400) // 900) * 900
            for i in range(96):
                ts = start_epoch + 900 * i
                key = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M")
                entry: dict[str, Any] = {"interval_start": key}
                for cat in sorted(categories):
                    entry[cat] = bucket_map.get(key, {}).get(cat, 0)
                chart_buckets.append(entry)
        chart_categories = sorted(categories)

    # Bulletin total (still surfaced in the stats box even though the
    # bulletins widget itself is gone — the pipeline hasn't fully retired)
    bulletin_count = 0
    bulletins_table = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_bulletins'"
    )
    if bulletins_table:
        b_total = await db.fetch_one("SELECT COUNT(*) AS c FROM memory_bulletins")
        bulletin_count = (b_total["c"] if b_total else 0) or 0

    # Active entity count + recent memory activity (claims are the atomic unit
    # now that extraction is going silent-per-turn; each row surfaces the
    # subject entity plus object/value so the feed shows what was just learned)
    entity_count = 0
    recent_memory: list[dict[str, Any]] = []
    entities_table = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_entities'"
    )
    if entities_table:
        e_total = await db.fetch_one(
            "SELECT COUNT(*) AS c FROM memory_entities WHERE status = 'active'"
        )
        entity_count = (e_total["c"] if e_total else 0) or 0

        claims_table = await db.fetch_one(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_claims'"
        )
        if claims_table:
            c_rows = await db.fetch_all(
                """SELECT c.id, c.claim_type_key, c.subject_id, c.object_id, c.value,
                          c.created_at,
                          se.display_name AS subject_name,
                          se.entity_type  AS subject_type,
                          oe.display_name AS object_name,
                          oe.entity_type  AS object_type
                   FROM memory_claims c
                   LEFT JOIN memory_entities se ON se.entity_id = c.subject_id
                   LEFT JOIN memory_entities oe ON oe.entity_id = c.object_id
                   WHERE c.status = 'active'
                   ORDER BY c.created_at DESC
                   LIMIT 10"""
            )
            for row in c_rows:
                recent_memory.append({
                    "id": row["id"],
                    "claim_type": row["claim_type_key"],
                    "subject_id": row["subject_id"],
                    "subject_name": row["subject_name"] or row["subject_id"],
                    "subject_type": row["subject_type"],
                    "object_id": row["object_id"],
                    "object_name": row["object_name"] if row["object_id"] else None,
                    "object_type": row["object_type"],
                    "value": row["value"],
                    "created_at": _utc(row["created_at"]),
                })

    # Estimated 24h costs by call category
    cost_by_category: list[dict[str, Any]] = []
    total_cost_24h = 0.0
    if log_exists:
        cost_rows = await db.fetch_all(
            """SELECT call_category, model,
                      SUM(COALESCE(prompt_tokens, 0)) as total_prompt_tokens,
                      SUM(COALESCE(completion_tokens, 0)) as total_completion_tokens,
                      SUM(COALESCE(cached_tokens, 0)) as total_cached_tokens,
                      COUNT(*) as call_count
               FROM llm_call_log
               WHERE created_at >= datetime('now', '-24 hours')
               GROUP BY call_category, model
               ORDER BY call_category, model"""
        )
        # Pricing per 1M tokens (input, output). Cached input is billed at 10%
        # of the input rate (OpenAI's 90% prompt-cache discount).
        _PRICING: dict[str, tuple[float, float]] = {
            "gpt-5.4-mini": (0.75, 4.50),
            "gpt-5.5": (5.00, 30.00),
        }
        category_totals: dict[str, dict[str, Any]] = {}
        for row in cost_rows:
            cat = row["call_category"] or "other"
            model = row["model"] or "gpt-5.4-mini"
            prompt = row["total_prompt_tokens"] or 0
            completion = row["total_completion_tokens"] or 0
            cached = row["total_cached_tokens"] or 0
            rate_in, rate_out = _PRICING.get(model, _PRICING.get("gpt-5.4-mini", (0.75, 4.50)))
            cost = ((prompt - cached) * rate_in + cached * rate_in * 0.1 + completion * rate_out) / 1_000_000
            cost = max(cost, 0)
            if cat not in category_totals:
                category_totals[cat] = {"category": cat, "cost": 0.0, "call_count": 0, "prompt_tokens": 0, "completion_tokens": 0}
            category_totals[cat]["cost"] += cost
            category_totals[cat]["call_count"] += row["call_count"] or 0
            category_totals[cat]["prompt_tokens"] += prompt
            category_totals[cat]["completion_tokens"] += completion
        cost_by_category = sorted(category_totals.values(), key=lambda x: x["cost"], reverse=True)
        total_cost_24h = round(sum(c["cost"] for c in cost_by_category), 4)
        for c in cost_by_category:
            c["cost"] = round(c["cost"], 4)

    return {
        "active_sessions": active_sessions,
        "chart_buckets": chart_buckets,
        "chart_categories": chart_categories,
        "recent_memory": recent_memory,
        "entity_count": entity_count,
        "bulletin_count": bulletin_count,
        "cost_by_category": cost_by_category,
        "total_cost_24h": total_cost_24h,
    }


