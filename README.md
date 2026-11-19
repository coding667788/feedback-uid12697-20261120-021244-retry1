# authplanner —— 完全离线的构建代理权限清单规划器

只处理团队提供的**虚构**能力标识、项目、角色与申报需求。
本工具**不连接任何真实构建代理，不执行任何系统权限变更**：
求解结果是一份 JSON 计划，`rollout` 只把模拟变更写入本地 SQLite 台账。

- 运行环境：Windows 原生 Python 3.12（开发与测试版本 3.12.8），**仅标准库**，SQLite 为可选持久化（`sqlite3` 同样是标准库）。
- 网络访问：无。代码中不存在任何 socket / HTTP 调用。
- 候选授权包上限：24 个（状态空间上限 2^24，约 1677 万，整数位掩码承载）。

## 1. 目录结构

```
.
├── planner.py                 # 根启动器，支持 python -I 隔离模式
├── authplanner/
│   ├── __main__.py            # python -m authplanner 入口
│   ├── cli.py                 # solve / verify / rollout / status 四个子命令
│   ├── model.py               # 输入校验、位掩码预计算、状态不变量、单步转移、依赖环检测
│   ├── solver.py              # BFS 完整穷举、三级目标择优、字典序最短路径
│   ├── verify.py              # 独立重放核对（含最优性复核与不可达报告复核）
│   ├── ledger.py              # SQLite 台账、代次、幂等键、链式哈希、故障注入
│   └── util.py                # 规范 JSON 与 SHA-256 哈希
├── tests/
│   ├── fixtures/              # 六个场景夹具（全部为虚构数据）
│   ├── bruteforce.py          # 独立枚举交叉校验器（不导入 solver，另一套实现）
│   ├── test_model.py
│   ├── test_solver.py
│   ├── test_verify.py
│   ├── test_rollout.py
│   ├── test_cli.py
│   └── test_bruteforce_crosscheck.py
└── README.md
```

## 2. 安装与运行

无需安装第三方依赖。在仓库根目录任选一种方式：

```bash
# 常规方式
python -m authplanner --version

# 完全隔离方式（-I 忽略环境变量与用户站点包，经根启动器引导）
python -I planner.py --version
```

运行全部测试：

```bash
python -m unittest discover -s tests -v
```

## 3. 输入清单格式

```json
{
  "packages": [
    {
      "id": "a",
      "project": "P1",
      "capabilities": [["repo", "read"]],
      "cost": 1,
      "conflicts": ["b"],
      "requires": [["pre1", "pre2"], ["pre3"]]
    }
  ],
  "projects": [
    {"id": "P1", "required": [["repo", "read"]], "forbidden": [["audit", "bypass"]]}
  ],
  "replacements": [{"remove": "old", "add": "new"}],
  "old_state": ["old"],
  "must_keep": [{"project": "P1", "capabilities": [["repo", "read"]]}],
  "notes": ["任意说明文字，不参与计算"]
}
```

字段语义：

| 字段 | 含义 |
| --- | --- |
| `packages[].capabilities` | 该包覆盖的**精确**能力对 `(scope, capability)`，作用于其 `project` |
| `packages[].cost` | 非负整数维护成本 |
| `packages[].conflicts` | 冲突包 ID 集合；输入可单向声明，加载时双向归一化 |
| `packages[].requires` | 依赖：外层列表各项为 AND；每项是候选前置包列表，**任一**候选启用即满足该 OR 组 |
| `projects[].required` | 项目必须被覆盖的能力对 |
| `projects[].forbidden` | 项目绝对禁止的能力对；覆盖禁用对的包**整包不可启用，不能分割剔除** |
| `replacements` | 输入明确允许的原子替换对（停用 `remove` 与启用 `add` 同一步完成） |
| `old_state` | 旧策略当前启用包；允许包含计划淘汰的包，允许已存在依赖环 |
| `must_keep` | 当前正在运行的必保能力，过渡期每个项目都必须持续覆盖 |

加载时会做完整结构与跨引用校验（未知包、自依赖/自冲突、重复 ID、必需与禁用重叠、旧状态违反不变量等），错误以退出码 1 报告。

## 4. 优化目标与路径规则

### 4.1 终态可行性

一个状态是合法终态，当且仅当同时满足：

1. 任意两个启用包不冲突；
2. 每个启用包的每个依赖 OR 组至少有一个前置包启用；
3. 没有任何项目被授予禁用能力对；
4. 每个项目的必保能力持续被覆盖；
5. 每个项目的 `required` 能力对全部被覆盖。

其中 1–4 是**每个过渡状态都必须满足的不变量**，第 5 项只在终态判定。

### 4.2 三级目标（按顺序字典序比较）

1. **未申报能力数最少**：所有代理在终态获得但未出现在任何项目 `required` 中的能力，
   按 `(project, scope, capability)` 三元组去重计数（同一能力标识授予两个项目计 2 次）；
2. **总成本最低**：启用包 `cost` 之和；
3. **包 ID 序列择先**：排序后的启用包 ID 元组字典序最小。

### 4.3 切换路径规则

- 每一步只能：启用一个未启用包、停用一个启用包，或执行一条输入声明的原子替换（计 1 步）。
- 路径上的每个状态（含旧状态）都必须满足全部过渡不变量。
- **依赖环规则**：旧状态中已经存在的依赖环视为历史包袱，加载合法；
  但任何单步都**不得新建**依赖环（判定方式：仅统计包含本次新增包、且其余环节点在动作前已启用的环）。
- 最优终态必须存在从旧状态出发的合法路径，否则按不可达处理。
- 同一最优终态的合法路径中：步数最少优先；同步数按**操作字典序**择先。

### 4.4 公开的操作字典序

操作规范键为：

- `disable:<包ID>`
- `enable:<包ID>`
- `replace:<remove>-><add>`

按键字符串字典序排序，因此天然有 `disable:* < enable:* < replace:*`，同类型内按包 ID 排序。
求解器使用标准 FIFO BFS：父状态按发现顺序出队（同层即按字典序前缀排列），
每个父状态的出边按规范键排序展开，**首次发现即认领**，
从而每个状态记录的路径同时满足“步数最少”与“完整操作序列字典序最小”。

## 5. 四个命令

### 5.1 solve —— 完整穷举求解

```bash
python -m authplanner solve tests/fixtures/tiny_reachable.json -o plan.json
python -I planner.py solve input.json --dump-reachable reachable.json
```

- 从旧状态 BFS 穷举**全部**可达合法状态并缓存已见状态；**没有任何探测上限**，
  `search_stats.search_capped` 恒为 `false`，绝不会因截断而返回“最优”。
- 可达时输出 `report_kind=plan`，退出码 `0`；计划含每步操作、前后状态哈希、
  前后包集合、三级目标值、终态摘要、求解状态数与防篡改 `plan_hash`。
- 无可达终态时输出 `report_kind=unreachable`、`conclusion=no_reachable_target`，
  退出码 `3`；报告公开 `states_discovered` 等统计，供独立复核。
- `--dump-reachable` 额外导出全部可达状态、是否可行终态与 BFS 深度。

### 5.2 verify —— 独立重放核对

```bash
python -m authplanner verify input.json plan.json --check-optimality
```

- 不信任求解产物：重新构造领域模型，逐步重放并独立检查每步结构合法性、
  过渡不变量、依赖环规则，以及每步自带的前后哈希/包集合，最后判断终态全部约束。
- `--check-optimality` 会**重跑一遍完整穷举**，复核三级目标最优性，
  以及该计划路径是否为同最优解下的最少步数/字典序首选路径。
- 对 `unreachable` 报告同样可复核：重跑穷举确认零可行终态，并核对公开的状态数。
- 核对失败退出码 `4`。

### 5.3 rollout —— 模拟台账执行

```bash
python -m authplanner rollout input.json plan.json --ledger work/rollout.db
python -m authplanner rollout input.json plan.json --ledger work/rollout.db --fault 0:exception
```

- 每一步先验证当前代次与预期状态（计划 `before_hash` 必须等于台账推导状态），
  再在**单个事务**内写入台账并提交。
- 每步记录：序号、操作键、前后状态哈希、前后完整摘要（JSON）、
  幂等键 `idem-<sha256>` 与链式哈希 `chain-<sha256>`（前驱链断裂可被发现）。
- **重启接续**：重新打开台账时先重放全部已提交步做链校验，
  已提交步幂等跳过，从首个未提交步继续；全部完成后再次执行直接幂等返回。
- 拒绝场景（退出码 `5`）：
  - 服务外的**旧代计划**：`generation` 不一致；
  - **同 ID 异内容**：`plan_id` 相同但 `plan_hash` 不同；
  - 外来输入：`input_hash` 不一致、总步数不一致；
  - **意外状态**：台账链上任何一步哈希/操作/推导状态与计划不符。
- 故障注入（仅限本进程、仅限测试用途）：
  - `--fault <seq>:exception`：第 `seq` 步持久化前抛出受控异常 `ControlledFault`（退出码 6）；
  - `--fault <seq>:exit`：第 `seq` 步持久化前 `os._exit(0)` 正常退出；
  - 两种故障都发生在任何 INSERT/COMMIT 之前，台账保持“已提交步完整、故障步无痕”，重启可原样接续。

### 5.4 status —— 台账只读查询

```bash
python -m authplanner status work/rollout.db
python -m authplanner status work/rollout.db --input input.json --plan plan.json
```

- 输出代次、状态、已提交步数与每步链记录；台账不存在时退出码 `2`。
- 附带 `--input/--plan` 时额外重放推导当前模拟状态，并判断是否已到计划目标态。

### 退出码一览

| 码 | 含义 |
| --- | --- |
| 0 | 成功 |
| 1 | 用法或输入模型错误 |
| 2 | 文件/台账不存在 |
| 3 | 求解完成但无可达目标 |
| 4 | verify 核对失败 |
| 5 | 台账/代次/意外状态拒绝 |
| 6 | 受控异常故障注入触发 |

## 6. 夹具与需求场景对照

| 夹具 | 覆盖的需求场景 |
| --- | --- |
| `tiny_reachable.json` | 小实例可达；独立枚举校验目标与步数；enable/disable 字典序路径 |
| `unreachable_optimal.json` | **终态静态最优但不可落地**：`{z}` 静态最优，但必保能力只由与之冲突的 `old` 覆盖，无可达目标 |
| `atomic_replace_only.json` | **只有原子替换才能通过**：先停用丢必保、先启用触发冲突，唯一合法单步是声明的替换 |
| `dedup_ranking.json` | **共享能力去重影响排名**：同一 `shared.secret` 授予两项目按三元组计 2，便宜方案输给零未申报的较贵方案 |
| `legacy_cycle.json` | **依赖循环可在旧态已存在但无法单步新建**：`cycleA/cycleB` 历史环合法，新建环被逐边拒绝 |
| `or_dependencies.json` | OR 依赖与多前置 AND；兼作**代次冲突与中途恢复**的 rollout 夹具 |

## 7. 独立枚举交叉校验

`tests/bruteforce.py` 是一套**刻意不同的实现**：用 `frozenset` 而非整数位掩码表示状态、
用集合运算重写全部约束与环检测、不导入 `authplanner.solver`。
`tests/test_bruteforce_crosscheck.py` 在全部小夹具上逐一双向比对：

- 可达状态集合、可达终态集合；
- 最优终态与三级目标值；
- 最短路径长度与字典序操作序列；
- 不可达结论与公开状态数。

## 8. 安全与边界说明

- 全部输入数据视为团队提供的虚构标识；工具不做任何外部调用。
- 计划与报告带 `plan_hash` / `report_hash`（对剔除摘要字段后的规范 JSON 取 SHA-256），
  任何内容篡改都会被 verify 或台账链校验识别。
- 状态用 32 位以内整数承载（最多 24 包）；无探测上限意味着最坏情况会遍历全部
  2^24 个状态，这是需求明确要求的完整性代价，而不是静默截断。
- 实测（Windows 11 + CPython 3.12.8，无约束 24 包、可达集等于全部 2^24=16,777,216 状态）：
  完整穷举约 5 分钟，峰值工作集约 2.6GB（紧凑记录约 165 字节/状态），
  求解正确且 `search_capped=false`；真实输入受冲突、依赖、必保与禁用约束，
  可达集通常远小于理论上界。
- 台账文件是普通 SQLite 数据库，可用任意 SQLite 工具只读检查，不依赖专有格式。
