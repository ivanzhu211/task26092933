"""固定利率贷款增量还款核算库。

内存态、串行调用、不考虑崩溃恢复。还款按 (date, id) 字典序排列，
两个日期之间按未偿本金 * 年利率 * 实际天数 / 365 累计单利，未付利息不生息。

增量结构：以 (date, id) 为键的 treap。每个节点缓存该笔还款处理后的
状态（未偿本金、未付利息）与逐笔分配。任何变更只从受影响最早的排序键
起重算后缀，前缀状态直接复用，单次变更 O(logN + K)，查询 O(logN)。
"""
from __future__ import annotations

import random
import sys
from dataclasses import dataclass
from datetime import date
from typing import Callable, Iterator, Optional, Tuple

sys.setrecursionlimit(max(sys.getrecursionlimit(), 1_000_000))

_DENOMINATOR = 365 * 10000  # 天数/365 年 * 基点/10000


def _round_half_up_cents(numerator: int) -> int:
    """对非负整数 numerator / _DENOMINATOR 四舍五入到分，纯整数运算。"""
    return (2 * numerator + _DENOMINATOR) // (2 * _DENOMINATOR)


@dataclass(frozen=True)
class Allocation:
    """单笔还款的分配结果（单位：分）。"""
    to_interest: int
    to_principal: int
    unallocated: int


@dataclass(frozen=True)
class Balance:
    """某查询日的未偿本金与应付利息（单位：分）。"""
    principal: int
    interest: int


class _Node:
    __slots__ = (
        "pid", "pay_date", "cents", "key", "priority", "left", "right",
        "principal_after", "interest_after", "alloc",
    )

    def __init__(self, pid: str, pay_date: date, cents: int, priority: int) -> None:
        self.pid = pid
        self.pay_date = pay_date
        self.cents = cents
        self.key = (pay_date, pid)
        self.priority = priority
        self.left: Optional[_Node] = None
        self.right: Optional[_Node] = None
        # 该笔还款处理后的缓存状态，由后缀重算维护
        self.principal_after = 0
        self.interest_after = 0
        self.alloc = Allocation(0, 0, 0)


class LoanLedger:
    """固定利率贷款核算实例。

    :param start_date: 起息日
    :param principal_cents: 本金（分，整数）
    :param rate_bp: 年利率（基点，10000 基点 = 100%）
    :param on_allocate: 观察回调，每次实际执行一笔还款分配时以其 id 调用
    """

    def __init__(
        self,
        start_date: date,
        principal_cents: int,
        rate_bp: int,
        on_allocate: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._start_date = start_date
        self._principal0 = principal_cents
        self._rate_bp = rate_bp
        self._on_allocate = on_allocate
        self._root: Optional[_Node] = None
        self._by_id: dict[str, _Node] = {}
        self._rng = random.Random()

    # ---- 公开操作 -------------------------------------------------------

    def put(self, payment_id: str, pay_date: date, cents: int) -> None:
        """按 id 新增或替换一笔还款（允许补录过去、改日期或金额）。"""
        old = self._by_id.get(payment_id)
        new_key = (pay_date, payment_id)
        if old is not None:
            impact = min(old.key, new_key)
            self._root = self._erase(self._root, old.key)
        else:
            impact = new_key
        node = _Node(payment_id, pay_date, cents, self._rng.getrandbits(64))
        self._root = self._insert(self._root, node)
        self._by_id[payment_id] = node
        self._recompute_from(impact)

    def remove(self, payment_id: str) -> None:
        """删除一笔还款。"""
        node = self._by_id.pop(payment_id)
        impact = node.key
        self._root = self._erase(self._root, node.key)
        self._recompute_from(impact)

    def balance(self, on_date: date) -> Balance:
        """截至 on_date 全部已发生还款处理后的未偿本金与应付利息。

        只读操作，不改变任何状态。
        """
        node = self._floor_by_date(on_date)
        if node is None:
            principal, interest, last = self._principal0, 0, self._start_date
        else:
            principal, interest, last = (
                node.principal_after, node.interest_after, node.pay_date,
            )
        days = (on_date - last).days
        interest += _round_half_up_cents(principal * self._rate_bp * days)
        return Balance(principal, interest)

    def allocation(self, payment_id: str) -> Allocation:
        """该笔还款分配到利息、本金与未分配余额的金额。"""
        return self._by_id[payment_id].alloc

    # ---- treap 结构维护 -------------------------------------------------

    @staticmethod
    def _rotate_right(node: _Node) -> _Node:
        child = node.left
        node.left = child.right
        child.right = node
        return child

    @staticmethod
    def _rotate_left(node: _Node) -> _Node:
        child = node.right
        node.right = child.left
        child.left = node
        return child

    def _insert(self, root: Optional[_Node], node: _Node) -> _Node:
        if root is None:
            return node
        if node.key < root.key:
            root.left = self._insert(root.left, node)
            if root.left.priority > root.priority:
                root = self._rotate_right(root)
        else:
            root.right = self._insert(root.right, node)
            if root.right.priority > root.priority:
                root = self._rotate_left(root)
        return root

    def _erase(self, root: Optional[_Node], key: Tuple[date, str]) -> Optional[_Node]:
        if root is None:
            return None
        if key < root.key:
            root.left = self._erase(root.left, key)
        elif key > root.key:
            root.right = self._erase(root.right, key)
        else:
            if root.left is None:
                return root.right
            if root.right is None:
                return root.left
            if root.left.priority > root.right.priority:
                root = self._rotate_right(root)
                root.right = self._erase(root.right, key)
            else:
                root = self._rotate_left(root)
                root.left = self._erase(root.left, key)
        return root

    def _floor_by_key(self, key: Tuple[date, str]) -> Optional[_Node]:
        """排序键严格小于 key 的最后一笔（重算起点的前驱）。"""
        node = self._root
        best: Optional[_Node] = None
        while node is not None:
            if node.key < key:
                best = node
                node = node.right
            else:
                node = node.left
        return best

    def _floor_by_date(self, on_date: date) -> Optional[_Node]:
        """日期不晚于 on_date 的最后一笔还款。"""
        node = self._root
        best: Optional[_Node] = None
        while node is not None:
            if node.pay_date <= on_date:
                best = node
                node = node.right
            else:
                node = node.left
        return best

    def _iter_from(self, key: Tuple[date, str]) -> Iterator[_Node]:
        """按排序键顺序产出键 >= key 的全部节点，O(logN + K)。"""
        stack: list[_Node] = []
        node = self._root
        while node is not None:
            if node.key < key:
                node = node.right
            else:
                stack.append(node)
                node = node.left
        while stack:
            node = stack.pop()
            yield node
            node = node.right
            while node is not None:
                stack.append(node)
                node = node.left

    # ---- 后缀重算 --------------------------------------------------------

    def _recompute_from(self, key: Tuple[date, str]) -> None:
        """从排序键 key 起重算后缀，前缀缓存状态原样复用。"""
        prev = self._floor_by_key(key)
        if prev is None:
            principal, interest, last = self._principal0, 0, self._start_date
        else:
            principal, interest, last = (
                prev.principal_after, prev.interest_after, prev.pay_date,
            )
        for node in self._iter_from(key):
            days = (node.pay_date - last).days
            interest += _round_half_up_cents(principal * self._rate_bp * days)
            to_interest = min(node.cents, interest)
            rest = node.cents - to_interest
            to_principal = min(rest, principal)
            node.alloc = Allocation(to_interest, to_principal, rest - to_principal)
            principal -= to_principal
            interest -= to_interest
            node.principal_after = principal
            node.interest_after = interest
            last = node.pay_date
            if self._on_allocate is not None:
                self._on_allocate(node.pid)
