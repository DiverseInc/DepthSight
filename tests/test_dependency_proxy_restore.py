"""`monkeypatch` on a runtime dependency must not damage the module it proxies.

`bot_module.runtime_dependencies.crud` is a lazy proxy in front of the
`api.crud` module. Tests stub the functions it forwards to with
`monkeypatch.setattr(crud, "some_name", fake)`, and pytest's `monkeypatch`
fixture undoes that patch at the end of the test.

pytest 9.2.0 changed how `monkeypatch.setattr` records the value it has to
put back. It now reads `target.__dict__` (see `_pytest/monkeypatch.py`, the
`elif not _is_data_descriptor(...)` branch) instead of `getattr(target, name)`.
A proxied attribute is VIRTUAL - it is produced by `DependencyProxy.__getattr__`,
so it is not an entry in the proxy's `__dict__` - and pytest therefore
recorded "there was nothing there to restore" and undid the patch with
`delattr(proxy, name)`.

`DependencyProxy.__delattr__` forwarded that to `delattr(self._target, name)`,
which took the attribute out of the REAL `api.crud` module - for the rest of
the pytest session, because the module is a process-wide singleton. Every
later test that touched it then failed with
`AttributeError: module 'api.crud' has no attribute 'get_paper_wallet'`, and
`monkeypatch.setattr` itself failed with
`AttributeError: <bot_module.runtime_dependencies.DependencyProxy object ...>
has no attribute 'get_user_symbol_selection_config'`.

The proxy has to PUT THE ORIGINAL BACK, not delete it. These tests pin that
contract, and the second one pins the other half of it: the proxy must still
forward to the LIVE target, so that a patch applied to the module directly is
visible through the proxy.

The undo has one more rule, and it is the one that makes "an undo must never
destroy something it did not create" true: the proxy only reverts what it
put there. If the proxied object was changed out of band since the proxy's
write, that change is not the proxy's to revert, so the undo leaves it
alone. And a snapshot of a previous target is meaningless once `configure()`
swaps the target, so it is dropped then.

The rule the undo follows is OWNERSHIP. The proxy only ever reverts a name
it wrote to the proxied object ITSELF, and it never removes a name it
cannot prove it still owns: a name the proxy never wrote is left alone, an
underscore name is the proxy's own and never reaches the proxied object at
all, and a name the proxy CREATED is left in place when it is undone,
because the proxied object may have taken that name over out of band and
identity cannot tell that apart from "it still holds what we put there".
"""

from __future__ import annotations

from typing import Any

import pytest

from bot_module.runtime_dependencies import DependencyProxy


class _Target:
    """A stand-in for the proxied module.

    `existing` is an INSTANCE attribute, not a class attribute: deleting it
    must take it away for good, so the test can tell "restored" apart from
    "still there by accident".
    """

    def __init__(self, value: Any = "original") -> None:
        self.existing = value


class _Value:
    """A distinct, identifiable value.

    The proxy compares what it wrote by identity, so the tests need values
    that are objects in their own right - two equal strings can be the same
    object, two `_Value`s never are.
    """

    def __init__(self, label: str) -> None:
        self.label = label

    def __repr__(self) -> str:
        return f"_Value({self.label!r})"


def test_deleting_an_override_puts_the_original_back():
    """`del proxy.name` restores; it must not delete the target's attribute."""

    target = _Target()
    proxy = DependencyProxy("t")
    proxy.configure(target)

    proxy.existing = "patched"  # type: ignore[assignment]
    assert target.existing == "patched"

    del proxy.existing  # type: ignore[attr-defined]

    assert hasattr(target, "existing"), (
        "deleting the proxy's override deleted the attribute from the proxied "
        "object instead of restoring it"
    )
    assert target.existing == "original"


def test_deleting_an_override_of_a_name_the_target_never_had_leaves_it_in_place():
    """A name the target did not have is LEFT IN PLACE after the undo.

    2026-10-29 - DELIBERATE BEHAVIOUR CHANGE. This test used to assert
    `not hasattr(target, "brand_new")`: the undo DELETED the name the proxy
    had created. Deleting is not safe. The proxy created the name, but it
    cannot prove the target still holds only what the proxy put there - the
    target may have adopted the very same object out of band, and identity
    cannot tell the two cases apart (see
    `test_the_undo_does_not_delete_a_name_the_module_adopted_out_of_band`).
    So the undo now leaves the attribute in place and records the name as
    abandoned instead of deleting it.

    The common case is unaffected: a name the target DID have is restored
    with `setattr(target, name, saved)` and is never deleted.
    """

    target = _Target()
    proxy = DependencyProxy("t")
    proxy.configure(target)

    proxy.brand_new = 1  # type: ignore[attr-defined]
    assert target.brand_new == 1  # type: ignore[attr-defined]

    del proxy.brand_new  # type: ignore[attr-defined]

    assert hasattr(target, "brand_new"), (
        "the undo deleted a name the proxy created. The proxy may only "
        "revert what it put there, and it cannot prove it still owns it."
    )
    assert target.brand_new == 1  # type: ignore[attr-defined]


def test_the_proxy_still_forwards_to_the_live_target():
    """The proxy is not a snapshot: a module-level patch stays visible."""

    target = _Target()
    proxy = DependencyProxy("t")
    proxy.configure(target)

    assert proxy.existing == "original"

    target.existing = "patched"
    assert proxy.existing == "patched"


def test_monkeypatch_undo_keeps_the_proxied_module_intact(monkeypatch):
    """The real `api.crud` module must survive a monkeypatched proxy.

    This is the shape that broke CI: a test stubs a function through the
    proxy, pytest undoes the patch at teardown, and the undo took the
    attribute out of the real module for every test that ran afterwards.
    """

    from api import crud as crud_module
    from bot_module.runtime_dependencies import crud

    original = crud_module.get_user_symbol_selection_config

    async def _fake(db, user_id):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(crud, "get_user_symbol_selection_config", _fake)

    # The stub is live while the patch is live - through the proxy, and
    # through the module for the code that imports `api.crud` directly.
    assert crud.get_user_symbol_selection_config is _fake
    assert crud_module.get_user_symbol_selection_config is _fake

    monkeypatch.undo()

    assert hasattr(crud_module, "get_user_symbol_selection_config"), (
        "monkeypatch.undo() deleted the attribute from the proxied module; "
        "every later test in the session would fail on it"
    )
    assert crud_module.get_user_symbol_selection_config is original
    assert crud.get_user_symbol_selection_config is original


def test_a_second_patch_of_the_same_name_still_works(monkeypatch):
    """Two patches of one name, undone in order, leave the original in place."""

    from api import crud as crud_module
    from bot_module.runtime_dependencies import crud

    original = crud_module.get_paper_wallet

    async def _first(db, user_id):  # type: ignore[no-untyped-def]
        return None

    async def _second(db, user_id):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(crud, "get_paper_wallet", _first)
    monkeypatch.setattr(crud, "get_paper_wallet", _second)
    assert crud.get_paper_wallet is _second

    monkeypatch.undo()

    assert hasattr(crud_module, "get_paper_wallet")
    assert crud_module.get_paper_wallet is original


def test_the_configured_target_is_still_the_one_that_is_used():
    """`configure()` keeps working: the proxy reads from its target."""

    target = _Target()
    proxy = DependencyProxy("t")
    proxy.configure(target)
    assert isinstance(proxy._target, _Target)

    other = _Target()
    proxy.configure(other)
    assert proxy._target is other

    value: Any = proxy.existing
    assert value == "original"


def test_undo_does_not_clobber_a_change_made_out_of_band():
    """A change made directly on the target is not the proxy's to revert.

    The proxy wrote `patched`; somebody then changed the same name on the
    target WITHOUT going through the proxy. Undoing the proxy's write must
    leave that later change alone - the proxy did not make it, so it must
    not destroy it.
    """

    target = _Target()
    proxy = DependencyProxy("t")
    proxy.configure(target)

    patched = _Value("patched")
    changed_elsewhere = _Value("changed-elsewhere")

    proxy.existing = patched  # type: ignore[assignment]
    assert target.existing is patched

    target.existing = changed_elsewhere  # out of band, bypassing the proxy

    del proxy.existing  # type: ignore[attr-defined]

    assert target.existing is changed_elsewhere, (
        "the undo clobbered a change that was made on the target directly, "
        "outside the proxy - that change is not the proxy's to revert"
    )


def test_two_undos_of_the_same_name_leave_the_original_in_place():
    """Two patches of one name, undone twice, must not delete the attribute.

    pytest's monkeypatch can record the same (target, name) twice, so the
    undo of the second patch must not be able to remove the target's real
    attribute - the module has to end with its ORIGINAL attribute, and that
    attribute has to still exist.
    """

    target = _Target()
    proxy = DependencyProxy("t")
    proxy.configure(target)

    original = target.existing
    first = _Value("first")
    second = _Value("second")

    proxy.existing = first  # type: ignore[assignment]
    proxy.existing = second  # type: ignore[assignment]
    assert target.existing is second

    del proxy.existing  # type: ignore[attr-defined]
    del proxy.existing  # type: ignore[attr-defined]

    assert hasattr(target, "existing"), (
        "the second undo deleted the attribute from the proxied object "
        "instead of leaving it restored"
    )
    assert target.existing == original


def test_two_explicit_undos_of_a_real_attribute_leave_it_in_place(monkeypatch):
    """The real `api.crud` attribute must survive two undos of the same name.

    This is the CI shape: pytest's monkeypatch records the same
    (target, name) twice and undoes it twice, and the second undo must not
    be able to take the module's real attribute away.
    """

    from api import crud as crud_module
    from bot_module.runtime_dependencies import crud

    original = crud_module.get_paper_wallet

    async def _fake(db, user_id):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(crud, "get_paper_wallet", _fake)
    monkeypatch.undo()

    # A second undo for the same name must not remove the real attribute.
    delattr(crud, "get_paper_wallet")

    assert hasattr(crud_module, "get_paper_wallet"), (
        "the second undo deleted the attribute from the proxied module; "
        "every later test in the session would fail on it"
    )
    assert crud_module.get_paper_wallet is original


def test_configure_clears_what_the_proxy_remembered():
    """A new target must not inherit the previous target's snapshot.

    `configure()` swaps the proxied object, so what the proxy remembers
    about the old one is meaningless and must not be applied to the new one.
    """

    first = _Target("first-original")
    second = _Target("second-original")

    proxy = DependencyProxy("t")
    proxy.configure(first)

    proxy.existing = "patched"  # type: ignore[assignment]
    assert first.existing == "patched"

    proxy.configure(second)

    proxy.existing = "second-patched"  # type: ignore[assignment]
    del proxy.existing  # type: ignore[attr-defined]

    assert (
        second.existing == "second-original"
    ), "the undo restored the PREVIOUS target's snapshot onto the new target"
    assert first.existing == "patched", "the undo touched the previous target"


def test_a_later_undo_does_not_clobber_an_out_of_band_change():
    """An out-of-band change must survive a LATER undo of the same name.

    The first undo already refuses to revert a change made directly on the
    target. But the snapshot the proxy kept for that name still described the
    value that was there BEFORE the out-of-band change, so a later patch of
    the same name reused that stale snapshot, and the undo after it wrote the
    stale original over the out-of-band value - destroying a change the proxy
    never made.

    So once the proxy sees that the target was changed out of band, it must
    re-baseline on what the target holds NOW and stop claiming the name.
    """

    target = _Target()
    proxy = DependencyProxy("t")
    proxy.configure(target)

    original = target.existing
    patched = _Value("patched")
    changed_elsewhere = _Value("changed-elsewhere")
    patched_again = _Value("patched-again")

    proxy.existing = patched  # type: ignore[assignment]
    assert target.existing is patched

    target.existing = changed_elsewhere  # out of band, bypassing the proxy

    del proxy.existing  # type: ignore[attr-defined]
    assert (
        target.existing is changed_elsewhere
    ), "the first undo clobbered a change made on the target directly"

    proxy.existing = patched_again  # type: ignore[assignment]
    assert target.existing is patched_again

    del proxy.existing  # type: ignore[attr-defined]

    assert target.existing is changed_elsewhere, (
        "the second undo wrote the proxy's stale pre-out-of-band snapshot over "
        "the change that was made on the target directly. The proxy's "
        "original snapshot no longer described reality and had to be "
        "re-baselined when the out-of-band change was first seen."
    )
    assert target.existing is not original


def test_undoing_an_abandoned_name_again_changes_nothing():
    """Once the proxy has abandoned a name, undoing it again is inert.

    Abandoning means the value on the target is not the proxy's to touch. A
    second undo must therefore neither raise nor change the target.
    """

    target = _Target()
    proxy = DependencyProxy("t")
    proxy.configure(target)

    patched = _Value("patched")
    changed_elsewhere = _Value("changed-elsewhere")

    proxy.existing = patched  # type: ignore[assignment]
    target.existing = changed_elsewhere  # out of band, bypassing the proxy

    del proxy.existing  # type: ignore[attr-defined]
    del proxy.existing  # type: ignore[attr-defined]  # must not raise

    assert (
        target.existing is changed_elsewhere
    ), "a repeated undo of an abandoned name changed the value on the target"


def test_a_second_out_of_band_change_is_also_preserved():
    """A second out-of-band change must survive too.

    After the first out-of-band change the proxy re-baselines on what the
    target holds then. If the target is changed out of band AGAIN before the
    next patch, the proxy must re-baseline again - otherwise it would put back
    the value from the FIRST out-of-band change and destroy the second one.
    """

    target = _Target()
    proxy = DependencyProxy("t")
    proxy.configure(target)

    patched = _Value("patched")
    changed_once = _Value("changed-once")
    changed_twice = _Value("changed-twice")
    patched_again = _Value("patched-again")

    proxy.existing = patched  # type: ignore[assignment]

    target.existing = changed_once  # out of band, bypassing the proxy
    del proxy.existing  # type: ignore[attr-defined]
    assert target.existing is changed_once

    target.existing = changed_twice  # out of band again, bypassing the proxy

    proxy.existing = patched_again  # type: ignore[assignment]
    del proxy.existing  # type: ignore[attr-defined]

    assert target.existing is changed_twice, (
        "the undo put back the value from the FIRST out-of-band change and "
        "destroyed the second one. The proxy must re-baseline on what the "
        "target holds now, every time it patches an abandoned name."
    )


def test_an_out_of_band_change_between_two_patches_survives_the_undo():
    """A change made on the target BETWEEN two patches must survive the undo.

    The proxy wrote `DIRECT_PATCH`; somebody then changed the same name on
    the target WITHOUT going through the proxy; the proxy then wrote `STUB2`.
    The snapshot the proxy kept still described the value from BEFORE the
    out-of-band change - the proxy had no way to notice it, because nothing
    had been undone in between - so the undo after the second patch wrote
    that stale original over the out-of-band value, destroying a change the
    proxy never made.

    So a patch of a name whose value on the target is no longer the one the
    proxy put there must re-baseline on what the target holds NOW.
    """

    target = _Target()
    target.get_paper_wallet = "ORIGINAL"  # type: ignore[attr-defined]
    proxy = DependencyProxy("t")
    proxy.configure(target)

    out_of_band = "OUT_OF_BAND"

    proxy.get_paper_wallet = "DIRECT_PATCH"  # type: ignore[attr-defined]
    assert target.get_paper_wallet == "DIRECT_PATCH"  # type: ignore[attr-defined]

    target.get_paper_wallet = out_of_band  # type: ignore[attr-defined]

    proxy.get_paper_wallet = "STUB2"  # type: ignore[attr-defined]
    assert target.get_paper_wallet == "STUB2"  # type: ignore[attr-defined]

    del proxy.get_paper_wallet  # type: ignore[attr-defined]

    assert target.get_paper_wallet == "OUT_OF_BAND", (  # type: ignore[attr-defined]
        "the undo wrote the proxy's stale snapshot over the change that was "
        "made on the target directly. The proxy's snapshot no longer "
        "described reality and had to be re-baselined on what the target "
        "holds now."
    )
    assert target.get_paper_wallet != "ORIGINAL"  # type: ignore[attr-defined]
    assert target.get_paper_wallet is out_of_band  # type: ignore[attr-defined]


def test_a_name_the_module_gained_out_of_band_is_not_deleted_by_the_undo():
    """An out-of-band name must survive the undo; it is not the proxy's to delete.

    The proxy created the name - the module did not have it - so the proxy's
    snapshot says "there was nothing there to restore". If the module then
    gains the name out of band, the snapshot no longer describes reality,
    and the undo must not delete the module's attribute: that would take it
    out of the module for good, which is exactly what this proxy exists to
    prevent.
    """

    target = _Target()
    proxy = DependencyProxy("t")
    proxy.configure(target)

    proxy.brand_new = "STUB1"  # type: ignore[attr-defined]
    assert target.brand_new == "STUB1"  # type: ignore[attr-defined]

    target.brand_new = "REAL"  # type: ignore[attr-defined]

    proxy.brand_new = "STUB2"  # type: ignore[attr-defined]
    assert target.brand_new == "STUB2"  # type: ignore[attr-defined]

    del proxy.brand_new  # type: ignore[attr-defined]

    assert hasattr(target, "brand_new"), (
        "the undo deleted a name the module gained out of band. The proxy's "
        "snapshot said 'the module never had this name', but that snapshot "
        "no longer described reality."
    )
    assert target.brand_new == "REAL"  # type: ignore[attr-defined]


def test_the_pytest_9_2_undo_path_keeps_the_real_function():
    """`pytest.MonkeyPatch` must not take the module's own function away.

    pytest 9.2 records the value it has to put back from `target.__dict__`,
    and a proxied attribute is VIRTUAL - it is produced by
    `DependencyProxy.__getattr__`, so it is not an entry in the proxy's
    `__dict__` - so the undo goes through `delattr(proxy, name)`. That has to
    put the module's own function back, not delete it.
    """

    from api import crud as crud_module
    from bot_module.runtime_dependencies import crud

    original = crud_module.get_user_by_email

    async def _fake(db, email):  # type: ignore[no-untyped-def]
        return None

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(crud, "get_user_by_email", _fake)
        assert crud.get_user_by_email is _fake
        assert crud_module.get_user_by_email is _fake
    finally:
        mp.undo()

    assert hasattr(crud_module, "get_user_by_email"), (
        "the undo deleted the function from the proxied module; every later "
        "test in the session would fail on it"
    )
    assert crud_module.get_user_by_email is original


def test_two_patches_of_one_name_undone_together_keep_the_real_function():
    """Two patches of one name, undone together, leave the original in place.

    pytest's `MonkeyPatch` records the same (target, name) twice here, and
    undoes both at once. Neither undo may take the module's real attribute
    away, and the module has to end up with its own function again.
    """

    from api import crud as crud_module
    from bot_module.runtime_dependencies import crud

    original = crud_module.get_user_by_username

    async def _first(db, username):  # type: ignore[no-untyped-def]
        return None

    async def _second(db, username):  # type: ignore[no-untyped-def]
        return None

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(crud, "get_user_by_username", _first)
        mp.setattr(crud, "get_user_by_username", _second)
        assert crud.get_user_by_username is _second
    finally:
        mp.undo()

    assert hasattr(crud_module, "get_user_by_username"), (
        "the undo deleted the function from the proxied module; every later "
        "test in the session would fail on it"
    )
    assert crud_module.get_user_by_username is original


def test_deleting_a_name_the_proxy_never_wrote_leaves_the_target_alone():
    """An undo must not delete an attribute the proxy never wrote.

    The proxy may only revert what it put there. This name was on the
    target from the start and the proxy never touched it, so the undo has
    nothing to do on the target at all - deleting it would take a real
    attribute out of the proxied object for good.
    """

    target = _Target()
    proxy = DependencyProxy("t")
    proxy.configure(target)

    original = target.existing

    del proxy.existing  # type: ignore[attr-defined]

    assert hasattr(target, "existing"), (
        "deleting a name the proxy never wrote took the attribute off the "
        "proxied object; the proxy may only revert what it put there"
    )
    assert target.existing == original


def test_deleting_an_underscore_name_leaves_the_target_alone():
    """An underscore name belongs to the proxy, never to the target.

    `__setattr__` keeps underscore names on the proxy and never writes them
    to the proxied object, so `__delattr__` must not delete them from the
    proxied object either. It removes them from the proxy - and only from
    the proxy.
    """

    target = _Target()
    target._real_private = "REAL"  # type: ignore[attr-defined]
    proxy = DependencyProxy("t")
    proxy.configure(target)

    # Underscore names are stored on the proxy; they never reach the target.
    proxy._real_private = "patched"  # type: ignore[attr-defined]
    assert target._real_private == "REAL"

    del proxy._real_private  # type: ignore[attr-defined]

    assert target._real_private == "REAL", (
        "deleting an underscore name deleted the attribute from the proxied "
        "object; underscore names are the proxy's own and never reach it"
    )
    assert (
        "_real_private" not in proxy.__dict__
    ), "the underscore name was not removed from the proxy it belongs to"


def test_the_undo_does_not_delete_a_name_the_module_adopted_out_of_band():
    """A name the module took over out of band must survive the undo.

    The proxy created the name, so the proxy's snapshot says "there was
    nothing there to restore". The module then adopts the very SAME object
    the proxy put there, out of band. Identity cannot tell that apart from
    "the module still holds exactly what the proxy wrote", so the undo must
    not delete the module's attribute: the proxy cannot prove it still owns
    that name.
    """

    target = _Target()
    proxy = DependencyProxy("t")
    proxy.configure(target)

    shared = _Value("shared")

    # The proxy creates the name - the target did not have it.
    proxy.brand_new = shared  # type: ignore[attr-defined]
    assert target.brand_new is shared

    # The module adopts the very same object, out of band.
    target.brand_new = shared  # type: ignore[attr-defined]

    del proxy.brand_new  # type: ignore[attr-defined]

    assert hasattr(target, "brand_new"), (
        "the undo deleted a name the module holds. The proxy created the "
        "name, but it cannot prove the module still holds only what the "
        "proxy put there, so it must not delete it."
    )
    assert target.brand_new is shared


def test_the_undo_forgets_the_name_it_restored():
    """After the undo, the proxy must not keep remembering that name.

    The proxy remembers what the proxied object held for each name it wrote,
    so that it can put the original back. Once the undo is done the proxy no
    longer owns the name and no longer needs the memory: keeping it would make
    the bookkeeping grow with every distinct name ever patched, for the rest of
    the session, and would keep a reference to a value the proxy has no further
    use for.
    """

    target = _Target()
    proxy = DependencyProxy("t")
    proxy.configure(target)

    proxy.existing = "patched"  # type: ignore[assignment]
    assert target.existing == "patched"

    del proxy.existing  # type: ignore[attr-defined]

    assert target.existing == "original", (
        "the undo did not put the original back"
    )
    assert "existing" not in proxy._restore, (
        "the proxy still remembers the original value for a name it has "
        "already reverted"
    )
    assert "existing" not in proxy._written, (
        "the proxy still remembers what it wrote for a name it has already "
        "reverted"
    )
    assert "existing" not in proxy._touched, (
        "the proxy still claims a name it has already reverted"
    )
