from django.urls import path
from . import views

urlpatterns = [
    path("reference-data/", views.reference_data),
    path("locations/search/", views.locations_search),
]
