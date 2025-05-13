from functools import cached_property
from .payload import create_payload


class Notice(object):
    def __init__(self, *args, **kwargs):
        self.exception = kwargs.get("exception", None)
        self.error_class = kwargs.get("error_class", None)
        self.error_message = kwargs.get("error_message", None)
        self.exc_traceback = kwargs.get("exc_traceback", None)
        self.fingerprint = kwargs.get("fingerprint", None)
        self.thread_local = kwargs.get("thread_local", None)
        self.config = kwargs.get("config", None)
        self.context = kwargs.get("context", {})
        self.tags = self._construct_tags(kwargs.get("tags", []))

        self._process_exception()
        self._process_context()
        self._process_tags()

    def _process_exception(self):
        if self.exception is None and self.error_class:
            self.exception = {
                "error_class": self.error_class,
            }
            if self.error_message:
                self.exception.update({"error_message": self.error_message})
        elif self.exception and self.error_message:
            self.context["error_message"] = self.error_message

    def _process_context(self):
        self.context = dict(**self._get_thread_context(), **self.context)

    def _process_tags(self):
        tags_from_context = self._construct_tags(
            self._get_thread_context().get("_tags", [])
        )
        self.tags = list(set(tags_from_context + self.tags))

    @cached_property
    def payload(self):
        return create_payload(
            self.exception,
            self.exc_traceback,
            fingerprint=self.fingerprint,
            context=self.context,
            tags=self.tags,
            config=self.config,
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

    def _get_thread_context(self):
        if self.thread_local is None:
            return {}
        return getattr(self.thread_local, "context", {})

    def _construct_tags(self, tags):
        constructed_tags = []
        if isinstance(tags, str):
            constructed_tags = [tag.strip() for tag in tags.split(",")]
        elif isinstance(tags, list):
            constructed_tags = tags
        return constructed_tags
