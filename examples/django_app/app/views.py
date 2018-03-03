# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render

# Create your views here.


def greet(request):
    return JsonResponse({'message': 'Hello world!'})


def buggy_div(request):
    """
    A buggy endpoint to perform division between query parameters a and b. It will fail if b is equal to 0 or
    either a or b are not float.

    :param request: request object
    :return:
    """
    a = float(request.GET.get('a', '0'))
    b = float(request.GET.get('b', '0'))
    return JsonResponse({'result': a / b})