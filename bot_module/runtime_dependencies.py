from typing import Any, Callable, Optional

# Sentinel for "the proxied object has no such attribute". Distinct from None,
# which is a legitimate attribute value.
_MISSING = object()

# Sentinel for "this proxy no longer owns this name". It is set when the
# proxied object was changed out of band, so the proxy's snapshot for that
# name no longer describes reality and must not be applied again.
_ABANDONED = object()


class DependencyProxy:
    def __init__(self, name: str):
        self._name = name
        self._target: Optional[Any] = None
        self._overrides: dict[str, Any] = {}
        # What the proxied object had for a name before this proxy wrote to it.
        # Used to put the original back instead of deleting it.
        self._restore: dict[str, Any] = {}
        # What this proxy last wrote to the proxied object for a name. Used to
        # tell "the target still holds what we put there" (ours to revert)
        # from "the target was changed out of band since" (not ours to revert).
        self._written: dict[str, Any] = {}

    def configure(self, target: Any) -> None:
        self._target = target
        # A snapshot of the previous target means nothing for the new one.
        self._restore.clear()
        self._written.clear()

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
            # An abandoned name lands here as well: `_ABANDONED` is never the
            # value the object holds, so patching an abandoned name
            # re-baselines the same way.
            self._restore[name] = getattr(self._target, name, _MISSING)
        setattr(self._target, name, value)
        self._written[name] = value

    def __delattr__(self, name: str) -> None:
        if name in self._overrides:
            del self._overrides[name]
            return
        if self._target is None:
            raise AttributeError(name)
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
            # An abandoned name is caught here too: `_ABANDONED` is never the
            # value the target holds, so a repeated undo lands here, re-baselines
            # on the same value and changes nothing.
            self._restore[name] = getattr(self._target, name, _MISSING)
            self._written[name] = _ABANDONED
            return
        if saved is _MISSING:
            # The proxied object never had this name - the proxy created it,
            # so removing the override removes it again.
            delattr(self._target, name)
            return
        # Put the original back. Deleting it instead would take the attribute
        # out of the proxied module for good.
        setattr(self._target, name, saved)
        self._written[name] = saved


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
