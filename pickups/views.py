from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from common.responses import failure


# POST /api/v1/reservations/<int:pk>/pickup/
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def reservations_detail_pickup(request, pk):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# GET /api/v1/pickups/
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def pickups(request):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# GET, PATCH /api/v1/pickups/<int:pk>/
@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated])
def pickups_detail(request, pk):
    if request.method == "GET":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)
    if request.method == "PATCH":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# POST /api/v1/pickups/<int:pk>/complete/
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def pickups_detail_complete(request, pk):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# POST /api/v1/pickups/<int:pk>/no-show/
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def pickups_detail_no_show(request, pk):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# GET /api/v1/admin/pickups/
@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_pickups(request):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# GET, PATCH /api/v1/admin/pickups/<int:pk>/
@api_view(['GET', 'PATCH'])
@permission_classes([IsAdminUser])
def admin_pickups_detail(request, pk):
    if request.method == "GET":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)
    if request.method == "PATCH":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# POST /api/v1/admin/reservations/<int:pk>/pickup/
@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_reservations_detail_pickup(request, pk):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# POST /api/v1/admin/pickups/<int:pk>/complete/
@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_pickups_detail_complete(request, pk):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# POST /api/v1/admin/pickups/<int:pk>/no-show/
@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_pickups_detail_no_show(request, pk):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)
