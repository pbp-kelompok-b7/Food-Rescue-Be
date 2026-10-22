from django.db import models


class ResourceType(models.TextChoices):
    PACKAGED_FOOD_SURPLUS = "PACKAGED_FOOD_SURPLUS"
    COMPOSTABLE_FOOD_WASTE = "COMPOSTABLE_FOOD_WASTE"


class Mode(models.TextChoices):
    DONATE = "DONATE"
    SELL = "SELL"


class Unit(models.TextChoices):
    BOX = "box"
    PCS = "pcs"
    PAKET = "paket"
    KG = "kg"


class Suitability(models.TextChoices):
    HUMAN_CONSUMPTION = "HUMAN_CONSUMPTION"
    COMPOSTING = "COMPOSTING"
    BSF = "BSF"
    ANIMAL_FEED = "ANIMAL_FEED"
    UPCYCLING = "UPCYCLING"
