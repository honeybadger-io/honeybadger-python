from django.http import JsonResponse


def plain_view(request):
    # Test view
    return JsonResponse({})


def always_fails(request):
    raise ValueError("always fails")
