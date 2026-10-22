from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from common.responses import failure


# POST /api/v1/listings/<int:pk>/claims/
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def listings_detail_claims(request, pk):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# POST /api/v1/requests/<int:pk>/offers/
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def requests_detail_offers(request, pk):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# GET /api/v1/reservations/
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def reservations(request):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# GET, PATCH /api/v1/reservations/<int:pk>/
@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated])
def reservations_detail(request, pk):
    if request.method == "GET":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)
    if request.method == "PATCH":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# POST /api/v1/reservations/<int:pk>/accept/
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def reservations_detail_accept(request, pk):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# POST /api/v1/reservations/<int:pk>/reject/
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def reservations_detail_reject(request, pk):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# POST /api/v1/reservations/<int:pk>/cancel/
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def reservations_detail_cancel(request, pk):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# GET /api/v1/admin/reservations/
@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_reservations(request):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# GET, PATCH /api/v1/admin/reservations/<int:pk>/
@api_view(['GET', 'PATCH'])
@permission_classes([IsAdminUser])
def admin_reservations_detail(request, pk):
    if request.method == "GET":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)
    if request.method == "PATCH":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# POST /api/v1/admin/reservations/<int:pk>/accept/
@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_reservations_detail_accept(request, pk):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# POST /api/v1/admin/reservations/<int:pk>/reject/
@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_reservations_detail_reject(request, pk):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# POST /api/v1/admin/reservations/<int:pk>/cancel/
@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_reservations_detail_cancel(request, pk):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)
