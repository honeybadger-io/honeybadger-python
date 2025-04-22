import django

from . import views

if django.__version__.startswith("1.11"):
    # pylint: disable-next=no-name-in-module
    from django.conf.urls import url as path
else:
    from django.urls import path


urlpatterns = [
    path("plain_view/", views.plain_view),
    path("always_fails/", views.always_fails),
]
