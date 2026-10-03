"""Mandate: a signed spending capability shaped like AP2's Intent Mandate.
Limits are required integers (rupees); a vague limit cannot become a mandate."""
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

KNOWN_CATEGORIES = {"milk", "atta", "diabetic_biscuits", "rice", "vegetables", "books", "electronics", "toys"}


class Limits(BaseModel):
    per_month_inr: int = Field(gt=0)
    per_order_inr: int = Field(gt=0)


class Scope(BaseModel):
    categories: list[str] = Field(min_length=1)

    @field_validator("categories")
    @classmethod
    def known(cls, v):
        bad = [c for c in v if c not in KNOWN_CATEGORIES]
        if bad:
            raise ValueError(f"unknown categories {bad}")
        return sorted(set(v))


class Merchants(BaseModel):
    block: list[str] = []
    block_reason: dict[str, str] = {}  # merchant -> reason (with date)


class Pause(BaseModel):
    start: str = Field(alias="from")  # ISO date
    end: Optional[str] = Field(default=None, alias="to")  # inclusive ISO date; None = until resumed
    categories: Optional[list[str]] = None  # None = whole mandate
    set_by: Optional[str] = None

    model_config = {"populate_by_name": True}


class Mandate(BaseModel):
    mandate_id: str
    household_id: str
    label: str
    owner: str
    scope: Scope
    limits: Limits
    merchants: Merchants = Merchants()
    members_allowed: list[str] = Field(min_length=1)
    requires_approval_from: list[str] = []
    valid_from: str
    valid_to: Optional[str] = None
    paused: list[Pause] = []
    auto_reorder: bool = False
    utterance: str = ""
    utterance_audio_hash: Optional[str] = None
    version: int = 1
    supersedes: Optional[str] = None
    signature: Optional[dict] = None

    @model_validator(mode="after")
    def order_le_month(self):
        if self.limits.per_order_inr > self.limits.per_month_inr:
            raise ValueError("per-order limit cannot exceed the monthly limit")
        return self

    def doc(self) -> dict:
        return self.model_dump(by_alias=True, mode="json")


def validate_doc(doc: dict) -> dict:
    """Round-trip a mandate document through the schema (raises on invalid)."""
    return Mandate.model_validate(doc).doc()
