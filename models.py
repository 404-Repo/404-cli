from pydantic import BaseModel


class State(BaseModel):
    current_round: int
    stage: str


class Schedule(BaseModel):
    earliest_reveal_block: int
    latest_reveal_block: int