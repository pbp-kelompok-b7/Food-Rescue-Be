from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from common.responses import failure


# GET /api/v1/reference-data/
@api_view(['GET'])
@permission_classes([AllowAny])
def reference_data(request):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# GET /api/v1/locations/search/
@api_view(['GET'])
@permission_classes([AllowAny])
def locations_search(request):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)
