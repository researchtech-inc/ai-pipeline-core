"""``CitedText`` and ``DocumentCitation`` — evidence-grounded prose primitives.

A ``CitedText`` field on a ``PromptContract`` output model carries prose
together with the document citations that support it. ``DocumentCitation``
is the framework-owned citation metadata: it can point at a supplied
pipeline document (``document_id``) or carry a URL citation from a
search-enabled model.

``PromptResult.citations`` mirrors the citations the framework collected
while satisfying ``CitedText`` fields and the URL citations attached by
search-enabled models.
"""

from ai_pipeline_core._pydantic_base import FrozenBaseModel

__all__ = ["CitedText", "DocumentCitation"]


class DocumentCitation(FrozenBaseModel):
    """One citation attached to a ``CitedText`` field or to a model response.

    Either ``document_id`` (pointing at a supplied pipeline document) or
    ``url`` (URL citation from a search-enabled model) is typically set;
    both may be ``None`` for citations carrying only a title or location.
    ``start_index`` / ``end_index`` are character offsets into the cited
    prose when the model surfaces them. ``field`` records which response
    field path the citation was collected from (e.g. ``"body"``,
    ``"findings[0].summary"``); engine-side URL citations leave it ``None``.
    """

    document_id: str | None = None
    url: str | None = None
    title: str | None = None
    start_index: int | None = None
    end_index: int | None = None
    field: str | None = None


class CitedText(FrozenBaseModel):
    """Evidence-grounded prose with attached document citations.

    Use ``CitedText`` for output fields whose prose must be grounded in
    supplied evidence. The model writes the prose in ``text`` and the
    supporting citations in ``citations``.
    """

    text: str
    citations: tuple[DocumentCitation, ...] = ()
