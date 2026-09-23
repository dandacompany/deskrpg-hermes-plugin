"""모델에게 카드 제안을 언제 부르고 언제 부르지 않는지 알리는 상시 시스템 프롬프트 절."""

SECTION_ID = "deskrpg_card_proposal"

SECTION_TEXT = """## Work card proposals

Call `propose_kanban_card` when the user's request fits the following:

- It has several steps or takes time, so one answer will not finish it
- Someone will need to check its progress later
- You can state a criterion for when it is done

Do not call it for: questions, requests for explanation, small talk, requests you can answer right away,
or when the user already asked explicitly for a card (then create it directly with the kanban tools).

After calling the tool, say in one sentence that you proposed a card, and do not ask whether to register it —
the user sees buttons for that. Do not say you created a card. It has not been created yet.
"""
