"""Card catalog fixtures.

These are real cards, with values verified against the live Duels.ink catalog
(the same ones the eval set uses). Writing them by hand rather than recording
traffic keeps the suite deterministic and the fixtures readable.
"""

CARDS: list[dict] = [
    {
        "id": "10-71",
        "fullId": "71/204 EN 10",
        "name": "Flotsam",
        "title": "Slippery as an Eel",
        "fullName": "Flotsam - Slippery as an Eel",
        "slug": "flotsam-slippery-as-an-eel",
        "type": "character",
        "colors": ["emerald"],
        "cost": 3,
        "inkable": True,
        "rarity": "common",
        "legality": ["core", "infinity"],
        "strength": 4,
        "willpower": 2,
        "lore": 1,
        "subtypes": ["Storyborn", "Ally"],
        "abilities": [{"ability": "Evasive"}],
        "specialAbilities": [],
        "rulesText": "Evasive (Only characters with Evasive can challenge this character.)",
        "flavorText": '"You\'re in our world now."',
        "imageUrl": "https://cards.duels.ink/lorcana/en/full/10-71.webp",
    },
    {
        "id": "11-97",
        "fullId": "97/204 EN 11",
        "name": "Education or Elimination",
        "title": "",
        "fullName": "Education or Elimination",
        "slug": "education-or-elimination",
        "type": "action",
        "colors": ["emerald"],
        "cost": 4,
        "inkable": True,
        "rarity": "uncommon",
        "legality": ["core", "infinity"],
        "subtypes": ["Song"],
        "rulesText": (
            "(A character with cost 4 or more can sing this song for free.) "
            "Choose one: draw a card and chosen character of yours gets +1 strength, "
            "or banish chosen damaged character."
        ),
        "imageUrl": "https://cards.duels.ink/lorcana/en/full/11-97.webp",
    },
    {
        "id": "12-133",
        "fullId": "133/204 EN 12",
        "name": "Dangerous Plan",
        "title": "",
        "fullName": "Dangerous Plan",
        "slug": "dangerous-plan",
        "type": "action",
        "colors": ["ruby"],
        "cost": 1,
        "inkable": True,
        "rarity": "common",
        "legality": ["core", "infinity"],
        "subtypes": [],
        "rulesText": "Chosen character gets +2 strength this turn.",
        "imageUrl": "https://cards.duels.ink/lorcana/en/full/12-133.webp",
    },
    {
        "id": "10-45",
        "fullId": "45/204 EN 10",
        "name": "Elsa",
        "title": "Exploring the Unknown",
        "fullName": "Elsa - Exploring the Unknown",
        "slug": "elsa-exploring-the-unknown",
        "type": "character",
        "colors": ["amethyst"],
        "cost": 3,
        "inkable": True,
        "rarity": "common",
        "legality": ["core", "infinity"],
        "strength": 1,
        "willpower": 3,
        "lore": 1,
        "subtypes": ["Dreamborn", "Hero", "Queen", "Sorcerer"],
        "rulesText": "CLOSER LOOK When you play this character, you may draw a card.",
        "imageUrl": "https://cards.duels.ink/lorcana/en/full/10-45.webp",
    },
    {
        "id": "13-80",
        "fullId": "80/204 EN 13",
        "name": "Rapunzel",
        "title": "Tower Defender",
        "fullName": "Rapunzel - Tower Defender",
        "slug": "rapunzel-tower-defender",
        "type": "character",
        "colors": ["emerald"],
        "cost": 4,
        "inkable": True,
        "rarity": "common",
        "legality": ["core", "infinity"],
        "strength": 3,
        "willpower": 3,
        "lore": 1,
        "subtypes": ["Storyborn", "Hero", "Princess"],
        "rulesText": (
            "THE FATES' DESIGN When you play this character, you may choose and discard "
            "a card. If you do, return chosen character to their player's hand."
        ),
        "imageUrl": "https://cards.duels.ink/lorcana/en/full/13-80.webp",
    },
    {
        "id": "5-195",
        "fullId": "195/204 EN 5",
        "name": "Pete",
        "title": "Games Referee",
        "fullName": "Pete - Games Referee",
        "slug": "pete-games-referee",
        "type": "character",
        "colors": ["steel"],
        "cost": 3,
        "inkable": True,
        "rarity": "uncommon",
        "legality": ["core", "infinity"],
        "strength": 3,
        "willpower": 3,
        "lore": 1,
        "subtypes": ["Dreamborn", "Villain"],
        "abilities": [],
        "specialAbilities": [
            {
                "name": "BLOW THE WHISTLE",
                "slug": "blow-the-whistle",
                "effect": "Opponents can't play actions until the start of your next turn.",
            }
        ],
        "rulesText": (
            "BLOW THE WHISTLE Opponents can't play actions until the start of your next turn."
        ),
        "imageUrl": "https://cards.duels.ink/lorcana/en/full/5-195.webp",
    },
    {
        "id": "10-103",
        "fullId": "103/204 EN 10",
        "name": "Mushu",
        "title": "Stealthy Dragon",
        "fullName": "Mushu - Stealthy Dragon",
        "slug": "mushu-stealthy-dragon",
        "type": "character",
        "colors": ["ruby"],
        "cost": 5,
        "inkable": False,
        "rarity": "rare",
        "legality": ["core", "infinity"],
        "strength": 4,
        "willpower": 4,
        "lore": 2,
        "subtypes": ["Storyborn", "Ally"],
        "abilities": [{"ability": "Rush"}],
        "specialAbilities": [],
        "rulesText": "Rush (This character can challenge the turn they're played.)",
        "imageUrl": "https://cards.duels.ink/lorcana/en/full/10-103.webp",
    },
    {
        # A location, so the location/item zone is covered too.
        "id": "13-12",
        "fullId": "12/204 EN 13",
        "name": "Corona",
        "title": "Sunlit Kingdom",
        "fullName": "Corona - Sunlit Kingdom",
        "slug": "corona-sunlit-kingdom",
        "type": "location",
        "colors": ["amber"],
        "cost": 3,
        "inkable": True,
        "rarity": "rare",
        "legality": ["core", "infinity"],
        "willpower": 6,
        "lore": 1,
        "moveCost": 1,
        "subtypes": [],
        "abilities": [{"ability": "Ward"}],
        "specialAbilities": [],
        "rulesText": "Characters gain Ward while here.",
        "imageUrl": "https://cards.duels.ink/lorcana/en/full/13-12.webp",
    },
    {
        # Resist e Singer trazem `value` - o outro formato real de `abilities`.
        "id": "7-11",
        "fullId": "11/204 EN 7",
        "name": "The Troubadour",
        "title": "Musical Narrator",
        "fullName": "The Troubadour - Musical Narrator",
        "slug": "the-troubadour-musical-narrator",
        "type": "character",
        "colors": ["amber", "steel"],
        "cost": 2,
        "inkable": True,
        "rarity": "common",
        "legality": ["core", "infinity"],
        "strength": 1,
        "willpower": 3,
        "lore": 1,
        "subtypes": ["Storyborn", "Ally"],
        "abilities": [{"ability": "Resist", "value": 1}, {"ability": "Singer", "value": 4}],
        "specialAbilities": [],
        "rulesText": (
            "Resist +1 (Damage dealt to this character is reduced by 1.) "
            "Singer 4 (This character counts as cost 4 to sing songs.)"
        ),
        "imageUrl": "https://cards.duels.ink/lorcana/en/full/7-11.webp",
    },
    {
        # A carta que me custou 2 lore por eu não ver o texto dela.
        "id": "12-77",
        "fullId": "77/204 EN 12",
        "name": "RC",
        "title": "Remote-Controlled Car",
        "fullName": "RC - Remote-Controlled Car",
        "slug": "rc-remote-controlled-car",
        "type": "character",
        "colors": ["emerald"],
        "cost": 1,
        "inkable": True,
        "rarity": "uncommon",
        "legality": ["core", "infinity"],
        "strength": 3,
        "willpower": 2,
        "lore": 2,
        "subtypes": ["Storyborn"],
        "abilities": [],
        "specialAbilities": [
            {
                "name": "LOW BATTERIES",
                "slug": "low-batteries",
                "effect": (
                    "This character can't quest or challenge unless you pay 1 [INKCOST]. "
                    "(You pay this cost each time.)"
                ),
            }
        ],
        "rulesText": (
            "LOW BATTERIES This character can't quest or challenge unless you pay "
            "1 [INKCOST]. (You pay this cost each time.)"
        ),
        "imageUrl": "https://cards.duels.ink/lorcana/en/full/12-77.webp",
    },
    {
        # The item whose question to its owner held up a four-player table:
        # a quest was refused with "Waiting for opponent to respond".
        "id": "10-167",
        "fullId": "167/204 EN 10",
        "name": "Ink Amplifier",
        "title": "",
        "fullName": "Ink Amplifier",
        "slug": "ink-amplifier",
        "type": "item",
        "colors": ["sapphire"],
        "cost": 3,
        "inkable": True,
        "rarity": "rare",
        "legality": ["core", "infinity"],
        "subtypes": [],
        "abilities": [],
        "specialAbilities": [
            {
                "name": "ENERGY CAPTURE",
                "slug": "energy-capture",
                "effect": (
                    "Whenever an opponent draws a card during their turn, if it's the second "
                    "card they've drawn this turn, you may put the top card of your deck into "
                    "your inkwell facedown and exerted."
                ),
            }
        ],
        "rulesText": (
            "ENERGY CAPTURE Whenever an opponent draws a card during their turn, if it's the "
            "second card they've drawn this turn, you may put the top card of your deck into "
            "your inkwell facedown and exerted."
        ),
        "imageUrl": "https://cards.duels.ink/lorcana/en/full/10-167.webp",
    },
]

BY_ID: dict[str, dict] = {c["id"]: c for c in CARDS}


def page(cards: list[dict], limit: int, offset: int, total: int | None = None) -> dict:
    """Build the /api/cards envelope exactly as the site returns it."""
    total = len(cards) if total is None else total
    window = cards[offset : offset + limit]
    return {
        "meta": {
            "total": total,
            "limit": limit,
            "offset": offset,
            "hasMore": total > offset + len(window),
        },
        "cards": window,
    }


def cards_in_set(set_number: int) -> list[dict]:
    """Every fixture card belonging to one set."""
    return [c for c in CARDS if c["id"].split("-", 1)[0] == str(set_number)]


def wordy_cards(count: int = 60, set_number: int = 90) -> list[dict]:
    """Stand-ins the size of real cards. Not real cards: only their weight matters.

    A late four-player game has well over a hundred cards in view - boards,
    hands, items, four discards - most of them different, each carrying a few
    hundred characters of rules text and the same text again as named
    abilities. Built rather than copied, so the JSON budget test does not need
    a hundred hand-verified records.
    """
    kinds = ("character", "character", "character", "action", "item")
    out = []
    for n in range(1, count + 1):
        kind = kinds[n % len(kinds)]
        # One named ability of ordinary length; every fifth card has two, and
        # every third a keyword with its reminder text - as real ones do.
        named = [{
            "name": f"STAND-IN RULE {n}",
            "effect": (
                "When you play this character, if you have 2 or more other "
                "characters in play, you may draw a card."
            ),
        }]
        if n % 5 == 0:
            named.append({
                "name": f"SECOND RULE {n}",
                "effect": "Chosen opposing character gets -2 {S} until the start of your next turn.",
            })
        card = {
            "id": f"{set_number}-{n}",
            "name": f"Stand-in {n}",
            "title": "Of Realistic Length",
            "fullName": f"Stand-in {n} - Of Realistic Length",
            "type": kind,
            "colors": ["amber", "sapphire"][n % 2: n % 2 + 1],
            "cost": 1 + n % 8,
            "inkable": n % 7 != 0,
            "subtypes": ["Storyborn", "Hero"] if kind == "character" else [],
            "abilities": [{"ability": "Evasive"}] if n % 3 == 0 else [],
            "specialAbilities": named,
            "rulesText": (
                ("Evasive (Only characters with Evasive can challenge this character.)\n"
                 if n % 3 == 0 else "")
                + "\n".join(f"{a['name']} {a['effect']}" for a in named)
            ),
        }
        if kind == "character":
            card.update(strength=n % 6, willpower=1 + n % 7, lore=1 + n % 3)
        out.append(card)
    return out
