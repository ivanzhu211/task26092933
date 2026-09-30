# task26092933

固定利率贷款的增量还款核算库（纯 Python，无第三方依赖）。

## 文件

- `loan_ledger.py` — 实现：`LoanLedger(start_date, principal_cents, rate_bp, observer=None)`
  - `put(id, date, cents)`：按 id 新增或替换还款（可补录过去、改日期、改金额）
  - `remove(id)`：删除还款
  - `balance(date)` → `(未偿本金分, 应付利息分)`，只读，不改变状态
  - `allocation(id)` → `(本金分, 利息分, 未分配分)`
  - `observer`：可选回调，每实际执行一笔还款分配时以 id 调用一次
- `test_loan_ledger.py` — 自动化测试（unittest，含独立朴素全量参照实现）

## 运行

```sh
python3 -m unittest discover -v
```

## 核算规则

还款按 `(date, id)` 字典序排列。区间利息 = `round_half_up(未偿本金 × 年利率基点 × 实际天数 / (365 × 10000))`，
全部整数运算，不用浮点；未付利息不生息。还款先付息、再付本，超出部分记为该笔的
未分配余额，不抵扣未来还款；本金不为负。`balance` 取不晚于查询日的最后一笔还款后的
状态，按同样规则计息到查询日；无还款时从起息日起算。

## 复杂度

数据结构：以 `(date, id)` 为键的 treap（随机优先级平衡二叉搜索树）+ `id → 节点` 哈希表。
每个节点持久保存该笔还款处理后的本金、未付利息与逐笔分配，空间 O(N)，不保留历史副本。

- `put` / `remove`：treap 插入/删除期望 O(log N)；影响起点 = 新旧排序键中较早者，
  仅从该点中序遍历重算后缀，重算量 = 变更后从该点起的笔数 K，合计期望 O(log N + K)。
  前缀各笔状态与分配原样复用（由测试用 observer 回调逐笔断言）。
- `balance`：按日期在 treap 中下行查找最后一笔 ≤ 查询日的还款，O(log N) 期望，只读。
- `allocation`：哈希表定位，O(1)。

无全量回放、无全量排序、无线性搬移（treap 只改指针）。
