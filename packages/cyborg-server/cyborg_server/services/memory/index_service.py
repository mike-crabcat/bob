"""Index service — build and maintain derived lookup structures."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from cyborg_server.services.memory.models import ENTITY_CATEGORIES, parse_frontmatter

logger = logging.getLogger(__name__)


def build_entity_map(memory_dir: Path) -> dict[str, dict[str, str]]:
    """Scan entities/ directory and build entity-map.yml.

    Returns dict mapping entity_id -> {"entity_type": ..., "display_name": ..., "path": ...}
    Also writes to memory/indexes/entity-map.yml.
    """
    entities_dir = memory_dir / "entities"
    entity_map: dict[str, dict[str, str]] = {}

    if not entities_dir.is_dir():
        return entity_map

    for type_dir in entities_dir.iterdir():
        if not type_dir.is_dir():
            continue
        entity_type = type_dir.name
        for md_file in type_dir.glob("*.md"):
            text = md_file.read_text(encoding="utf-8")
            fm, _ = parse_frontmatter(text)
            entity_id = fm.get("entity_id", md_file.stem)
            display_name = fm.get("display_name", "")
            entity_map[entity_id] = {
                "entity_type": entity_type,
                "display_name": display_name,
                "path": str(md_file.relative_to(memory_dir)),
            }

    _write_index(memory_dir, "entity-map.yml", entity_map)
    return entity_map


def build_reverse_links(memory_dir: Path) -> dict[str, list[str]]:
    """Scan entity Related Entities sections and build reverse-links.yml.

    Returns dict mapping entity_id -> [list of entity_ids that reference it].
    Also writes to memory/indexes/reverse-links.yml.
    """
    entities_dir = memory_dir / "entities"
    reverse_links: dict[str, list[str]] = {}

    if not entities_dir.is_dir():
        return reverse_links

    for type_dir in entities_dir.iterdir():
        if not type_dir.is_dir():
            continue
        for md_file in type_dir.glob("*.md"):
            text = md_file.read_text(encoding="utf-8")
            fm, _ = parse_frontmatter(text)
            source_id = fm.get("entity_id", md_file.stem)

            # Find Related Entities section in body
            body = text.split("## Related Entities", 1)
            if len(body) < 2:
                continue

            related_section = body[1].split("##", 1)[0]
            for line in related_section.splitlines():
                line = line.strip().strip("- ").strip()
                if line and not line.endswith(":") and not line.endswith("[]"):
                    # This is an entity ID reference
                    if line not in reverse_links:
                        reverse_links[line] = []
                    if source_id not in reverse_links[line]:
                        reverse_links[line].append(source_id)

    _write_index(memory_dir, "reverse-links.yml", reverse_links)
    return reverse_links


def build_aliases(memory_dir: Path) -> dict[str, str]:
    """Build aliases.yml from entity display names.

    Returns dict mapping display_name -> entity_id.
    Also writes to memory/aliases/aliases.yml.
    """
    entities_dir = memory_dir / "entities"
    aliases: dict[str, str] = {}

    if not entities_dir.is_dir():
        return aliases

    for type_dir in entities_dir.iterdir():
        if not type_dir.is_dir():
            continue
        for md_file in type_dir.glob("*.md"):
            text = md_file.read_text(encoding="utf-8")
            fm, _ = parse_frontmatter(text)
            entity_id = fm.get("entity_id", md_file.stem)
            display_name = fm.get("display_name", "")
            if display_name:
                aliases[display_name] = entity_id
                # Also add lowercase version
                aliases[display_name.lower()] = entity_id

    aliases_dir = memory_dir / "aliases"
    aliases_dir.mkdir(parents=True, exist_ok=True)
    aliases_path = aliases_dir / "aliases.yml"
    aliases_path.write_text(yaml.dump(aliases, default_flow_style=False, allow_unicode=True, sort_keys=True), encoding="utf-8")
    return aliases


def build_memory_index_text(memory_dir: Path) -> str:
    """Build a compact memory index for injection into system prompts.

    Lists entities by type with display names and summaries.
    """
    entities_dir = memory_dir / "entities"
    if not entities_dir.is_dir():
        return ""

    lines: list[str] = []
    for type_dir in sorted(entities_dir.iterdir()):
        if not type_dir.is_dir():
            continue
        entity_type = type_dir.name
        entries: list[str] = []
        for md_file in sorted(type_dir.glob("*.md")):
            text = md_file.read_text(encoding="utf-8")
            fm, body = parse_frontmatter(text)
            display_name = fm.get("display_name", md_file.stem)
            # Extract first line of summary
            summary = ""
            for line in body.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    summary = stripped[:80]
                    break
            entry_str = display_name
            if summary:
                entry_str += f" — {summary}"
            entries.append(entry_str)
        if entries:
            lines.append(f"**{entity_type}**: " + ", ".join(entries))

    if not lines:
        return ""

    from cyborg_server.services.memory.prompts import MEMORY_INDEX_HEADER
    return MEMORY_INDEX_HEADER + "\n\n" + "\n".join(lines)


def rebuild_all(memory_dir: Path) -> dict[str, int]:
    """Rebuild all indexes and aliases. Returns counts."""
    entity_map = build_entity_map(memory_dir)
    reverse_links = build_reverse_links(memory_dir)
    aliases = build_aliases(memory_dir)
    return {
        "entities": len(entity_map),
        "reverse_links": sum(len(v) for v in reverse_links.values()),
        "aliases": len(aliases),
    }


def _write_index(memory_dir: Path, filename: str, data: dict) -> None:
    indexes_dir = memory_dir / "indexes"
    indexes_dir.mkdir(parents=True, exist_ok=True)
    path = indexes_dir / filename
    path.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=True), encoding="utf-8")
    logger.info("Rebuilt index: %s (%d entries)", filename, len(data))
