from functools import cached_property
from .payload import create_payload


class Notice(object):
    def __init__(self, *args, **kwargs):
        self.exception = kwargs.get("exception", None)
        self.error_class = kwargs.get("error_class", None)
        self.error_message = kwargs.get("error_message", None)
        self.exc_traceback = kwargs.get("exc_traceback", None)
        self.fingerprint = kwargs.get("fingerprint", None)
        self.config = kwargs.get("config", None)
        self.context = kwargs.get("context", {})
        self.request_id = kwargs.get("request_id", None)
        self.tags = self._construct_tags(kwargs.get("tags", []))

        self._process_exception()

    def _process_exception(self):
        if self.exception and self.error_message:
            self.context["error_message"] = self.error_message

        if self.exception is None:
            self.exception = {
                "error_class": self.error_class,
                "error_message": self.error_message,
            }

    @cached_property
    def payload(self):
        return create_payload(
            self.exception,
            self.exc_traceback,
            fingerprint=self.fingerprint,
            context=self.context,
            tags=self.tags,
            config=self.config,
            correlation_context=self._correlation_context(),
        )

    def excluded_exception(self):
        if self.config.excluded_exceptions:
            if (
                self.exception
                and self.exception.__class__.__name__ in self.config.excluded_exceptions
            ):
                return True
            elif (
                self.error_class and self.error_class in self.config.excluded_exceptions
            ):
                return True
        return False

    def _correlation_context(self):
        if self.request_id:
            return {"request_id": self.request_id}
        return None

    def _construct_tags(self, tags):
        """
        Accepts either:
          - a single string (possibly comma-separated)
          - a list of strings (each possibly comma-separated)
        and returns a flat list of stripped tags.
        """
        raw = []
        if isinstance(tags, str):
            raw = [tags]
        elif isinstance(tags, (list, tuple)):
            raw = tags
        out = []
        for item in raw:
            if not isinstance(item, str):
                continue
            for part in item.split(","):
                t = part.strip()
                if t:
                    out.append(t)
        return out
