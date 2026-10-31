from typing import Any, Callable, Optional

# Sentinel for "the proxied object has no such attribute". Distinct from None,
# which is a legitimate attribute value.
_MISSING = object()

# The proxy's own bookkeeping. These names live on the proxy, never on the
# proxied object, and the proxy must not be able to delete them from itself.
_INTERNAL_ATTRS = frozenset(
    {"_name", "_target", "_overrides", "_restore", "_written", "_touched"}
)


class DependencyProxy:
    def __init__(self, name: str):
        self._name = name
        self._target: Optional[Any] = None
        self._overrides: dict[str, Any] = {}
        # What the proxied object had for a name before this proxy wrote to it.
        # Used to put the original back instead of deleting it. The entry is
        # dropped as soon as the undo for that name is over - see _forget.
        self._restore: dict[str, Any] = {}
        # What this proxy last wrote to the proxied object for a name. Used to
        # tell "the target still holds what we put there" (ours to revert)
        # from "the target was changed out of band since" (not ours to revert).
        # The entry is dropped as soon as the undo for that name is over - see
        # _forget.
        self._written: dict[str, Any] = {}
        # The names this proxy has actually written TO THE PROXIED OBJECT.
        # A name that is not in here was never the proxy's to revert, so the
        # proxy must not remove it from the proxied object. A name the proxy
        # has given up is removed from here too: the proxy does not own it any
        # more. See _forget.
        self._touched: set[str] = set()

    def configure(self, target: Any) -> None:
        self._target = target
        # A snapshot of the previous target means nothing for the new one.
        self._restore.clear()
        self._written.clear()
        self._touched.clear()

    def __bool__(self) -> bool:
        return self._target is not None or bool(self._overrides)

    def __getattr__(self, name: str) -> Any:
        if name in self._overrides:
            return self._overrides[name]
        if self._target is None:
            raise RuntimeError(
                f"Runtime dependency '{self._name}' is not configured. "
                "Configure bot_module.runtime_dependencies from the process entrypoint."
            )
        return getattr(self._target, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        if self._target is None:
            self._overrides[name] = value
            return
        # Remember what the proxied object had, so that removing the override
        # puts the original back instead of deleting it (see __delattr__).
        written = self._written.get(name, _MISSING)
        if written is _MISSING:
            # First write of this name: the baseline is what the proxied
            # object holds NOW.
            self._restore[name] = getattr(self._target, name, _MISSING)
        elif getattr(self._target, name, _MISSING) is not written:
            # The proxied object no longer holds what this proxy wrote, so it
            # was changed out of band. The snapshot kept for this name
            # describes a value that is no longer there, so re-baseline on
            # what the object holds NOW - otherwise the next undo would put a
            # stale original over the out-of-band value, or delete a name the
            # object gained in the meantime.
            #
            # A name the proxy has GIVEN UP lands here as well: the proxy no
            # longer remembers it (see _forget), so this is a first write for
            # the proxy and the baseline is taken from what the object holds
            # NOW. That is the out-of-band value, which must never be
            # overwritten by the stale original the proxy has already dropped.
            self._restore[name] = getattr(self._target, name, _MISSING)
        setattr(self._target, name, value)
        self._written[name] = value
        # The name is on the proxied object because this proxy put it there,
        # so the proxy owns it and is the one allowed to undo it.
        self._touched.add(name)

    def __delattr__(self, name: str) -> None:
        if name.startswith("_"):
            # Underscore names are the proxy's own: __setattr__ keeps them on
            # the proxy and never writes them to the proxied object, so the
            # proxied object must not be touched here at all. Deleting one
            # removes it from the proxy - and only from the proxy.
            if name in _INTERNAL_ATTRS or name not in self.__dict__:
                raise AttributeError(name)
            object.__delattr__(self, name)
            return
        if name in self._overrides:
            del self._overrides[name]
            return
        if self._target is None:
            raise AttributeError(name)
        if name not in self._touched:
            # This proxy never wrote this name to the proxied object, or it
            # wrote it and has since given it up. Either way there is nothing
            # of the proxy's there to revert. Touching the proxied object
            # would delete an attribute the proxy does not own - the real one
            # the proxied object always had.
            #
            # A name the proxy gave up is in the same position as one it never
            # wrote: the proxy has forgotten it, so there is nothing left to
            # revert and the undo leaves the proxied object alone. If the
            # proxied object does not have the name either, the undo raises
            # AttributeError - exactly as it does for a name the proxy never
            # wrote.
            self._restore.pop(name, None)
            self._written.pop(name, None)
            if not hasattr(self._target, name):
                raise AttributeError(name)
            return
        saved = self._restore.get(name, _MISSING)
        written = self._written.get(name, _MISSING)
        if (
            written is not _MISSING
            and getattr(self._target, name, _MISSING) is not written
        ):
            # The proxied object no longer holds what this proxy put there: it
            # was changed out of band, and that change is not this proxy's to
            # revert. Leave it alone rather than clobbering it.
            #
            # The name is ABANDONED: the snapshot kept for it describes a
            # value that is no longer there, so it must never be applied
            # again. The next patch of this name re-baselines on what the
            # target holds now (see __setattr__), so the out-of-band value
            # can never be overwritten by a stale original - which is exactly
            # what this guard exists to prevent.
            #
            # The proxy stops claiming the name here, and it stops REMEMBERING
            # it as well: the snapshot it kept describes a value that is gone
            # and the value the target holds now is not the proxy's to keep a
            # reference to. A repeated undo of this name finds nothing the
            # proxy owns and leaves the target alone.
            self._forget(name)
            return
        if saved is _MISSING:
            # This proxy CREATED the name: the proxied object did not have it
            # before the proxy wrote it. Deleting it would remove an attribute
            # of the proxied object that the proxy cannot prove it still
            # owns - the proxied object may have adopted the very same object
            # out of band, and identity cannot tell that apart from "it still
            # holds exactly what we put there" (which is all the guard above
            # can check). So the attribute is LEFT IN PLACE and the proxy
            # stops claiming the name.
            #
            # The trade-off, stated plainly: a name the proxy created stays on
            # the proxied object after the undo. That is the safe direction -
            # a leftover stub can be overwritten, while a deleted attribute
            # takes a real function out of the module for the rest of the
            # session. The common case is untouched: a name the proxied
            # object DID have is restored with
            # `setattr(self._target, name, saved)` and is never deleted, so
            # the pytest undo path that caused the original incident behaves
            # exactly as before.
            #
            # As with the out-of-band case above, the proxy stops remembering
            # the name: there is no original to remember, and the name is no
            # longer the proxy's to revert.
            self._forget(name)
            return
        # Put the original back. Deleting it instead would take the attribute
        # out of the proxied module for good.
        setattr(self._target, name, saved)
        # The undo is complete for this name: the proxy restored the original
        # and does not own the name any more, so it must not keep remembering
        # it. Keeping the entry would make _restore/_written/_touched grow
        # with every distinct name ever patched for the rest of the session,
        # and would keep a reference to a value the proxy has no further use
        # for. A later undo of this name now finds it untouched and leaves the
        # target alone, which is the correct outcome: the proxy has nothing
        # left to revert.
        self._forget(name)

    def _forget(self, name: str) -> None:
        """Drop every trace of a name the proxy no longer owns.

        Called when the undo for a name is over - the original was put back -
        and when the proxy gives a name up: the proxied object was changed
        out of band, or the proxy created the name and cannot prove it still
        owns it.

        All three pieces of bookkeeping describe a write the proxy has
        finished with. The snapshot describes a value that is no longer where
        the proxy put it, the recorded write is no longer the proxy's to
        revert, and the claim on the name is over. Keeping them would make the
        bookkeeping grow with every distinct name ever patched, for the rest
        of the session, and would keep a reference to values the proxy has no
        further use for - including, in the out-of-band case, somebody else's
        value.

        Forgetting the name is also what makes a LATER undo of it inert: the
        name is no longer in `_touched`, so the undo takes the "this proxy
        never wrote that name" path, which leaves the proxied object alone.

        It is safe to forget the snapshot: `__setattr__` takes the baseline
        from what the proxied object holds at the moment of the NEXT write, so
        a forgotten name is re-baselined from reality before any undo can
        read it. The original value is therefore never lost for a name the
        proxied object really had - the undo of that name restores it with
        `setattr(self._target, name, saved)` before this is ever reached.
        """
        self._restore.pop(name, None)
        self._written.pop(name, None)
        self._touched.discard(name)


class CallableDependency:
    def __init__(self, name: str):
        self._name = name
        self._target: Optional[Callable[..., Any]] = None

    def configure(self, target: Callable[..., Any]) -> None:
        self._target = target

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self._target is None:
            raise RuntimeError(
                f"Runtime dependency '{self._name}' is not configured. "
                "Configure bot_module.runtime_dependencies from the process entrypoint."
            )
        return self._target(*args, **kwargs)


crud = DependencyProxy("crud")
get_db = CallableDependency("get_db")
send_push_notification = CallableDependency("send_push_notification")


def configure_runtime_dependencies(
    *,
    crud_module: Any,
    get_db_factory: Callable[..., Any],
    push_sender: Callable[..., Any],
) -> None:
    crud.configure(crud_module)
    get_db.configure(get_db_factory)
    send_push_notification.configure(push_sender)
