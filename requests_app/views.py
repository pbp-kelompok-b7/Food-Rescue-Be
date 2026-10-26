from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import JSONParser
from rest_framework.permissions import IsAdminUser, IsAuthenticated, IsAuthenticatedOrReadOnly
from common.responses import failure
from .queries import browse_requests, request_detail
from .writes import create_request, update_request


# GET, POST /api/v1/requests/
@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticatedOrReadOnly])
@parser_classes([JSONParser])
def requests(request):
    if request.method == "GET":
        return browse_requests(request)
    if request.method == "POST":
        return create_request(request)


# GET, PATCH, DELETE /api/v1/requests/<int:pk>/
@api_view(['GET', 'PATCH', 'DELETE'])
@permission_classes([IsAuthenticatedOrReadOnly])
@parser_classes([JSONParser])
def requests_detail(request, pk):
    if request.method == "GET":
        return request_detail(request, pk)
    if request.method == "PATCH":
        return update_request(request, pk)
    if request.method == "DELETE":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# POST /api/v1/requests/<int:pk>/close/
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def requests_detail_close(request, pk):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# GET, POST /api/v1/admin/requests/
@api_view(['GET', 'POST'])
@permission_classes([IsAdminUser])
def admin_requests(request):
    if request.method == "GET":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)
    if request.method == "POST":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# GET, PATCH, DELETE /api/v1/admin/requests/<int:pk>/
@api_view(['GET', 'PATCH', 'DELETE'])
@permission_classes([IsAdminUser])
def admin_requests_detail(request, pk):
    if request.method == "GET":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)
    if request.method == "PATCH":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)
    if request.method == "DELETE":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# POST /api/v1/admin/requests/<int:pk>/close/
@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_requests_detail_close(request, pk):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)
