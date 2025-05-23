import logging
from .types import EventsSendResult, EventsSendStatus

logger = logging.getLogger(__name__)


def send_notice(config, notice):
    payload = notice.payload
    notice_id = payload.get("error", {}).get("token", None)
    logger.info(
        "Development mode is enabled; this error will be reported if it occurs after you deploy your app."
    )
    return notice_id


def send_events(config, payload) -> EventsSendResult:
    logger.info(
        "Development mode is enabled; this event will be reported if it occurs after you deploy your app."
    )
    logger.debug(
        "[send_events] config used is {} with payload {}".format(config, payload)
    )
    return EventsSendResult(EventsSendStatus.OK)
