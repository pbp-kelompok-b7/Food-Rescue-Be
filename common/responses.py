from rest_framework.response import Response


def success(data=None, message="OK", status=200):
    if status == 204:
        return Response(status=204)
    return Response({"success": True, "message": message, "data": data}, status=status)


def failure(message, errors, status=400):
    return Response({"success": False, "message": message, "errors": errors}, status=status)
