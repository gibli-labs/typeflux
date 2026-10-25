"""Run from this directory with TYPESAFE_API_KEY set and typeflux[typesafe] installed."""

import json

from schemas import Input, Output

from typeflux import ChatMessage, TypeSafeProvider

questions = {
    "route": {
        "type": "choice",
        "instructions": "Which team should handle this ticket?",
        "criteria": {
            "billing": "Invoices, refunds and payments",
            "technical": "Bugs and integration problems",
            "human_review": "Ambiguous requests or cases needing human judgment",
        },
    }
}
request = Input(text="I was charged twice. Please refund the duplicate.")
result = TypeSafeProvider().structured_call(
    messages=[
        ChatMessage(role="system", content=json.dumps(questions)),
        ChatMessage(role="user", content=request.text),
    ],
    output_schema=Output,
)
assert isinstance(result, Output)
print(json.dumps({"path": result.selected_path(), **result.model_dump()}))
