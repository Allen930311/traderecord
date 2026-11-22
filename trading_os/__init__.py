"""Evidence-driven trading system OS core contracts."""

from .contracts import ValidationError, validate_record, validate_strategy

__all__ = ["ValidationError", "validate_record", "validate_strategy"]
