from django.conf import settings  # type: ignore

# Make sure we do this only once
settings.configure(ALLOWED_HOSTS=["testserver"])
