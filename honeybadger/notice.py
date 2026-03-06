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

    # Convenience properties for accessing/modifying payload data in before_notify
    # callbacks. Accessing any of these triggers payload generation (cached on first
    # access). After that, use these properties or notice.payload directly to read/write
    # values — changes to notice.context or notice.tags will still be reflected since
    # the payload holds references to the same objects.
    @property
    def backtrace(self):
        return self.payload.get("error", {}).get("backtrace", [])

    @backtrace.setter
    def backtrace(self, value):
        self.payload["error"]["backtrace"] = value

    @property
    def url(self):
        return self.payload.get("request", {}).get("url", "")

    @url.setter
    def url(self, value):
        self.payload["request"]["url"] = value

    @property
    def component(self):
        return self.payload.get("request", {}).get("component", "")

    @component.setter
    def component(self, value):
        self.payload["request"]["component"] = value

    @property
    def action(self):
        return self.payload.get("request", {}).get("action", "")

    @action.setter
    def action(self, value):
        self.payload["request"]["action"] = value

    @property
    def params(self):
        return self.payload.get("request", {}).get("params", {})

    @params.setter
    def params(self, value):
        self.payload["request"]["params"] = value

    @property
    def cgi_data(self):
        return self.payload.get("request", {}).get("cgi_data", {})

    @cgi_data.setter
    def cgi_data(self, value):
        self.payload["request"]["cgi_data"] = value

    @property
    def session(self):
        return self.payload.get("request", {}).get("session", {})

    @session.setter
    def session(self, value):
        self.payload["request"]["session"] = value

    @property
    def local_variables(self):
        return self.payload.get("request", {}).get("local_variables")

    @local_variables.setter
    def local_variables(self, value):
        self.payload["request"]["local_variables"] = value

    @property
    def causes(self):
        return self.payload.get("error", {}).get("causes", [])

    @causes.setter
    def causes(self, value):
        self.payload["error"]["causes"] = value

    @property
    def id(self):
        return self.payload.get("error", {}).get("token")

    @property
    def controller(self):
        return self.component

    @controller.setter
    def controller(self, value):
        self.component = value

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
