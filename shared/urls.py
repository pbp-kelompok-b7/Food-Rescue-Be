from django.urls import path, re_path
from . import views

urlpatterns = [
    path("reference-data/", views.reference_data),
    path("locations/search/", views.locations_search),
    path("locations/suggestions/", views.locations_suggestions),
    re_path(r"^media/photos/(?P<filename>[0-9a-f]{32}\.jpg)$", views.resource_photo),
]
