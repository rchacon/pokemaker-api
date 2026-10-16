import pytest
from pydantic import ValidationError

from pokemaker.models.card import Attack, CardCreate, CardUpdate, card_status


def test_defaults_make_a_valid_blank_card():
    card = CardCreate()
    assert card.type == "colorless"
    assert card.hp == 60
    assert card.attacks == []
    assert card_status(card) == "draft"


def test_text_is_cleaned():
    card = CardCreate(name="  Blaze\u0000​   Fox  ", flavor_text="line\none")
    assert card.name == "Blaze Fox"
    assert card.flavor_text == "line one"


@pytest.mark.parametrize(
    "data",
    [
        {"hp": 0},
        {"hp": 510},
        {"type": "cosmic"},
        {"name": "x" * 25},
        {"description": "x" * 401},
        {"attacks": [{"name": "a"}] * 3},
        {"attacks": [{"name": "a", "cost": ["fire"] * 5}]},
        {"attacks": [{"name": "a", "damage": 301}]},
        {"retreatCost": 5},
        {"unexpected": True},
    ],
)
def test_invalid_cards_are_rejected(data):
    with pytest.raises(ValidationError):
        CardCreate.model_validate(data)


def test_camel_case_aliases_round_trip():
    card = CardCreate.model_validate({"flavorText": "hi", "retreatCost": 2, "evolvesFrom": "Egg"})
    dumped = card.model_dump(by_alias=True)
    assert dumped["flavorText"] == "hi"
    assert dumped["retreatCost"] == 2
    assert dumped["evolvesFrom"] == "Egg"


def test_update_requires_version():
    with pytest.raises(ValidationError):
        CardUpdate.model_validate({"name": "x"})
    assert CardUpdate.model_validate({"name": "x", "version": 3}).version == 3


def test_status_complete_needs_name_and_named_attack():
    assert card_status(CardCreate(name="A", attacks=[Attack(name="")])) == "draft"
    assert card_status(CardCreate(name="A", attacks=[Attack(name="Hit")])) == "complete"
