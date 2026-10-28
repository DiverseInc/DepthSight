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
"""

from __future__ import annotations

from typing import Any

from bot_module.runtime_dependencies import DependencyProxy


class _Target:
    """A stand-in for the proxied module.

    `existing` is an INSTANCE attribute, not a class attribute: deleting it
    must take it away for good, so the test can tell "restored" apart from
    "still there by accident".
    """

    def __init__(self) -> None:
        self.existing = "original"


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


def test_deleting_an_override_of_a_name_the_target_never_had():
    """A name the target does not have is still absent afterwards."""

    target = _Target()
    proxy = DependencyProxy("t")
    proxy.configure(target)

    proxy.brand_new = 1  # type: ignore[attr-defined]
    assert target.brand_new == 1  # type: ignore[attr-defined]

    del proxy.brand_new  # type: ignore[attr-defined]

    assert not hasattr(target, "brand_new")


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
