import logging

logger = logging.getLogger(__name__)


def send_notice(config, payload):
    notice_id = payload.get("error", {}).get("token", None)
    logger.info('Development mode is enabled; this error will be reported if it occurs after you deploy your app.')
    logger.debug('The config used is {} with payload {}'.format(config, payload))
    return notice_id
