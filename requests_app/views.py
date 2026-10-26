from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAdminUser, IsAuthenticated, IsAuthenticatedOrReadOnly
from common.responses import failure
from .queries import browse_requests, request_detail


# GET, POST /api/v1/requests/
@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticatedOrReadOnly])
def requests(request):
    if request.method == "GET":
        return browse_requests(request)
    if request.method == "POST":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# GET, PATCH, DELETE /api/v1/requests/<int:pk>/
@api_view(['GET', 'PATCH', 'DELETE'])
@permission_classes([IsAuthenticatedOrReadOnly])
def requests_detail(request, pk):
    if request.method == "GET":
        return request_detail(request, pk)
    if request.method == "PATCH":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)
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
