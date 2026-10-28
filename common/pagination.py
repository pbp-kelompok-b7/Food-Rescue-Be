from rest_framework.pagination import PageNumberPagination
from rest_framework.exceptions import NotFound, ValidationError
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

    def paginate_queryset(self, queryset, request, view=None):
        # The contract uses the ``page_not_found`` code for an out-of-range page
        # (sections 6.1 / 7.4.3), whereas DRF raises NotFound with the generic
        # ``not_found`` code. Translate it.
        try:
            return super().paginate_queryset(queryset, request, view)
        except NotFound:
            raise NotFound("Invalid page.", code="page_not_found")

    def get_paginated_response(self, data):
        return success({"count": self.page.paginator.count, "next": self.get_next_link(),
                        "previous": self.get_previous_link(), "results": data})
