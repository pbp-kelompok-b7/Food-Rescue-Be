from django.urls import path
from . import views

urlpatterns = [
    path("listings/", views.listings),
    path("listings/<int:pk>/", views.listings_detail),
    path("listings/<int:pk>/close/", views.listings_detail_close),
    path("admin/listings/", views.admin_listings),
    path("admin/listings/<int:pk>/", views.admin_listings_detail),
    path("admin/listings/<int:pk>/close/", views.admin_listings_detail_close),
]
