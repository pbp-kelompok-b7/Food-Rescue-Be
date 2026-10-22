from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from common.responses import failure
from django.views.decorators.csrf import csrf_protect


# GET /api/v1/auth/csrf/
@api_view(['GET'])
@permission_classes([AllowAny])
def auth_csrf(request):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# POST /api/v1/auth/register/
@api_view(['POST'])
@permission_classes([AllowAny])
@csrf_protect
def auth_register(request):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# POST /api/v1/auth/login/
@api_view(['POST'])
@permission_classes([AllowAny])
@csrf_protect
def auth_login(request):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# POST /api/v1/auth/logout/
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def auth_logout(request):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# GET /api/v1/auth/session/
@api_view(['GET'])
@permission_classes([AllowAny])
def auth_session(request):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# GET, PATCH /api/v1/users/me/
@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated])
def users_me(request):
    if request.method == "GET":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)
    if request.method == "PATCH":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# GET /api/v1/admin/users/
@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_users(request):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# GET, PATCH /api/v1/admin/users/<int:pk>/
@api_view(['GET', 'PATCH'])
@permission_classes([IsAdminUser])
def admin_users_detail(request, pk):
    if request.method == "GET":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)
    if request.method == "PATCH":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)
