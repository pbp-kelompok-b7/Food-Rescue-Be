from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from django.views.decorators.cache import never_cache
from common.responses import success
from common.serializers import StrictSerializer
from .reference_data import reference_payload
from .serializers import LocationSearchQuerySerializer, LocationSuggestionQuerySerializer
from .services import search_locations, supports_location_suggestions, LocationUnavailable


@api_view(['GET'])
@permission_classes([AllowAny])
def resource_photo(request, filename):
    """Photos follow the parent record's visibility, including after closure."""
    from django.http import FileResponse
    from django.db.models import Q
    from django.utils import timezone
    from rest_framework.exceptions import NotFound
    from listings.queries import listings_with_state
    from requests_app.views import requests_with_quantities, public_requests

    name = f"photos/{filename}"
    source = listings_with_state(timezone.now()).filter(image=name).first()
    if source is not None:
        public = (source.effective_status == 'AVAILABLE' and source.quantity_available > 0
                  and source.available_until > timezone.now() and source.archived_at is None)
    else:
        source = requests_with_quantities().filter(image=name).first()
        public = source is not None and public_requests(requests_with_quantities()).filter(pk=source.pk).exists()
    if source is None:
        raise NotFound()
    user = request.user
    authorized = user.is_authenticated and user.is_active and (
        user.is_staff or source.owner_id == user.pk
        or source.reservations.filter(Q(provider=user) | Q(receiver=user)).exists())
    if not public and not authorized:
        raise NotFound()
    try:
        response = FileResponse(source.image.open('rb'), content_type='image/jpeg')
    except FileNotFoundError as exc:
        raise NotFound() from exc
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


# GET /api/v1/reference-data/
@api_view(['GET'])
@permission_classes([AllowAny])
def reference_data(request):
    StrictSerializer(data=request.query_params).is_valid(raise_exception=True)
    return success(reference_payload())


# GET /api/v1/locations/search/
@never_cache
@api_view(['GET'])
@permission_classes([AllowAny])
def locations_search(request):
    query = LocationSearchQuerySerializer(data=request.query_params)
    query.is_valid(raise_exception=True)
    return success({"results": search_locations(**query.validated_data)})


@never_cache
@api_view(['GET'])
@permission_classes([AllowAny])
def locations_suggestions(request):
    from django.conf import settings
    query = LocationSuggestionQuerySerializer(data=request.query_params)
    query.is_valid(raise_exception=True)
    # Public Nominatim forbids autocomplete. Never send typing-triggered traffic
    # there if an operator switches the manual-search provider back to it.
    if not supports_location_suggestions(settings.LOCATION_SEARCH_URL):
        raise LocationUnavailable('Saran lokasi tidak tersedia pada penyedia ini. Gunakan pencarian manual.')
    return success({"results": search_locations(**query.validated_data)})
