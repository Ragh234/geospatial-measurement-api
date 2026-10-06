"""A single exception for "the uploaded file is the problem", so bad input always becomes a clear 4xx."""


class InputError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        # Set once the upload has a database row, so the client can look up the FAILED record.
        self.file_id: str | None = None
