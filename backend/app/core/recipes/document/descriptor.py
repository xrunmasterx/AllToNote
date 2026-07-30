from app.core.recipes.contracts import RecipeDescriptor, RecipeKey


DOCUMENT_NOTE_V1 = RecipeDescriptor(
    RecipeKey("alltonote.document-note", 1),
    "Document note",
    ("file",),
    ("knowledge-note",),
)


__all__ = ["DOCUMENT_NOTE_V1"]
