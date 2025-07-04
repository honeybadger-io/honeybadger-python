from contextvars import ContextVar
from contextlib import contextmanager
from typing import Any, Dict, Optional


class ContextStore:
    def __init__(self, name: str):
        self._ctx: ContextVar[Optional[Dict[str, Any]]] = ContextVar(name, default=None)

    def get(self) -> Dict[str, Any]:
        data = self._ctx.get()
        return {} if data is None else data.copy()

    def clear(self) -> None:
        self._ctx.set({})

    def update(self, ctx: Optional[Dict[str, Any]] = None, **kwargs: Any) -> None:
        """
        Merge into the current context. Accepts either:
          - update({'foo': 'bar'})
          - update(foo='bar', baz=123)
          - or both: update({'foo': 'bar'}, baz=123)
        """
        to_merge: Dict[str, Any] = {}
        if ctx:
            to_merge.update(ctx)
        to_merge.update(kwargs)

        new = self.get()
        new.update(to_merge)
        self._ctx.set(new)

    @contextmanager
    def override(self, ctx: Optional[Dict[str, Any]] = None, **kwargs: Any):
        """
        Temporarily merge these into context for the duration of the with-block.
        """
        to_merge: Dict[str, Any] = {}
        if ctx:
            to_merge.update(ctx)
        to_merge.update(kwargs)

        token = self._ctx.set({**self.get(), **to_merge})
        try:
            yield
        finally:
            self._ctx.reset(token)
