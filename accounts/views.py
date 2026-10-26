from functools import partial

from django.conf import settings
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from common.responses import failure, success
from django.views.decorators.csrf import csrf_protect
from django.middleware.csrf import get_token
from .models import User
from .serializers import GoogleLoginSerializer, UserSelfSerializer, LoginSerializer, LogoutSerializer
from .throttles import GoogleLoginThrottle, LoginThrottle


def session_response(user):
    response = success({
        "authenticated": user.is_authenticated,
        "user": UserSelfSerializer(user).data if user.is_authenticated else None,
    })
    response["Cache-Control"] = "no-store"
    return response


def google_login_error(code, message, status):
    response = failure(message, [{"field": None, "code": code, "message": message}], status=status)
    response["Cache-Control"] = "no-store"
    return response


# GET /api/v1/auth/csrf/
@api_view(['GET'])
@permission_classes([AllowAny])
def auth_csrf(request):
    token = get_token(request)
    response = success({"csrf_token": token})
    response["Cache-Control"] = "no-store"
    return response


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
@throttle_classes([LoginThrottle])
@csrf_protect
def auth_login(request):
    serializer = LoginSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    
    user = authenticate(request, **serializer.validated_data)

    if user is None:
        return failure(
            message='Kredensial tidak valid', 
            errors=[{'field': None, 'code': 'invalid_credentials', 'message': 'Kredensial tidak valid'}],
            status=401,
        )

    login(request, user)
    data = UserSelfSerializer(user).data
    response = success(data=data, message='Login berhasil') 
    response['Cache-Control'] = 'no-store'
  
    return response 



# POST /api/v1/auth/google/
@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([GoogleLoginThrottle])
@csrf_protect
def auth_google(request):
    serializer = GoogleLoginSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    if not settings.GOOGLE_CLIENT_ID:
        return google_login_error("google_unavailable", "Google Sign-In belum tersedia.", 503)

    try:
        claims = id_token.verify_oauth2_token(
            serializer.validated_data["credential"],
            partial(GoogleRequest(), timeout=10),
            audience=settings.GOOGLE_CLIENT_ID,
        )
    except ValueError:
        return google_login_error("invalid_credentials", "Kredensial tidak valid.", 401)
    except GoogleAuthError:
        return google_login_error("google_unavailable", "Google Sign-In sementara tidak tersedia.", 503)

    subject = claims.get("sub")
    if (not isinstance(subject, str) or not subject or len(subject) > 255
            or claims.get("email_verified") is not True):
        return google_login_error("invalid_credentials", "Kredensial tidak valid.", 401)

    user = User.objects.filter(google_sub=subject).first()
    if user is None:
        email = claims.get("email")
        # Validate Google's profile before persisting it in the local account.
        try:
            if not isinstance(email, str) or len(email) > 254:
                raise ValidationError("Invalid email.")
            validate_email(email)
        except ValidationError:
            return google_login_error("invalid_credentials", "Kredensial tidak valid.", 401)
        email = email.lower()
        if User.objects.filter(email__iexact=email).exists():
            return google_login_error("account_link_required", "Email sudah terdaftar. Masuk dengan metode akun yang sudah ada.", 409)
        name = claims.get("name")
        display_name = name.strip() if isinstance(name, str) else ""
        try:
            with transaction.atomic():
                user, _ = User.objects.get_or_create(
                    google_sub=subject,
                    defaults={
                        "email": email,
                        "display_name": (display_name or email.split("@")[0])[:100],
                        "password": make_password(None),
                    },
                )
        except IntegrityError:
            # A concurrent first sign-in may have created this Google account.
            user = User.objects.filter(google_sub=subject).first()
            if user is None:
                return google_login_error("account_link_required", "Email sudah terdaftar. Masuk dengan metode akun yang sudah ada.", 409)

    if not user.is_active:
        return google_login_error("invalid_credentials", "Kredensial tidak valid.", 401)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return session_response(user)


# POST /api/v1/auth/logout/
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def auth_logout(request):
    serializer = LogoutSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    logout(request)
    response = success(status=204)
    response["Cache-Control"] = "no-store"
    return response


# GET /api/v1/auth/session/
@api_view(['GET'])
@permission_classes([AllowAny])
def auth_session(request):
    return session_response(request.user)


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
