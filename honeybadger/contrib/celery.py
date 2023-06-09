from honeybadger import honeybadger
from honeybadger.plugins import Plugin, default_plugin_manager


class CeleryPlugin(Plugin):
    def __init__(self):
        super().__init__("Celery")

    def supports(self, config, context):
        from celery import current_task

        """
        Check whether this is a a celery task or not.
        :param config: honeybadger configuration.
        :param context: current honeybadger configuration.
        :return: True if this is a celery task, False else.
        """
        return current_task != None

    def generate_payload(self, default_payload, config, context):
        """
        Generate payload by checking celery task object.
        :param context: current context.
        :param config: honeybadger configuration.
        :return: a dict with the generated payload.
        """
        from celery import current_task

        payload = {
            "component": current_task.__module__,
            "action": current_task.name,
            "params": {
                "args": list(current_task.request.args),
                "kwargs": current_task.request.kwargs,
            },
            "context": {
                "task_id": current_task.request.id,
                "retries": current_task.request.retries,
                "max_retries": current_task.max_retries,
            },
        }
        default_payload["request"].update(payload)
        return default_payload


class CeleryHoneybadger(object):
    def __init__(self, app, report_exceptions=False):
        self.app = app
        self.report_exceptions = report_exceptions
        default_plugin_manager.register(CeleryPlugin())
        if app is not None:
            self.init_app()

    def init_app(self):
        """
        Initialize honeybadger and listen for errors.
        """
        from celery.signals import task_failure, task_postrun

        self._initialize_honeybadger(self.app.conf)
        if self.report_exceptions:
            task_failure.connect(self._on_task_failure, weak=False)
        task_postrun.connect(self._on_task_postrun, weak=False)

    def _on_task_postrun(self, *args, **kwargs):
        """
        Callback executed after a task is finished.
        """
        honeybadger.reset_context()

    def _on_task_failure(self, *args, **kwargs):
        """
        Report exception to honeybadger when a task fails.
        """
        honeybadger.notify(exception=kwargs["exception"])

    def _initialize_honeybadger(self, config):
        """
        Initializes honeybadger using the given config object.
        :param dict config: a dict or dict-like object that contains honeybadger configuration properties.
        """
        api_key = config.get("HONEYBADGER_API_KEY")
        if not api_key:
            return
        honeybadger_config = {
            "api_key": api_key,
            "environment": config.get("HONEYBADGER_ENVIRONMENT", "development"),
        }
        honeybadger.configure(**honeybadger_config)
        honeybadger.config.set_12factor_config()  # environment should override celery settings

    def tearDowm(self):
        """
        Disconnects celery signals.
        """
        from celery.signals import task_failure, task_postrun

        task_postrun.disconnect(self._on_task_postrun)
        if self.report_exceptions:
            task_failure.disconnect(self._on_task_failure)
