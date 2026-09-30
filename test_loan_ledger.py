"""loan_ledger 的自动化测试。

包含：朴素全量参照实现的随机差分测试、固定回归案例、
10 万笔历史下的增量核算计数测试、早期还款移至末尾的影响起点测试。
"""
import random
import unittest
from datetime import date, timedelta

from loan_ledger import Allocation, Balance, LoanLedger, _round_half_up_cents

START = date(2025, 1, 1)
PRINCIPAL = 1_000_000
RATE_BP = 1200


class NaiveLedger:
    """独立朴素参照实现：每次变更后从起息日全量重算。"""

    def __init__(self, start_date, principal, rate_bp):
        self.start = start_date
        self.principal0 = principal
        self.rate_bp = rate_bp
        self.payments = {}  # id -> (date, cents)

    def put(self, pid, pay_date, cents):
        self.payments[pid] = (pay_date, cents)

    def remove(self, pid):
        del self.payments[pid]

    def _replay(self, upto=None):
        ordered = sorted(
            ((d, pid, c) for pid, (d, c) in self.payments.items())
        )
        principal, interest, last = self.principal0, 0, self.start
        allocs = {}
        for pay_date, pid, cents in ordered:
            if upto is not None and pay_date > upto:
                break
            days = (pay_date - last).days
            interest += _round_half_up_cents(principal * self.rate_bp * days)
            to_interest = min(cents, interest)
            rest = cents - to_interest
            to_principal = min(rest, principal)
            allocs[pid] = Allocation(to_interest, to_principal, rest - to_principal)
            principal -= to_principal
            interest -= to_interest
            last = pay_date
        return principal, interest, last, allocs

    def allocation(self, pid):
        return self._replay()[3][pid]

    def balance(self, on_date):
        principal, interest, last, _ = self._replay(upto=on_date)
        days = (on_date - last).days
        interest += _round_half_up_cents(principal * self.rate_bp * days)
        return Balance(principal, interest)


def make_pair(on_allocate=None):
    return (
        LoanLedger(START, PRINCIPAL, RATE_BP, on_allocate=on_allocate),
        NaiveLedger(START, PRINCIPAL, RATE_BP),
    )


def assert_same_state(testcase, fast, naive, probe_dates):
    _, _, _, allocs = naive._replay()
    for pid, alloc in allocs.items():
        testcase.assertEqual(fast.allocation(pid), alloc, f"allocation {pid}")
    for d in probe_dates:
        testcase.assertEqual(fast.balance(d), naive.balance(d), f"balance {d}")


class RandomDiffTest(unittest.TestCase):
    """随机混合补录、跨日期移动、改金额、删除，与朴素全量实现逐笔对比。"""

    def test_random_mixed_ops(self):
        rng = random.Random(20260930)
        fast, naive = make_pair()
        live_ids = []
        counter = 0
        probe_dates = [START + timedelta(days=o) for o in (0, 30, 77, 200, 400)]

        for step in range(600):
            op = rng.random()
            if not live_ids or op < 0.45:
                # 新增（可能补录过去，也可能同日不同 id）
                pid = f"p{counter:04d}"
                counter += 1
                d = START + timedelta(days=rng.randrange(0, 150))
                # 金额分布：小额（只够部分利息）、正常、巨额（超额还款）
                roll = rng.random()
                if roll < 0.3:
                    cents = rng.randrange(1, 2_000)
                elif roll < 0.85:
                    cents = rng.randrange(2_000, 200_000)
                else:
                    cents = rng.randrange(200_000, 3_000_000)
                fast.put(pid, d, cents)
                naive.put(pid, d, cents)
                live_ids.append(pid)
            elif op < 0.75:
                # 替换：跨日期移动或改金额
                pid = rng.choice(live_ids)
                d = START + timedelta(days=rng.randrange(0, 150))
                cents = rng.randrange(1, 500_000)
                fast.put(pid, d, cents)
                naive.put(pid, d, cents)
            else:
                # 删除
                pid = rng.choice(live_ids)
                fast.remove(pid)
                naive.remove(pid)
                live_ids.remove(pid)
            if step % 7 == 0:
                assert_same_state(self, fast, naive, probe_dates)
        assert_same_state(self, fast, naive, probe_dates)

    def test_same_date_ordering(self):
        """同日多笔还款按 id 字典序处理，顺序变化影响分配。"""
        fast, naive = make_pair()
        d = START + timedelta(days=60)
        for pid, cents in [("b", 50_000), ("a", 50_000), ("c", 50_000)]:
            fast.put(pid, d, cents)
            naive.put(pid, d, cents)
        assert_same_state(self, fast, naive, [d, d + timedelta(days=10)])
        # 把 a 改名为 z（删除+新增等价于 id 变化），同日顺序改变
        fast.remove("a")
        naive.remove("a")
        fast.put("z", d, 50_000)
        naive.put("z", d, 50_000)
        assert_same_state(self, fast, naive, [d, d + timedelta(days=10)])


class FixedCaseTest(unittest.TestCase):
    def test_partial_interest_and_overpay(self):
        """部分还息 + 超额还款产生未分配余额。"""
        fast, naive = make_pair()
        fast.put("tiny", START + timedelta(days=90), 100)  # 不够付利息
        naive.put("tiny", START + timedelta(days=90), 100)
        alloc = fast.allocation("tiny")
        self.assertEqual(alloc.to_interest, 100)
        self.assertEqual(alloc.to_principal, 0)
        fast.put("huge", START + timedelta(days=120), 5_000_000)
        naive.put("huge", START + timedelta(days=120), 5_000_000)
        alloc = fast.allocation("huge")
        self.assertGreater(alloc.unallocated, 0)
        self.assertEqual(fast.balance(START + timedelta(days=120)).principal, 0)
        assert_same_state(self, fast, naive, [START + timedelta(days=200)])

    def test_backfill_revives_paid_off_loan(self):
        """提前还清后修正历史还款，后续未分配款重新参与还本。"""
        fast, naive = make_pair()
        # 两笔巨额还款足以还清贷款，第二笔产生未分配余额
        fast.put("p1", START + timedelta(days=30), 900_000)
        fast.put("p2", START + timedelta(days=60), 500_000)
        naive.put("p1", START + timedelta(days=30), 900_000)
        naive.put("p2", START + timedelta(days=60), 500_000)
        self.assertGreater(fast.allocation("p2").unallocated, 0)
        self.assertEqual(fast.balance(START + timedelta(days=60)).principal, 0)
        # 修正历史：p1 实际只还了 200_000，贷款重新出现欠款，
        # p2 原先的未分配余额转为偿债金额
        fast.put("p1", START + timedelta(days=30), 200_000)
        naive.put("p1", START + timedelta(days=30), 200_000)
        alloc = fast.allocation("p2")
        self.assertGreater(alloc.to_principal, 0)
        self.assertEqual(alloc.unallocated, 0)
        self.assertGreater(fast.balance(START + timedelta(days=90)).principal, 0)
        assert_same_state(self, fast, naive, [START + timedelta(days=90)])

    def test_balance_is_read_only(self):
        """查询不改变状态、不触发核算回调、不影响后续核算。"""
        calls = []
        fast, naive = make_pair(on_allocate=calls.append)
        fast.put("a", START + timedelta(days=30), 100_000)
        naive.put("a", START + timedelta(days=30), 100_000)
        calls.clear()
        before = fast.balance(START + timedelta(days=90))
        for _ in range(5):
            self.assertEqual(fast.balance(START + timedelta(days=90)), before)
        self.assertEqual(calls, [])
        fast.put("b", START + timedelta(days=60), 100_000)
        naive.put("b", START + timedelta(days=60), 100_000)
        assert_same_state(self, fast, naive, [START + timedelta(days=90)])


class IncrementalCountTest(unittest.TestCase):
    def test_tail_edits_recompute_only_suffix(self):
        """10 万笔历史上连续 1000 次尾部变更，每次核算不超过 K 笔。"""
        n = 100_000
        calls = []
        fast = LoanLedger(START, PRINCIPAL * 100, RATE_BP, on_allocate=calls.append)
        ids = []
        for i in range(n):
            pid = f"p{i:06d}"
            d = START + timedelta(days=i // 10)
            fast.put(pid, d, 1_000)
            ids.append(pid)
        self.assertEqual(len(calls), n)  # 尾部追加，每次只算 1 笔

        rng = random.Random(7)
        pos_of = {pid: i for i, pid in enumerate(ids)}
        for _ in range(1000):
            calls.clear()
            idx = rng.randrange(n - 20, n)  # 尾部 20 笔之一
            pid = ids[idx]
            fast.put(pid, START + timedelta(days=idx // 10), rng.randrange(500, 5_000))
            k = n - idx  # 变更后从影响点起的笔数
            self.assertLessEqual(len(calls), k)
            # 前缀一笔都不许重算：所有被算 id 的位置 >= 影响点
            self.assertTrue(all(pos_of[c] >= idx for c in calls))

    def test_move_earliest_to_end_impacts_old_position(self):
        """把早期还款移到末尾：影响起点取旧位置，结果与全量一致。"""
        calls = []
        fast, naive = make_pair(on_allocate=calls.append)
        ids = []
        for i in range(200):
            pid = f"p{i:04d}"
            d = START + timedelta(days=i)
            fast.put(pid, d, 3_000)
            naive.put(pid, d, 3_000)
            ids.append(pid)
        calls.clear()
        victim = ids[10]
        new_date = START + timedelta(days=300)
        fast.put(victim, new_date, 3_000)
        naive.put(victim, new_date, 3_000)
        # 影响起点是旧位置：p00010..p00199 全部重算，前缀 p00000..p00009 不动
        self.assertEqual(set(calls), set(ids[10:]))
        self.assertNotIn(ids[9], calls)
        probes = [START + timedelta(days=o) for o in (5, 50, 150, 250, 400)]
        assert_same_state(self, fast, naive, probes)


if __name__ == "__main__":
    unittest.main()
