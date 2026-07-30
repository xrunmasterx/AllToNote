from __future__ import annotations

from app.core.recipes.contracts import RecipeDescriptor, RecipeKey


VIDEO_COURSE_NOTE_V1 = RecipeDescriptor(
    RecipeKey("alltonote.video-course-note", 1),
    "Video course note",
    ("source",),
    ("knowledge-note",),
)
VIDEO_PRODUCER_V2 = RecipeDescriptor(
    RecipeKey("alltonote.video-producer", 2),
    "Video producer",
    ("source",),
    ("knowledge-note", "faithful-edition"),
)
VIDEO_DESCRIPTORS = (VIDEO_COURSE_NOTE_V1, VIDEO_PRODUCER_V2)


__all__ = ["VIDEO_COURSE_NOTE_V1", "VIDEO_DESCRIPTORS", "VIDEO_PRODUCER_V2"]
