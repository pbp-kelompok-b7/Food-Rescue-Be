"""Stable v1 vocabulary shared by form options and resource validation."""

from .enums import Mode, ResourceType, Suitability, Unit


CATEGORY_DEFINITIONS = (
    ("prepared_meal", "Makanan siap saji", ResourceType.PACKAGED_FOOD_SURPLUS),
    ("bakery", "Roti dan kue", ResourceType.PACKAGED_FOOD_SURPLUS),
    ("snack", "Makanan ringan", ResourceType.PACKAGED_FOOD_SURPLUS),
    ("coffee_grounds", "Ampas kopi", ResourceType.COMPOSTABLE_FOOD_WASTE),
    ("fruit_vegetable_scraps", "Sisa buah dan sayuran", ResourceType.COMPOSTABLE_FOOD_WASTE),
    ("eggshells", "Cangkang telur", ResourceType.COMPOSTABLE_FOOD_WASTE),
)
CATEGORY_RESOURCE_TYPES = {code: resource_type for code, _, resource_type in CATEGORY_DEFINITIONS}
CATEGORIES = {
    resource_type: tuple(code for code, _, kind in CATEGORY_DEFINITIONS if kind == resource_type)
    for resource_type in ResourceType.values
}


def reference_payload():
    return {
        "categories": [
            {"code": code, "label": label, "resource_type": resource_type}
            for code, label, resource_type in CATEGORY_DEFINITIONS
        ],
        "units": [
            {"code": code, "label": label, "allows_decimal": code == Unit.KG,
             "max_decimal_places": 3 if code == Unit.KG else 0}
            for code, label in ((Unit.BOX, "Box"), (Unit.PCS, "Buah"),
                                (Unit.PAKET, "Paket"), (Unit.KG, "Kilogram"))
        ],
        "resource_types": [
            {"code": ResourceType.PACKAGED_FOOD_SURPLUS, "label": "Surplus pangan kemasan"},
            {"code": ResourceType.COMPOSTABLE_FOOD_WASTE, "label": "Sisa pangan organik"},
        ],
        "suitabilities": [
            {"code": code, "label": label}
            for code, label in (
                (Suitability.HUMAN_CONSUMPTION, "Konsumsi manusia"),
                (Suitability.COMPOSTING, "Kompos"), (Suitability.BSF, "Budidaya BSF"),
                (Suitability.ANIMAL_FEED, "Pakan hewan"), (Suitability.UPCYCLING, "Upcycling"),
            )
        ],
        "modes": [{"code": Mode.DONATE, "label": "Donasi"}, {"code": Mode.SELL, "label": "Jual"}],
    }
