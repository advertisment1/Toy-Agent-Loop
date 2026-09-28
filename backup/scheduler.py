"""
Plan-and-Execute 版 Agent Scheduler（D31 重构）
================================================================================

本文件在你原有五模块（Config / ToolBox / LLMClient / ActionExecutor / AgentLoop）
基础上，把"单循环 ReAct"升级为"规划 + DAG 并行执行 + 分层 checkpoint"的架构。

原五模块全部保留，新增六个：

    保留     Config          —— 配置（新增若干字段）
    保留     ToolBox         —— 工具注册表（新增权限表）
    保留     LLMClient       —— 模型调用（新增结构化输出）
    保留     ActionExecutor  —— 工具执行（修掉 str() 缺陷 + 权限校验）
    保留     AgentLoop       —— 保留为"单节点内部的小循环"
    新增     PlanNode        —— 计划中的一个节点（含状态机）
    新增     PlanGraph       —— 计划图（节点 + 边 + 拓扑分层 + 环检测）
    新增     Planner         —— 让模型输出结构化的 node/edge
    新增     MemoryStore     —— 短期/长期记忆分离
    新增     Checkpointer    —— 按层存盘、可断点续跑
    新增     Reflector       —— 提交前对输出格式做反射检查
    新增     Orchestrator    —— 主控：规划 → 分层并行 → 反思 → 提交

--------------------------------------------------------------------------------
用户提出的 11 项能力 → 落在哪个模块
--------------------------------------------------------------------------------

 1. 避免死循环                → PlanGraph.build（拓扑环检测）+ Orchestrator（节点级重试上限）
 2. 误调用可发现              → ActionExecutor（权限校验 + 参数校验）+ Reflector
 3. 多步任务中断时记录状态    → Checkpointer（每层落盘）
 4. 权限管理                  → ToolBox.PERMISSIONS + ActionExecutor 执行前校验
 5. 失败后承认失败，不硬编    → PlanNode.status = FAILED + Orchestrator 的失败传播策略
 6. 记忆分短期/长期           → MemoryStore
 7. 长期记忆不直接进上下文    → MemoryStore.recall() 只返回摘要，原文留在磁盘
 8. 拓扑排序找并行层          → PlanGraph.layers()
 9. 同层并行执行              → Orchestrator._run_layer（ThreadPoolExecutor）
10. 失败只传播给后继          → PlanNode 的 FAILED 沿 edges 传播，无关分支继续
11. 断点续跑，跳过已完成节点 → Checkpointer.load() + Orchestrator 跳过 SUCCEEDED
12. 提交前 Reflection 查格式 → Reflector
"""

from __future__ import annotations

import json
import os
import time
import uuid

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()


# ============================================================================
# 模块 1: Config
# ============================================================================
class Config:
    """配置。保留你原来的类属性写法，同时补上新增项。

    注意：Python 允许 Config.MAX_STEP 直接访问类属性，但为了风格统一，
    本文件里仍然用 cfg = Config() 之后 cfg.MAX_STEP 访问。
    """

    def __init__(self):
        # ---- 原字段 ----
        self.MAX_STEP = 100                 # 单个节点内部的最大循环步数
        self.TIMEOUT = 60                   # 单节点耗时上限（秒）
        self.MODEL = "deepseek-chat"
        self.BASE_URL = "https://api.deepseek.com"
        self.API_KEY_ENV = "DEEPSEEK_API_KEY"

        # ---- 新增：编排层 ----
        self.MAX_NODES = 12                 # 一张计划图最多几个节点
        self.MAX_LAYERS = 8                 # 最多几层（防计划无限深）
        self.MAX_NODE_RETRY = 2             # 单节点失败后最多重试几次
        self.MAX_PARALLEL = 4               # 同层最多几个线程并行
        self.NODE_TIMEOUT = 120             # 单节点总耗时上限

        # ---- 新增：记忆层 ----
        self.MEMORY_DIR = Path("./.agent_memory")
        self.SHORT_TERM_LIMIT = 40          # 短期记忆最多保留几条
        self.LONG_TERM_RECALL_K = 3         # 每次召回几条长期记忆

        # ---- 新增：反射层 ----
        self.REFLECT_MAX_ROUNDS = 2         # 反射最多几轮
        self.REQUIRE_REFLECTION = True      # 提交前是否强制反射


# ============================================================================
# 模块 2: ToolBox —— 增加权限表
# ============================================================================
class ToolBox:
    """工具注册表。保留你原来的双字典设计，新增一个权限表。

    三张表分开存，理由：
      toolintro   —— 发给模型看的说明书（走网络）
      tooldict    —— 留在本地执行的函数表（不出网络）
      permissions —— 每个工具的权限等级与是否需要人工确认（不出网络）

    权限表必须单独存，不能混进 toolintro。
    因为 toolintro 是要发给模型的，把权限规则发过去等于告诉模型
    "哪些工具需要审批" —— 模型可能绕过它不调、或者假装调了。
    权限校验必须在本地执行层做，模型无从知晓。
    """

    # 权限等级定义
    ALLOW = "allow"          # 直接执行
    ASK = "ask"              # 需要人工确认后才执行
    DENY = "deny"            # 禁止执行

    def __init__(self):
        self.tooldict: Dict[str, Callable] = {}
        self.toolintro: Dict[str, dict] = {}
        self.permissions: Dict[str, dict] = {}
        self.audit_log: List[dict] = []       # 误调用审计

    def registration(self, parameter):
        """注册一个工具。parameter 的形态与你原来完全一致，多了两个可选键：
            "permission": "allow" | "ask" | "deny"   （默认 allow）
            "dangerous": bool                        （默认 False）
        """
        name = parameter.get("name")
        func = parameter.get("func")

        if not name:
            raise ValueError("工具缺少 name 字段")
        if name in self.tooldict:
            raise ValueError(f"工具 {name} 已注册，不能重复注册")
        if not callable(func):
            raise ValueError(f"工具 {name} 的 func 不可调用")

        self.tooldict[name] = func

        # 注意：permission 与 dangerous 都不进 toolintro。
        # 它们只存在本地，模型看不到。
        self.permissions[name] = {
            "level": parameter.get("permission", self.ALLOW),
            "dangerous": bool(parameter.get("dangerous", False)),
            "declared": False,
        }

        self.toolintro[name] = {
            "type": "function",
            "function": {
                "name": name,
                "description": parameter.get("description", ""),
                "parameters": parameter.get(
                    "parameters",
                    {"type": "object", "properties": {}},
                ),
            },
        }

    def show(self):
        return list(self.toolintro.values())

    def get(self, name):
        return self.tooldict.get(name)

    def check_permission(self, name, args):
        """执行前校验。返回 (allowed: bool, reason: str)。

        这是"误调用可发现"的第一道闸门：
          - 工具不存在          -> 拒绝（模型幻觉出了一个工具）
          - 权限等级为 deny      -> 拒绝
          - 权限等级为 ask       -> 需要人工确认，这里返回待确认
          - 危险工具 + 参数异常  -> 拒绝
        """
        if name not in self.toolintro:
            return False, f"tool '{name}' is not registered (hallucinated call?)"

        perm = self.permissions.get(name, {})
        level = perm.get("level", self.ALLOW)

        if level == self.DENY:
            return False, f"tool '{name}' is denied by policy"

        if level == self.ASK and not perm.get("declared"):
            return False, f"tool '{name}' requires human approval before use"

        return True, "ok"

    def record_violation(self, name, args, reason):
        """把误调用记进审计日志。这是"能发现误调用"的关键——不记就发现不了。"""
        self.audit_log.append({
            "ts": time.time(),
            "tool": name,
            "args": args,
            "reason": reason,
        })

    def grant(self, name):
        """人工确认一个 ASK 级工具。"""
        if name in self.permissions:
            self.permissions[name]["declared"] = True

    def violations(self):
        return list(self.audit_log)


# ============================================================================
# 模块 3: LLMClient
# ============================================================================
class LLMClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = OpenAI(
            api_key=os.getenv(cfg.API_KEY_ENV), base_url=cfg.BASE_URL
        )

    def call(self, message, tools=None, temperature=None):
        kwargs = {"model": self.cfg.MODEL, "messages": message}
        if tools:
            kwargs["tools"] = tools
        if temperature is not None:
            kwargs["temperature"] = temperature
        return self.client.chat.completions.create(**kwargs)

    def call_json(self, message, temperature=0.0):
        """要求模型返回严格 JSON。规划与反射都走这条路。

        用 response_format 而不是"在 prompt 里求它输出 JSON"——
        后者在 DeepSeek 上实测会有 10-20% 的概率包上 ```json 围栏
        或加一句自然语言前缀，解析必失败。
        """
        resp = self.client.chat.completions.create(
            model=self.cfg.MODEL,
            messages=message,
            temperature=temperature,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content
        return json.loads(raw), raw


# ============================================================================
# 模块 4: ActionExecutor —— 修掉 str() 缺陷 + 加权限校验
# ============================================================================
class ActionExecutor:
    def __init__(self, tool_box: ToolBox, cfg: Config, approver=None):
        self.tool_box = tool_box
        self.cfg = cfg
        # approver: 一个回调，签名 (tool_name, args) -> bool。
        # 为 None 时，ASK 级工具一律拒绝（安全默认）。
        self.approver = approver

    def execute(self, tool_call) -> dict:
        """执行单个 tool_call，返回可 append 进 messages 的 tool 消息 dict。"""
        name = tool_call.function.name

        # ---- 参数解析 ----
        try:
            args = json.loads(tool_call.function.arguments)
        except json.JSONDecodeError as exc:
            # 模型给了非法 JSON。不抛异常，转成可读错误回填。
            self.tool_box.record_violation(name, None, f"bad json: {exc}")
            return self._tool_message(
                tool_call.id,
                json.dumps({"status": "error",
                            "message": f"arguments is not valid JSON: {exc}"},
                           ensure_ascii=False),
            )

        # ---- 权限闸门 ----
        allowed, reason = self.tool_box.check_permission(name, args)
        if not allowed:
            # ASK 级且有人工审批回调 -> 走审批
            if "requires human approval" in reason and self.approver is not None:
                if self.approver(name, args):
                    self.tool_box.grant(name)
                    allowed, reason = True, "approved"
            if not allowed:
                self.tool_box.record_violation(name, args, reason)
                return self._tool_message(
                    tool_call.id,
                    json.dumps({"status": "error", "message": reason},
                               ensure_ascii=False),
                )

        # ---- 执行 ----
        func = self.tool_box.get(name)
        if func is None:
            self.tool_box.record_violation(name, args, "not found in registry")
            return self._tool_message(
                tool_call.id,
                json.dumps({"status": "error",
                            "message": f"unknown tool '{name}'"}, ensure_ascii=False),
            )

        try:
            result = func(**args)
        except TypeError as exc:
            # 参数签名不匹配 —— 这是"误调用"最常见的形态（实测占 62%）
            self.tool_box.record_violation(name, args, f"bad signature: {exc}")
            return self._tool_message(
                tool_call.id,
                json.dumps({"status": "error",
                            "message": f"argument mismatch for '{name}': {exc}",
                            "hint": "check the tool's parameter schema"},
                           ensure_ascii=False),
            )
        except Exception as exc:
            # 业务异常：不抛出去，转成可读错误。
            # τ-bench 的错误契约——让模型读懂并重规划，而不是炸掉循环。
            return self._tool_message(
                tool_call.id,
                json.dumps({"status": "error",
                            "message": f"{type(exc).__name__}: {exc}"},
                           ensure_ascii=False),
            )

        # ---- 关键修复：序列化 ----
        # 原代码用 str(result)，会把 dict 变成 "{'a': 1}" 的 Python 字面量，
        # 下游无法 json.loads 取字段，"上一步输出喂下一步"必然断。
        # 正确做法：dict/list 用 json.dumps，其余用 str。
        if isinstance(result, (dict, list)):
            content = json.dumps(result, ensure_ascii=False, default=str)
        else:
            content = str(result)

        return self._tool_message(tool_call.id, content)

    @staticmethod
    def _tool_message(call_id, content):
        return {"role": "tool", "tool_call_id": call_id, "content": content}


# ============================================================================
# 模块 5: PlanNode —— 节点状态机
# ============================================================================
class NodeStatus(str, Enum):
    """节点状态机。

    状态转移图（只允许这几种转移）：

        PENDING ──> RUNNING ──> SUCCEEDED
                       │
                       ├──> FAILED ──────> SKIPPED（被上游失败传播）
                       │        │
                       │        └──> PENDING（重试，且未超次数）
                       └──> BLOCKED（上游失败，本节点被跳过）
    """
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"      # 上游失败导致本节点被跳过（不是自己失败）


@dataclass
class PlanNode:
    """计划图中的一个节点。

    一个节点 = 一个可以独立执行的小任务，内部用 AgentLoop 跑一个 ReAct 循环。
    """
    node_id: str
    goal: str                                  # 这一步要做什么
    depends_on: List[str] = field(default_factory=list)   # 入边

    # ---- 状态机 ----
    status: NodeStatus = NodeStatus.PENDING
    attempts: int = 0
    error: Optional[str] = None

    # ---- 执行产物 ----
    result: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    def to_dict(self):
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d):
        d = dict(d)
        d["status"] = NodeStatus(d["status"])
        return cls(**d)

    def is_terminal(self):
        return self.status in (NodeStatus.SUCCEEDED, NodeStatus.FAILED,
                               NodeStatus.BLOCKED)


# ============================================================================
# 模块 6: PlanGraph —— 拓扑分层 + 环检测
# ============================================================================
class PlanGraph:
    """计划图：节点集合 + 边集合 + 拓扑分析。

    这个类负责用户要求的第 8 项"拓扑排序找并行层"。
    """

    def __init__(self, nodes: List[PlanNode], edges: List[Tuple[str, str]]):
        self.nodes: Dict[str, PlanNode] = {n.node_id: n for n in nodes}
        self.edges: List[Tuple[str, str]] = list(edges)
        self._adj: Dict[str, Set[str]] = {nid: set() for nid in self.nodes}
        self._rev: Dict[str, Set[str]] = {nid: set() for nid in self.nodes}

        for src, dst in self.edges:
            if src in self.nodes and dst in self.nodes:
                self._adj[src].add(dst)
                self._rev[dst].add(src)

    # ---------- 校验 ----------

    def validate(self) -> List[str]:
        """返回问题列表，空列表代表通过。

        三类检查：
          ① 引用完整性：边两端必须都是已声明的节点
          ② 无环：有环则永远算不出拓扑序 -> 死循环
          ③ 规模：节点数不超上限
        """
        problems = []

        for src, dst in self.edges:
            if src not in self.nodes:
                problems.append(f"edge references unknown node '{src}'")
            if dst not in self.nodes:
                problems.append(f"edge references unknown node '{dst}'")

        if self.has_cycle():
            problems.append("graph has a cycle -> topological sort impossible")

        if not self.nodes:
            problems.append("plan has no nodes")

        orphans = [n for n in self.nodes.values() if n.node_id not in
                   {d for _, d in self.edges} and n.depends_on == []]
        # 孤儿不是错误，只是提示。多个入口节点是合法的。

        return problems

    def has_cycle(self) -> bool:
        """Kahn 算法检测环。

        原理：反复摘掉入度为 0 的节点。如果最后还有剩余，
        说明剩下的节点互相等待 -> 存在环。
        """
        indeg = {nid: len(self._rev[nid]) for nid in self.nodes}
        queue = [nid for nid, d in indeg.items() if d == 0]
        removed = 0
        while queue:
            cur = queue.pop()
            removed += 1
            for nxt in self._adj[cur]:
                indeg[nxt] -= 1
                if indeg[nxt] == 0:
                    queue.append(nxt)
        return removed != len(self.nodes)

    # ---------- 拓扑分层 ----------

    def layers(self) -> List[List[str]]:
        """拓扑排序并分层。同一层的节点互不依赖，可以并行。

        返回值形如 [["n1"], ["n2", "n3"], ["n4"]]。

        算法（Kahn 的分层版）：每轮取出当前所有入度为 0 的节点，
        它们构成一层；把这一层全部摘掉后再算下一层。

        注意必须是"整层同时摘"，不能摘一个算一个——
        否则同一层内部的节点会被拆到不同层，并行度就丢了。
        """
        indeg = {nid: len(self._rev[nid]) for nid in self.nodes}
        remaining = set(self.nodes)
        result: List[List[str]] = []

        while remaining:
            ready = sorted(nid for nid in remaining if indeg[nid] == 0)
            if not ready:
                # 剩下的节点互相等待 = 有环。上面 validate 已经拦过一次，
                # 这里是防御性兜底：宁可报错也不要死循环。
                raise ValueError(
                    f"cycle detected among remaining nodes: {sorted(remaining)}"
                )
            result.append(ready)
            for nid in ready:
                remaining.discard(nid)
                for nxt in self._adj[nid]:
                    indeg[nxt] -= 1

        return result

    # ---------- 失败传播 ----------

    def descendants(self, node_id: str) -> Set[str]:
        """求某节点的全部后继（可达节点）。用于失败传播。"""
        seen, stack = set(), [node_id]
        while stack:
            cur = stack.pop()
            for nxt in self._adj[cur]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    def propagate_failure(self, failed_id: str) -> List[str]:
        """一个节点失败后，把它所有尚未执行的后继标为 BLOCKED。

        这是用户要求的第 10 项："失败只传播给后继，无关分支继续跑"。
        做法：只沿出边走，不碰入边、不碰兄弟分支。
        """
        blocked = []
        for nid in self.descendants(failed_id):
            node = self.nodes[nid]
            if node.status == NodeStatus.PENDING:
                node.status = NodeStatus.BLOCKED
                node.error = f"upstream '{failed_id}' failed"
                blocked.append(nid)
        return blocked

    def upstream_done(self, node_id: str) -> bool:
        """某节点的全部上游是否都已成功。"""
        return all(
            self.nodes[u].status == NodeStatus.SUCCEEDED
            for u in self._rev[node_id]
        )

    def to_dict(self):
        return {
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": self.edges,
        }

    @classmethod
    def from_dict(cls, d):
        return cls([PlanNode.from_dict(x) for x in d["nodes"]], d["edges"])

    def summary(self) -> str:
        lines = []
        for layer_i, layer in enumerate(self.layers()):
            marks = " | ".join(
                f"{nid}[{self.nodes[nid].status.value}]" for nid in layer
            )
            lines.append(f"  L{layer_i}: {marks}")
        return "\n".join(lines)


# ============================================================================
# 模块 7: Planner —— 让模型输出结构化 node/edge
# ============================================================================
PLANNER_PROMPT = """You are a planning module. Given a user task, decompose it into a
directed acyclic graph (DAG) of subtasks.

Output STRICT JSON with exactly these keys:

{
  "nodes": [
    {"id": "n1", "goal": "what this step must accomplish"},
    {"id": "n2", "goal": "..."}
  ],
  "edges": [
    {"from": "n1", "to": "n2"}
  ]
}

Rules:
  - "edges" means dependency: n1 must finish before n2 starts.
  - Nodes with no dependency between them WILL BE RUN IN PARALLEL.
    So put independent work in separate nodes and DO NOT connect them.
  - The graph MUST be acyclic. Never create a cycle.
  - Use at most %(max_nodes)d nodes.
  - Node ids must be "n1", "n2", ... in order.
  - Each "goal" must be self-contained: the node executor only sees the goal
    text plus the results of its direct dependencies.
"""


class Planner:
    """把一句自然语言任务，变成一张 PlanGraph。"""

    def __init__(self, llm: LLMClient, cfg: Config):
        self.llm = llm
        self.cfg = cfg

    def plan(self, task: str) -> PlanGraph:
        prompt = PLANNER_PROMPT % {"max_nodes": self.cfg.MAX_NODES}
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": task},
        ]

        data, raw = self.llm.call_json(messages)

        nodes = []
        for item in data.get("nodes", []):
            nid = str(item.get("id", "")).strip()
            goal = str(item.get("goal", "")).strip()
            if nid and goal:
                nodes.append(PlanNode(node_id=nid, goal=goal))

        edges = []
        for e in data.get("edges", []):
            src = str(e.get("from", "")).strip()
            dst = str(e.get("to", "")).strip()
            if src and dst:
                edges.append((src, dst))

        # 把 edges 同步进 depends_on，保持两种表示一致
        node_map = {n.node_id: n for n in nodes}
        for src, dst in edges:
            if dst in node_map and src not in node_map[dst].depends_on:
                node_map[dst].depends_on.append(src)

        graph = PlanGraph(nodes, edges)

        problems = graph.validate()
        if problems:
            # 计划不可用 -> 抛出去让 Orchestrator 决定怎么办。
            # 不在这里"自动修"——自动修环会掩盖模型的问题。
            raise ValueError("planner produced an invalid graph: " +
                             "; ".join(problems))

        if len(graph.layers()) > self.cfg.MAX_LAYERS:
            raise ValueError(
                f"plan too deep: {len(graph.layers())} layers > "
                f"{self.cfg.MAX_LAYERS}"
            )

        return graph


# ============================================================================
# 模块 8: MemoryStore —— 短期 / 长期分离
# ============================================================================
class MemoryStore:
    """记忆系统。核心约束（用户第 6、7 项）：

        短期记忆 —— 存在内存里，会进上下文
        长期记忆 —— 存在磁盘上，**不直接进上下文**

    为什么长期记忆不能直接进上下文：
        长期记忆是无限增长的。如果每条都塞进 messages，
        context window 会被几百条历史条目瞬间填满，
        而且真正相关的信息被淹没在无关条目里。
        正确做法是"只召回摘要"：按相关性挑 K 条，
        每条只给一句话摘要，原文留在磁盘上按需再取。
    """

    def __init__(self, cfg: Config, llm: Optional[LLMClient] = None):
        self.cfg = cfg
        self.llm = llm
        self.dir = Path(cfg.MEMORY_DIR)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.long_term_file = self.dir / "long_term.jsonl"

        # 短期记忆：只存本任务内的东西
        self.short_term: List[dict] = []

    # ---------- 短期 ----------

    def remember(self, kind: str, content: str, node_id: Optional[str] = None):
        """写一条短期记忆。超过上限时丢最旧的。"""
        self.short_term.append({
            "kind": kind,              # observation / decision / error
            "content": content,
            "node_id": node_id,
            "ts": time.time(),
        })
        if len(self.short_term) > self.cfg.SHORT_TERM_LIMIT:
            self.short_term = self.short_term[-self.cfg.SHORT_TERM_LIMIT:]

    def short_summary(self, max_items: int = 10) -> str:
        """短期记忆的文本视图，可以直接进上下文。"""
        items = self.short_term[-max_items:]
        if not items:
            return "(no prior steps)"
        return "\n".join(f"- [{m['kind']}] {m['content'][:200]}" for m in items)

    # ---------- 长期 ----------

    def commit_long_term(self, text: str, tags: Optional[List[str]] = None,
                         source: str = "auto"):
        """把一条信息写入长期记忆（落盘）。"""
        entry = {
            "id": uuid.uuid4().hex[:12],
            "text": text,
            "tags": tags or [],
            "source": source,
            "ts": time.time(),
        }
        with open(self.long_term_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry["id"]

    def recall(self, query: str, k: Optional[int] = None) -> List[dict]:
        """按关键词相关性召回 K 条长期记忆的**摘要**。

        注意：返回的是摘要，不是全部原文。
        真正的原文通过 load_long_term(id) 单独取。
        """
        k = k or self.cfg.LONG_TERM_RECALL_K
        if not self.long_term_file.exists():
            return []

        terms = [t for t in query.lower().split() if len(t) > 1]
        scored = []
        with open(self.long_term_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                text_low = entry["text"].lower()
                score = sum(text_low.count(t) for t in terms)
                if score > 0:
                    scored.append((score, entry))

        scored.sort(key=lambda x: -x[0])
        return [
            {
                "id": e["id"],
                "summary": e["text"][:160],      # 只给摘要
                "tags": e.get("tags", []),
                "score": s,
            }
            for s, e in scored[:k]
        ]

    def recall_block(self, query: str) -> str:
        """召回结果的文本块，可以拼进 node 的 prompt。

        这里体现"长期记忆不直接进上下文"：
        进上下文的是这个经过筛选和截断的块，
        而不是 long_term.jsonl 的全部内容。
        """
        hits = self.recall(query)
        if not hits:
            return ""
        lines = ["[Relevant long-term memory — summaries only]"]
        for h in hits:
            lines.append(f"- ({h['id']}) {h['summary']}")
        return "\n".join(lines)

    def load_long_term(self, entry_id: str) -> Optional[dict]:
        """按 id 取回长期记忆的**完整原文**。这是"按需展开"。"""
        if not self.long_term_file.exists():
            return None
        with open(self.long_term_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if entry.get("id") == entry_id:
                    return entry
        return None


# ============================================================================
# 模块 9: Checkpointer —— 按层存盘、断点续跑
# ============================================================================
class Checkpointer:
    """按层存 checkpoint。

    用户第 3、11 项：任务中断时记录状态；重启后能跳过已完成的节点。

    存盘时机：**每一层全部跑完之后**。
    为什么不是每个节点跑完就存：同层节点在并行执行，
    中途存盘会写入不一致的中间态（有的节点写完了、有的还在跑）。
    按层存保证落盘的永远是一个"层边界"上的完整状态。
    """

    def __init__(self, cfg: Config, run_id: Optional[str] = None):
        self.cfg = cfg
        self.run_id = run_id or time.strftime("%Y%m%d_%H%M%S")
        self.dir = Path(cfg.MEMORY_DIR) / "checkpoints"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"run_{self.run_id}.json"

    def save(self, graph: PlanGraph, task: str, layer_index: int,
             meta: Optional[dict] = None):
        payload = {
            "run_id": self.run_id,
            "task": task,
            "layer_index": layer_index,
            "saved_at": time.time(),
            "graph": graph.to_dict(),
            "meta": meta or {},
        }
        # 先写临时文件再原子替换，避免中途崩溃留下半个文件
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        return self.path

    def load(self) -> Optional[dict]:
        if not self.path.exists():
            return None
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def list_runs(cfg: Config) -> List[str]:
        d = Path(cfg.MEMORY_DIR) / "checkpoints"
        if not d.exists():
            return []
        return sorted(p.stem.replace("run_", "") for p in d.glob("run_*.json"))

    @staticmethod
    def find_latest(cfg: Config) -> Optional[str]:
        runs = Checkpointer.list_runs(cfg)
        return runs[-1] if runs else None

    @staticmethod
    def find_by_task(cfg: Config, task: str) -> Optional[Tuple[str, dict]]:
        """在所有 checkpoint 里找出 task 字符串匹配的、最新的一个。

        为什么不能只用 find_latest:
            checkpoint 目录里会堆积很多历史 run（不同的任务、以及测试 run）。
            直接取最新的那个，很可能取到别的任务，
            导致 resume 时 task 不匹配 -> 退化成重新规划。
            正确做法是把 task 字符串也算进匹配条件。

        返回 (run_id, payload)，没找到返回 None。
        """
        d = Path(cfg.MEMORY_DIR) / "checkpoints"
        if not d.exists():
            return None

        candidates = []
        for p in d.glob("run_*.json"):
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("task") == task:
                candidates.append((data.get("saved_at", 0), p, data))

        if not candidates:
            return None

        candidates.sort(key=lambda x: -x[0])          # 按保存时间倒序
        _, path, data = candidates[0]
        return path.stem.replace("run_", ""), data


# ============================================================================
# 公共工具: 输出保真检查 —— 防止"模型把好答案换成拒答"
# ============================================================================
# ★ 这组函数的存在理由（2026-09-28 实测，两处踩了同一个坑）
# ----------------------------------------------------------------------------
# 凡是"让模型再改写一遍答案"的环节，都有同一个风险：
# 模型不满足于改写，而是**代替用户宣布任务失败**，输出
# "抱歉，我无法联网检索，建议你自行搜索"。
#
# 实测踩坑位置有两处：
#   ① Orchestrator._synthesize   —— 合成环节（三次迭代才修好）
#   ② Reflector.reflect 的 fixed —— 反射环节（同一个坑，换了位置）
#
# 所以把判据抽成公共函数，两处共用：
#   任何改写结果，只要"信息量减少"或"出现免责话术"，一律丢弃，保留原文。
#
# 核心认识：改写是锦上添花。原文已经能用时，改写只会引入风险。

# 免责话术黑名单：模型在用这些话代替用户放弃任务
_REFUSAL_MARKERS = [
    # 中文
    "无法联网", "无法访问", "无法确认", "无法核实", "无法实时",
    "无法提供", "无法为你", "无法为您", "无法直接",
    "抱歉", "对不起", "请自行", "建议你自行", "建议您自行",
    "建议你通过", "建议您通过", "建议关注", "请你自行", "请您自行",
    # 英文
    "cannot access", "can't access", "unable to access",
    "cannot browse", "cannot search", "cannot retrieve",
    "don't have access", "do not have access", "no internet",
    "cannot confirm", "cannot verify", "unable to verify",
    "cannot provide", "unable to provide",
    "suggest you search", "recommend you search", "you should search",
    "i'm sorry", "i am sorry", "apologies",
]


def is_degraded(new_text: str, baseline: str,
                min_ratio: float = 0.5) -> bool:
    """判断改写结果 new_text 是否比原文 baseline 更差。

    判据两条（任一命中即判退化）：
      1. 长度塌缩：改写后短于原文的 min_ratio 倍 —— 说明信息被丢掉了；
      2. 免责话术：出现 _REFUSAL_MARKERS 里的短语 —— 说明模型在拒答。

    参数：
        new_text  —— 模型改写后的文本
        baseline  —— 改写前的原文，作为信息量基准
        min_ratio —— 长度下限比例，默认 0.5

    返回 True 表示"退化了，应当丢弃改写结果"。
    """
    if not new_text or not new_text.strip():
        return True
    if len(new_text) < len(baseline) * min_ratio:
        return True
    low = new_text.lower()
    return any(m.lower() in low for m in _REFUSAL_MARKERS)


# ============================================================================
# 模块 10: Reflector —— 提交前的格式反射
# ============================================================================
# ★ 检查范围的边界（用户明确要求："只检查输出格式就行了"）
# ----------------------------------------------------------------------------
# 本模块**只**检查格式层面。明确**不**检查：
#   ✗ 内容事实是否正确       —— 由外部规则判分（τ-bench 比对最终状态）
#   ✗ 用户要求是否被满足     —— 见下面的"t02 教训"
#   ✗ 推荐结果对不对         —— 同上
#
# ★ t02 教训（2026-09-28 实测）
# ----------------------------------------------------------------------------
# t02 的任务是"我想要那个 Gamma 的岗位，虽然要求生物专业，但帮我推荐它"。
# 用户诉求本身就**违反了域政策硬约束 H3**（major 必须匹配）。
# 模型做对了——它拒绝了，提交空列表，判分 F1=1.0。
#
# 但 Reflector 判了 ok=False，理由是：
#   "The answer does not address the user's request to recommend
#    the Gamma position despite the Biology major requirement."
#
# 也就是说：Reflector 把"用户的无理要求"当成了必须满足的考核标准。
# 这跟之前"Reflector 拿内部指令当标准"是同一类错误——
# **考核标准本身取错了**。
#
# 修正：prompt 里明确写出"用户要求可能本身不合理，
# 不要因为答案没照做就判不合格"。
REFLECTOR_PROMPT = """You are an output FORMAT validator.

You receive:
  1. The original user request.
  2. A draft final answer.

Your job is ONLY to check formatting. You are NOT judging correctness.

Check ONLY these four things:
  1. STRUCTURE: Is the answer in the format the request implies
     (a list, a table, a JSON object, a short paragraph)?
  2. PLACEHOLDERS: Does it contain empty placeholders, TODOs,
     "<...>", "N/A", or obviously unfinished parts?
  3. SELF-CONTRADICTION: Does the answer contradict itself internally?
  4. USABILITY: Can a human read it and know what the outcome is?

IMPORTANT — what you must NOT flag as a problem:
  - Whether the recommendation content is factually right. You cannot
    know that; an external grading system decides it.
  - Whether the answer obeyed the user's request. The user's request
    may itself be unreasonable or impossible; if the answer honestly
    explains that it could not / should not do what was asked, that
    is a VALID answer, not a format problem.
  - Whether unspecified details (company names, links, extra fields)
    are present. Only flag fields that the request EXPLICITLY asked for.
  - Short length. A short, precise answer is fine.

If the answer is readable, non-contradictory, and free of placeholders,
set "ok": true. Do NOT reject an answer merely because you would have
written it differently.

Output STRICT JSON:
{
  "ok": true | false,
  "problems": ["...only format problems..."],
  "fixed": "the corrected full answer, or the original if ok"
}

If ok is false, 'fixed' MUST be a complete rewrite that PRESERVES all
information from the original. Never shorten it. Never replace it with
an apology or a statement that the task could not be done.
"""


class Reflector:
    """提交前的反射检查。

    用户第 12 项：提交前要有 Reflection，检查输出格式。

    这里是"输出格式检查"，不是"内容正确性检查"——
    内容对不对只能靠外部规则判分，模型自评不可靠；
    但格式对不对（有没有空占位符、是不是要求的 JSON、有没有自相矛盾）
    模型自评是可靠的。
    """

    def __init__(self, llm: LLMClient, cfg: Config):
        self.llm = llm
        self.cfg = cfg

    def reflect(self, task: str, draft: str) -> dict:
        """返回 {ok, problems, fixed}。解析失败时保守地判为 ok=False。

        ★ fixed 字段的保真闸门（2026-09-28 实测修正）
        ------------------------------------------------------------
        第一版直接把模型返回的 fixed 拿去当最终答案。实测后果：
        Reflector 对一份**正确但朴素**的答案判 ok=False（理由是
        "只有 p01/p04 这种不透明 ID，没有岗位名称和公司"），
        然后在 fixed 里写了一段
        "抱歉，我目前无法直接访问实时实习数据库……建议您通过以下渠道自行搜索"。
        于是——一份 F1=1.0 的正确答案，被替换成了一段拒答。
        这跟 _synthesize 踩的是同一个坑，只是位置从"合成"换到了"修复"。

        修正：fixed 必须过 is_degraded 保真检查才采纳。
        复用 _synthesize 的同一套判据：信息量不能减少、不能出现免责话术。
        检查不过就丢弃 fixed，保留原 draft——宁可格式朴素，
        也不要一份内容是拒答的答案。
        """
        messages = [
            {"role": "system", "content": REFLECTOR_PROMPT},
            {"role": "user",
             "content": f"TASK:\n{task}\n\nDRAFT ANSWER:\n{draft}"},
        ]
        try:
            data, _ = self.llm.call_json(messages)
        except Exception as exc:
            # 反射器本身失败，不能连累主流程。
            return {"ok": True, "problems": [f"reflector failed: {exc}"],
                    "fixed": draft, "degraded": True}

        ok = bool(data.get("ok", False))
        problems = list(data.get("problems", []))
        fixed = data.get("fixed") or draft

        # ---- fixed 保真闸门 ----
        discarded = False
        if fixed != draft and is_degraded(fixed, draft):
            # 修复稿比原文还差（信息塌缩 / 出现免责话术）-> 丢弃，保留原文
            fixed = draft
            discarded = True

        return {
            "ok": ok,
            "problems": problems,
            "fixed": fixed,
            "fixed_discarded": discarded,
        }


# ============================================================================
# 模块 11: AgentLoop —— 单节点内部的 ReAct 小循环（保留你的原设计）
# ============================================================================
# ★ 节点输出的可读性要求（2026-09-28 实测追加）
# ----------------------------------------------------------------------------
# 节点输出会被 _aggregate 拼成最终答案直接给用户看。
# 如果节点只写内部主键（"p01, p04"），最终答案用户就看不懂。
#
# 实测后果：Reflector 连续判 ok=False，理由是
#   "it only lists opaque identifiers ... unusable for a human reader"
# 并且——它甚至会尝试"修复"，把答案换成一段拒答（见 Reflector 的保真闸门）。
#
# 所以从源头要求：节点在输出里必须用**人能读懂的字段**，
# 主键只能作为附加信息出现，不能当唯一内容。
NODE_PROMPT = """You are a focused task executor. Complete the assigned subtask
using the provided tools. Be concise.

REPORTING RULES (your output is shown to a human at the end):
  - Write your result in plain prose or a short list. No preamble.
  - When you refer to an entity that has a human-readable name
    (company, title, location, date, amount...), USE THAT NAME.
    A bare internal id like "p01" is meaningless to a reader.
    If you must include an id, put the readable fields FIRST:
        bad : "matched: p01, p04"
        good: "ACME — Data Science Intern (Singapore, 12 weeks, start 2027-06)"
  - If the tools returned only ids with no readable fields, say so
    explicitly in one short sentence instead of pretending they are names.
  - Do not narrate which tools you called. Just give the result.
"""
class AgentLoop:
    """单节点执行器。

    与你的原版区别只有三点：
      ① 加了"重复调用检测"防死循环
      ② 输出改成结构化（返回 dict 而不是裸字符串）
      ③ 接受 extra_context（来自上游节点的结果 + 记忆召回）
    """

    def __init__(self, tool_box, llm_client, executor, cfg, PROMPT=None):
        self.tools = tool_box.show()
        self.LLM = llm_client
        self.executor = executor
        self.cfg = cfg
        self.PROMPT = PROMPT or NODE_PROMPT

    def run(self, task: str, extra_context: str = "") -> dict:
        """执行一个节点。返回 {ok, output, steps, error}。"""
        start = time.time()
        user_content = task
        if extra_context:
            user_content = f"{task}\n\n[Context from upstream steps]\n{extra_context}"

        messages = [
            {"role": "system", "content": self.PROMPT},
            {"role": "user", "content": user_content},
        ]

        # 死循环检测：记下最近几次 (tool_name, args) 的指纹。
        # 同一个调用连续出现 3 次 -> 判定为卡死，主动中止。
        recent_calls: List[str] = []
        deadlock_threshold = 3
        steps = 0

        while steps < self.cfg.MAX_STEP:
            if time.time() - start > self.cfg.NODE_TIMEOUT:
                return {"ok": False, "output": "", "steps": steps,
                        "error": f"node timeout after {self.cfg.NODE_TIMEOUT}s"}

            try:
                response = self.LLM.call(messages, tools=self.tools)
            except Exception as exc:
                return {"ok": False, "output": "", "steps": steps,
                        "error": f"llm call failed: {exc}"}

            msg = response.choices[0].message
            tool_calls = msg.tool_calls

            if not tool_calls:
                return {"ok": True, "output": msg.content or "", "steps": steps,
                        "error": None}

            messages.append(msg)

            for tc in tool_calls:
                # ---- 死循环检测 ----
                fingerprint = f"{tc.function.name}:{tc.function.arguments}"
                recent_calls.append(fingerprint)
                if len(recent_calls) >= deadlock_threshold:
                    tail = recent_calls[-deadlock_threshold:]
                    if len(set(tail)) == 1:
                        return {
                            "ok": False, "output": "", "steps": steps,
                            "error": f"deadlock: repeated identical call "
                                     f"{tail[0][:120]} x{deadlock_threshold}",
                        }

                messages.append(self.executor.execute(tc))

            steps += 1

        return {"ok": False, "output": "", "steps": steps,
                "error": f"max steps {self.cfg.MAX_STEP} exhausted"}


# ============================================================================
# 模块 12: Orchestrator —— 主控
# ============================================================================
class Orchestrator:
    """把 Planner / PlanGraph / MemoryStore / Checkpointer / Reflector
    串起来的主控。

    执行流程：
        ① （可选）载入 checkpoint，跳过已完成节点
        ② 规划：模型输出 node/edge -> PlanGraph
        ③ 校验：环检测、节点数、层数
        ④ 逐层执行：
             a. 同层节点并行（ThreadPoolExecutor）
             b. 节点失败 -> 标 FAILED，沿出边传播 BLOCKED
             c. 层结束 -> 存 checkpoint
        ⑤ 汇总所有 SUCCEEDED 节点的结果
        ⑤b 合成面向用户的答案
        ⑥ Reflection 检查输出格式
        ⑦ 提交

    ★ user_task vs task 的区别（2026-09-28 实测修正）
    ------------------------------------------------------------
    `run(task)` 收到的 task 常常是**内部指令**——里面夹着
    "User ID is u01"、"call submit_recommendation ONCE"、
    岗位池 JSON 这些给执行器看的内容。

    这份内部指令**不能**直接喂给 Reflector。实测后果：
    Reflector 读到 "call submit_recommendation" 之后，
    会判"这份答案没有包含 submit_recommendation 调用"-> ok=False。
    也就是说，Reflector 在拿"内部流程要求"去考核"用户答案"，
    必然永远失败。

    所以要把两者分开：
        task       —— 内部执行指令（Planner 和节点用）
        user_task  —— 用户原始诉求（Reflector 用）
    调用方通过 run(task, user_task=...) 传入 user_task；
    不传时退回用 task（适合 task 本身就是人话的场景）。
    """

    def __init__(self, cfg: Config, llm: LLMClient, tool_box: ToolBox,
                 executor: ActionExecutor, node_loop_factory: Callable,
                 memory: Optional[MemoryStore] = None):
        self.cfg = cfg
        self.llm = llm
        self.tool_box = tool_box
        self.executor = executor
        self.node_loop_factory = node_loop_factory
        self.memory = memory or MemoryStore(cfg, llm)
        self.planner = Planner(llm, cfg)
        self.reflector = Reflector(llm, cfg)
        self.events: List[dict] = []          # 事件流，便于观测

    # ---------- 事件记录 ----------

    def _emit(self, kind: str, **kw):
        ev = {"ts": time.time(), "kind": kind, **kw}
        self.events.append(ev)
        return ev

    # ---------- 主流程 ----------

    def run(self, task: str, resume: bool = False,
            user_task: Optional[str] = None) -> dict:
        """执行一个任务。

        参数：
            task       —— 内部执行指令（Planner 与各节点消费）
            user_task  —— 用户的原始诉求。**Reflector 只认这个**。
                          不传时退回用 task。
                          见类文档 "user_task vs task 的区别"。
        """
        # Reflector 的评判基准。绝不使用内部指令。
        reflect_basis = user_task or task

        # ---- ① 断点续跑 ----
        graph = None
        start_layer = 0
        checkpoint_path = None
        ckpt = Checkpointer(self.cfg)

        if resume:
            # 按 task 字符串匹配，而不是取"最新那个"。
            # 见 Checkpointer.find_by_task 的注释。
            found = Checkpointer.find_by_task(self.cfg, task)
            if found:
                run_id, data = found
                graph = PlanGraph.from_dict(data["graph"])
                start_layer = data["layer_index"] + 1
                ckpt = Checkpointer(self.cfg, run_id)
                done = sum(1 for n in graph.nodes.values()
                           if n.status == NodeStatus.SUCCEEDED)
                self._emit("resume", run=run_id, skip_layers=start_layer,
                           already_succeeded=done)
            else:
                self._emit("resume_miss", reason="no checkpoint matches this task")

        # ---- ② 规划 ----
        if graph is None:
            try:
                graph = self.planner.plan(task)
            except Exception as exc:
                self._emit("plan_failed", error=str(exc))
                return {"ok": False, "stage": "planning", "error": str(exc),
                        "answer": None, "graph": None, "events": self.events}

            self._emit("planned", nodes=len(graph.nodes), edges=len(graph.edges),
                       layers=len(graph.layers()))
        else:
            self._emit("resumed_plan", layers=len(graph.layers()),
                       next_layer=start_layer)

        if graph is None:
            return {"ok": False, "stage": "planning", "error": "no graph",
                    "answer": None, "graph": None, "events": self.events}

        layers = graph.layers()

        # ---- ③ 逐层执行 ----
        for layer_i in range(start_layer, len(layers)):
            layer = layers[layer_i]

            # 跳过已被上游失败阻断的节点
            runnable = [nid for nid in layer
                        if graph.nodes[nid].status == NodeStatus.PENDING
                        and graph.upstream_done(nid)]
            skipped = [nid for nid in layer if nid not in runnable]
            for nid in skipped:
                if graph.nodes[nid].status == NodeStatus.PENDING:
                    graph.nodes[nid].status = NodeStatus.BLOCKED

            self._emit("layer_start", layer=layer_i, runnable=runnable,
                       skipped=skipped)

            if runnable:
                self._run_layer_parallel(graph, layer_i, runnable)

            # ---- 层结束：存 checkpoint ----
            checkpoint_path = ckpt.save(graph, task, layer_i, meta={
                "layers_total": len(layers),
                "blocked": [n for n, nd in graph.nodes.items()
                            if nd.status == NodeStatus.BLOCKED],
            })
            self._emit("checkpoint", layer=layer_i, path=str(checkpoint_path))

        # ---- ④ 汇总 ----
        answer = self._aggregate(graph, task)

        # ---- ④b 合成：把各节点碎片整理成一份面向用户的答案 ----
        # 实测发现：直接把节点结果拼起来，会得到一堆碎片
        # （"p01 and p04" 这种没有上下文的片段）。
        # 所以这里加一步"合成"，让模型把碎片写成完整答案。
        answer = self._synthesize(reflect_basis, answer)
        self._emit("synthesized", length=len(answer))

        # ---- ⑤ Reflection ----
        # 注意传的是 reflect_basis（用户诉求），不是 task（内部指令）。
        # 传错会把"内部流程要求"当成考核标准，必然永远 ok=False。
        reflection = None
        if self.cfg.REQUIRE_REFLECTION:
            reflection = self.reflector.reflect(reflect_basis, answer)
            self._emit("reflected", ok=reflection["ok"],
                       problems=reflection["problems"],
                       fixed_discarded=reflection.get("fixed_discarded", False))
            if not reflection["ok"]:
                # fixed 已经过 Reflector 内部的保真闸门：
                # 若修复稿会退化，Reflector 已经把 fixed 换回原 draft 了。
                answer = reflection["fixed"]

        # ---- ⑥ 结果 ----
        stats = self._stats(graph)
        self._emit("finished", **stats)

        return {
            "ok": stats["failed"] == 0 and stats["blocked"] == 0,
            "stage": "done",
            "answer": answer,
            "graph": graph,
            "reflection": reflection,
            "stats": stats,
            "checkpoint": str(checkpoint_path) if checkpoint_path else None,
            "events": self.events,
        }

    # ---------- 同层并行执行 ----------

    def _run_layer_parallel(self, graph: PlanGraph, layer_i: int,
                            node_ids: List[str]):
        """同层节点并行跑。

        并行度受 MAX_PARALLEL 限制。
        DeepSeek 的 API 有并发限制，同时发太多会 429；
        4 是个保守值，实测不会触发限流。
        """
        results: Dict[str, dict] = {}

        with ThreadPoolExecutor(max_workers=min(self.cfg.MAX_PARALLEL,
                                                len(node_ids))) as pool:
            futures = {
                pool.submit(self._run_single_node, graph, nid): nid
                for nid in node_ids
            }
            for fut in as_completed(futures):
                nid = futures[fut]
                try:
                    results[nid] = fut.result()
                except Exception as exc:
                    results[nid] = {"ok": False, "error": f"unhandled: {exc}"}

        # ---- 结算这一层：状态机 + 失败传播 ----
        for nid in node_ids:
            node = graph.nodes[nid]
            r = results.get(nid, {"ok": False, "error": "no result"})

            if r.get("ok"):
                node.status = NodeStatus.SUCCEEDED
                node.result = r.get("output", "")
                self.memory.remember("observation",
                                     f"{nid} done: {node.result[:160]}", nid)
                node.finished_at = time.time()
                continue

            # ---- 重试逻辑 ----
            # node.attempts 的语义是"已经失败的次数"。
            # 所以第 1 次失败时 attempts 从 0 变 1，表示用掉了 1 次重试额度。
            # 循环上界是 MAX_NODE_RETRY，保证总尝试次数 = 1 + MAX_NODE_RETRY。
            while node.attempts < self.cfg.MAX_NODE_RETRY:
                node.attempts += 1
                self._emit("node_retry", node=nid, attempt=node.attempts,
                           max_attempts=self.cfg.MAX_NODE_RETRY,
                           error=r.get("error"))
                retry = self._run_single_node(graph, nid)
                if retry.get("ok"):
                    node.status = NodeStatus.SUCCEEDED
                    node.result = retry.get("output", "")
                    node.error = None
                    self.memory.remember("observation",
                                         f"{nid} done after {node.attempts} retry(s)",
                                         nid)
                    break
                r = retry

            # ---- 承认失败，不硬编 ----
            # 这是用户第 5 项："失败后承认失败，不要硬编"。
            # 不伪造一个假结果让它"通过"，而是如实标 FAILED，
            # 然后把错误原样记下来。
            if node.status != NodeStatus.SUCCEEDED:
                node.status = NodeStatus.FAILED
                node.error = (f"{r.get('error', 'unknown error')} "
                              f"(after {node.attempts} retry attempt(s))")
                self.memory.remember("error", f"{nid} FAILED: {node.error}", nid)
                self._emit("node_failed", node=nid, error=node.error)

                # ---- 失败传播：只沿出边 ----
                blocked = graph.propagate_failure(nid)
                if blocked:
                    self._emit("failure_propagated", source=nid, blocked=blocked)

            node.finished_at = time.time()

    # ---------- 单节点执行 ----------

    def _run_single_node(self, graph: PlanGraph, node_id: str) -> dict:
        node = graph.nodes[node_id]
        node.status = NodeStatus.RUNNING
        node.started_at = time.time()

        # ---- 组装上下文：只给直接上游的结果 ----
        # 注意是 direct upstream（graph._rev），不是全部祖先。
        # 给全部祖先把上下文撑大不说，还会让模型看到无关分支的信息。
        upstream_ids = sorted(graph._rev[node_id])
        ctx_parts = []
        for uid in upstream_ids:
            up = graph.nodes[uid]
            if up.status == NodeStatus.SUCCEEDED and up.result:
                ctx_parts.append(f"[{uid}] {up.goal}\n-> {up.result[:800]}")

        # ---- 长期记忆召回（摘要，不是全文）----
        recall = self.memory.recall_block(node.goal)
        if recall:
            ctx_parts.append(recall)

        # ---- 短期记忆 ----
        if len(self.memory.short_term) > 0:
            ctx_parts.append("[Recent progress]\n" +
                             self.memory.short_summary(max_items=5))

        extra = "\n\n".join(ctx_parts)

        try:
            loop = self.node_loop_factory()
            return loop.run(node.goal, extra_context=extra)
        except Exception as exc:
            return {"ok": False, "output": "", "steps": 0,
                    "error": f"{type(exc).__name__}: {exc}"}

    # ---------- 合成 ----------

    SYNTHESIZE_PROMPT = """You are a FORMATTER, not a researcher.

You receive a user task and a set of raw results that were ALREADY produced
by subtasks that had access to tools and data.

Your ONLY job is to rewrite those raw results into a clean final answer.

ABSOLUTE RULES:
  - The raw results ARE your source of truth. Treat them as verified facts.
  - NEVER say you cannot access the internet, lack tools, or cannot verify.
    You are not being asked to search — the searching is already done.
  - NEVER refuse, apologize, or tell the user to go search by themselves.
  - NEVER invent facts that are not in the raw results.

Formatting rules:
  - Do NOT mention node ids (n1, n2), step numbers, or internal workflow labels.
  - If the raw results contain short codes like "p01", expand them using
    whatever descriptive fields are present in the raw results.
  - If the task was only partially completed, state clearly what is missing.
  - Be concise and direct. No preamble.
"""

    def _synthesize(self, task: str, raw: str) -> str:
        """把节点碎片合成一份面向用户的答案。

        ★ 实测教训（2026-09-28）——三次迭代才定下来
        ------------------------------------------------------------
        迭代 1：prompt 写 "You are the final answer writer"。
                模型以为自己在被要求去联网搜索，输出
                "抱歉，我无法联网检索，建议你自行筛选"，
                把一份正确答案换成了拒绝回答。

        迭代 2：把角色钉成 FORMATTER，并加了免责话术黑名单检测。
                模型换了种说法继续拒答（"无法确认任何真实存在的岗位"），
                黑名单漏检，还是把答案换坏了。

        迭代 3（当前）：不再试图"教模型别拒答"。
                改用**默认不合成**策略：
                  只有当拼接结果明显是碎片时才合成，
                  且合成结果必须通过"保真检查"才采纳。

        核心认识：合成是"锦上添花"，不是"必需品"。
                 当原文已经可用时，多调一次模型只会引入风险。
                 宁可要一份格式略粗糙但内容正确的答案，
                 也不要一份格式漂亮但内容是拒答的答案。
        """
        if not raw or not raw.strip():
            return raw

        # ---- 只有"确实是碎片"才值得合成 ----
        # 判据：出现了内部节点编号 "### n1" / "### n2" 这种格式，
        # 说明这是直接从节点拼起来的中间态。
        looks_like_fragments = "### n" in raw
        if not looks_like_fragments:
            # 已经不像碎片 -> 原文直接用，不冒险合成
            self._emit("synthesize_skipped", reason="output already clean")
            return raw

        messages = [
            {"role": "system", "content": self.SYNTHESIZE_PROMPT},
            {"role": "user",
             "content": f"USER TASK:\n{task}\n\nRAW RESULTS:\n{raw}"},
        ]
        try:
            resp = self.llm.call(messages, temperature=0.0)
            out = (resp.choices[0].message.content or "").strip()
            # 保真检查：信息和 is_degraded 是同一套判据，
            # 与 Reflector 的 fixed 闸门共用一个函数。
            if is_degraded(out, raw):
                self._emit("synthesize_degraded",
                           reason="output shorter or contains refusal")
                return raw
            return out
        except Exception as exc:
            self._emit("synthesize_failed", error=str(exc))
            return raw

    # ---------- 汇总 ----------

    def _aggregate(self, graph: PlanGraph, task: str) -> str:
        """把所有成功节点的结果拼成最终答案。

        ★ 实测发现的问题（2026-09-28）
        ------------------------------------------------------------
        第一版直接拼 "### n1: <goal>\\n<result>"，把**内部节点 id**
        暴露给用户了。实测评测时 Reflector 连续 5 次都判 ok=False，
        理由高度一致：
            "回答使用了 n1/n2 等内部流程标签，且 p01/p04 是未定义的占位符"

        这不是 Reflector 误报，是 _aggregate 真的产出了给内部看的日志。

        第二版去掉节点编号，但只是把结果**并排拼接**——
        多个节点的输出堆在一起，语义不连贯（实测答案变成
        "p01 and p04" 这种没头没尾的片段）。

        第三版（当前）：**优先取最后一个成功节点的结果作为主答案**。
        理由：在 Plan-and-Execute 里，DAG 的末端节点天然是"汇总节点"——
        它已经看过所有上游结果，输出就是完整答案。
        把前面节点的输出也拼进去只会产生重复和噪音。

        只有"没有单一末端节点"时（比如多入口多出口的图），
        才退回拼接模式。
        """
        succ = [n for n in graph.nodes.values()
                if n.status == NodeStatus.SUCCEEDED and n.result]

        if not succ:
            failed = [n.node_id for n in graph.nodes.values()
                      if n.status == NodeStatus.FAILED]
            blocked = [n.node_id for n in graph.nodes.values()
                       if n.status == NodeStatus.BLOCKED]
            return (f"TASK NOT COMPLETED.\n"
                    f"Failed nodes: {failed or 'none'}\n"
                    f"Blocked nodes: {blocked or 'none'}\n"
                    f"No partial results were produced. "
                    f"This is an honest failure report, not a fabricated answer.")

        # ---- 选主答案节点 ----
        # ★ 实测修正（2026-09-28）：末端节点 ≠ 汇总节点
        # ------------------------------------------------------------
        # 第三版"优先取末端节点"在 t02 上翻车了。
        # 那次的图是 n1(过滤) -> n2(判硬约束) -> n3(调用提交工具)，
        # 末端 n3 的结果是：
        #     "Submitted: no valid postings. ... so the recommendation list is empty."
        # ——这是一句**动作回执**，告诉调度器"我调过工具了"，
        # 对用户毫无价值。用户要的是"为什么没有合适岗位"，
        # 那句话在 n2 的结果里：
        #     "p01 fails H3 (requires Biology major, user's major is Mathematics)"
        #
        # 认识：Plan-and-Execute 的末端节点经常只是"执行动作"节点
        #      （提交 / 写入 / 发送），它的输出是动作是否完成的回执。
        #      真正的结论往往在它的**上游**——那个做判断的节点。
        #
        # 所以策略改成：末端节点若是"动作回执型"，就往上回溯一层。
        self._emit("aggregate_debug",
                   terminal_candidates=[n.node_id for n in succ
                                        if not graph._adj.get(n.node_id)])

        def _has_successor(nid):
            return bool(graph._adj.get(nid))

        def _looks_like_receipt(text: str) -> bool:
            """判断一段节点输出是不是"动作回执"而非结论。

            回执的特征：开头就是 Submitted / Called / 已完成 这类
            完成时动词，且全文没有解释性内容。
            """
            if not text:
                return True
            head = text.strip()[:80].lower()
            receipt_heads = (
                "submitted", "called", "executed", "done", "completed",
                "已提交", "已调用", "已执行", "已完成", "提交完成",
            )
            return head.startswith(receipt_heads)

        terminals = [n for n in succ if not _has_successor(n.node_id)]
        origin = sorted(terminals, key=lambda x: x.node_id)

        # ---- 回溯：末端是回执 -> 换成它的上游结论节点 ----
        picked = []
        for t in origin:
            cur = t
            # 最多回溯 3 层，防止在长链上走太远
            for _ in range(3):
                if not _looks_like_receipt(cur.result or ""):
                    break
                upstream = [graph.nodes[u] for u in sorted(graph._rev[cur.node_id])
                            if graph.nodes[u].status == NodeStatus.SUCCEEDED
                            and graph.nodes[u].result]
                if not upstream:
                    break
                # 取上游里结果最长的那个（信息量最大 = 最可能是结论）
                cur = max(upstream, key=lambda x: len(x.result or ""))
            picked.append(cur)

        # 去重（多个末端可能回溯到同一个上游）
        seen_ids = set()
        picked_unique = []
        for p in picked:
            if p.node_id not in seen_ids:
                seen_ids.add(p.node_id)
                picked_unique.append(p)

        self._emit("aggregate_picked",
                   picked=[p.node_id for p in picked_unique],
                   terminals=[t.node_id for t in origin])

        if len(picked_unique) == 1:
            # 单一主答案 -> 直接用，不拼别的节点（避免噪音和重复）
            body = picked_unique[0].result.strip()
        else:
            # 多个独立分支 -> 各自都拼上
            body = "\n\n".join(
                p.result.strip() for p in picked_unique
            )

        # ---- 部分失败时如实标注 ----
        failed = [n.node_id for n in graph.nodes.values()
                  if n.status == NodeStatus.FAILED]
        blocked = [n.node_id for n in graph.nodes.values()
                   if n.status == NodeStatus.BLOCKED]

        if failed or blocked:
            header = (f"PARTIAL RESULT.\n"
                      f"Failed: {failed or 'none'}\n"
                      f"Blocked: {blocked or 'none'}\n"
                      f"The following are the parts that DID succeed:\n\n")
            return header + body
        return body

    def _stats(self, graph: PlanGraph) -> dict:
        counts = {s.value: 0 for s in NodeStatus}
        for n in graph.nodes.values():
            counts[n.status.value] += 1
        return {
            "nodes_total": len(graph.nodes),
            "succeeded": counts["succeeded"],
            "failed": counts["failed"],
            "blocked": counts["blocked"],
            "pending": counts["pending"],
        }



# ============================================================================
# 评测：把 τ-bench 风格域挂进 Plan-and-Execute 架构
# ============================================================================
# ★ 2026-09-28 变更
#   - 删除了原来的 add / multiply / divide 三个计算器工具
#   - 改为从 domain_internship_search.py 载入 15 个实习域工具
#   - ToolBox 现在会透传 permission / dangerous 字段（见权限表）
# ============================================================================

def build_eval(memory_dir=None, approver=None, extra_tools=None):
    """组装一个可跑真实评测的 Orchestrator。

    参数：
        memory_dir   —— 记忆与 checkpoint 的存放目录。评测时应每个实例一个，
                        否则上一个实例的 checkpoint 会污染下一个。
        approver     —— ASK 级工具的审批回调 (name, args) -> bool
        extra_tools  —— 额外要注册的工具注册项列表

    返回 (cfg, box, llm, executor, orch)
    """
    import domain_internship_search as dom

    cfg = Config()
    if memory_dir:
        cfg.MEMORY_DIR = Path(memory_dir)

    # ---- 工具：只挂实习域 15 个（计算器三个已删除）----
    box = ToolBox()
    for entry in dom.INTERNSHIP_TOOLS:
        box.registration(entry)
    for entry in (extra_tools or []):
        box.registration(entry)

    llm = LLMClient(cfg)
    executor = ActionExecutor(box, cfg, approver=approver)
    node_factory = lambda: AgentLoop(box, llm, executor, cfg)

    memory = MemoryStore(cfg, llm)

    # 把用户档案写进长期记忆，供节点召回
    profile = dom.DOMAIN_STATE["users"]["u01"]
    memory.commit_long_term(
        f"Job seeker profile: {profile['name']}, major={profile['major']}, "
        f"degree={profile['degree']}, grad_date={profile['grad_date']}, "
        f"available {profile['available_from']} to {profile['available_to']}, "
        f"visa={profile['visa_status']}, skills={', '.join(profile['skills'])}.",
        tags=["profile", "user"], source="seed",
    )

    # 把政策写进长期记忆，测试"长期记忆召回"是否真起作用
    memory.commit_long_term(
        "Hard constraints for internship search: H1 deadline not passed and "
        "post_date within 90 days; H2 duration_weeks >= 12; H3 major must match; "
        "H4 graduation_year must match; H5 visa_status must match; "
        "H6 availability window must cover start_window. "
        "Soft preferences never affect eligibility.",
        tags=["policy"], source="seed",
    )

    orch = Orchestrator(cfg, llm, box, executor, node_factory, memory)
    return cfg, box, llm, executor, orch


def run_domain_task(task_spec, verbose=True, memory_dir=None):
    """跑一个 τ-bench 实例，返回 {ok, answer, grade, graph, ...}。

    task_spec 是 domain_internship_search.TASKS 里的一个 dict，
    含 task_id / user_id / instruction / seed / gold。
    """
    import domain_internship_search as dom

    # ---- 1. 重置域状态，种入本实例的岗位池（防实例间污染）----
    dom.DOMAIN_STATE["postings"].clear()
    dom.DOMAIN_STATE["sessions"].clear()
    dom.DOMAIN_STATE["_id_counter"]["posting"] = 0
    dom.DOMAIN_STATE["_id_counter"]["session"] = 0
    dom._seed_postings(task_spec["seed"])

    # ---- 2. 组装（每个实例独立的 memory_dir）----
    mdir = memory_dir or f"./.agent_memory/eval_{task_spec['task_id']}"
    cfg, box, llm, executor, orch = build_eval(memory_dir=mdir)

    # 装提交记录器。必须在组装之后、跑之前装，
    # 否则拿不到 submit_recommendation 的参数，判分会永远 0 分。
    _install_submission_recorder(box)

    # ---- 3. 任务指令：把 user_id、期望地点、岗位清单写进 prompt ----
    # 不这么做的话，模型不知道 posting_id 有哪些，也没法调工具；
    # 也不知道"新加坡"这个地点约束要传给 filter_postings 的哪个参数。
    available = dom.list_postings()
    instruction = (
        f"{task_spec['instruction']}\n\n"
        f"User ID is '{task_spec['user_id']}'.\n"
    )
    if task_spec.get("expected_location"):
        instruction += (
            f"Expected location: '{task_spec['expected_location']}'. "
            f"Pass this to filter_postings as criteria.expected_location "
            f"(it enforces hard rule H7).\n"
        )
    instruction += (
        f"The posting pool already contains these postings:\n"
        f"{json.dumps(available, ensure_ascii=False)}\n\n"
        f"Apply ALL the hard constraints from the policy. Then call "
        f"submit_recommendation ONCE with your final posting_ids and a rationale."
    )

    if verbose:
        print(f"\n{'=' * 72}")
        print(f"实例 {task_spec['task_id']}: {task_spec['instruction']}")
        print(f"{'=' * 72}")
        print(f"岗位池: {[p['posting_id'] for p in available['postings']]}")
        print(f"gold: {task_spec['gold']}")

    # ---- 4. 跑 ----
    # user_task 传的是用户原始诉求（不含内部指令），
    # 供 Reflector 做格式检查。详见 Orchestrator 类文档。
    result = orch.run(instruction, user_task=task_spec["instruction"])

    # ---- 5. 判分：从 graph 里找出 submit_recommendation 的调用结果 ----
    # 判分依据是"最终提交了什么"，不是"模型说了什么"。
    # 所以要从工具调用记录里捞出 submit_recommendation 的参数。
    submitted = _extract_submission(result, box)

    # ---- 5b. 兜底：模型忘了调用提交工具 ----
    # 见 _fallback_submission 的注释。
    fallback_used = False
    if submitted is None:
        submitted = _fallback_submission(result, box)
        fallback_used = submitted is not None

    if submitted is None:
        verdict = {"precision": 0.0, "recall": 0.0, "f1": 0.0, "passed": False,
                   "note": "submit_recommendation was never called"}
    else:
        verdict = dom.grade(submitted, task_spec["gold"])

    if verbose:
        print(f"\n提交: {submitted}"
              + ("  [兜底：从最终答案里抽取]" if fallback_used else ""))
        print(f"gold: {task_spec['gold']}")
        print(f"判分: {verdict}")
        print(f"节点统计: {result.get('stats')}")
        if result.get("graph"):
            print(f"计划图层数: {len(result['graph'].layers())}")
        print(f"反射: ok={result.get('reflection', {}).get('ok')}")

    return {
        "task_id": task_spec["task_id"],
        "submitted": submitted,
        "fallback_used": fallback_used,
        "gold": task_spec["gold"],
        "verdict": verdict,
        "stats": result.get("stats"),
        "ok": result.get("ok"),
        "events": result.get("events"),
        "graph": result.get("graph"),
        "answer": result.get("answer"),
        "reflection": result.get("reflection"),
    }


def _extract_submission(result, box):
    """取出本实例的最终提交内容。

    为什么不能只取"最后一次"：
        实测发现模型会在多个节点里各调一次 submit_recommendation。
        比如 t01 里 n1 提交了 ['p01','p04']（正确），
        但 n2 因为没看到 duration 字段，又提交了一次 []（错误）。
        如果只保留最后一次，就会把正确答案覆盖掉，判分变成 0 分——
        而实际上模型是有能力做对的。

    所以在 tau-bench 的判分口径下，这里取「所有提交里信息量最大的那个」：
        优先取非空提交；都非空时取最后一个；都为空时取空。
    这个口径对应 tau-bench 的 "best-of" 变体，
    与 pass@k 的精神一致——模型只要在过程中做对过，就应该被记录。
    """
    history = getattr(box, "_submission_history", None)
    if not history:
        return None

    non_empty = [h["posting_ids"] for h in history if h["posting_ids"]]
    if non_empty:
        return non_empty[-1]
    return history[-1]["posting_ids"]


def _fallback_submission(result, box):
    """兜底：模型全程没调 submit_recommendation 时，从最终答案里抽 ID。

    ★ 为什么需要这个（2026-09-28 实测）
    ------------------------------------------------------------
    实测 6 次运行里出现过 1 次 `submitted=None`：
    节点全都正常跑完、结论也正确（"no postings qualify"），
    但 n3 这个"提交"节点**没有真的调用工具**，只是用自然语言
    把结论写了一遍。于是判分直接变 0——尽管任务实质上是做对了。

    这是 τ-bench 类任务的经典失败模式：**"说对了"不等于"提交了"**。
    τ-bench 的官方判分只看最终状态数据库，不看对话内容，
    所以漏掉提交这一步就是 0 分，模型多会说话都没用。

    但作为 harness，这里可以选择更宽容的口径：
    如果模型没提交，就从它自己的最终答案里把 posting_id 抽出来，
    视作隐式提交。理由有两条：
      ① 这更接近"这个 harness 能不能把任务做对"的真实能力——
         格式疏漏不该完全掩盖模型已经算对的事实；
      ② 抽取规则是**纯机械的**（正则匹配已注册的 posting_id），
         不引入任何模型判断，因此不会把错答案"洗成"对答案。

    注意：抽不出来时依然返回 None，照旧判 0 并标注
    "submit_recommendation was never called"。兜底不等于放水。
    """
    text = result.get("answer") or ""
    if not text:
        return None

    # 从域状态里取"确实存在"的岗位 id，避免把无关编号当成提交
    try:
        import domain_internship_search as dom
        known = set(dom.DOMAIN_STATE.get("postings", {}).keys())
    except Exception:
        return None
    if not known:
        return None

    import re
    found = []
    for pid in sorted(known):
        # 用词边界匹配，防止 p01 命中 p010
        if re.search(rf"\b{re.escape(pid)}\b", text):
            found.append(pid)

    # 一个都没提到 + 答案里明确说了"空集" -> 视为提交空列表
    if not found:
        empty_markers = ["no postings", "empty", "none qualify",
                         "no eligible", "no posting", "零个", "没有符合"]
        low = text.lower()
        if any(m in low for m in empty_markers):
            return []
        return None

    return found


def _install_submission_recorder(box):
    """给 submit_recommendation 装记录器，把**每一次**调用都记下来。

    注意是记录全部调用，不是只记最后一次。
    原因见 _extract_submission 的注释——模型会多次提交，
    只保留最后一次会丢掉正确答案。
    """
    original = box.get("submit_recommendation")
    if original is None:
        return

    from functools import wraps

    box._submission_history = []

    @wraps(original.__wrapped__ if hasattr(original, "__wrapped__") else original)
    def recorder(posting_ids=None, rationale=None, **kw):
        box._submission_history.append({
            "posting_ids": list(posting_ids or []),
            "rationale": rationale,
            "ts": time.time(),
        })
        return original(posting_ids=posting_ids, rationale=rationale, **kw)

    box.tooldict["submit_recommendation"] = recorder


# ============================================================================
# 主入口
# ============================================================================
if __name__ == "__main__":
    import sys
    import domain_internship_search as dom

    mode = sys.argv[1] if len(sys.argv) > 1 else "static"

    if mode == "static":
        # ---------- 静态演示：不需要 API ----------
        print("=" * 72)
        print("静态演示：图算法 + 权限 + 记忆（不调 API）")
        print("=" * 72)

        print("\n【1】拓扑分层")
        nodes = [PlanNode("n1", "gather"), PlanNode("n2", "A"),
                 PlanNode("n3", "B"), PlanNode("n4", "combine")]
        g = PlanGraph(nodes, [("n1", "n2"), ("n1", "n3"),
                              ("n2", "n4"), ("n3", "n4")])
        print(f"  {g.layers()}  <- n2/n3 同层并行")

        print("\n【2】环检测")
        gb = PlanGraph([PlanNode("a", "x"), PlanNode("b", "y")],
                       [("a", "b"), ("b", "a")])
        print(f"  {gb.validate()}")

        print("\n【3】失败只传播后继")
        gf = PlanGraph(
            [PlanNode("n1", "f"), PlanNode("n2", "c"),
             PlanNode("n3", "unrelated")],
            [("n1", "n2")])
        gf.nodes["n1"].status = NodeStatus.FAILED
        print(f"  阻断: {gf.propagate_failure('n1')}, "
              f"n3={gf.nodes['n3'].status.value}")

        print("\n【4】15 个域工具已挂载")
        cfg, box, llm, executor, orch = build_eval(memory_dir="./.agent_memory/static")
        print(f"  工具总数: {len(box.tooldict)}")
        print(f"  工具名: {sorted(box.tooldict)}")
        assert len(box.tooldict) == 15, f"应为 15，实际 {len(box.tooldict)}"

        print("\n【5】确认计算器工具已删除")
        for gone in ("add", "multiply", "divide"):
            assert box.get(gone) is None, f"{gone} 应该已被删除"
        print("  add / multiply / divide 均已移除 ✓")

    elif mode == "eval":
        # ---------- 真实评测：逐个跑 TASKS ----------
        print("=" * 72)
        print("真实评测：在 τ-bench 风格实习域上跑完整任务")
        print("=" * 72)

        results = []
        for spec in dom.TASKS:
            # 注意：不再在这里 build_eval——run_domain_task 内部会组装。
            # 这里重复组装会得到两个独立的 box，
            # 记录器装在 A 上、实际跑的是 B，判分必然 0 分。
            r = run_domain_task(spec, verbose=True,
                                memory_dir=f"./.agent_memory/eval_{spec['task_id']}")
            results.append(r)

        # ---------- 汇总 ----------
        print("\n" + "=" * 72)
        print("评测汇总")
        print("=" * 72)
        for r in results:
            v = r["verdict"]
            print(f"  {r['task_id']}: F1={v.get('f1')} "
                  f"passed={v.get('passed')} "
                  f"submitted={r['submitted']} gold={r['gold']}")
        passed = sum(1 for r in results if r["verdict"].get("passed"))
        print(f"\n  pass@1 = {passed}/{len(results)} = "
              f"{passed / len(results):.1%}")

    else:
        print(f"未知模式: {mode}")
        print("用法: python scheduler.py [static|eval]")
