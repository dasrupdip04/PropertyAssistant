from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field


class SlotStatus(StrEnum):
    SPECIFIED = "specified"
    UNSURE = "unsure"
    UNSPECIFIED = "unspecified"


class SearchFilters(BaseModel):
    city: str | None = None
    sector: str | None = None
    excluded_sectors: list[str] = Field(default_factory=list)
    project: str | None = None
    bhk_in: list[int] | None = None
    bhk_min: int | None = None
    price_min_lakh: Decimal | None = None
    price_max_lakh: Decimal | None = None
    floor_min: int | None = None
    floor_max: int | None = None
    area_min: int | None = None
    area_max: int | None = None
    availability: set[str] = Field(default_factory=lambda: {"available"})

    # Track status per slot: 'city', 'sector', 'project', 'bhk', 'budget', 'floor', 'area'
    slot_status: dict[str, SlotStatus] = Field(default_factory=dict)

    def get_slot_status(self, slot: str) -> SlotStatus:
        if slot in self.slot_status:
            return self.slot_status[slot]
        if slot == "city" and self.city is not None:
            return SlotStatus.SPECIFIED
        if slot == "sector" and self.sector is not None:
            return SlotStatus.SPECIFIED
        if slot == "project" and self.project is not None:
            return SlotStatus.SPECIFIED
        if slot in ("bhk", "bhk_in") and (self.bhk_in or self.bhk_min is not None):
            return SlotStatus.SPECIFIED
        if slot in ("budget", "price") and (
            self.price_min_lakh is not None or self.price_max_lakh is not None
        ):
            return SlotStatus.SPECIFIED
        if slot == "floor" and (self.floor_min is not None or self.floor_max is not None):
            return SlotStatus.SPECIFIED
        if slot == "area" and (self.area_min is not None or self.area_max is not None):
            return SlotStatus.SPECIFIED
        return SlotStatus.UNSPECIFIED

    def set_slot_status(self, slot: str, status: SlotStatus) -> None:
        self.slot_status[slot] = status

    def mark_unsure(self, slot: str) -> None:
        self.slot_status[slot] = SlotStatus.UNSURE

    def active_constraints(self) -> list[str]:
        active = []
        if self.city is not None:
            active.append("city")
        if self.sector is not None:
            active.append("sector")
        if self.project is not None:
            active.append("project")
        if self.bhk_in or self.bhk_min is not None:
            active.append("bhk")
        if self.price_min_lakh is not None or self.price_max_lakh is not None:
            active.append("budget")
        if self.floor_min is not None or self.floor_max is not None:
            active.append("floor")
        if self.area_min is not None or self.area_max is not None:
            active.append("area")
        return active
