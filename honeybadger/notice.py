from functools import cached_property
from .payload import create_payload


class Notice(object):
    def __init__(self, *args, **kwargs):
        self._exception = kwargs.get("exception", None)
        self._error_class = kwargs.get("error_class", None)
        self._error_message = kwargs.get("error_message", None)
        self._tags = kwargs.get("tags", None)
        self._context = kwargs.get("context", None)
        self._halted = False

        self.exc_traceback = kwargs.get("exc_traceback", None)
        self.fingerprint = kwargs.get("fingerprint", None)
        self.thread_local = kwargs.get("thread_local", None)
        self.config = kwargs.get("config", None)

        if self._exception is None and self._error_class is None:
            raise ValueError("Either exception or error_class must be provided")

        if self.excluded_exception():
            self._halted = True

        self.payload = create_payload(
            self.exception,
            self.exc_traceback,
            fingerprint=self.fingerprint,
            context=self.context,
            tags=self.tags,
            config=self.config,
        )

    @property
    def context(self):
        merged_context = self._get_thread_context()
        if self._context:
            merged_context.update(self._context)
        return merged_context

    @property
    def tags(self):
        merged_context = self._get_thread_context()
        tags_from_context = self._construct_tags(merged_context.get("_tags", []))
        tags_from_args = self._construct_tags(self._tags or [])
        return list(set(tags_from_context + tags_from_args))

    @property
    def notice_id(self):
        return self.payload.get("error", {}).get("token", None)

    @property
    def exception(self):
        if self._exception is None:
            if self._error_class and self._error_message:
                return {
                    "error_class": self._error_class,
                    "error_message": self._error_message,
                }
            else:
                return None

        return self._exception

    @property
    def halted(self):
        return self._halted

    @halted.setter
    def halted(self, value):
        if not isinstance(value, bool):
            raise TypeError("halted must be a boolean")
        self._halted = value

    def excluded_exception(self):
        if self.config.excluded_exceptions:
            if (
                self._exception
                and self._exception.__class__.__name__
                in self.config.excluded_exceptions
            ):
                return True
            elif (
                self._error_class
                and self._error_class in self.config.excluded_exceptions
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

    def __getitem__(self, key):
        return self.payload[key]

    def __contains__(self, key):
        return key in self.payload
