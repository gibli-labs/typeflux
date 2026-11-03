from pydantic import BaseModel


class Value(BaseModel):
    text: str
