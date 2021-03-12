from honeybadger import honeybadger

honeybadger.configure(api_key='your api key')


def lambda_handler(event, context):
    """
    A buggy lambda function that tries to perform a zero division
    """
    a = 1
    b = 0

    return (a/b)
