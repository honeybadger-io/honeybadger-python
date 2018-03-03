# Example Django Honeybadger app

This is an example Django application. 

## Installing

Run the following commands to create a virtual environment and install required dependencies.

```bash
virtualenv --no-site-packages env
. env/bin/activate
pip install -r requirements.txt
```

## Starting the server

You 'll need to configure honeybadger environment variables. If you don't,
exceptions won't be logged at Honeybadger's console.

```bash
export HONEYBADGER_API_KEY=<your API key>
export HONEYBADGER_ENVIRONMENT=test_environment
```

Then run 
```bash
python manage.py runserver
```

## Testing some errors

The example contains a [view](app/views.py) that reads two request arguments (`a` and `b`) and returns the result of `a / b`.
You can use this endpoint to generate some errors. You can try the following examples:

- [http://localhost:8000/api/div?a=1&b=0](http://localhost:8000/api/div?a=1&b=0). This will cause a division by zero.
- [http://localhost:8000/api/div?a=1&b=foo](http://localhost:8000/api/div?a=1&b=foo). Parameter `b` is not valid, as it's a string and not a number.

If everything has been configured properly, you'll see the results in Honeybadger console.