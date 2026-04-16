from typing import TypedDict


class Transaction(TypedDict):
    id: str
    date: str
    name: str
    amount: float
    category: str


class Group(TypedDict):
    month: str
    name: str
    amount: float
    count: int
    category: str


class CategorizedGroup(TypedDict):
    month: str
    name: str
    amount: float
    count: int
    category: str
    path: list[str]
