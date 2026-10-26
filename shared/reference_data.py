"""Category seed vocabulary proposed in Technical Design Revisi 2, section 7.6.1.

Keep filter validation and the future reference-data endpoint on the same codes.
"""

from .enums import ResourceType


CATEGORY_RESOURCE_TYPES = {
    "prepared_meal": ResourceType.PACKAGED_FOOD_SURPLUS,
    "bakery": ResourceType.PACKAGED_FOOD_SURPLUS,
    "snack": ResourceType.PACKAGED_FOOD_SURPLUS,
    "coffee_grounds": ResourceType.COMPOSTABLE_FOOD_WASTE,
    "fruit_vegetable_scraps": ResourceType.COMPOSTABLE_FOOD_WASTE,
    "eggshells": ResourceType.COMPOSTABLE_FOOD_WASTE,
}
