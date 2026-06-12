from __future__ import annotations

import pytest

from bob_server.services.memory.contact_directory import ContactDirectory


@pytest.mark.asyncio
async def test_directory_loads_contacts_by_uuid_and_name(memory_db):
    dir_ = await ContactDirectory.load(memory_db)

    blair = dir_.get_by_name("Blair Nicol")
    assert blair is not None
    assert blair.canonical_id == "contact-03f3902d"
    assert blair.uuid == "03f3902d-330b-4f15-bf2a-b1385a917677"
    assert blair.email == ""
    assert blair.phone_number == "+61401589328"

    by_id = dir_.get_by_canonical_id("contact-03f3902d")
    assert by_id is blair


@pytest.mark.asyncio
async def test_directory_case_insensitive_name_lookup(memory_db):
    dir_ = await ContactDirectory.load(memory_db)
    assert dir_.get_by_name("blair nicol").canonical_id == "contact-03f3902d"
    assert dir_.get_by_name("BLAIR NICOL").canonical_id == "contact-03f3902d"


@pytest.mark.asyncio
async def test_directory_first_name_only_falls_back_to_full_name_match(memory_db):
    """A first-name query returns the unique match if only one contact has that first name."""
    dir_ = await ContactDirectory.load(memory_db)
    assert dir_.get_by_name("Blair").canonical_id == "contact-03f3902d"
    assert dir_.get_by_name("Bob") is None
