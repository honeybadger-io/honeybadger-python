from django.conf import settings

# Make sure we do this only once
settings.configure(
    ALLOWED_HOSTS=['testserver']
)
