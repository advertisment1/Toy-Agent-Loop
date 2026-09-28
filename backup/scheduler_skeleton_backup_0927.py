"""
D29 手写 Agent Scheduler —— 五模块裸 OpenAI SDK 版（完整可运行）

对照物: calculator.py (LangGraph 版)
目标:   跑通 "Add 3 and 4."，产出结构相同的四段轨迹
        user -> assistant(tool_calls) -> tool(result) -> assistant(content)

设计参考:
  - LangGraph 设计文档: node / edge / state
  - Anthropic《Building Effective Agents》:
    "LLMs using tools based on environmental feedback in a loop"

模块之间的真实关系（全部是组合，没有继承）:
  Config        —— 定义配置字段；用 cfg = Config() 实例化后访问 cfg.MODEL
  LLMClient     —— 实例化；构造时接收 cfg，持有 OpenAI 客户端
  ActionParser  —— 无状态，静态方法即可
  ToolExecutor  —— 实例化；持有工具注册表
  AgentLoop     —— 实例化；在 __init__ 里持有 cfg / llm / executor
"""

import json
import os
import time

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()


# ============================================================
# 模块 1: Config
# 这里只定义配置字段。使用时应先实例化: cfg = Config()
# 然后通过实例访问: cfg.MODEL / cfg.MAX_STEPS
# （Python 虽允许 Config.MODEL 这样直接访问类属性，但容易与实例访问混淆，
#   本文件统一改为实例访问，保持风格一致。）
# ============================================================
class Config:
    def __init__(self):
        self.MAX_STEPS = 6                       # 保险丝 1: 最大循环步数
        self.TIMEOUT = 60                        # 保险丝 2: 总耗时上限（秒）
        self.MODEL = "deepseek-chat"             # 小写连字符，不是 "DEEPSEEK chat"
        self.BASE_URL = "https://api.deepseek.com"
        self.API_KEY_ENV = "DEEPSEEK_API_KEY"    # 只存变量名，不存 key


# ============================================================
# 模块 2: LLMClient —— 模型调用的唯一出口
# 构造时接收 Config 实例（依赖注入），自己不再直接读类属性。
# ============================================================
class LLMClient:
    def __init__(self, cfg):
        self.cfg = cfg                           # 持有 Config 实例
        api_key = os.getenv(self.cfg.API_KEY_ENV)
        if not api_key:
            raise RuntimeError(
                f"环境变量 {self.cfg.API_KEY_ENV} 未设置；请确认 .env 在当前工作目录"
            )
        self.client = OpenAI(api_key=api_key, base_url=self.cfg.BASE_URL)

    def call(self, messages, tools=None):
        """发一次请求，返回原始 ChatCompletion 对象，不在此处做解析。"""
        return self.client.chat.completions.create(
            model=self.cfg.MODEL,
            messages=messages,
            tools=tools,
        )


# ============================================================
# 模块 3: ActionParser —— 从 response 里抽出"意图"
# 只判断这一次是"要调工具"还是"直接作答"，不执行任何东西。
# ============================================================
class ActionParser:
    @staticmethod
    def parse(response):
        """
        返回 (message, actions)
          message —— assistant 消息对象本身，需要原样塞回历史
          actions —— list of (call_id, tool_name, args_dict)
                     模型直接作答时为空列表
        """
        message = response.choices[0].message

        # 裸 SDK 的两个关键字段:
        #   message.content    -> str | None
        #   message.tool_calls -> list[ChatCompletionMessageToolCall] | None
        # 二者互斥，不会同时有值。
        if not message.tool_calls:
            return message, []

        actions = []
        for tc in message.tool_calls:
            # 与 LangChain 版的差异（手写时最容易踩）:
            #   LangChain: tc["name"]          / tc["args"]（已是 dict）
            #   裸 SDK:    tc.function.name    / tc.function.arguments（JSON 字符串）
            actions.append((
                tc.id,                                 # 回填 tool 消息时必须配对
                tc.function.name,
                json.loads(tc.function.arguments),
            ))
        return message, actions


# ============================================================
# 模块 4: ToolExecutor —— 真正执行工具
# 持有工具注册表: 工具名 -> 可调用对象。
# 注册表的 key 必须与 TOOL_SCHEMAS 里的 function.name 一一对应。
# ============================================================
class ToolExecutor:
    def __init__(self):
        self.registry = {
            "add": lambda a, b: a + b,
            "multiply": lambda a, b: a * b,
            "divide": lambda a, b: a / b,
        }

    def execute(self, name, args):
        """按名字取出函数并调用；返回值强制转字符串（API 要求 tool content 为 string）。"""
        func = self.registry.get(name)
        if func is None:
            return f"Error: unknown tool '{name}'"
        try:
            return str(func(**args))
        except Exception as exc:
            return f"Error: {exc}"


# 给模型的"工具说明书"（JSON Schema），只有描述，没有代码。
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "add",
            "description": "Add two integers together.",
            "parameters": {
                "type": "object",
                "properties": {
                    "a": {"type": "integer", "description": "The first integer"},
                    "b": {"type": "integer", "description": "The second integer"},
                },
                "required": ["a", "b"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "multiply",
            "description": "Multiply two integers together.",
            "parameters": {
                "type": "object",
                "properties": {
                    "a": {"type": "integer", "description": "The first integer"},
                    "b": {"type": "integer", "description": "The second integer"},
                },
                "required": ["a", "b"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "divide",
            "description": "Divide the first integer by the second.",
            "parameters": {
                "type": "object",
                "properties": {
                    "a": {"type": "integer", "description": "The numerator"},
                    "b": {"type": "integer", "description": "The denominator"},
                },
                "required": ["a", "b"],
            },
        },
    },
]

SYSTEM_PROMPT = (
    "You are a helpful assistant tasked with performing arithmetic "
    "on a set of inputs. Use the provided tools to compute the answer."
)


# ============================================================
# 模块 5: AgentLoop —— 主循环
# 组合发生在这里: __init__ 里 new 出 LLMClient / ToolExecutor 存到 self，
# 之后 run() 里的 self.llm.call(...) / self.executor.execute(...) 才有东西可指。
# 它们不是 AgentLoop 的父类，所以必须显式持有 —— 这就是你问的那一点。
# ============================================================
class AgentLoop:
    def __init__(self, task):
        self.task = task
        self.cfg = Config()                  # 组合: 先实例化配置
        self.llm = LLMClient(self.cfg)       # 组合: 把 cfg 注入 LLMClient
        self.executor = ToolExecutor()       # 组合: 手里有一个 ToolExecutor
        self.messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ]

    def run(self):
        start = time.time()

        for step in range(self.cfg.MAX_STEPS):
            if time.time() - start > self.cfg.TIMEOUT:    # 保险丝 2
                return f"[中止] 超过 {self.cfg.TIMEOUT}s 未完成"

            print(f"\n===== step {step + 1}: 调模型 =====")
            response = self.llm.call(self.messages, tools=TOOL_SCHEMAS)
            message, actions = ActionParser.parse(response)

            # 出口: 不再要工具 -> 已经给出最终答案
            if not actions:
                self.messages.append({"role": "assistant", "content": message.content})
                print("===== 结束: 模型直接作答 =====")
                return message.content

            # 要调工具: 先把 assistant 消息（内含 tool_calls）塞回历史
            self.messages.append(message)

            # 逐条执行，把观察结果以 role=tool 塞回历史
            for call_id, name, args in actions:
                result = self.executor.execute(name, args)
                print(f"  调用 {name}({args}) -> {result}")
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,   # 必须与上面的 tool_call 配对
                    "content": result,         # 必须是字符串
                })

        return f"[中止] 达到最大步数 {self.cfg.MAX_STEPS}"     # 保险丝 1


def show_trace(loop):
    """打印完整轨迹，用于和 LangGraph 版对照。"""
    print("\n===== 完整轨迹 =====")
    for msg in loop.messages:
        if isinstance(msg, dict):
            role, content = msg["role"], msg.get("content")
            tool_calls = msg.get("tool_calls")
            tool_call_id = msg.get("tool_call_id")
        else:
            role, content = msg.role, getattr(msg, "content", None)
            tool_calls = getattr(msg, "tool_calls", None)
            tool_call_id = None

        print(f"--- {role} ---")
        if tool_calls:
            for tc in tool_calls:
                print(f"  tool_call: {tc.function.name}({tc.function.arguments})")
        if content:
            print(f"  {content}")
        if tool_call_id:
            print(f"  (tool_call_id={tool_call_id})")


if __name__ == "__main__":
    loop = AgentLoop("Add 3 and 4.")
    answer = loop.run()
    print(f"\n最终答案: {answer}")
    show_trace(loop)
