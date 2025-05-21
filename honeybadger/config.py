import os
import socket
import re

from dataclasses import is_dataclass, dataclass, field, fields, MISSING
from typing import List, Callable, Any, Dict, Optional, ClassVar, Union, Pattern, Tuple


def default_excluded_queries() -> List[Union[str, Pattern[Any]]]:
    return [
        re.compile(r"^PRAGMA"),
        re.compile(r"^SHOW\s"),
        re.compile(r"^SELECT .* FROM information_schema\."),
        re.compile(r"^SELECT .* FROM pg_catalog\."),
        re.compile(r"^BEGIN"),
        re.compile(r"^COMMIT"),
        re.compile(r"^ROLLBACK"),
        re.compile(r"^SAVEPOINT"),
        re.compile(r"^RELEASE SAVEPOINT"),
        re.compile(r"^ROLLBACK TO SAVEPOINT"),
        re.compile(r"^VACUUM"),
        re.compile(r"^ANALYZE"),
        re.compile(r"^SET\s"),
        re.compile(r".*django_migrations.*"),
        re.compile(r".*django_admin_log.*"),
        re.compile(r".*auth_permission.*"),
        re.compile(r".*auth_group.*"),
        re.compile(r".*auth_group_permissions.*"),
        re.compile(r".*django_session.*"),
    ]


@dataclass
class DBConfig:
    disabled: bool = False
    exclude_queries: List[Union[str, Pattern]] = field(
        default_factory=default_excluded_queries
    )
    include_params: bool = False


@dataclass
class DjangoConfig:
    disabled: bool = False
    include_params: bool = False


@dataclass
class FlaskConfig:
    disabled: bool = False
    include_params: bool = False


@dataclass
class CeleryConfig:
    disabled: bool = False
    exclude_tasks: List[Union[str, Pattern]] = field(default_factory=list)
    include_args: bool = False


@dataclass
class InsightsConfig:
    db: DBConfig = field(default_factory=DBConfig)
    django: DjangoConfig = field(default_factory=DjangoConfig)
    flask: FlaskConfig = field(default_factory=FlaskConfig)
    celery: CeleryConfig = field(default_factory=CeleryConfig)


@dataclass
class BaseConfig:
    DEVELOPMENT_ENVIRONMENTS: ClassVar[List[str]] = ["development", "dev", "test"]

    api_key: str = ""
    project_root: str = field(default_factory=os.getcwd)
    environment: str = "production"
    hostname: str = field(default_factory=socket.gethostname)
    endpoint: str = "https://api.honeybadger.io"
    params_filters: List[str] = field(
        default_factory=lambda: [
            "password",
            "password_confirmation",
            "credit_card",
            "CSRF_COOKIE",
        ]
    )
    force_report_data: bool = False
    force_sync: bool = False
    excluded_exceptions: List[str] = field(default_factory=list)
    report_local_variables: bool = False
    before_notify: Callable[[Any], Any] = lambda notice: notice

    insights_enabled: bool = False
    insights_config: InsightsConfig = field(default_factory=InsightsConfig)

    events_batch_size: int = 1000
    events_max_queue_size: int = 10_000
    events_timeout: float = 5.0
    events_max_batch_retries: int = 3
    events_throttle_wait: float = 60.0


class Configuration(BaseConfig):
    def __init__(self, **kwargs):
        valid_fields = {f.name for f in fields(self)}
        unknown = set(kwargs) - valid_fields
        if unknown:
            raise AttributeError(
                f"Unknown configuration option(s): {', '.join(sorted(unknown))}"
            )

        for k, v in list(kwargs.items()):
            field_info = next((f for f in fields(type(self)) if f.name == k), None)
            if field_info and is_dataclass(field_info.type) and isinstance(v, dict):
                kwargs[k] = dataclass_from_dict(field_info.type, v)

        super().__init__(**kwargs)
        self.set_12factor_config()
        self.set_config_from_dict(kwargs)

    def set_12factor_config(self):
        for f in fields(self):
            env_val = os.environ.get(f"HONEYBADGER_{f.name.upper()}")
            if env_val is not None:
                typ = f.type
                try:
                    if typ == list or typ == List[str]:
                        val = env_val.split(",")
                    elif typ == int:
                        val = int(env_val)
                    elif typ == bool:
                        val = env_val.lower() in ("true", "1", "yes")
                    else:
                        val = env_val
                    setattr(self, f.name, val)
                except Exception:
                    pass

    def set_config_from_dict(self, config: Dict[str, Any]):
        for k, v in config.items():
            if not hasattr(self, k):
                raise AttributeError(f"Unknown configuration option: {k}")
            current_val = getattr(self, k)
            # If current_val is a dataclass and v is a dict, merge recursively
            if hasattr(current_val, "__dataclass_fields__") and isinstance(v, dict):
                # Merge current values and updates
                current_dict = {
                    f.name: getattr(current_val, f.name) for f in fields(current_val)
                }
                merged = {**current_dict, **v}
                hydrated = dataclass_from_dict(type(current_val), merged)
                setattr(self, k, hydrated)
            else:
                setattr(self, k, v)

    def is_dev(self):
        return self.environment in self.DEVELOPMENT_ENVIRONMENTS

    @property
    def is_aws_lambda_environment(self):
        return os.environ.get("AWS_LAMBDA_FUNCTION_NAME") is not None


def dataclass_from_dict(klass, d):
    """
    Recursively build a dataclass instance from a dict.
    """
    if not is_dataclass(klass):
        return d
    allowed = {f.name for f in fields(klass)}
    unknown = set(d) - allowed
    if unknown:
        raise AttributeError(
            f"Unknown configuration option(s) for {klass.__name__}: {', '.join(sorted(unknown))}"
        )
    kwargs = {}
    for f in fields(klass):
        if f.name in d:
            val = d[f.name]
            if is_dataclass(f.type) and isinstance(val, dict):
                val = dataclass_from_dict(f.type, val)
            kwargs[f.name] = val
    return klass(**kwargs)
