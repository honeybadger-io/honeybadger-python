import os
import sys

from honeybadger import honeybadger
from honeybadger.plugins import Plugin
from honeybadger.utils import filter_dict

from threading import local

_thread_locals = local()
REQUEST_LOCAL_KEY = '__awslambda_current_request'

def current_event():
    """
    Return current execution event for this thread.
    """
    return getattr(_thread_locals, REQUEST_LOCAL_KEY, None)

def set_event(aws_event):
    """
    Set current execution event for this thread.
    """

    setattr(_thread_locals, REQUEST_LOCAL_KEY, aws_event)

def clear_event():
    """
    Clears execution event for this thread.
    """
    if hasattr(_thread_locals, REQUEST_LOCAL_KEY):
        setattr(_thread_locals, REQUEST_LOCAL_KEY, None)

def reraise(tp, value, tb=None):
    """
    Re-raises a caught error
    """
    assert value is not None
    if value.__traceback__ is not tb:
        raise value.with_traceback(tb)
    raise value
    
def get_lambda_bootstrap():
    """
    Get AWS Lambda bootstrap module
    """

    if "bootstrap" in sys.modules:
        return sys.modules["bootstrap"]
    elif "__main__" in sys.modules:
        if hasattr(sys.modules["__main__"], "bootstrap"):
            return sys.modules["__main__"].bootstrap
        return sys.modules["__main__"]
    else:
        return None

def _wrap_lambda_handler(handler):

    def wrapped_handler(aws_event, aws_context, *args, **kwargs):
        set_event(aws_event)

        honeybadger.begin_request(aws_event)
        try:
            return handler(aws_event, aws_context, *args, **kwargs)
        except Exception as e:
            honeybadger.notify(e)
            exc_info = sys.exc_info()
            clear_event()
            honeybadger.reset_context()

            #Rerase exception to proceed with normal aws error handling
            reraise(*exc_info)

    return wrapped_handler


class AWSLambdaPlugin(Plugin):

    def __init__(self):
        super(AWSLambdaPlugin, self).__init__('AWSLambda')
        lambda_bootstrap = get_lambda_bootstrap()
        self.initialize_request_handler(lambda_bootstrap)
        
    def supports(self, config, context):
        return config.is_aws_lambda_environment

    def generate_payload(self, default_payload, config, context):
        """
        Generate payload by checking the lambda's
        request event
        """
        request_payload = {
            "params": {
                "event": current_event()
            },
            "context": context
        }
        default_payload["request"] = filter_dict(request_payload, config.params_filters)

        AWS_ENV_MAP = (
            ("_HANDLER", "handler"),
            ("AWS_REGION", "region"),
            ("AWS_EXECUTION_ENV", "runtime"),
            ("AWS_LAMBDA_FUNCTION_NAME", "function"),
            ("AWS_LAMBDA_FUNCTION_MEMORY_SIZE", "memory"),
            ("AWS_LAMBDA_FUNCTION_VERSION", "version"),
            ("AWS_LAMBDA_LOG_GROUP_NAME", "log_group"),
            ("AWS_LAMBDA_LOG_STREAM_NAME", "log_name")
        )

        lambda_details = {detail[1]: os.environ.get(detail[0], None) for detail in AWS_ENV_MAP}
        default_payload["details"] = {}
        default_payload["details"]["Lambda Details"] = lambda_details
        default_payload["request"]["component"] = lambda_details["function"]
        default_payload["request"]["action"] = lambda_details["handler"]
        trace_id = os.environ.get("_X_AMZN_TRACE_ID", None)
        if trace_id:
            default_payload["request"]["context"]["lambda_trace_id"] = trace_id

        return default_payload
        
    def initialize_request_handler(self, lambda_bootstrap):
        """
        Here we fetch the http & event handler from the lambda bootstrap module
        and override it with a wrapped version
        """
        #Get the original handler for events & http request
        original_event_handler = lambda_bootstrap.handle_event_request
        original_http_handler = lambda_bootstrap.handle_http_request
        
        def event_handler(request_handler, *args, **kwargs):
            request_handler = _wrap_lambda_handler(request_handler)
            return original_event_handler(request_handler, *args, **kwargs)
            
        def http_handler(request_handler, *args, **kwargs):
            request_handler = _wrap_lambda_handler(request_handler)
            return original_http_handler(request_handler, *args, **kwargs)
        
        #Replace the original handlers for events & http request with a wrapped one
        lambda_bootstrap.handle_event_request = event_handler
        lambda_bootstrap.handle_http_request = http_handler
        
        
        
