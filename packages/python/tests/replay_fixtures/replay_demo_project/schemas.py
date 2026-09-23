from pydantic import BaseModel


class InputModel(BaseModel):
    value: str


class MiddleModel(BaseModel):
    value: str


class OutputModel(BaseModel):
    value: str


class BatchInputModel(BaseModel):
    items: list[InputModel]


class BatchModel(BaseModel):
    results: list[MiddleModel]


class FanoutModel(BaseModel):
    """Collect object of the composition fixture's parallel block (#55): fields ARE
    the branch ids; the gated ``screen`` branch's field is Optional."""

    screen: MiddleModel | None
    plain: MiddleModel


class FailModel(BaseModel):
    """Output type of the compensation fixture's failing step (#299): the provider raises
    for this schema, so the step fails and the workflow unwinds the compensation LIFO."""

    value: str


class FanModel(BaseModel):
    """Collect object for the compensation unit tests' parallel block (#299): fields ARE the
    branch ids; branch ``b`` is when-gated so its field is Optional."""

    a: MiddleModel
    b: MiddleModel | None = None
