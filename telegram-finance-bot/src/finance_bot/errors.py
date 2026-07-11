class FinanceBotError(RuntimeError):
    """Base application error safe to classify at integration boundaries."""


class FinancialParseError(FinanceBotError):
    """The configured financial model returned unusable structured data."""


class ImageFormatError(FinancialParseError):
    """The receipt image cannot be sent to the vision model."""
