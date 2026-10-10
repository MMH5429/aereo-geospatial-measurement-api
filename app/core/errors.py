class APIError(Exception):
    """An error with a deliberate HTTP status, rendered as {detail, error_type}."""

    def __init__(
        self,
        status_code: int,
        detail: str,
        error_type: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.error_type = error_type
        self.headers = headers
