from django.urls import path
from . import views

urlpatterns = [
    path("listings/", views.listings),
    path("listings/<str:pk>/", views.listings_detail),
    path("listings/<str:pk>/close/", views.listings_detail_close),
    path("admin/listings/", views.admin_listings),
    path("admin/listings/<int:pk>/", views.admin_listings_detail),
    path("admin/listings/<int:pk>/close/", views.admin_listings_detail_close),
]
