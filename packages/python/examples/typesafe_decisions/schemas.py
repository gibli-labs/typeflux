from typing import Literal

from pydantic import BaseModel, Field


class Input(BaseModel):
    text: str


class RouteProbabilities(BaseModel):
    billing: float = Field(ge=0, le=1)
    technical: float = Field(ge=0, le=1)
    human_review: float = Field(ge=0, le=1)


class RouteAnswer(BaseModel):
    type: Literal["choice"]
    choice: Literal["billing", "technical", "human_review"]
    confidence: float = Field(ge=0, le=1)
    probabilities: RouteProbabilities


class Output(BaseModel):
    route: RouteAnswer

    def selected_path(self, threshold: float = 0.8) -> str:
        """Workflow code chooses the threshold; model confidence is not human approval."""
        return self.route.choice if self.route.confidence >= threshold else "human_review"
