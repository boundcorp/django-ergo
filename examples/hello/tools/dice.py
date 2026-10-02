import random

from django_ergo.bots import bot_tool


@bot_tool
def roll_dice(sides: int = 6, count: int = 1) -> list[int]:
    """Roll `count` dice with `sides` sides each."""
    return [random.randint(1, sides) for _ in range(count)]
