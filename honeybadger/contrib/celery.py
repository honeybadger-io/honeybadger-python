import time
import logging

from honeybadger import honeybadger
from honeybadger.plugins import Plugin, default_plugin_manager
from honeybadger.utils import filter_dict, extract_honeybadger_config, get_duration

logger = logging.getLogger(__name__)


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

        # Ensure we have a mutable context dictionary
        context = dict(context or {})

        # Add Celery task information to context
        context.update(
            task_id=current_task.request.id,
            retries=current_task.request.retries,
            max_retries=current_task.max_retries,
        )

        payload = {
            "component": current_task.__module__,
            "action": current_task.name,
            "params": {
                "args": list(current_task.request.args),
                "kwargs": current_task.request.kwargs,
            },
            "context": context,
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
        from celery.signals import (
            task_failure,
            task_postrun,
            task_prerun,
            before_task_publish,
            worker_process_init,
        )

        self._task_starts = {}
        self._initialize_honeybadger(self.app.conf)

        if self.report_exceptions:
            task_failure.connect(self._on_task_failure, weak=False)
        task_postrun.connect(self._on_task_postrun, weak=False)

        if honeybadger.config.insights_enabled:
            # Enable task events, as we need to listen to
            # task-finished events
            worker_process_init.connect(self._on_worker_process_init, weak=False)
            task_prerun.connect(self._on_task_prerun, weak=False)
            before_task_publish.connect(self._on_before_task_publish, weak=False)

    def _initialize_honeybadger(self, config):
        """
        Initializes honeybadger using the given config object.
        :param dict config: a dict or dict-like object that contains honeybadger configuration properties.
        """
        config_kwargs = extract_honeybadger_config(config)

        if not config_kwargs.get("api_key"):
            return

        honeybadger.configure(**config_kwargs)
        honeybadger.config.set_12factor_config()  # environment should override celery settings

    def _on_worker_process_init(self, *args, **kwargs):
        # Restart the events worker to ensure it is running in the new worker
        # process.
        try:
            honeybadger.events_worker.restart()
        except Exception as e:
            logger.warning(f"Warning: Failed to restart Honeybadger events worker: {e}")

    def _on_before_task_publish(self, sender=None, body=None, headers=None, **kwargs):
        # Inject Honeybadger event context into task headers
        if headers is not None:
            current_context = honeybadger._get_event_context()
            if current_context:
                headers["honeybadger_event_context"] = current_context

    def _on_task_prerun(self, task_id=None, task=None, *args, **kwargs):
        self._task_starts[task_id] = time.time()

        if task:
            context = getattr(task.request, "honeybadger_event_context", None)
            if context:
                honeybadger.set_event_context(context)

    def _on_task_postrun(self, task_id=None, task=None, *args, **kwargs):
        """
        Callback executed after a task is finished.
        """

        insights_config = honeybadger.config.insights_config

        exclude = insights_config.celery.exclude_tasks
        should_exclude = exclude and any(
            (
                pattern.search(task.name)
                if hasattr(pattern, "search")
                else pattern == task.name
            )
            for pattern in exclude
        )

        if (
            honeybadger.config.insights_enabled
            and not insights_config.celery.disabled
            and not should_exclude
        ):
            payload = {
                "task_id": task_id,
                "task_name": task.name,
                "retries": task.request.retries,
                "group": task.request.group,
                "state": kwargs["state"],
                "duration": get_duration(self._task_starts.pop(task_id, None)),
            }

            if insights_config.celery.include_args:
                payload["args"] = task.request.args
                payload["kwargs"] = filter_dict(
                    task.request.kwargs,
                    honeybadger.config.params_filters,
                    remove_keys=True,
                )

            honeybadger.event("celery.task_finished", payload)

        honeybadger.reset_context()

    def _on_task_failure(self, *args, **kwargs):
        """
        Report exception to honeybadger when a task fails.
        """
        honeybadger.notify(exception=kwargs["exception"])

    def tearDown(self):
        """
        Disconnects celery signals.
        """
        from celery.signals import task_failure, task_postrun

        task_postrun.disconnect(self._on_task_postrun)
        if self.report_exceptions:
            task_failure.disconnect(self._on_task_failure)

        if honeybadger.config.insights_enabled:
            from celery.signals import (
                task_prerun,
                worker_process_init,
                before_task_publish,
            )

            task_prerun.disconnect(self._on_task_prerun)
            worker_process_init.disconnect(self._on_worker_process_init, weak=False)
            before_task_publish.disconnect(self._on_before_task_publish, weak=False)

    # Keep the misspelled method for backward compatibility
    def tearDowm(self):
        """
        Disconnects celery signals. (backward compatibility method)
        """
        self.tearDown()
