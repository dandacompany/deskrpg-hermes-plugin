"""Names shared by the review hooks, the approval store and the routes. Changing one is a contract change."""

REVIEW_HOOKS_CAPABILITY = "review_hooks_v1"
REVIEW_MODES = ("human", "agent", "mixed")
SHARED_DIR_ENV = "DESKRPG_SHARED_DIR"
SIDECAR_RELATIVE = ("plugin-data", "deskrpg-shared", "review.sqlite")

BLOCK_SUBMIT_MESSAGE = (
    "This card needs approval before it is done. Submit your result with kanban_request_review "
    "(put a short summary in `summary`) instead of kanban_complete."
)
BLOCK_VERDICT_MESSAGE = (
    "A person makes the final decision on this card. Record your verdict with kanban_request_review "
    "(pass or fail and why in `summary`) instead of kanban_complete, or use kanban_request_changes to send it back."
)
BLOCK_RETURN_MESSAGE = (
    "This card is waiting for a person's approval. Call kanban_request_review now without changing anything."
)
BLOCK_TERMINAL_MESSAGE = (
    "Completing kanban cards from the shell is not allowed on this board. Use kanban_request_review to submit."
)
BLOCK_UNAVAILABLE_MESSAGE = (
    "The approval store is unavailable, so this card cannot be completed now. Call kanban_request_review to submit."
)
