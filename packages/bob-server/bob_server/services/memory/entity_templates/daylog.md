# {{ display_name }} [{{ entity_id }}]

{% set has_details = claims.get("date") or claims.get("notes") or claims.get("associated_trip") %}
{% if has_details %}
## Details

{% for c in claims.get("date", []) %}- **Date:** {{ c.value }}
{% endfor %}
{% for c in claims.get("notes", []) %}- **Notes:** {{ c.value }}
{% endfor %}
{% for c in claims.get("associated_trip", []) %}- **Trip:** {{ resolved.get(c.object_id, {}).get("display_name", c.value or c.object_id) if c.object_id else c.value }}
{% endfor %}
{% endif %}
{% set media = claims.get("media_ref", []) %}
{% if media %}
## Media

{% for c in media %}- {{ c.value }}
{% endfor %}{% endif %}
{% set attractions = claims.get("attraction", []) %}
{% if attractions %}
## Attractions

{% for c in attractions %}- {{ resolved.get(c.object_id, {}).get("display_name", c.value or c.object_id) if c.object_id else c.value }}
{% endfor %}{% endif %}
{% if orphans %}
## Additional

{% for key, vals in orphans | dictsort %}
{% if vals | length == 1 %}- **{{ key }}:** {{ vals[0].value or vals[0].object_id }}
{% else %}- **{{ key }}:**
{% for v in vals %}  - {{ v.value or v.object_id }}
{% endfor %}
{% endif %}
{% endfor %}
{% endif %}
