"""Incremental fixed-rate loan repayment accounting.

Repayments are ordered by (date, id). State is kept in a treap keyed by
(date, id); mutations only recompute the affected suffix.
"""

from __future__ import annotations

import random
from datetime import date

_DAYS_PER_YEAR = 365
_BP_PER_UNIT = 10_000


def _round_half_up(numerator: int, denominator: int) -> int:
    """Round numerator / denominator to the nearest integer, halves up."""
    return (2 * numerator + denominator) // (2 * denominator)


class _Node:
    __slots__ = (
        "key", "prio", "left", "right", "cents",
        "principal", "interest",
        "alloc_principal", "alloc_interest", "alloc_unallocated",
    )

    def __init__(self, key: tuple[date, str], cents: int, prio: int) -> None:
        self.key = key
        self.prio = prio
        self.left = None
        self.right = None
        self.cents = cents
        self.principal = 0
        self.interest = 0
        self.alloc_principal = 0
        self.alloc_interest = 0
        self.alloc_unallocated = 0


class LoanLedger:
    """Incremental ledger for a single fixed-rate loan."""

    def __init__(
        self,
        start_date: date,
        principal_cents: int,
        rate_bp: int,
        observer=None,
    ) -> None:
        self._start_date = start_date
        self._principal0 = principal_cents
        self._rate_bp = rate_bp
        self._observer = observer
        self._root = None
        self._by_id = {}
        self._rng = random.Random()

    def put(self, id: str, date: date, cents: int) -> None:
        key = (date, id)
        old = self._by_id.get(id)
        if old is not None:
            self._root = _delete(self._root, old.key)
            impact = min(old.key, key)
        else:
            impact = key
        node = _Node(key, cents, self._rng.getrandbits(64))
        self._by_id[id] = node
        self._root = _insert(self._root, node)
        self._recompute_from(impact)

    def remove(self, id: str) -> None:
        node = self._by_id.pop(id)
        self._root = _delete(self._root, node.key)
        self._recompute_from(node.key)

    def balance(self, date: date) -> tuple[int, int]:
        node = _floor_by_date(self._root, date)
        if node is None:
            principal, interest, prev_day = self._principal0, 0, self._start_date
        else:
            principal, interest, prev_day = (
                node.principal, node.interest, node.key[0])
        days = (date - prev_day).days
        interest += _round_half_up(
            principal * self._rate_bp * days, _DAYS_PER_YEAR * _BP_PER_UNIT)
        return (principal, interest)

    def allocation(self, id: str) -> tuple[int, int, int]:
        node = self._by_id[id]
        return (node.alloc_principal, node.alloc_interest, node.alloc_unallocated)

    def _accrue(self, principal: int, days: int) -> int:
        return _round_half_up(
            principal * self._rate_bp * days, _DAYS_PER_YEAR * _BP_PER_UNIT)

    def _recompute_from(self, start_key) -> None:
        pred = _predecessor(self._root, start_key)
        if pred is None:
            principal, interest = self._principal0, 0
            prev_day = self._start_date
        else:
            principal, interest = pred.principal, pred.interest
            prev_day = pred.key[0]
        for node in _iter_from(self._root, start_key):
            interest += self._accrue(principal, (node.key[0] - prev_day).days)
            to_interest = min(node.cents, interest)
            interest -= to_interest
            remainder = node.cents - to_interest
            to_principal = min(remainder, principal)
            principal -= to_principal
            node.principal = principal
            node.interest = interest
            node.alloc_interest = to_interest
            node.alloc_principal = to_principal
            node.alloc_unallocated = remainder - to_principal
            prev_day = node.key[0]
            if self._observer is not None:
                self._observer(node.key[1])


def _rotate_left(node: _Node) -> _Node:
    child = node.right
    node.right = child.left
    child.left = node
    return child


def _rotate_right(node: _Node) -> _Node:
    child = node.left
    node.left = child.right
    child.right = node
    return child


def _insert(root, node: _Node):
    if root is None:
        return node
    if node.key < root.key:
        root.left = _insert(root.left, node)
        if root.left.prio < root.prio:
            root = _rotate_right(root)
    else:
        root.right = _insert(root.right, node)
        if root.right.prio < root.prio:
            root = _rotate_left(root)
    return root


def _merge(left, right):
    if left is None:
        return right
    if right is None:
        return left
    if left.prio < right.prio:
        left.right = _merge(left.right, right)
        return left
    right.left = _merge(left, right.left)
    return right


def _delete(root, key):
    if root is None:
        return None
    if key < root.key:
        root.left = _delete(root.left, key)
    elif key > root.key:
        root.right = _delete(root.right, key)
    else:
        root = _merge(root.left, root.right)
    return root


def _predecessor(root, key):
    """Rightmost node with key strictly less than `key`."""
    best = None
    node = root
    while node is not None:
        if node.key < key:
            best = node
            node = node.right
        else:
            node = node.left
    return best


def _floor_by_date(root, day: date):
    """Rightmost node whose payment date is not after `day`."""
    best = None
    node = root
    while node is not None:
        if node.key[0] <= day:
            best = node
            node = node.right
        else:
            node = node.left
    return best


def _iter_from(root, key):
    """In-order iteration over all nodes with key >= `key`."""
    stack = []
    node = root
    while node is not None:
        if node.key >= key:
            stack.append(node)
            node = node.left
        else:
            node = node.right
    while stack:
        node = stack.pop()
        yield node
        child = node.right
        while child is not None:
            stack.append(child)
            child = child.left
