import time
import re
from honeybadger import honeybadger
from honeybadger.utils import get_duration


class DBHoneybadger:
    @staticmethod
    def django_execute(orig_exec):
        def wrapper(self, sql, params=None):
            start = time.time()
            try:
                return orig_exec(self, sql, params)
            finally:
                DBHoneybadger.execute(sql, start, params)

        return wrapper

    @staticmethod
    def execute(sql, start, params=None):
        db_config = honeybadger.config.insights_config.db
        if db_config.disabled:
            return

        q = db_config.exclude_queries
        if q and any(
            (pattern.search(sql) if hasattr(pattern, "search") else pattern in sql)
            for pattern in q
        ):
            return

        data = {
            "query": sql,
            "duration": get_duration(start),
        }

        if params and db_config.include_params:
            data["params"] = params

        honeybadger.event("db.query", data)
