from rest_framework.pagination import PageNumberPagination
from rest_framework.exceptions import ValidationError
from .responses import success


class ContractPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100

    def get_page_size(self, request):
        raw = request.query_params.get("page_size")
        if raw is None:
            return self.page_size
        try:
            size = int(raw)
        except (ValueError, TypeError):
            raise ValidationError({"page_size": "Must be an integer from 1 to 100."})
        if not 1 <= size <= 100:
            raise ValidationError({"page_size": "Must be an integer from 1 to 100."})
        return size

    def get_paginated_response(self, data):
        return success({"count": self.page.paginator.count, "next": self.get_next_link(),
                        "previous": self.get_previous_link(), "results": data})
