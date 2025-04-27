from typing import Protocol, Dict, Any, Optional
from .types import EventsSendResult, Notice, Event


class Connection(Protocol):
    def send_notice(self, config: Any, payload: Notice) -> Optional[str]:
        """
        Send an error notice to Honeybadger.

        Args:
            config: The Honeybadger configuration object
            payload: The error payload to send

        Returns:
            The notice ID if available
        """
        ...

    def send_event(self, config: Any, payload: Event) -> Any:
        """
        Send an event to Honeybadger.

        Args:
            config: The Honeybadger configuration object
            payload: The event payload to send

        Returns:
            Implementation-specific return value
        """
        ...

    def send_events(self, config: Any, payload: Event) -> EventsSendResult:
        """
        Send event batch to Honeybadger.

        Args:
            config: The Honeybadger configuration object
            payload: The events payload to send

        Returns:
            EventsSendResult: The result of the send operation
        """
        ...
