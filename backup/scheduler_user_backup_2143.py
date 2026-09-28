from dotenv import load_dotenv

load_dotenv()  # 读当前目录下的 .env

import os
import time

# 资源：参考 LangGraph 设计文档 https://docs.langchain.com/oss/python/langgraph
# + Anthropic《Building Effective Agents》(https://www.anthropic.com/engineering/building-effective-agents)。
# 要求：确定 300–500 行简化 scheduler 的模块划分；
# 先写主循环：接收任务 → 调 LLM → 解析动作 → 执行 →
# 回写观察，带最大步数/超时两道保险丝。今天只跑通"能循环、会停止"。
from openai import OpenAI


class config:
    MAX_STEP = 6
    TIMEOUT = 60
    MODEL = "DEEPSEEK chat"


class memory:
    def __init__(self):
        self.status = None

    pass


class toolsbox:
    # tools的管理类
    # 维护一个toolintro是工具的符合openai调用的结构化参数，记录所有可以调用的工具
    # 维护一个tooldict是工具的实例
    def __init__(self):
        self.tooldict = {}
        self.toolintro = {}
        pass

    def registration(self, parameter):
        # 如果工具合法就注册到tooldict和toolintro中
        pass

    def show(self):
        return self.toolintro


class LLMclient:
    def __init__(self):
        self.client = OpenAI(
            api_key=os.getenv("DEEPSEEK_API_KEY"), base_url="https://api.deepseek.com"
        )

    def call(self, message, tools=None):
        response = None
        return response


class Actionexecutor:
    def __init__(self):
        pass

    def execute(self, tool_calls):

        return


class agentloop:
    def __init__(self, toolsbox1, LLMclient1, Actionexecute1, config1):
        self.tools = toolsbox1.show()
        self.LLM = LLMclient1
        self.executor = Actionexecute1
        self.cfg = config1

    def run(self, task):
        start = time.time()
        message = [{"role": "user", "content": task}]
        try_time = 0
        while try_time < self.cfg.MAX_STEP:
            response = self.LLM.call(message=message, tools=self.tools)
            tool_calls = response.choices[0].message.tool_calls
            if tool_calls:
                result = self.executor.execute(tool_calls)
                # 向message中添加tool：result，逐个添加

            else:
                return message
            # 增加超时判断
            try_time += 1
        return  # 超时错误
