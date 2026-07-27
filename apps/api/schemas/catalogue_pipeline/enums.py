"""Controlled vocabularies for catalogue pipeline contracts."""

from enum import Enum


class SourceFormat(str, Enum):
    PDF = "PDF"
    PDF_TABLE = "PDF_TABLE"
    SPREADSHEET = "SPREADSHEET"
    CSV = "CSV"
    IMAGE = "IMAGE"
    TEXT = "TEXT"
    OTHER = "OTHER"


class ExtractionMethod(str, Enum):
    OCR = "OCR"
    PDF_TEXT = "PDF_TEXT"
    SPREADSHEET_CELL = "SPREADSHEET_CELL"
    MODEL_VISION = "MODEL_VISION"
    MODEL_TEXT = "MODEL_TEXT"
    MANUAL_ENTRY = "MANUAL_ENTRY"
    OTHER = "OTHER"


class UnitCode(str, Enum):
    PIECE = "PIECE"
    UNIT = "UNIT"
    PACK = "PACK"
    BOX = "BOX"
    CASE = "CASE"
    CARTON = "CARTON"
    BOTTLE = "BOTTLE"
    BAG = "BAG"
    CAN = "CAN"
    POUCH = "POUCH"
    SACHET = "SACHET"
    TABLET = "TABLET"
    CAPSULE = "CAPSULE"
    VIAL = "VIAL"
    TUBE = "TUBE"
    TEST = "TEST"
    STRIP = "STRIP"
    KG = "KG"
    G = "G"
    OZ = "OZ"
    LB = "LB"
    L = "L"
    ML = "ML"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class ReviewRequirement(str, Enum):
    NOT_REQUIRED = "NOT_REQUIRED"
    RECOMMENDED = "RECOMMENDED"
    REQUIRED = "REQUIRED"
    BLOCKING = "BLOCKING"


class MbbScope(str, Enum):
    SUPPLIER_SKU = "SUPPLIER_SKU"
    PRODUCT_GROUP = "PRODUCT_GROUP"
    SUPPLIER_ORDER = "SUPPLIER_ORDER"


class FixedDiscountBasis(str, Enum):
    UNIT_PRICE = "UNIT_PRICE"
    SUPPLIER_SKU_TOTAL = "SUPPLIER_SKU_TOTAL"
    SUPPLIER_ORDER_TOTAL = "SUPPLIER_ORDER_TOTAL"


class MbbSelectionMethod(str, Enum):
    AUTOMATIC = "AUTOMATIC"
    OVERRIDDEN = "OVERRIDDEN"


class ResolutionState(str, Enum):
    UNRESOLVED = "UNRESOLVED"
    PROPOSED_MATCH = "PROPOSED_MATCH"
    PROPOSED_CREATE = "PROPOSED_CREATE"
    CONFIRMED_MATCH = "CONFIRMED_MATCH"
    CONFIRMED_CREATE = "CONFIRMED_CREATE"
    # Multiple existing records plausibly match; a human must pick one via a
    # candidate correction. Never approvable, never fails the run.
    AMBIGUOUS = "AMBIGUOUS"
    REJECTED = "REJECTED"


class ReviewStatus(str, Enum):
    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    APPROVED_WITH_OVERRIDE = "APPROVED_WITH_OVERRIDE"
    REJECTED = "REJECTED"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"


class ValidationStage(str, Enum):
    SOURCE_EXTRACTION = "SOURCE_EXTRACTION"
    RAW_OBSERVATION = "RAW_OBSERVATION"
    STAGING = "STAGING"
    MASTERING = "MASTERING"
    SERVING = "SERVING"


class IssueSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    BLOCKING = "BLOCKING"


class IssueResolutionStatus(str, Enum):
    OPEN = "OPEN"
    CONFIRMED = "CONFIRMED"
    CORRECTED = "CORRECTED"
    ACCEPTED_AS_IS = "ACCEPTED_AS_IS"
    DISMISSED = "DISMISSED"


class ExtractionProfileStatus(str, Enum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


class ProfileMatchStrategy(str, Enum):
    SUPPLIER_SKU = "SUPPLIER_SKU"
    BARCODE = "BARCODE"
    DESCRIPTION = "DESCRIPTION"
    COMPOSITE = "COMPOSITE"
