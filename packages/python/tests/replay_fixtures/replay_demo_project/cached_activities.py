"""Activities for the cache-enabled map fixture (cached_map.yaml).

Kept in a separate module so the plain/lifecycle fixtures collect exactly the
activities they always did — adding coverage here never churns their recorded
histories.
"""

from replay_demo_project.schemas import InputModel, MiddleModel
from typeflux.core import AIActivity, PromptRef
from typeflux.core.contracts import SessionCacheConfig

review_item = AIActivity(
    name="review_item",
    input_type=InputModel,
    output_type=MiddleModel,
    prompt_ref=PromptRef("review_item"),
    session_cache=SessionCacheConfig(ttl_seconds=300),
)

ALL_ACTIVITIES = (review_item,)
