from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from django.views.decorators.cache import never_cache
from common.responses import success
from common.serializers import StrictSerializer
from .reference_data import reference_payload
from .serializers import LocationSearchQuerySerializer
from .services import search_locations


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
