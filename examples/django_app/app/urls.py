from django.conf.urls import url

from . import views

urlpatterns = [
    url(r'^greet$', views.greet, name='greet'),
    url(r'^div$', views.buggy_div, name='div'),
]