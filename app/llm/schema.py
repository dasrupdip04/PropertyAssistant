from enum import StrEnum

from pydantic import BaseModel, Field


class IntentType(StrEnum):
    SEARCH = "search"
    PROPERTY_QUESTION = "property_question"
    COMPARE = "compare"
    SHOW_MORE = "show_more"
    RESET = "reset"
    SMALLTALK = "smalltalk"
    OUT_OF_SCOPE = "out_of_scope"


class FilterAction(StrEnum):
    KEEP = "keep"
    SET = "set"
    CLEAR = "clear"
    UNSURE = "unsure"
    EXCLUDE = "exclude"


class CityFilterValue(BaseModel):
    action: FilterAction = FilterAction.KEEP
    raw_text: str | None = None


class SectorFilterValue(BaseModel):
    action: FilterAction = FilterAction.KEEP
    raw_text: str | None = None


class ProjectFilterValue(BaseModel):
    action: FilterAction = FilterAction.KEEP
    raw_text: str | None = None


class BHKFilterValue(BaseModel):
    action: FilterAction = FilterAction.KEEP
    values: list[int] = Field(default_factory=list)
    min_value: int | None = None
    raw_text: str | None = None


class BudgetAmountUnit(BaseModel):
    amount: float | None = None
    unit: str | None = None


class BudgetQualifier(StrEnum):
    MAX = "max"
    MIN = "min"
    AROUND = "around"
    BETWEEN = "between"
    EXACT = "exact"


class BudgetFilterValue(BaseModel):
    action: FilterAction = FilterAction.KEEP
    qualifier: BudgetQualifier | None = None
    low: BudgetAmountUnit | None = None
    high: BudgetAmountUnit | None = None
    raw_text: str | None = None


class FloorFilterValue(BaseModel):
    action: FilterAction = FilterAction.KEEP
    band: str | None = None  # low | mid | high
    min: int | None = None
    max: int | None = None
    raw_text: str | None = None


class AreaFilterValue(BaseModel):
    action: FilterAction = FilterAction.KEEP
    min: int | None = None
    max: int | None = None
    raw_text: str | None = None


class AvailabilityFilterValue(BaseModel):
    action: FilterAction = FilterAction.KEEP
    raw_value: str | list[str] | None = None


class NLUFilters(BaseModel):
    city: CityFilterValue = Field(default_factory=CityFilterValue)
    sector: SectorFilterValue = Field(default_factory=SectorFilterValue)
    project: ProjectFilterValue = Field(default_factory=ProjectFilterValue)
    bhk: BHKFilterValue = Field(default_factory=BHKFilterValue)
    budget: BudgetFilterValue = Field(default_factory=BudgetFilterValue)
    floor: FloorFilterValue = Field(default_factory=FloorFilterValue)
    area: AreaFilterValue = Field(default_factory=AreaFilterValue)
    availability: AvailabilityFilterValue = Field(default_factory=AvailabilityFilterValue)


class SortType(StrEnum):
    PRICE_ASC = "price_asc"
    PRICE_DESC = "price_desc"
    NEWEST = "newest"
    AREA_DESC = "area_desc"
    NONE = "none"


class ReferenceKind(StrEnum):
    ORDINAL = "ordinal"
    LAST = "last"
    PROPERTY_ID = "property_id"
    PROJECT_NAME = "project_name"
    SUPERLATIVE = "superlative"
    NONE = "none"


class SuperlativeKind(StrEnum):
    CHEAPEST = "cheapest"
    LARGEST = "largest"
    HIGHEST_FLOOR = "highest_floor"
    NEWEST = "newest"


class Reference(BaseModel):
    kind: ReferenceKind = ReferenceKind.NONE
    ordinal: int | None = None
    ordinals: list[int] = Field(default_factory=list)
    property_ids: list[str] = Field(default_factory=list)
    project_text: str | None = None
    superlative: SuperlativeKind | None = None


class AskedAttribute(StrEnum):
    FLOOR = "floor"
    PRICE = "price"
    AREA = "area"
    STATUS = "status"
    LOCATION = "location"
    BHK = "bhk"
    UPDATED = "updated"
    PRICE_PER_SQFT = "price_per_sqft"
    PROJECT = "project"
    OTHER = "other"


class NLUResult(BaseModel):
    intent: IntentType
    starts_new_search: bool = False
    wants_results_now: bool = False
    filters: NLUFilters = Field(default_factory=NLUFilters)
    sort: SortType = SortType.NONE
    reference: Reference = Field(default_factory=Reference)
    asked_attributes: list[AskedAttribute] = Field(default_factory=list)
