"""Activities for the subject-carrier cache fixture (subjects_cached.yaml, #715).

Separate module (same convention as cached_activities.py) so the plain/lifecycle
fixtures collect exactly the activities they always did. The single activity
declares CROSS-RUN caching, so a live run through the real worker writes a cache
record — the surface the subject carrier must reach.
"""

from replay_demo_project.schemas import InputModel, OutputModel
from typeflux.core import AIActivity, PromptRef
from typeflux.core.contracts import CacheConfig

assess_subject = AIActivity(
    name="assess_subject",
    input_type=InputModel,
    output_type=OutputModel,
    prompt_ref=PromptRef("assess"),
    cache=CacheConfig(enabled=True),
)

ALL_ACTIVITIES = (assess_subject,)
