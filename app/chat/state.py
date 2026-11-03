from pydantic import BaseModel, Field

from app.domain.filters import SearchFilters


class ShownItem(BaseModel):
    position: int
    property_id: str


class LastSearchState(BaseModel):
    search_id: int | None = None
    shown: list[ShownItem] = Field(default_factory=list)
    total: int = 0
    offset: int = 0
    sort: str | None = None

    def get_by_position(self, pos: int) -> str | None:
        for item in self.shown:
            if item.position == pos:
                return item.property_id
        return None

    def get_last(self) -> str | None:
        if self.shown:
            return self.shown[-1].property_id
        return None

    def all_ids(self) -> list[str]:
        return [item.property_id for item in self.shown]


class ChatState(BaseModel):
    filters: SearchFilters = Field(default_factory=SearchFilters)
    followups_asked: int = 0
    pending_slots: list[str] = Field(default_factory=list)
    include_unsure_hint_used: bool = False
    last_search: LastSearchState | None = None
    search_done: bool = False

    def reset_search_state(self) -> None:
        self.filters = SearchFilters()
        self.followups_asked = 0
        self.pending_slots = []
        self.include_unsure_hint_used = False
        self.last_search = None
        self.search_done = False
