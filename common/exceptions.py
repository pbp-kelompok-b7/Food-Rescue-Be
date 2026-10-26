import logging
from rest_framework.exceptions import (
    APIException,
    NotAuthenticated,
    PermissionDenied,
)
from rest_framework.views import exception_handler as default_handler


class BusinessConflict(APIException):
    status_code = 409
    default_detail = "State conflicts with this operation."
    default_code = "invalid_transition"


def flatten(value, field=None):
    if isinstance(value, dict):
        return [item for key, child in value.items() for item in flatten(child, str(key) if field is None else f"{field}.{key}")]
    if isinstance(value, (list, tuple)):
        return [item for child in value for item in flatten(child, field)]
    return [{"field": None if field == "detail" else field, "code": getattr(value, "code", "error"), "message": str(value)}]


def exception_handler(exc, context):
    response = default_handler(exc, context)
    if response is not None:
        errors = flatten(response.data)
        if isinstance(exc, NotAuthenticated):
            for error in errors:
                error["code"] = "authentication_required"
        elif (isinstance(exc, PermissionDenied) and str(exc.detail).startswith("CSRF Failed:")):
            for error in errors:
                error["code"] = "csrf_failed"
        
        response.data = {"success": False, "message": "Request failed.", "errors": errors} 
    else:
        logging.getLogger(__name__).error("Unhandled API exception", exc_info=(type(exc), exc, exc.__traceback__))
        from .responses import failure
        response = failure("Internal server error.", [{"field": None, "code": "server_error", "message": "Internal server error."}], 500)
    return response
