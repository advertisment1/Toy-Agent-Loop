from dotenv import load_dotenv

load_dotenv()  # 读当前目录下的 .env

import json
import os
import time

# 资源：参考 LangGraph 设计文档 https://docs.langchain.com/oss/python/langgraph
# + Anthropic《Building Effective Agents》(https://www.anthropic.com/engineering/building-effective-agents)。
# 要求：确定 300–500 行简化 scheduler 的模块划分；
# 先写主循环：接收任务 → 调 LLM → 解析动作 → 执行 →
# 回写观察，带最大步数/超时两道保险丝。今天只跑通"能循环、会停止"。
from openai import OpenAI


class Config:
    MAX_STEP = 100
    TIMEOUT = 60
    # 注意：模型名必须是 "deepseek-chat"（小写 + 连字符），
    # 中间带空格会直接 400。
    MODEL = "deepseek-chat"
    BASE_URL = "https://api.deepseek.com"


class ToolBox:
    # tools的管理类
    # 维护一个toolintro是工具的符合openai调用的结构化参数，记录所有可以调用的工具
    # 维护一个tooldict是工具的实例
    #
    # 两者必须分开存，这是关键设计：
    #   toolintro —— 发给模型看的「说明书」(JSON Schema)，走网络
    #   tooldict  —— 留给本地执行的「函数表」, 不出网络
    def __init__(self):
        self.tooldict = {}
        self.toolintro = {}
        pass

    def registration(self, parameter):
        # 如果工具合法就注册到tooldict和toolintro中
        #
        # parameter 期望是一个 dict，形如:
        # {
        #     "name": "add",
        #     "description": "Add two integers together.",
        #     "parameters": { "type": "object", "properties": {...}, "required": [...] },
        #     "func": lambda a, b: a + b,
        # }
        # 校验三件事：name 存在、不重复注册、func 可调用。
        name = parameter.get("name")
        func = parameter.get("func")

        if not name:
            raise ValueError("工具缺少 name 字段")
        if name in self.tooldict:
            raise ValueError(f"工具 {name} 已注册，不能重复注册")
        if not callable(func):
            raise ValueError(f"工具 {name} 的 func 不可调用")

        # ① 存可调用对象（本地执行用）
        self.tooldict[name] = func

        # ② 存 OpenAI tool schema（发给模型用）
        #    注意结构：外层包 type: function，真正的描述在 "function" 里
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

    # def registration_json(self, loc, func_map):

    def show(self):
        # 给模型用的 tools 参数，必须是 list（不能是 dict）
        return list(self.toolintro.values())

    def get(self, name):
        # 给本地执行用的函数表查询
        return self.tooldict.get(name)


class LLMClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = OpenAI(
            api_key=os.getenv("DEEPSEEK_API_KEY"), base_url=cfg.BASE_URL
        )

    def call(self, message, tools=None):
        # 真正发一次请求，返回原始 ChatCompletion 对象。
        # 不在这里做任何解析——解析是上层的事。
        return self.client.chat.completions.create(
            model=self.cfg.MODEL,
            messages=message,
            tools=tools,
        )


class ActionExecutor:
    def __init__(self, tool_box):
        # 执行器需要拿到「函数表」才能按名字找到工具
        self.tool_box = tool_box

    def execute(self, tool_call):
        # 这里接收的是「单个」tool_call，不是列表。
        # 返回一条可以直接 append 进 message 的 tool 消息 dict。
        #
        # 裸 SDK 的取法（与 LangChain 版不同）:
        #   tc.function.name        <- 工具名
        #   tc.function.arguments   <- 参数，是 JSON 字符串，需要 json.loads
        name = tool_call.function.name
        args = json.loads(tool_call.function.arguments)

        func = self.tool_box.get(name)
        if func is None:
            content = f"Error: unknown tool '{name}'"
        else:
            try:
                content = str(func(**args))
            except Exception as exc:
                content = f"Error: {exc}"

        return {
            "role": "tool",
            "tool_call_id": tool_call.id,  # 必须与对应的 tool_call 配对
            "content": content,  # 必须是字符串
        }


class AgentLoop:
    def __init__(
        self,
        tool_box,
        llm_client,
        executor,
        cfg,
        PROMPT="You are a helpful assistant. You can use the provided tools to answer.",
    ):
        self.tools = tool_box.show()
        self.LLM = llm_client
        self.executor = executor
        self.cfg = cfg
        self.PROMPT = PROMPT
        # 补充agent提示词，提交给onpenai借口，角色为system.

    def run(self, task):
        start = time.time()

        message = [
            {"role": "system", "content": self.PROMPT},
            {"role": "user", "content": task},
        ]
        try_time = 0
        while try_time < self.cfg.MAX_STEP:
            # 增加超时判断：必须放在循环体开头。
            # 放末尾的话，一次慢请求会拖到返回后才检查，保险丝就失效了。
            if time.time() - start > self.cfg.TIMEOUT:
                return "[中止] 超时"

            response = self.LLM.call(message=message, tools=self.tools)
            tool_calls = response.choices[0].message.tool_calls
            if tool_calls:
                # ① 先把 assistant 这条塞回历史。
                #    它带着 tool_calls，是后面 tool 消息能配对的前提；
                #    缺了它，API 会因「tool 消息前面没有对应的 tool_call」报 400。
                message.append(response.choices[0].message)

                # ② 向message中添加tool：result，逐个添加
                for tc in tool_calls:
                    result = self.executor.execute(tc)
                    message.append(
                        result
                    )  # 因为executor本身的返回就是结构化的message dict

            else:
                # 出口：模型不再要工具 = 已经给出最终答案。
                # 返回的是答案文本，不是整个历史列表。
                return response.choices[0].message.content
            try_time += 1
        return "达到最大步数，任务未完成"  # 保险丝 1：步数耗尽


if __name__ == "__main__":
    # ---------- 组装（依赖注入）----------
    cfg = Config()
    box = ToolBox()

    box.registration(
        {
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
            "func": lambda a, b: a + b,
        }
    )
    box.registration(
        {
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
            "func": lambda a, b: a * b,
        }
    )
    box.registration(
        {
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
            "func": lambda a, b: a / b,
        }
    )

    llm = LLMClient(cfg)
    executor = ActionExecutor(box)
    loop = AgentLoop(box, llm, executor, cfg)

    answer = loop.run("对比华为和小米手机")
    print("最终答案:", answer)
