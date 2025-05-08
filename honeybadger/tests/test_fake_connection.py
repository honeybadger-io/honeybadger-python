from honeybadger.fake_connection import send_notice
from honeybadger.config import Configuration
from honeybadger.notice import Notice

from testfixtures import log_capture  # type: ignore
import json


@log_capture("honeybadger.fake_connection")
def test_send_notice_logging(l):
    config = Configuration(api_key="aaa")
    notice = Notice(
        error_class="TestError", error_message="Test message", config=config
    )

    send_notice(config, notice)

    l.check(
        (
            "honeybadger.fake_connection",
            "INFO",
            "Development mode is enabled; this error will be reported if it occurs after you deploy your app.",
        ),
    )
