# task26092933 — 固定利率贷款增量还款核算库

## 文件

- `loan_ledger.py` — 核算库（`LoanLedger`，仅标准库，无第三方依赖）
- `test_loan_ledger.py` — 自动化测试（`unittest`）

## 运行

```bash
python3 -m unittest test_loan_ledger -v
```

## 接口

```python
from datetime import date
from loan_ledger import LoanLedger

ledger = LoanLedger(
    start_date=date(2025, 1, 1),   # 起息日
    principal_cents=1_000_000,     # 本金（分，整数）
    rate_bp=1200,                  # 年利率基点（10000 = 100%）
    on_allocate=lambda pid: None,  # 可选：每笔还款实际分配时的观察回调
)
ledger.put("p1", date(2025, 2, 1), 100_000)  # 新增或替换还款
ledger.remove("p1")                          # 删除还款
ledger.balance(date(2025, 3, 1))             # -> Balance(principal, interest)
ledger.allocation("p1")                      # -> Allocation(to_interest, to_principal, unallocated)
```

## 核算规则

- 还款按 `(date, id)` 字典序处理；区间利息 = 未偿本金 × 基点 × 实际天数 / (365 × 10000)，
  四舍五入到分，全程整数运算（`(2n + d) // (2d)`），不使用浮点。
- 未付利息不生息；还款先付息、再还本，超出部分记为该笔的未分配余额，不自动抵扣未来还款；
  本金不为负。
- `Balance(date)` 取不晚于查询日的最后一笔还款后的缓存状态，计息至查询日，只读不改状态。

## 增量结构与复杂度

以 `(date, id)` 为键的 **treap**（随机优先级，期望平衡）。每个节点缓存该笔还款处理后的
未偿本金、未付利息与逐笔分配。

- **变更 `Put`/`Remove`**：删除/插入节点 O(logN)（期望），随后从本次变更旧、新排序键中
  最早者起，仅重算该后缀的 K 笔还款（就地更新节点缓存，不搬移数据），此前各笔状态直接复用。
  合计 **O(logN + K)** 期望复杂度。
- **查询 `Balance`**：按日期在 treap 中下行找到不晚于查询日的最后一笔，**O(logN)**。
- **查询 `Allocation`**：哈希表按 id 直取节点缓存，**O(1)**。
- **空间 O(N)**：每个还款一个节点加一条哈希表项，不保留历史副本。
- `on_allocate` 回调在每次实际执行一笔还款分配时触发，测试用它精确计量重算笔数，
  验证前缀零重算、每次变更核算不超过 K 笔。

## 测试覆盖

- `RandomDiffTest`：独立朴素全量实现作参照，随机混合补录、跨日期移动、改金额、删除，
  逐笔对比分配与多日期余额；含同日顺序变化用例。
- `FixedCaseTest`：部分还息、超额还款产生未分配余额；提前还清后修正历史还款，
  后续未分配款重新参与还本；`Balance` 只读性。
- `IncrementalCountTest`：10 万笔历史 + 连续 1000 次尾部变更（K ≤ 20），
  通过核算回调断言每次核算不超过 K 笔且前缀未被重算；早期还款移至末尾时
  影响起点取旧位置，并与全量结果一致。
