"""Prompt fixtures - the shapes captured from the live UI.

Each one was observed by watching what the real client sends back, which is how
the nested `response.promptId` requirement was found (see docs/PROTOCOL.md).
"""

from .games import FIELD_ELSA, FIELD_RAPUNZEL, HAND_FLOTSAM, HAND_MUSHU, HAND_SONG

SELECT_TRIGGER = {
    "id": "prompt-trigger-1",
    "player": 1,
    "type": "select_trigger",
    "message": "chooseTriggerOrder",
    "required": True,
    "triggers": [
        {
            "id": "trigger-1",
            "sourceCardInstanceId": FIELD_RAPUNZEL,
            "sourceCardId": "13-80",
            "sourceCardName": "Rapunzel - Tower Defender",
            "abilityName": "THE FATES' DESIGN",
            "abilityDescription": (
                "When you play this character, you may choose and discard a card. "
                "If you do, return chosen character to their player's hand."
            ),
            "optional": True,
            "status": "pending",
        }
    ],
}

BOOLEAN = {
    "id": "prompt-boolean-1",
    "player": 1,
    "type": "boolean",
    "message": "chooseOne",
    "sourceCardInstanceId": FIELD_ELSA,
    "sourceAbility": "EDUCATION OR ELIMINATION",
    "required": True,
    "yesLabel": "optionDrawAndBuff",
    "noLabel": "optionBanishDamagedCharacter",
    "yesDescription": "Draw a card and chosen character gets +1 strength.",
    "noDescription": "Banish chosen damaged character.",
    "recommendedChoice": "neither",
}

SELECT_TARGET = {
    "id": "prompt-target-1",
    "player": 1,
    "type": "select_target",
    "message": "chooseYourCharacter",
    "validTargets": [FIELD_ELSA, FIELD_RAPUNZEL],
    "sourceCardInstanceId": FIELD_ELSA,
    "sourceAbility": "EDUCATION OR ELIMINATION",
    "required": True,
    "minSelect": 1,
    "maxSelect": 1,
    "intent": "beneficial",
    "selfDirected": True,
}

SELECT_CARD = {
    "id": "prompt-card-1",
    "player": 1,
    "type": "select_card",
    "message": "chooseCardToDiscard",
    "required": True,
    "minSelect": 1,
    "maxSelect": 1,
    # The engine lists the choices under cardInstanceIds, and expects the
    # answer under that same key - see TestPrompts.test_select_card_*.
    "cardInstanceIds": ["inst-hand-song"],
}

ORDER_CARDS = {
    "id": "prompt-order-1",
    "player": 1,
    "type": "order_cards",
    "message": "orderCardsBottomOfDeck",
    "required": True,
    # Offered under cardInstanceIds, like select_card - but answered under
    # orderedCardInstanceIds. See TestPrompts.test_order_cards_*.
    "cardInstanceIds": [HAND_FLOTSAM, HAND_SONG, HAND_MUSHU],
}

SELECT_NUMERIC = {
    "id": "prompt-numeric-1",
    "player": 1,
    "type": "select_numeric",
    "message": "chooseAmount",
    "required": True,
    "min": 1,
    "max": 3,
}
