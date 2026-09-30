"""Tests for loan_ledger, checked against an independent naive full-replay
reference implementation."""

import bisect
import random
import unittest
from datetime import date, timedelta

from loan_ledger import LoanLedger

DEN = 365 * 10_000


def round_half_up(num, den=DEN):
    return (2 * num + den) // (2 * den)


class NaiveLedger:
    """Independent reference: replays all payments from scratch on every query."""

    def __init__(self, start, principal, rate_bp):
        self.start = start
        self.principal0 = principal
        self.rate_bp = rate_bp
        self.payments = {}  # id -> (date, cents)

    def put(self, pid, day, cents):
        self.payments[pid] = (day, cents)

    def remove(self, pid):
        del self.payments[pid]

    def _replay(self, upto=None):
        principal, interest, prev = self.principal0, 0, self.start
        allocs = {}
        for day, pid, cents in sorted(
                (d, i, c) for i, (d, c) in self.payments.items()):
            if upto is not None and day > upto:
                break
            interest += round_half_up(principal * self.rate_bp * (day - prev).days)
            to_interest = min(cents, interest)
            interest -= to_interest
            rem = cents - to_interest
            to_principal = min(rem, principal)
            principal -= to_principal
            allocs[pid] = (to_principal, to_interest, rem - to_principal)
            prev = day
        return principal, interest, prev, allocs

    def balance(self, day):
        principal, interest, prev, _ = self._replay(upto=day)
        interest += round_half_up(principal * self.rate_bp * (day - prev).days)
        return (principal, interest)

    def allocation(self, pid):
        return self._replay()[3][pid]


def make_pair(seed):
    start = date(2021, 3, 1)
    rng = random.Random(seed)
    rate = rng.choice([0, 500, 1200, 2500])
    principal = rng.choice([1, 50000, 100000, 999999])
    ledger = LoanLedger(start, principal, rate)
    naive = NaiveLedger(start, principal, rate)
    return ledger, naive, rng, start


def check_all(tc, ledger, naive, live, start):
    for pid in live:
        tc.assertEqual(ledger.allocation(pid), naive.allocation(pid),
                       f"allocation {pid}")
    for delta in (0, 17, 200, 400):
        qd = start + timedelta(days=delta)
        tc.assertEqual(ledger.balance(qd), naive.balance(qd),
                       f"balance {qd}")


class RandomizedTest(unittest.TestCase):
    def run_random(self, seed, steps):
        ledger, naive, rng, start = make_pair(seed)
        ids = [f"p{i:02d}" for i in range(15)]
        live = set()
        for step in range(steps):
            pid = rng.choice(ids)
            if rng.random() < 0.65 or pid not in live:
                day = start + timedelta(days=rng.randint(0, 300))
                if rng.random() < 0.15:
                    cents = rng.randint(100000, 300000)  # overpayment territory
                else:
                    cents = rng.randint(1, 8000)  # often only partial interest
                ledger.put(pid, day, cents)
                naive.put(pid, day, cents)
                live.add(pid)
            else:
                ledger.remove(pid)
                naive.remove(pid)
                live.discard(pid)
            check_all(self, ledger, naive, live, start)
        # queries must not mutate state: allocations unchanged after extra reads
        ledger.balance(start)
        ledger.balance(start + timedelta(days=500))
        for pid in live:
            self.assertEqual(ledger.allocation(pid), naive.allocation(pid))

    def test_random_mixed_ops_match_naive(self):
        self.run_random(seed=42, steps=400)

    def test_more_seeds(self):
        for seed in (1, 7, 99):
            self.run_random(seed=seed, steps=120)


class FixedCasesTest(unittest.TestCase):
    def test_same_day_ordering(self):
        start = date(2024, 1, 1)
        ledger = LoanLedger(start, 100000, 1200)
        naive = NaiveLedger(start, 100000, 1200)
        day = start + timedelta(days=30)
        for pid, cents in [("b", 3000), ("a", 5000), ("c", 1000)]:
            ledger.put(pid, day, cents)
            naive.put(pid, day, cents)
        for pid in "abc":
            self.assertEqual(ledger.allocation(pid), naive.allocation(pid))
        # replace "a" with a different amount on the same day: order unchanged
        ledger.put("a", day, 2000)
        naive.put("a", day, 2000)
        for pid in "abc":
            self.assertEqual(ledger.allocation(pid), naive.allocation(pid))
        self.assertEqual(ledger.balance(day), naive.balance(day))

    def test_partial_interest_and_overpayment(self):
        start = date(2024, 1, 1)
        ledger = LoanLedger(start, 100000, 1200)
        naive = NaiveLedger(start, 100000, 1200)
        ledger.put("small", date(2024, 6, 1), 100)   # less than accrued interest
        naive.put("small", date(2024, 6, 1), 100)
        ledger.put("huge", date(2024, 7, 1), 500000)  # pays everything, excess
        naive.put("huge", date(2024, 7, 1), 500000)
        self.assertEqual(ledger.allocation("small"), naive.allocation("small"))
        alloc_p, alloc_i, unalloc = ledger.allocation("huge")
        self.assertEqual((alloc_p, alloc_i, unalloc), naive.allocation("huge"))
        self.assertGreater(unalloc, 0)
        self.assertEqual(ledger.balance(date(2024, 12, 31)), (0, 0))

    def test_payoff_then_backfill_reactivates_later_payment(self):
        start = date(2024, 1, 1)
        ledger = LoanLedger(start, 100000, 1200)
        naive = NaiveLedger(start, 100000, 1200)
        ledger.put("A", date(2024, 2, 1), 120000)   # pays loan off
        naive.put("A", date(2024, 2, 1), 120000)
        ledger.put("B", date(2024, 3, 1), 50000)    # nothing owed -> unallocated
        naive.put("B", date(2024, 3, 1), 50000)
        self.assertEqual(ledger.allocation("B"), (0, 0, 50000))
        # historical edit: A was actually much smaller -> debt reappears
        ledger.put("A", date(2024, 2, 1), 40000)
        naive.put("A", date(2024, 2, 1), 40000)
        alloc_b = ledger.allocation("B")
        self.assertEqual(alloc_b, naive.allocation("B"))
        self.assertGreater(alloc_b[0], 0)          # B now repays principal
        self.assertEqual(alloc_b[2], 0)            # no longer unallocated
        self.assertGreater(ledger.balance(date(2024, 3, 1))[0], 0)
        # moving A later in time must also reopen the loan from its old slot
        ledger.put("A", date(2024, 4, 1), 40000)
        naive.put("A", date(2024, 4, 1), 40000)
        for pid in ("A", "B"):
            self.assertEqual(ledger.allocation(pid), naive.allocation(pid))
        for qd in [date(2024, 2, 15), date(2024, 5, 1), date(2025, 1, 1)]:
            self.assertEqual(ledger.balance(qd), naive.balance(qd))
        # deleting A entirely: B recomputed from the start date
        ledger.remove("A")
        naive.remove("A")
        self.assertEqual(ledger.allocation("B"), naive.allocation("B"))
        self.assertEqual(ledger.balance(date(2024, 6, 1)),
                         naive.balance(date(2024, 6, 1)))

    def test_balance_before_any_payment(self):
        start = date(2024, 1, 1)
        ledger = LoanLedger(start, 100000, 1000)
        naive = NaiveLedger(start, 100000, 1000)
        self.assertEqual(ledger.balance(start), (100000, 0))
        qd = start + timedelta(days=365)
        self.assertEqual(ledger.balance(qd), naive.balance(qd))
        self.assertEqual(ledger.balance(qd), (100000, 10000))


class IncrementalTest(unittest.TestCase):
    def build(self, n, start):
        calls = []
        ledger = LoanLedger(start, 10**12, 1150, observer=calls.append)
        amounts = {}
        keys = []  # sorted (date, id) mirror of the ledger
        for i in range(n):
            pid = f"p{i:06d}"
            day = start + timedelta(days=i // 2)
            cents = 1000 + (i % 97)
            ledger.put(pid, day, cents)
            amounts[pid] = (day, cents)
            bisect.insort(keys, (day, pid))
        self.assertEqual(len(calls), n)  # each tail insert recomputes 1 payment
        return ledger, amounts, keys, calls

    def apply_put(self, ledger, amounts, keys, pid, day, cents):
        old = amounts.get(pid)
        ledger.put(pid, day, cents)
        if old is not None:
            keys.remove((old[0], pid))
        bisect.insort(keys, (day, pid))
        amounts[pid] = (day, cents)
        old_key = (old[0], pid) if old else None
        new_key = (day, pid)
        return min(old_key, new_key) if old_key else new_key

    def test_tail_changes_recompute_only_suffix(self):
        n = 100000
        start = date(2000, 1, 1)
        ledger, amounts, keys, calls = self.build(n, start)
        rng = random.Random(7)
        for it in range(1000):
            calls.clear()
            pos = len(keys) - 1 - (it % 10)
            pid = keys[pos][1]
            if it % 2 == 0:
                impact = self.apply_put(ledger, amounts, keys, pid,
                                        amounts[pid][0], rng.randint(1, 5000))
            else:
                impact = self.apply_put(ledger, amounts, keys, pid,
                                        keys[-1][0], rng.randint(1, 5000))
            idx = bisect.bisect_left(keys, impact)
            expected_suffix = [k[1] for k in keys[idx:]]
            self.assertLessEqual(len(expected_suffix), 20)
            # exactly the suffix was recomputed, in order; prefix untouched
            self.assertEqual(calls, expected_suffix)
        # final state equals one full naive replay
        naive = NaiveLedger(start, 10**12, 1150)
        for pid, (day, cents) in amounts.items():
            naive.put(pid, day, cents)
        end = keys[-1][0]
        for qd in [start, start + timedelta(days=10000), end,
                   end + timedelta(days=90)]:
            self.assertEqual(ledger.balance(qd), naive.balance(qd))
        for pid in [keys[0][1], keys[len(keys) // 2][1], keys[-1][1]]:
            self.assertEqual(ledger.allocation(pid), naive.allocation(pid))

    def test_move_early_payment_to_end(self):
        n = 3000
        start = date(2020, 1, 1)
        ledger, amounts, keys, calls = self.build(n, start)
        # randomize a bit first so amounts/dates are not trivial
        rng = random.Random(3)
        for _ in range(200):
            pos = rng.randrange(len(keys))
            pid = keys[pos][1]
            self.apply_put(ledger, amounts, keys, pid,
                           start + timedelta(days=rng.randint(0, 4000)),
                           rng.randint(1, 9000))
        calls.clear()
        victim = keys[2][1]  # third-earliest payment
        old_key = keys[2]
        new_day = keys[-1][0] + timedelta(days=30)
        impact = self.apply_put(ledger, amounts, keys, victim, new_day, 5000)
        self.assertEqual(impact, min(old_key, (new_day, victim)))
        self.assertEqual(impact, old_key)  # impact start is the OLD position
        idx = bisect.bisect_left(keys, impact)
        expected_suffix = [k[1] for k in keys[idx:]]
        # impact start is the OLD position: nearly the whole history recomputed
        self.assertGreater(len(calls), n // 2)
        self.assertEqual(calls, expected_suffix)
        naive = NaiveLedger(start, 10**12, 1150)
        for pid, (day, cents) in amounts.items():
            naive.put(pid, day, cents)
        for pid in amounts:
            self.assertEqual(ledger.allocation(pid), naive.allocation(pid))
        for qd in [start, keys[0][0], new_day, new_day + timedelta(days=365)]:
            self.assertEqual(ledger.balance(qd), naive.balance(qd))


if __name__ == "__main__":
    unittest.main()
