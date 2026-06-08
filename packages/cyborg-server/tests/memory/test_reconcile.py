from __future__ import annotations

from cyborg_server.services.memory.contact_directory import ContactDirectory, ContactRecord
from cyborg_server.services.memory.reconcile import reconcile_contact_id


def _directory_with_blair() -> ContactDirectory:
    return ContactDirectory([
        ContactRecord(
            uuid="03f3902d-330b-4f15-bf2a-b1385a917677",
            canonical_id="contact-03f3902d",
            name="Blair Nicol",
            phone_number="+61401589328",
            email="",
        ),
    ])


def test_canonical_id_unchanged():
    dir_ = _directory_with_blair()
    assert reconcile_contact_id("contact-03f3902d", "Blair Nicol", dir_) == "contact-03f3902d"


def test_name_slug_resolved_via_display_name():
    dir_ = _directory_with_blair()
    assert reconcile_contact_id("contact-blair-nicol", "Blair Nicol", dir_) == "contact-03f3902d"


def test_unresolved_resolved_via_display_name():
    dir_ = _directory_with_blair()
    assert reconcile_contact_id("unresolved-contact-blair", "Blair Nicol", dir_) == "contact-03f3902d"


def test_unknown_prefix_resolved_via_display_name():
    dir_ = _directory_with_blair()
    assert reconcile_contact_id("contact-unknown-blair", "Blair", dir_) == "contact-03f3902d"


def test_first_name_only_resolves_when_unique():
    dir_ = _directory_with_blair()
    assert reconcile_contact_id("contact-blair", "Blair", dir_) == "contact-03f3902d"


def test_no_directory_returns_input_unchanged():
    assert reconcile_contact_id("contact-blair-nicol", "Blair Nicol", None) == "contact-blair-nicol"


def test_slug_fallback_resolves_without_display_name():
    """When no display_name is provided, extract name from the slug itself."""
    dir_ = _directory_with_blair()
    assert reconcile_contact_id("contact-blair-nicol", "", dir_) == "contact-03f3902d"


def test_slug_fallback_resolves_unresolved_prefix():
    dir_ = _directory_with_blair()
    assert reconcile_contact_id("unresolved-contact-blair", "", dir_) == "contact-03f3902d"


def test_display_name_not_in_db_preserves_id():
    """Genuine non-DB contacts keep their unresolved ID."""
    dir_ = _directory_with_blair()
    assert reconcile_contact_id("unresolved-contact-sarah", "Sarah", dir_) == "unresolved-contact-sarah"


def test_non_contact_entity_id_unchanged():
    dir_ = _directory_with_blair()
    assert reconcile_contact_id("trip-bali-2026", "Bali", dir_) == "trip-bali-2026"
