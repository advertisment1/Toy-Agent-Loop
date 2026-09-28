"""
================================================================================
τ-bench 这一类方法：对特殊模块的要求与它自己立的规则
================================================================================

本节记录 τ-bench / τ²-bench 这一系「有状态、有政策、可判分」的基准，
对 agent 的哪些模块提出了额外要求，以及它自己遵守哪些设计规则。
配套实现见同目录 domain_internship_search.py。

────────────────────────────────────────────────────────────────────────────────
零、τ-bench 到底是什么
────────────────────────────────────────────────────────────────────────────────

它不是「一个数据集」，而是「一套任务实例的格式」。一个实例（task）由三件
东西构成，缺一不可：

    ① 工具集（tools）    —— 模型能调用的全部函数，自带 JSON Schema
    ② 政策（policy）     —— 一段自然语言规则，塞进 system prompt
    ③ 状态 + gold        —— 初始数据库，以及「跑完之后应该变成什么样」

判分方式是「比对最终状态」，不是「看模型说了什么」。这一点是本系基准和
GSM8K 那种「对答案」式评测的根本区别。

────────────────────────────────────────────────────────────────────────────────
一、对工具层（ToolBox / ToolExecutor）的要求
────────────────────────────────────────────────────────────────────────────────

【要求 1】工具必须持有跨调用的共享状态。

    你现在的 registry 里是 lambda a, b: a + b —— 无状态纯函数。
    τ-bench 的工具全部是有状态的：它们读写同一个 domain state。

    实测影响：save_posting 要能查到「之前存过哪些 URL」才能去重；
              check_hard_constraints 要能读到 posting 池才知道比什么。
              纯函数做不到这两件事。

【要求 2】工具失败必须用返回值表达，不能抛异常。

    这是 τ-bench 的「错误契约」。看 airline 域的官方实现：
    工具遇到业务规则违规时返回字符串 "Error: ..."，
    而不是 raise 一个异常。

    为什么要这样设计 —— 因为本系基准考的就是「恢复能力」。
    模型读到 "Error: duplicate: url already saved as p03"，
    才有机会改成「那我跳过这条，去抓下一个」。
    如果抛异常炸掉循环，这一题直接 0 分。

    对照你的代码：scheduler.py 的 ActionExecutor.execute() 已经有
    try/except 了（L124-127），这一条你是达标的。但注意
    scheduler_skeleton.py 的 ToolExecutor.execute() 也是同样的写法
    （L123-126）——两份都对了，保持住。

【要求 3】判定类工具要返回「逐条理由」，不能只返回布尔值。

    反例：check_eligibility -> {"eligible": false}
    正例：check_eligibility -> {"eligible": false,
                               "reasons": [{"rule": "duration",
                                            "pass": false,
                                            "detail": "8 weeks < required 12 weeks"}]}

    只给模型一个 false，它无从知道错在哪、能不能补救，只能瞎猜重试。
    给了逐条理由，它才能做出「换个岗位」还是「向用户说明」的正确决策。

【要求 4】工具数量控制在 15–20 个以内。

    τ-bench 用 14（airline）/ 16（retail）不是随便定的。
    实测数据：工具数从「1 个」涨到「20+ 个」时，选择准确率从 95-96%
    掉到 65-78%。15–20 是「能力足够」和「不淹没模型」的平衡点。

    本域取 15 个，正好落在这个区间内。

【要求 5】每个阶段要有「盘点工具」。

    list_postings 这类工具看起来多余，但它解决一个真实问题：
    模型跑到 20 步以后会失去对自己状态的掌握，开始重复抓同一个页面。
    给它一个「我做过什么」的查询口，能显著降低重复动作。

【要求 5b】工具的注册表要能从外部注入，不能写死在 __init__ 里。

    ★ 实测对比：
      scheduler.py 的 ToolBox        —— registry 由 registration() 动态填充，
                                        换域只需重新注册，不用改类。✓
      scheduler_skeleton.py 的
      ToolExecutor                   —— __init__ 里写死三个 lambda，
                                        换域必须改类定义。✗

    挂 15 个域工具实测时，必须整个替换 te.registry 才能用。
    这在小玩具上无所谓，但 τ-bench 这类任务要求「同一套循环
    跑不同域」，写死注册表就没法复用。

    建议：ToolExecutor 改成像 ToolBox 一样接受注入，
          或者直接复用 ToolBox 的 tooldict。

────────────────────────────────────────────────────────────────────────────────
二、对主循环（AgentLoop）的要求
────────────────────────────────────────────────────────────────────────────────

【要求 6】要能识别「终止工具」。

    τ-bench 的会话不是「模型不再调工具」就结束的（你现在的出口逻辑），
    而是模型显式调用 submit_recommendation 之类的工具来宣告完成。

    两种出口的区别：
      你的写法  —— 模型不调工具了 = 结束（隐式）
      τ-bench  —— 模型调用了终止工具 = 结束（显式）

    显式终止的好处：可以在终止工具的参数上做校验（比如推荐了不存在的
    posting_id 就打回去重来），隐式出口没有这个校验点。

实现建议：终止工具在返回值里带一个标志位（本域用 "_terminate": True），
        主循环见到它就 return，不再看 tool_calls 是否为空。

【要求 7】循环的「步数预算」要放宽。

    τ-bench 一个实例动辄 20–40 步（抓取 → 登录 → 抽取 → 入库 → 筛选 → 提交）。
    你的 MAX_STEP = 100 够用，但 scheduler_skeleton.py 的 MAX_STEPS = 6
    是不够的——那是给「Add 3 and 4」这种玩具任务定的。

【要求 8】要能跑「多个任务实例」，并在实例之间重置状态。

    你的 run(task) 是一次性的。τ-bench 要跑 50–115 个实例，
    每个实例开始前必须把 domain state 重置干净，
    否则上一个实例的岗位池会污染下一个。

实现建议：在 AgentLoop 外面套一层评测循环，
         每个实例开始前调用域提供的 reset 函数。

────────────────────────────────────────────────────────────────────────────────
三、对消息历史（messages）的要求
────────────────────────────────────────────────────────────────────────────────

【要求 9】工具返回的内容必须能被模型读懂，不能是 Python repr。

    你的 str(func(**args)) 对 "3 + 4 = 7" 够用，
    但对嵌套 dict 会输出 "{'a': 1, 'b': [1, 2]}" —— 单引号、无缩进的
    Python 字面量。模型能勉强读，但不如 json.dumps(..., ensure_ascii=False)
    清晰，而且中文会被转义成 unicode 转义序列。

    ★ 实测发现（比上面更严重的问题）：str() 会破坏下游的结构化访问。
      用 domain_internship_search.py 实测时暴露：
        ToolExecutor.execute("open_session", {...})
          -> 返回 "{'status': 'ok', 'session_id': 's02', ...}"（一个字符串）
          -> 拿到它之后 r["session_id"] 直接 TypeError
      因为字符串不能用字典键索引。

      结论：工具返回值应该在**工具内部**就 json.dumps 成字符串，
      然后 executor 不要再 str() 一次。否则「多步任务里上一步的输出
      作为下一步的输入」这条链路必然断掉——
      而这正是 τ-bench 这类多步任务的基本形态。

【要求 10】长内容要截断并标注。

    fetch_page 可能返回几万字的网页正文。直接塞进 messages 会：
      ① 爆 context window
      ② 稀释关键信息，模型找不到重点

    标准做法：截断到 N 个字符，并在返回值里带 "truncated": true
    和一个明确提示。本域工具已有这个字段。

────────────────────────────────────────────────────────────────────────────────
四、τ-bench 自己立的规则（设计者必须遵守的）
────────────────────────────────────────────────────────────────────────────────

【规则 A】政策必须写在 prompt 里，不能硬编码进工具。

    反例：把「duration >= 12 否则报错」写死在 check_hard_constraints 内部。
    正例：把规则写成自然语言文本，同时让工具返回「哪条规则没过」。

    原因：考的是模型能不能读懂规则并遵守，不是考它会不会调一个
         已经帮你判好了的函数。规则一旦硬编码，模型不需要读政策，
         这个考点就消失了。

【规则 B】硬约束和软约束必须分家。

    硬约束（hard）：
      - 客观、可程序判定
      - 不满足即出局
      - 存在结构化字段里（requirements[]）
      - 工具名要写清 hard，防止误用

    软约束（soft）：
      - 主观、不可程序判定
      - 只影响推荐理由，不影响是否推荐
      - 存在于自然语言正文里
      - 单独一个工具抽取，和过滤路径物理隔离

【规则 C】测试数据要冻结时间。

    本域用 NOW = datetime(2026, 9, 27) 而不是 datetime.now()。
    如果用后者，deadline 判断会随运行日期漂移，
    同一份代码今天跑是满分、下个月跑就不及格。

【规则 D】判分用 F1，不用准确率。

    「推荐 3 个对了 2 个」和「推荐 10 个对了 2 个」，
    准确率的算法会让后者看起来也没那么糟（都是 2 个对），
    但实际上后者引入了 8 个错误推荐。
    F1 同时惩罚漏推和错推，才是正确的度量。

【规则 E】工具数、任务数、步骤数都要有上限。

    τ-bench airline 只有 50 个实例、14 个工具。
    规模刻意压小，是为了让整份评测能在一杯咖啡的时间内跑完、
    结果可复现。不要一上来就造 1000 个实例。

────────────────────────────────────────────────────────────────────────────────
五、给你的 scheduler 的改造清单（按性价比排序）
────────────────────────────────────────────────────────────────────────────────

    优先级  改什么                                    改动量   收益
    ─────  ────────────────────────────────────────  ──────  ──────
      P0    工具持有共享状态（建 domain state 单例）    中      必须
      P0    终止工具 + _terminate 标志识别             小      高
      P0    工具内 json.dumps，去掉 executor 的 str()   极小    必须
      P1    步数上限从 6 提到 30+                      极小    高
      P1    ToolExecutor 的 registry 改成可注入         小      中
      P2    写判分函数（grade 用 F1）                   中      高
      P2    外层评测循环 + 每实例重置状态               中      高
      P3    长内容截断 + truncated 标志                小      中
      P3    跑 k 次报 pass^k（不是 pass@k）            小      中

    已达标项（不用改）：
      ✓ ActionExecutor.execute() 的 try/except（契约 A 已满足）
      ✓ tool 消息的 role / tool_call_id / content 三要素配对
      ✓ assistant 消息回填（tool_calls 能配对的前提）
      ✓ ToolBox 的双字典分离设计（toolintro 走网络 / tooldict 不出网络）
      ✓ ToolBox.registration() 的动态注册（换域不用改类）

────────────────────────────────────────────────────────────────────────────────
五-B、实测踩过的坑（2026-09-28，scheduler.py 真跑评测后追加）
────────────────────────────────────────────────────────────────────────────────

这一节记的是**真正跑起来之后**才暴露的问题。它们有一个共同主题：

    「模型会说话」不等于「任务做对了」；
    「模型做对了」也保不住——因为后续环节会把它改坏。

四个坑，按发现顺序：

  坑 1  统一的序列化契约必须**只在一处**做
        工具函数返回 dict，executor 又 json.dumps 一次
        -> 双重编码，模型拿到的是被转义过的字符串，要解析两层。
        定论：域文件的 _serialize() 负责编码，executor 只做兜底，
              两边都做是错的。实测确认单层编码后 json.loads 一次即得 dict。

  坑 2  节点输出只给内部主键 -> 最终答案用户看不懂
        filter_postings 第一版只返回 ["p01","p04"]，
        模型照抄进最终答案，用户看到两个编号完全不知道是哪家公司。
        定论：工具返回的**可读字段**决定最终答案的可读性。
              查询类工具必须返回 company/title/location/duration 这类
              人话字段，主键只能当附加信息。
              τ-bench 官方工具同样是这个口径。

  坑 3  反射环节会把**正确答案换成拒答**
        Reflector 判 ok=False 后，fixed 字段里写的是一段
        "抱歉，我无法访问实时数据库，建议您自行搜索"，
        直接把一份 F1=1.0 的答案替换成了拒答。
        与合成环节（_synthesize）踩的是同一个坑，只是位置换了。
        定论：任何"让模型改写答案"的环节都必须过**保真闸门**——
              信息量不能减少、不能出现免责话术，否则丢弃改写结果。
              本文件抽出公共函数 is_degraded() 供两处共用。

  坑 4  末端节点 ≠ 汇总节点
        图 n1(过滤) -> n2(判约束) -> n3(调提交工具) 里，
        末端 n3 的输出是 "Submitted: no valid postings ..."，
        这是**动作回执**，对用户毫无价值。
        真正有用的结论在 n2：p01 因 H3（专业不符）被排除。
        定论：末端节点若"看起来像回执"（以 Submitted/Called/已提交 开头），
              要向上回溯，取上游信息量最大的节点作为主答案。

另有一个"模型行为"坑，不是代码 bug 但会影响判分：

  坑 5  模型全程没调终止工具，只是用自然语言说了结论
        实测 8 次里出现过 1 次 submitted=None，任务实质做对了却判 0。
        τ-bench 的判分只看最终状态数据库，不看对话内容，
        所以"说对了"不算"提交了"。
        定论：harness 侧加机械兜底——从最终答案里正则抽取已注册的主键，
              抽不到就照旧判 0。兜底规则纯机械，不引入模型判断。

────────────────────────────────────────────────────────────────────────────────
六、一句话总结
────────────────────────────────────────────────────────────────────────────────

τ-bench 这类基准真正考的不是「模型会不会调工具」，而是三件事：
    ① 能不能从工具返回的错误里读出问题并换方案（恢复能力）
    ② 能不能顶住用户压力遵守写在 prompt 里的政策（政策服从）
    ③ 能不能在多步之后仍然清楚自己做到哪了（状态掌握）

这三件事都要求 scheduler 把「状态」当成一等公民——
而你现在最大的缺口，正是工具层没有任何共享状态。

================================================================================
"""

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
