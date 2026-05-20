"""Typed models for the feature-flag capabilities.

Kept narrow on purpose — the registry layer wraps these in ``ToolResult``, so
these models only describe the per-capability payload shape.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SetVariantResult(BaseModel):
    flag: str
    previous_variant: str
    new_variant: str
    noop: bool = Field(
        ...,
        description="True when previous_variant == new_variant; the configmap was not patched.",
    )


class GetVariantResult(BaseModel):
    flag: str
    variant: str


class ListVariantsResult(BaseModel):
    variants: dict[str, str] = Field(
        default_factory=dict,
        description="Mapping of flag name -> current defaultVariant.",
    )


class ResetEntry(BaseModel):
    flag: str
    from_: str = Field(..., alias="from")

    model_config = {"populate_by_name": True}


class ResetAllResult(BaseModel):
    reset_count: int
    touched: list[ResetEntry] = Field(default_factory=list)
