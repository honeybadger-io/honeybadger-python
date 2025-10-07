import os
import socket
import re
import logging

from dataclasses import is_dataclass, dataclass, field, fields, MISSING
from typing import List, Callable, Any, Dict, Optional, ClassVar, Union, Pattern, Tuple

logger = logging.getLogger(__name__)


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
class ASGIConfig:
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
    asgi: ASGIConfig = field(default_factory=ASGIConfig)


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
    development_environments: List[str] = field(
        default_factory=lambda: BaseConfig.DEVELOPMENT_ENVIRONMENTS
    )
    force_report_data: bool = False
    force_sync: bool = False
    excluded_exceptions: List[str] = field(default_factory=list)
    report_local_variables: bool = False
    before_notify: Callable[[Any], Any] = lambda notice: notice

    insights_enabled: bool = False
    insights_config: InsightsConfig = field(default_factory=InsightsConfig)

    before_event: Callable[[Any], Any] = lambda _: None

    events_sample_rate: int = 100
    events_batch_size: int = 1000
    events_max_queue_size: int = 10_000
    events_timeout: float = 5.0
    events_max_batch_retries: int = 3
    events_throttle_wait: float = 60.0


class Configuration(BaseConfig):
    def __init__(self, **kwargs):
        super().__init__()
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
        filtered = filter_and_warn_unknown(config, self.__class__)
        for k, v in filtered.items():
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
        """Returns wether you are in a dev environment or not

        Default dev environments are defined in the constant DEVELOPMENT_ENVIRONMENTS

        :rtype: bool
        """
        return self.environment in self.development_environments

    @property
    def is_aws_lambda_environment(self):
        """
        Checks if you are in an AWS Lambda environment by checking for the existence
        of "AWS_LAMBDA_FUNCTION_NAME" in the environment variables.

        :rtype: bool
        """
        return os.environ.get("AWS_LAMBDA_FUNCTION_NAME") is not None


def filter_and_warn_unknown(opts: Dict[str, Any], schema: Any) -> Dict[str, Any]:
    if is_dataclass(schema):
        if isinstance(schema, type):  # It's a class
            schema_name = schema.__name__
        else:  # It's an instance
            schema_name = type(schema).__name__
        allowed = {f.name for f in fields(schema)}
    else:
        raise TypeError(f"Expected a dataclass type or instance, got: {schema!r}")

    unknown = set(opts) - allowed
    if unknown:
        logger.warning(
            "Unknown %s option(s): %s",
            schema_name,
            ", ".join(sorted(unknown)),
        )
    return {k: opts[k] for k in opts.keys() & allowed}


def dataclass_from_dict(klass, d):
    """
    Recursively build a dataclass instance from a dict.
    """
    if not is_dataclass(klass):
        return d
    filtered = filter_and_warn_unknown(d, klass)
    kwargs = {}
    for f in fields(klass):
        if f.name in d:
            val = d[f.name]
            if is_dataclass(f.type) and isinstance(val, dict):
                val = dataclass_from_dict(f.type, val)
            kwargs[f.name] = val
    return klass(**kwargs)
