from honeybadger.contrib.db import DBConfig


@dataclass
class InsightsConfig:
    db: DBConfig = field(default_factory=DBConfig)


@dataclass
class ConfigTypes:
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
