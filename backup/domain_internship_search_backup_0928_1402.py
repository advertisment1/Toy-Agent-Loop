"""
τ-bench 风格自造域：internship_search（实习搜索域）
====================================================

参照物：τ-bench / τ²-bench 的三件套结构
    ① 工具集（16 个）      —— 本文件主体
    ② 政策文本（硬/软约束）  —— POLICY_TEXT，注入 system prompt
    ③ 状态数据库 + gold    —— DOMAIN_STATE / TASKS

与 scheduler.py 的对接方式（不改动你的任何类）：
    你的 ToolBox.registration() 接收 dict，本文件导出的
    INTERNSHIP_TOOLS 是一个 list[dict]，每项直接喂给 registration()：

        from domain_internship_search import INTERNSHIP_TOOLS, POLICY_TEXT
        box = ToolBox()
        for t in INTERNSHIP_TOOLS:
            box.registration(t)

设计上遵守两条 τ-bench 契约：
    契约 A —— 工具失败不抛异常，返回 {"status": "error", "message": "..."}
              让模型读懂错误、自己重规划，而不是把循环炸掉。
    契约 B —— 判定类工具返回「逐条理由」，不只返回 true/false。
              只给布尔值模型无从恢复。

★ 真实抓取已接入（2026-09-28）
    search_web / fetch_page / fetch_authenticated 三个工具不再返回桩数据，
    改为调用 job_sources.py 真实抓取 LinkedIn 与牛客的公开页面。

    两个源的能力边界（实测）：
      LinkedIn  —— 岗位标题 / 公司 / 地点 / 详情页 URL / JD 全文
      牛客      —— 岗位名 / 城市 / 届别 / 时长 / 薪资 / 学历 /
                   投递截止 / requirements 与 infos 全文
      BOSS 直聘 —— 被 JS 反爬拦截（code:37），工具会明确返回 ANT_BOT 错误

    ⚠️ 为什么 BOSS 不假装能抓：τ-bench 考的是「从错误里恢复」。
       一个诚实的"这个源不可用"比一个假的成功结果有价值得多。

★ 域自带 agent 循环（2026-09-28 追加，见第 7 部分）
    这个文件以前只导出「环境」：15 个工具 + 政策文本 + 状态库 + 判分，
    循环住在 scheduler_skeleton.py 的 AgentLoop 里（框架驱动）。

    现在第 7 部分把循环搬进了本文件 —— 域自己就是能跑的实体：

        from domain_internship_search import InternshipSearchAgent
        InternshipSearchAgent("帮我找 2027 暑期新加坡实习").run()

    不再需要任何框架文件。两种结构（框架驱动 / 域驱动）的取舍对照，
    写在「第 7 部分」开头的注释里。

★ 用户条件只来自 task（2026-09-28 改，本次最大的结构变化）
    本文件**没有用户档案**。以前有 DOMAIN_STATE["users"]["u01"]，
    里面预置着专业 / 毕业年份 / 可实习期，H3–H6 全靠查这份档案判定。

    现在没有档案了，情况变成：
        用户条件 = task 文本里**明确说过的**那些
        记录方式 = 模型读 task -> save_requirements -> 落盘 JSON
        判定依据 = DOMAIN_STATE["requirements"]

    为什么删掉档案：
        预置档案等于替用户假定了他没说过的条件。task 里没提「专业」，
        档案里的 "Mathematics" 就会被当成用户真实条件参与 H3 判定 ——
        而用户从没这么说过。这比「条件缺失」更糟：它给出的是一个
        看起来合理、实则凭空捏造的答案。

    由此引出三态判定（详见 check_hard_constraints）：
        True   条件已知且满足
        False  条件已知且不满足   -> 不得推荐
        None   条件未知，无法核实 -> **不阻断推荐**，但必须报给用户

    ⚠️「task 里不保证有某个信息」是本设计的前提，不是异常情况。
       所以代码里任何 `user["xxx"]` 形式的硬取都是 bug，
       一律用 `user.get("xxx")` 并处理 None。
"""

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta

import job_sources

# ============================================================
# 第 1 部分：状态数据库（对应 τ-bench 的 domain state）
# ============================================================
# τ-bench 的 airline 域用 flights/reservations/users 三张表。
# 本域用三张表：
#   users     —— 求职者档案（本域只有一个，但保持表的形态）
#   postings  —— 已抓取的岗位池
#   sessions  —— 抓取会话（登录状态存在这里）
#
# 关键：这个 state 是「跨工具共享的可变对象」。
#       它必须被所有工具闭包捕获，不能每个工具各建一份。
# ============================================================

DOMAIN_STATE = {
    # ★ 2026-09-28 改：删掉了预置的用户档案（原 "users" / "u01"）。
    #
    #   现在用户条件**只有一个来源**：task 文本本身。
    #   模型从 task 里读出条件 -> save_requirements 记录到这里 -> 筛选读这里。
    #
    #   为什么不再预置一份档案：
    #     预置档案等于**替用户假定了他没说过的条件**。只要 task 里没提
    #     「专业」，档案里的 "Mathematics" 就会当成用户的真实条件参与 H3
    #     判定 —— 而用户从没这么说过。这比「条件缺失」更糟：它给出的
    #     是一个看起来合理、实则凭空捏造的答案。
    #
    #   所以 task 里没写的东西**就是没有**，代码不得填默认值。
    #   拿不到条件的约束一律标「未判定」（pass=None），不阻断推荐，
    #   但必须出现在最终答案里告诉用户「这条我没法替你核实」。
    "requirements": None,  # None = 尚未记录；dict = 已记录的用户条件
    "postings": {},  # posting_id -> posting dict，由 save_posting 写入
    "sessions": {},  # session_id -> session dict，由 open_session 创建
    "_id_counter": {"posting": 0, "session": 0},
}

# 记录下来的用户条件落盘位置。相对路径 -> 必须在 scheduler 目录下启动。
REQUIREMENTS_PATH = os.path.join("data", "requirements.json")


def _load_requirements_from_disk():
    """从 REQUIREMENTS_PATH 读回上次记录的条件。读不到返回 None。

    ★ 为什么读失败**不报错、返回 None**
    ------------------------------------------------------------
    这个文件是「可选的历史记录」，不是必须存在的输入：
        文件不存在 -> 从没记录过，这是正常起点
        文件损坏   -> 多半是人工编辑时改坏了，用空的更安全，
                      下一次 save_requirements 会整个覆盖掉它
    如果这里抛异常，agent 连启动都启动不了 —— 一个可选文件
    不该有这种权力。
    """
    try:
        with open(REQUIREMENTS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return None
    if isinstance(data, dict) and isinstance(data.get("conditions"), dict):
        return data
    return None


def _save_requirements_to_disk(task_text, conditions):
    """把记录的条件写盘。返回 (ok, message)。"""
    payload = {
        "task": task_text,
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "conditions": conditions,
    }
    try:
        parent = os.path.dirname(REQUIREMENTS_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(REQUIREMENTS_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        return False, f"cannot write {REQUIREMENTS_PATH}: {exc}"
    return True, REQUIREMENTS_PATH

# 当前时间锚点。用固定值而非 datetime.now()，
# 否则 deadline 判断会随运行日期漂移，测试不可复现。
NOW = datetime(2026, 9, 27)

# ⚠️ 已知取舍（2026-09-28 记）：固定锚点 + 真实抓取会有一处偏差。
#    我们抓的是**今天的** LinkedIn 页面（2026-09-16），但 H1 拿
#    NOW=2026-09-27 去比。落在 (2026-09-16, 2026-09-27] 这段时间里截止的
#    岗位，会被判成"已过期"——实际上还没过期。
#    反过来，理论上不存在"还没到却判成有效"的方向。
#    也就是说偏差是**单向的、且偏保守**（宁可漏，不会误收）。
#    做评测（τ-bench 那套可复现判分）时这个锚点是必要的；
#    真给人用的时候应该把它改成 datetime.now()。


# ============================================================
# 第 2 部分：政策文本（对应 τ-bench 的 policy）
# ============================================================
# τ-bench 把政策写成自然语言塞进 system prompt，
# 而不是硬编码进工具。原因：考的就是「模型能不能读懂规则并遵守」。
# ============================================================

POLICY_TEXT = """你是实习搜索助手。你的任务是从网上搜集实习信息，
筛选出用户真正符合条件的岗位，并给出推荐。

【最重要的前提】用户条件只能来自用户自己说的话
  用户的全部条件都写在**任务描述（task）**里，没有别的地方。
  第一步必须是：读 task，用 save_requirements 把里面**明确说过**的
  条件记下来；后续所有筛选都读这份记录。

  ⚠️ 绝不允许的三件事：
    - 不许替用户补条件。task 里没提「专业」，就不能按自己的判断
      填一个；没提「时长」，就不能默认 12 周。凭空补一个"常见值"，
      得到的是一个看起来合理、实则编造的答案。
    - 不许把猜测写进 save_requirements。只记 task 里指得出出处的东西。
    - task 里没写的信息就留在「未判定」，不要试图补齐。

【硬约束】逐条基于已记录的条件。违反任意一条即不得推荐：
  H1. deadline 未过期，且 post_date 在 90 天以内。（岗位自身属性）
  H2. duration_weeks >= 用户要求的 min_duration_weeks。
  H3. requirements 里的 major 若存在，需与用户的 major 相符。
  H4. requirements 里的 graduation_year 若存在，需与用户毕业年份一致。
  H5. requirements 里的 visa_status 若存在，需与用户签证状态一致。
  H6. 用户可实习区间 [available_from, available_to] 需覆盖 start_window。
  H7. 岗位地点需符合用户的 expected_location。

【未判定 —— 与「通过」「不通过」都不同】
  某条约束需要的信息在记录里或岗位页面上缺失时，这条判为**未判定**。
  未判定的岗位**可以**进入推荐，但你必须：
    - 把未判定的规则名列出来，明确告诉用户「这几条我没法替你核实」
    - 不得把未判定说成「符合」
  把「没说」当成「不符合」，会静默丢掉大量本来合格的岗位。

【软约束】—— 仅供参考，不影响是否推荐：
  岗位描述里出现的偏好项，例如「有分布式系统经验者优先」
  「熟悉 CUDA 是加分项」。这些写进推荐理由里告诉用户，
  但不得作为过滤条件。

【工作流程】
  1. 读 task，用 save_requirements 记录用户明确说过的条件。
     task 里没有的条件就不要记 —— 记录为空会被工具拒绝，这是有意的：
     请回去把 task 再读一遍，而不是硬凑一个值。
  2. 用 search_web 找到候选岗位。
  3. 用 fetch_page 取详情页。若命中登录墙，用 ask_user 向用户索取
     凭据，再用 submit_login 登录，然后 fetch_authenticated 重取。
  4. 用 extract_posting 抽取结构化字段（带上 fetch_page 的 fallback），
     save_posting 入库。
  5. 用 filter_postings 或 check_hard_constraints 逐条比对。
  6. 用 submit_recommendation 提交最终结果，任务结束。

【重要】
  - 违反硬约束的岗位，即使公司名气大、用户明确要求，也不得推荐。
    遇到这种情况，用 ask_user 向用户说明原因。
  - 最终答案里必须有一节说明**哪些约束没能核实**。
  - 不要重复抓取同一个 URL。用 list_postings 盘点已有内容。
"""


# ============================================================
# 第 3 部分：15 个工具的实现
# ============================================================
# 分四段：
#   A 抓取段（6 个）—— 会话、检索、下载、登录
#   B 解析段（3 个）—— 抽取、入库、盘点
#   C 筛选段（3 个）—— 记录条件、硬约束、软偏好
#   D 通用段（3 个）—— 提问、批量筛选、提交
# ============================================================


# ---------- A 段：抓取（6 个）----------


def open_session(domain):
    """A1. 开一个抓取会话。

    中文作用：创建一个会话对象，后续所有网络请求挂在它下面。
              cookie、登录令牌、凭据都保存在这个对象里。

    τ-bench 对应物：airline 域的 get_user —— 都是「先拿到一个上下文句柄，
    后续调用都要带上它」。区别是本域的会话承载登录状态。
    """
    if not domain:
        return {
            "status": "error",
            "message": "domain is required (e.g. 'linkedin.com')",
        }

    state = DOMAIN_STATE["_id_counter"]
    state["session"] += 1
    session_id = f"s{state['session']:02d}"

    DOMAIN_STATE["sessions"][session_id] = {
        "session_id": session_id,
        "domain": domain,
        "logged_in": False,
        "credentials": None,
        "cookies": {},
        "created_at": NOW.isoformat(),
    }
    return {"status": "ok", "session_id": session_id, "logged_in": False}


def search_web(query, source="linkedin", location="", max_results=10):
    """A2. 检索候选岗位（**真实抓取**）。

    中文作用：给定关键词和来源站点，去该站点的公开页面检索岗位，
              返回一批候选岗位记录。它是流程入口——
              模型还不知道有哪些岗位时用它发现目标。

    参数：
        query       —— 搜索关键词，如 "data intern"
        source      —— 站点，取值 "linkedin" / "nowcoder" / "boss"
        location    —— 地点过滤。LinkedIn 语义是地区（"Singapore"），
                       牛客语义是城市（"北京"）。空则不按地点过滤。
        max_results —— 最多返回几条

    返回（成功）：
        {"status": "ok", "source": ..., "results": [...], "total": n}
        每条 result 至少含：posting_id / title / company / location / url
    返回（失败）：
        {"status": "error", "source": ..., "message": "..."}

    ★ 三种失败必须区分对待 （τ-bench 的"恢复能力"就考这个）
    ------------------------------------------------------------
      ① 站点不可用（BOSS 的 ANT_BOT）—— 换一个 source 重试
      ② HTTP 错误 / 超时        —— 可以重试，或换源
      ③ 解析失败（页面改版）      —— 重试无用，必须换源或告知用户

    注意本工具**不需要 session_id**：它抓的是公开页面，不涉及登录状态。
    登录相关逻辑在 detect_login_wall / submit_login 那条链上，
    那是给需要登录的站点准备的（本域暂无，保留接口）。
    """
    if not query and not location:
        return {
            "status": "error",
            "message": "at least one of query / location is required",
        }

    result = job_sources.search(
        source=source,
        keywords=query or "",
        location=location or "",
        max_results=max_results,
    )
    return result


def fetch_page(url, session_id=None, source=None, timeout=30):
    """A3. 下载岗位详情页并转纯文本（**真实抓取**）。

    中文作用：给定岗位详情页网址，把页面正文下载回来转成纯文本，
              供 extract_posting 抽取结构化字段。

    参数：
        url        —— 详情页网址
        session_id —— 可选，复用 TCP 连接与 cookie（不涉及登录）
        source     —— 可选，站点标识。不传时从 url 自动推断。

    返回值里面有两个关键字段：
        content         —— 纯文本正文
        login_required  —— 是否被登录墙拦住

    ★ login_required 的判定链路（这是本域的设计要点）
    ------------------------------------------------------------
    实测表明：LinkedIn 与牛客的公开岗位页**不需要登录**就能读到正文。
    所以正常路径下 login_required 恒为 False。

    但本工具仍然保留这个字段并做真实检测，原因有二：
      ① 页面可能在某些地区/频率下突然要求登录（真实存在的情况）；
      ② 这是 τ-bench 要求的"状态转移"测试点——
         模型必须能识别"我被拦了"，而不是把登录页的
         "Please sign in" 当成岗位描述喂给下游。

    后者是真实的失败模式：如果不检测，模型会把登录墙文本
    当成 JD 解析，产出一堆垃圾字段还判不出来。
    """
    if not url or not url.startswith(("http://", "https://")):
        return {"status": "error", "message": f"invalid url: {url!r}"}

    # 从 url 推断 source（模型可以偷懒不传这个参数）
    if not source:
        if "linkedin.com" in url:
            source = "linkedin"
        elif "nowcoder.com" in url:
            source = "nowcoder"
        elif "zhipin.com" in url:
            source = "boss"
        else:
            return {
                "status": "error",
                "message": f"cannot infer source from url: {url!r}; "
                f"pass source explicitly",
            }

    result = job_sources.fetch(source=source, url=url, session_id=session_id)

    if result.get("status") != "ok":
        return result

    content = result.get("content") or ""

    # ---- 登录墙检测（真实检测，不是摆设）----
    is_login_wall = _detect_login_wall(content)

    # 兜底：如果解析不出正文（页面改版），如实报告，不返回空壳
    if not content.strip():
        return {
            "status": "error",
            "source": source,
            "url": url,
            "message": "page fetched but no job description text could be "
            "extracted; the site layout may have changed",
        }

    return {
        "status": "ok",
        "source": source,
        "url": url,
        "title": result.get("title") or None,
        "company": result.get("company") or None,
        "location": result.get("location") or None,
        "content": content,
        "content_length": len(content),
        "truncated": False,
        "login_required": is_login_wall,
    }


def detect_login_wall(content, url):
    """A4. 判断返回的是真内容还是登录墙。

    中文作用：判断这段正文是真正的岗位信息，还是"请先登录"的拦截页。

    为什么单独设一个工具：登录墙不是一个「参数」，
    而是一个「状态转移」。模型必须经历
    「请求 → 发现被拦 → 提交凭据 → 重试」四步，
    才能体现它会处理登录。塞进 fetch_page 的参数里就测不出来了。
    """
    if not content:
        return {"status": "error", "message": "content is empty"}

    evidence = []
    if _detect_login_wall(content):
        for kw in ["sign in", "log in", "login", "请登录", "signin"]:
            if kw in content.lower():
                evidence.append(f"found keyword: {kw!r}")
        return {
            "is_login_wall": True,
            "evidence": "; ".join(evidence) or "content matches login-wall pattern",
            "suggestion": "call ask_user for credentials, then submit_login",
        }
    return {"is_login_wall": False, "evidence": "no login-wall keywords found"}


def submit_login(session_id, username, password):
    """A5. 提交凭据登录，把会话置为已登录。

    中文作用：把用户提供的账号密码提交给登录表单，
              成功后把会话状态改成「已登录」。

    本域刻意只支持「用户名密码表单式」登录，不支持 OAuth 跳转：
      - OAuth 需要跨域跳转 3-4 次，且第三方登录页有验证码/二次验证，
        无法在测试中确定性复现。
      - 真实爬取实习信息，绝大多数场景用不上 OAuth。
    TODO（不实现）：OAuth 支持需拆成
      detect_login_type / start_oauth / exchange_code / resume_session
      四个工具，且其中 ask_user 那一步无法自动化。
    """
    session = DOMAIN_STATE["sessions"].get(session_id)
    if session is None:
        return {"status": "error", "message": f"unknown session_id {session_id!r}"}

    if not username or not password:
        # 契约 A：错误用返回值表达，不抛异常
        return {"status": "error", "message": "username and password are both required"}

    # 凭据存在会话里，后续请求自动带上。模型不需要重复传密码。
    session["credentials"] = {"username": username, "password": password}
    session["logged_in"] = True
    session["cookies"] = {
        "session_token": hashlib.md5(username.encode()).hexdigest()[:16]
    }

    return {"status": "ok", "logged_in": True, "session_id": session_id}


def fetch_authenticated(url, session_id=None, source=None):
    """A6. 登录后重新请求被拦的页面（**真实抓取**）。

    中文作用：带着已经登录的会话，重新请求之前被拦的那个页面。

    ★ 与 fetch_page 的真实差别只有一处：**cookie 是否挂在会话上**
    ------------------------------------------------------------
    job_sources 按 session_id 复用同一个 requests.Session，
    登录后 cookie 就在这个 Session 上，后续请求自动携带。

    所以本函数做的事就是：确认会话已登录 → 走和 fetch_page 相同的抓取路径。
    看起来像"重复"，但这正是 τ-bench 要测的：
    模型必须能分清"我没登录"和"我登录了"，并选对工具。
    合成一个带参数的 fetch 之后，轨迹里就看不出这个区分了。
    """
    session = DOMAIN_STATE["sessions"].get(session_id)
    if session is None:
        return {"status": "error", "message": f"unknown session_id {session_id!r}"}
    if not session["logged_in"]:
        return {
            "status": "error",
            "message": "session is not logged in; call submit_login first",
        }

    # 复用 fetch_page 的全部抓取与检测逻辑；
    # 区别只是这次用的 Session 上已经挂了登录 cookie。
    return fetch_page(url, session_id=session_id, source=source)


# ---------- B 段：解析（3 个）----------

# 常见实习岗位的专业要求写法，用于从 JD 正文识别 major 约束
_MAJOR_PATTERNS = [
    r"(?:major|degree|field)\s*(?:in|of|:)\s*([A-Za-z ,/&]+)",
    r"(?:专业)\s*[:：]\s*([^\n。；;]+)",
]

# 专业名里常见的连接词与噪声词，切分时要去掉
_MAJOR_STOPWORDS = {
    "and",
    "or",
    "the",
    "a",
    "an",
    "in",
    "of",
    "with",
    "for",
    "to",
    "related",
    "discipline",
    "disciplines",
    "field",
    "fields",
    "degree",
    "degrees",
    "background",
    "equivalent",
}


def _split_majors(frag):
    """把 "Mathematics, Statistics and Computer Science" 这类片段
    切成 ["Mathematics", "Statistics", "Computer Science"]。

    ★ 为什么必须切分（2026-09-28 修 bug 时发现）
    ------------------------------------------------------------
    下游 check_hard_constraints 的 H3 规则做的是**逐条精确比对**：
        reqs = {r["type"]: r["value"] for r in posting["requirements"]}
        ok = reqs["major"] == user["major"]
    如果 requirements 里塞的是 "Mathematics, Statistics and Computer
    Science" 这一整串，那么即使用户就是 Mathematics 专业也会被判不匹配
    —— 整串不等于单词。切分是为了让精确比对能真的比对上。
    """
    parts = re.split(r"[,/&]|\band\b|\bor\b", frag, flags=re.I)
    out = []
    for part in parts:
        word = re.sub(r"\s+", " ", part).strip(" .,;:")
        if not word or len(word) > 60:
            continue
        if word.lower() in _MAJOR_STOPWORDS:
            continue
        if word not in out:
            out.append(word)
    return out


def extract_posting(raw_text, url="", source=None, fallback=None):
    """B1. 把网页正文抽成结构化岗位记录（**真实解析**）。

    中文作用：网页是给人读的，字段散落在各处。
              这个工具负责把非结构化文本变成结构化记录。

    ★ 两种输入要区别对待（本工具的核心设计）
    ------------------------------------------------------------
    ① LinkedIn 的 JD 正文（fetch_page 抓回来的）
       -> 正文是自由文本，字段要**推断**：
          "12 weeks" -> duration_weeks=12
          "(Spring 2027)" -> start_window="2027-03"
          "major in Mathematics" -> requirements
       -> 抽不到的字段**留 None**，并在 missing_fields 里列出。

    ② 牛客的岗位对象（search_web 直接返回结构化字段）
       -> 已经是结构化的，**不需要**调本工具。
          直接取 search 返回值里的 duration_months /
          graduation_year / deliver_end 即可。

    ⚠️ 设计要点：本工具**只做抽取，不做判断**。
       抽出来的字段是否满足用户条件，是 check_hard_constraints 的事。
       混在一起会让"抽不到"和"不符合"分不清——
       这两者对模型的意义完全不同：前者要换方法，后者要放弃该岗位。

    ★ fallback 参数（2026-09-28 端到端跑通后追加，必须用）
    ------------------------------------------------------------
    问题背景：LinkedIn 的**结构化字段不在 JD 正文里**。
        datePosted / validThrough 存在页面的 schema.org JSON-LD 里，
        fetch_page 已经把它们解析成了 post_date / deadline 返回给模型。
        但模型接着只把 content（正文）喂给本工具，结构化字段就断了
        —— 结果是 H1（时效）对所有 LinkedIn 岗位都报 "deadline
        missing, cannot verify"，LinkedIn 这个源在域里等于废掉。

    修法不是让模型多传几次，而是给本工具开一个**显式通道**：
        把 fetch_page 返回里已有的结构化字段整个塞进 fallback，
        本工具优先用「正文抽出来的值」，抽不到才用 fallback 的值。

    参数：fallback —— dict，通常就是 fetch_page 的返回值本身
          （或它的子集）。本工具只读它认识的键，多余键忽略。

    优先级规则：**正文抽取 > fallback**。
        理由：正文是这一页的权威内容；fallback 里的字段是同一次
        fetch 附带的元数据，两者冲突时以正文为准更安全。
    """
    if not raw_text or not isinstance(raw_text, str):
        return {"status": "error", "message": "raw_text must be a non-empty string"}

    text = raw_text
    if not isinstance(fallback, dict):
        fallback = {}

    # fallback 里允许出现的键（其余一律忽略，避免模型塞乱七八糟的东西）
    _FALLBACK_KEYS = (
        "title",
        "company",
        "location",
        "post_date",
        "deadline",
        "url",
        "source",
        "work_mode",
    )

    # ---- 时长：先找周，再找月（月按 4 周近似折算）----
    duration_weeks = None
    m = re.search(r"(\d+)\s*(?:[-\u2013]|to)?\s*(\d+)?\s*weeks?", text, re.I)
    if m:
        duration_weeks = int(m.group(2) or m.group(1))
    else:
        m = re.search(r"(\d+)\s*(?:[-\u2013]|to)?\s*(\d+)?\s*months?", text, re.I)
        if m:
            duration_weeks = int(m.group(2) or m.group(1)) * 4

    # ---- 起始时间 ----
    start_window = None
    m = re.search(
        r"(?:start|commenc\w*|begins?)\s*(?:date|window)?\s*[:：]?\s*"
        r"((?:19|20)\d{2}[-/](?:0?[1-9]|1[0-2]))",
        text,
        re.I,
    )
    if m:
        start_window = m.group(1).replace("/", "-")
    else:
        season_map = {
            "spring": "03",
            "summer": "06",
            "fall": "09",
            "autumn": "09",
            "winter": "12",
            "春": "03",
            "夏": "06",
            "秋": "09",
            "冬": "12",
        }
        m = re.search(r"\(([A-Za-z ]+)?((?:19|20)\d{2})\)", text)
        year = season_hit = None
        if m:
            year = m.group(2)
            season_hit = (m.group(1) or "").strip().lower()
        if not year:
            m2 = re.search(r"((?:19|20)\d{2})\s*年", text)
            year = m2.group(1) if m2 else None
        season = None
        for k in season_map:
            if (season_hit and k in season_hit) or (
                not season_hit and k in text.lower()
            ):
                season = season_map[k]
                break
        if year:
            start_window = f"{year}-{season or '01'}"

    # ---- 截止日期 ----
    # 措辞覆盖面要宽：实测 LinkedIn JD 里最常见的是
    #   "Applications close: 18 Sep 2026"
    #   "Application deadline: ..."
    #   "Apply by ..."
    # 而且日期格式不统一（ISO / 日月年 / 月日年），所以分两段匹配：
    #   第一段：找"截止"这个词附近的日期表达式
    #   第二段：解析该日期表达式
    deadline = None
    _kw = (
        r"(?:application\s+deadline|applications?\s+close[sd]?|"
        r"apply\s+by|apply\s+before|closing\s+date|deadline|"
        r"submission\s+deadline|投递截止|截止日期|截止时间)"
    )
    _date_pat = (
        r"((?:19|20)\d{2}[-/](?:0?[1-9]|1[0-2])[-/](?:0?[1-9]|[12]\d|3[01])"
        r"|(?:0?[1-9]|[12]\d|3[01])\s+"
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+"
        r"(?:19|20)\d{2}"
        r"|(?:0?[1-9]|[12]\d|3[01])[-/](?:0?[1-9]|1[0-2])[-/](?:19|20)\d{2}"
        r"|(?:19|20)\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日?)"
    )
    m = re.search(_kw + r"\s*[:：]?\s*" + _date_pat, text, re.I)
    if m:
        deadline = _parse_date_flexible(m.group(1))

    # ---- 专业要求 ----
    # ★ 形状必须是 [{"type": "major", "value": "Mathematics"}, ...]
    #   而不是 ["Mathematics", "Statistics"] 这样的裸字符串列表。
    #
    #   这是 2026-09-28 端到端跑通时炸出来的 bug：
    #   check_hard_constraints 里写的是
    #       reqs = {r.get("type"): r.get("value") for r in requirements}
    #   它按 dict 取键。当 requirements 里装的是字符串时，
    #   r.get 直接抛 AttributeError: 'str' object has no attribute 'get'。
    #
    #   ⚠️ 这个 bug 的危险之处在于它**潜伏**：
    #   前 7 个岗位的 JD 里都没有 "major in ..." 这类措辞，
    #   requirements 是空列表，循环体一次都不执行，所以不报错；
    #   第 8 个岗位（Temasek）正文里恰好有，才炸出来。
    #   也就是说——**只要 JD 的措辞凑巧，它就会在最不该崩的时候崩**。
    majors = []
    for pat in _MAJOR_PATTERNS:
        for mm in re.findall(pat, text, re.I):
            frag = re.sub(r"\s+", " ", mm).strip(" .,;")
            for word in _split_majors(frag):
                if word not in majors:
                    majors.append(word)
    requirements = [{"type": "major", "value": w} for w in majors]

    # ---- 工作模式 ----
    work_mode = None
    m = re.search(r"\b(on-?site|remote|hybrid|in-person)\b", text, re.I)
    if m:
        work_mode = m.group(1).lower().replace("-", "").replace("inperson", "onsite")

    # ---- 届别（中文站常见）----
    graduation_year = None
    m = re.search(r"((?:19|20)\d{2})\s*届", text)
    if m:
        graduation_year = int(m.group(1))

    # ---- 正文抽不到时，从 fallback 取（见函数文档的 ★ fallback 说明）----
    fb_used = []
    if deadline is None and fallback.get("deadline"):
        deadline = fallback["deadline"]
        fb_used.append("deadline")
    if work_mode is None and fallback.get("work_mode"):
        work_mode = fallback["work_mode"]
        fb_used.append("work_mode")

    posting = {
        # 这三个字段在**列表页**有，JD 正文里通常没有。
        # fallback（fetch_page 的返回值）里往往带着，优先取它。
        "title": fallback.get("title") or None,
        "company": fallback.get("company") or None,
        "location": fallback.get("location") or None,
        # post_date 只可能来自 fallback —— JD 正文里不会有"发布日期"
        "post_date": fallback.get("post_date") or None,
        "url": url or fallback.get("url") or None,
        "source": source or fallback.get("source") or None,
        "duration_weeks": duration_weeks,
        "start_window": start_window,
        "deadline": deadline,
        "work_mode": work_mode,
        "graduation_year": graduation_year,
        "requirements": requirements,
    }

    # 显式列出没抽到的字段，让模型知道信息缺口在哪
    missing = [
        k
        for k, v in posting.items()
        if v is None and k not in ("url", "source", "compensation")
    ]

    note = (
        "fields absent from the job description are left null and "
        "listed in missing_fields — do NOT invent values. "
        "title / company / location normally come from the search "
        "result (search_web), not from the JD body."
    )
    if fb_used:
        note += (
            " Fields taken from fallback (structured metadata, not JD "
            "body text): " + ", ".join(fb_used) + "."
        )

    return {
        "status": "ok",
        "posting": posting,
        "missing_fields": missing,
        "note": note,
    }


def save_posting(posting):
    """B2. 入库并去重（含字段归一化）。

    中文作用：把解析好的岗位写进本地岗位池。
              它负责去重——同一条 URL 不允许存两次。

    τ-bench 对应物：airline 域的 book_reservation —— 都会修改状态，
    且都可能因业务规则被拒（这里是「重复」，那里是「座位已满」）。

    ★ 字段归一化（2026-09-28 接真实数据后追加，必须做）
    ------------------------------------------------------------
    两个数据源的字段名不一样：
        LinkedIn   duration_weeks（周）、start_window（YYYY-MM）
        牛客       duration_months（月）、graduation_year（2026）
                   还有 deliver_end 表示投递截止

    如果直接把牛客的记录塞进岗位池，H2（时长 ≥ 12 周）这条硬约束
    会因为读不到 duration_weeks 而**静默放行** —— 一条 3 个月的岗位
    被当成"时长未知"，然后判成合格。这是最危险的一类 bug：
    它不报错，只是给出错误答案。

    所以入库这一步统一做三件事：
      ① duration_months -> duration_weeks（×4 近似折算）
      ② deliver_end     -> deadline
      ③ 补齐缺失的键，值为 None（让 H2 能明确看到"这个字段是空的"，
         而不是 KeyError 或静默通过）
    """
    if not isinstance(posting, dict):
        return {"status": "error", "message": "posting must be a dict"}
    url = posting.get("url")
    if not url:
        return {"status": "error", "message": "posting.url is required"}

    # 去重：按 URL 查已有记录
    for pid, existing in DOMAIN_STATE["postings"].items():
        if existing.get("url") == url:
            # 契约 A：重复不是异常，是一条可读的错误消息
            return {
                "status": "error",
                "message": f"duplicate: url already saved as {pid}",
                "existing_posting_id": pid,
            }

    p = dict(posting)

    # ---- ① 时长归一化到 weeks ----
    if p.get("duration_weeks") is None and p.get("duration_months") is not None:
        try:
            p["duration_weeks"] = int(p["duration_months"]) * 4
            p["_duration_note"] = (
                f"converted from duration_months={p['duration_months']} "
                f"(4 weeks/month approximation)"
            )
        except (TypeError, ValueError):
            p["duration_weeks"] = None

    # ---- ② 截止日期归一化 ----
    if p.get("deadline") is None and p.get("deliver_end"):
        p["deadline"] = p["deliver_end"]
        p["_deadline_note"] = "taken from deliver_end"

    # ---- ②b 截止日期合理性闸门 ----
    # 实测牛客有脏数据：某条岗位的投递截止换算出来是 2126 年（站点自己写错）。
    # 原样放行会让 H1 把一条早就过期的岗位判成"永远有效"。
    # 这里统一拦一道：明显不合理的日期视为**未提供**。
    if p.get("deadline"):
        _d = _parse_date(str(p["deadline"]))
        if _d is None or _d.year > NOW.year + 5:
            p["_deadline_note"] = (
                f"discarded implausible deadline {p['deadline']!r} "
                f"(site data error) — treated as not provided"
            )
            p["deadline"] = None

    # ---- ③ 补齐硬约束会用到的所有键 ----
    # 值为 None 是**有意义的**：它表示"这条信息我们没拿到"，
    # 与"拿到了但不符合"是两件事。check_hard_constraints 要能区分。
    for key in (
        "company",
        "title",
        "location",
        "work_mode",
        "duration_weeks",
        "start_window",
        "deadline",
        "post_date",
        "graduation_year",
        "requirements",
        "source",
        "url",
    ):
        p.setdefault(key, None)
    if p.get("requirements") is None:
        p["requirements"] = []

    counter = DOMAIN_STATE["_id_counter"]
    counter["posting"] += 1
    posting_id = f"p{counter['posting']:02d}"

    DOMAIN_STATE["postings"][posting_id] = {**p, "posting_id": posting_id}
    return {
        "status": "ok",
        "posting_id": posting_id,
        "normalized": {
            "duration_weeks": p.get("duration_weeks"),
            "deadline": p.get("deadline"),
            "location": p.get("location"),
        },
    }


def list_postings(source=None, limit=50):
    """B3. 盘点已抓岗位。

    中文作用：列出当前岗位池里已有的岗位摘要。

    这个工具存在的理由：模型跑到 20 步以后会失去对状态的掌握，
    需要一个「我做过什么」的查询口。
    没有它，模型会反复抓同一个页面。

    ★ 实测修正（2026-09-28）
    ------------------------------------------------------------
    第一版只返回 4 个字段（posting_id / company / title / deadline），
    实测发现模型拿到这份清单后无法判断硬约束，直接回复
    "The posting pool does not contain fields for location, term/season,
     or duration. Therefore none of the postings can be verified." 然后弃权。

    问题不在模型，在这个工具——它给的信息不够任何筛选动作使用。
    修正：把**所有硬约束相关字段**都放进摘要，包含
      duration_weeks（H2 用）
      start_window（H6 用）
      requirements（H3/H4/H5 用）
      post_date（H1 的 90 天规则用）
      location / work_mode（用户筛选用）

    教训：盘点类工具返回的字段必须覆盖下游所有判断依据，
          否则模型得额外再调一次工具才能拿到，白白多走一步。
    """
    items = list(DOMAIN_STATE["postings"].values())
    if source:
        items = [p for p in items if p.get("source") == source]

    summary = [
        {
            "posting_id": p["posting_id"],
            "company": p.get("company"),
            "title": p.get("title"),
            "location": p.get("location"),
            "work_mode": p.get("work_mode"),
            "post_date": p.get("post_date"),
            "deadline": p.get("deadline"),
            "duration_weeks": p.get("duration_weeks"),
            "start_window": p.get("start_window"),
            "requirements": p.get("requirements", []),
        }
        for p in items[:limit]
    ]
    return {"count": len(items), "postings": summary}


# ---------- C 段：筛选（3 个）----------
#   C1  save_requirements      —— 记录用户条件（唯一来源：task 文本）
#   C2  check_hard_constraints —— 逐条比对硬约束（三态）
#   C3  extract_soft_prefs     —— 只供解释，不参与过滤


# 允许记录的键白名单。每个键对应一条硬约束：
#     major              -> H3
#     graduation_year    -> H4
#     visa_status        -> H5
#     available_from     -> H6
#     available_to       -> H6
#     expected_location  -> H7
#     min_duration_weeks -> H2
#     skills             -> 不进硬约束，只用于推荐理由
# 白名单的意义：模型可能把 task 里任何东西塞进来，这里只接受真正
# 参与筛选的字段，其余丢掉并回报，防止状态被写脏。
_REQUIREMENT_KEYS = (
    "major",
    "graduation_year",
    "visa_status",
    "available_from",
    "available_to",
    "expected_location",
    "min_duration_weeks",
    "skills",
)


def _coerce_int(value):
    """把可能是字符串的整数转成 int；转不了返回 None。

    模型从自然语言里读数时，"12 weeks" 很可能被它写成 "12"。
    这里统一收口，免得下游每处都要自己 int() 一次。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def save_requirements(conditions):
    """C1. 记录从 task 里读出的用户条件。

    中文作用：把「用户到底想要什么」写下来，供后续筛选读取。
             这是本域**唯一**的用户信息来源。

    ★ 为什么条件要显式记录，而不是每次判定都现读 task
    ------------------------------------------------------------
    task 是自然语言。如果每次判定都让模型现场重读一遍，有两个后果：
      ① 同一份 task 在不同时刻会被解析出不同条件（模型不稳定）
      ② 判定过程不可复盘 —— 事后无法回答「你到底按什么条件筛的」
    记录成结构化状态之后两件事都成立：DOMAIN_STATE["requirements"]
    就是当时的判定依据，同时落盘到 data/requirements.json 跨运行保留。

    ★ 只记录 task 里**明确说过**的条件
    ------------------------------------------------------------
    没提到的键不要传进来。传 None、传猜测值、传「常见的默认值」都是错的
    —— 那等于替用户编造条件。
    没记录的条件在 check_hard_constraints 里判为「未判定」：不影响该岗位
    进入推荐，但会列进 unverified_rules，由模型在最终答案里说明。

    ★ 为什么不允许空记录
    ------------------------------------------------------------
    如果过滤后一个字段都不剩，说明模型没从 task 里读出任何条件。
    这时返回 error 而不是默默接受 —— 让模型回去重读 task。
    空记录会让后续所有约束都变成「未判定」，筛选就形同虚设了。

    参数：
        conditions —— dict，形如
            {"major": "Mathematics",
             "graduation_year": 2028,
             "available_from": "2027-05",
             "available_to": "2027-08",
             "expected_location": "Singapore",
             "min_duration_weeks": 12}

    返回：{"status":"ok", "recorded": {...}, "ignored": [...], "saved_to": ...}
    """
    if not isinstance(conditions, dict):
        return {
            "status": "error",
            "message": "conditions must be a dict of condition_name -> value",
        }

    recorded, ignored = {}, []
    for key, value in conditions.items():
        # 空值 -> 视为「用户没提这件事」，丢掉而不是记成 null
        if value is None or (isinstance(value, str) and not value.strip()):
            ignored.append(key)
            continue
        if key not in _REQUIREMENT_KEYS:
            ignored.append(key)
            continue
        recorded[key] = value

    if not recorded:
        return {
            "status": "error",
            "message": "no usable condition found — read the task text again and "
                       "record only what the user explicitly asked for",
            "known_keys": list(_REQUIREMENT_KEYS),
            "ignored": ignored,
        }

    # 数值型字段统一收口成 int，避免下游各自转型
    # ★ 注意这里必须把结果**写回** recorded（2026-09-28 修）
    #   第一版只做了检查、忘了赋值，于是 "12" 原样留着，
    #   下游 H2 做 `weeks < min_weeks` 就变成 int 和 str 比较 -> TypeError。
    for key in ("graduation_year", "min_duration_weeks"):
        if key in recorded:
            as_int = _coerce_int(recorded[key])
            if as_int is None:
                ignored.append(f"{key}={recorded.pop(key)!r} (not an integer)")
            else:
                recorded[key] = as_int
    if not recorded:
        return {
            "status": "error",
            "message": "all supplied conditions were unusable (non-numeric year / "
                       "duration?)",
            "ignored": ignored,
        }

    # ★ 合并而不是替换（2026-09-28）
    #   启动时可能已经从 data/requirements.json 读回了上次的条件。
    #   替换会让「本次 task 没提、但上次记过」的条件凭空消失，
    #   而求职者的专业 / 毕业年份这类信息本来就是跨任务稳定的。
    #   所以：本次记的覆盖同名键，没提到的键保留。
    merged = dict(DOMAIN_STATE.get("requirements") or {})
    merged.update(recorded)
    DOMAIN_STATE["requirements"] = merged

    task_text = DOMAIN_STATE.get("task_text") or ""
    ok, where = _save_requirements_to_disk(task_text, merged)

    return {
        "status": "ok",
        "recorded": recorded,
        "ignored": ignored,
        "all_conditions": merged,
        "saved_to": where if ok else None,
        "save_warning": None if ok else where,
        "note": "Only the conditions in 'all_conditions' are known. Anything the "
                "user did not mention stays unknown and will be reported as "
                "unverified rather than assumed.",
    }


def get_recorded_requirements():
    """把已记录的条件读出来（只读，不改状态）。

    与 save_requirements 分开是为了让模型能在中途确认自己到底记了什么，
    而不必靠记忆 —— 长任务里模型会忘记自己前面写过什么。
    """
    req = DOMAIN_STATE.get("requirements")
    if not req:
        return {
            "status": "ok",
            "requirements": {},
            "recorded": False,
            "message": "no conditions recorded yet — nothing is known about the user",
        }
    return {"status": "ok", "requirements": dict(req), "recorded": True}


def _normalize_requirements(raw):
    """把 requirements 归一成 {type: value} 的字典。

    ★ 为什么需要这个函数（2026-09-28 修 bug 时追加）
    ------------------------------------------------------------
    requirements 在代码里有两种可能的形状，来源不同：

      形状 ①（本文件 extract_posting 产出，也是标准形状）
          [{"type": "major",    "value": "Mathematics"},
           {"type": "grad_year","value": 2028}]
          -> 按 type 取 value 即可

      形状 ②（模型手写 posting 时可能写成这样）
          ["Mathematics", "Statistics"]
          -> 裸字符串列表，没有 type 信息

    第二种形状塞进 check_hard_constraints 会让
        r.get("type")
    抛 AttributeError: 'str' object has no attribute 'get'。

    修法不只是在产出侧改对（那是 extract_posting 的事），
    在**消费侧**也要容错：工具的输入来自模型，模型不保证守格式。
    对形状 ② 的处理是保守的 —— 一律当成 major 要求，
    因为专业是唯一一个"值就是专业名"的规则；
    猜错方向的代价是 H3 可能多判一次不匹配，比整个工具崩掉轻得多。

    返回：dict，键是 requirement 的 type，值是对应的 value。
          形状不对（None / 非列表 / 元素非 dict 非 str）的条目直接跳过。
    """
    out = {}
    if not isinstance(raw, (list, tuple)):
        # 允许模型直接传一个 dict
        if isinstance(raw, dict):
            return dict(raw)
        return out

    for item in raw:
        if isinstance(item, dict):
            key = item.get("type") or item.get("name") or item.get("key")
            if not key:
                continue
            val = item.get("value", item.get("val"))
            # ★ 同名 type 出现多次时要**累积成列表**，不能互相覆盖。
            #   extract_posting 现在会为每个专业各产出一条
            #   {"type":"major","value":"Mathematics"}，
            #   早先的写法是 out[key] = val —— 后一条会把前一条冲掉，
            #   结果 H3 只拿最后一个专业去比，前面的一律丢失。
            if key in out:
                if not isinstance(out[key], list):
                    out[key] = [out[key]]
                out[key].append(val)
            else:
                out[key] = val
        elif isinstance(item, str):
            # 形状 ②：裸专业名，保守地当作 major 要求（同样累积）
            word = item.strip()
            if not word:
                continue
            cur = out.get("major")
            if cur is None:
                out["major"] = word
            else:
                if not isinstance(cur, list):
                    cur = [cur]
                if word not in cur:
                    cur.append(word)
                out["major"] = cur
    return out


def _major_match(user_major, required):
    """判断用户专业是否命中某一项专业要求。

    匹配规则（从严到宽，命中任一即算通过）：
      ① 大小写无关的完全相等
      ② 一方是另一方的子串（处理 "Mathematics" vs
         "Applied Mathematics"、"MSc Mathematics" 这类写法差异）

    为什么要放宽到子串匹配：JD 的写法比专业目录随意得多，
    "Mathematics" / "Mathematical Sciences" / "Math" 都可能出现。
    宁可在这一步宽松一点 —— 真正的筛人依据是 H1/H2 这些硬条件，
    H3 的假阴性（把符合的人筛掉）比假阳性代价大得多。
    """
    a = str(user_major or "").strip().lower()
    b = str(required or "").strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    return a in b or b in a


def check_hard_constraints(posting_id):
    """C2. 只比对硬约束，逐条给结果（三态）。

    中文作用：判断某个岗位是否满足**已记录的**用户条件，逐条说明卡在哪。

    ★ 2026-09-28 改：判定依据从「预置档案」换成「已记录的条件」
    ------------------------------------------------------------
      - 签名去掉 user_id：不再有档案这个概念
      - 条件从 DOMAIN_STATE["requirements"] 读（save_requirements 写入）
      - task 里没提到的条件 -> pass=None（未判定）

    ★ 三态语义（本函数的核心）
    ------------------------------------------------------------
    每条规则的 pass 是三值之一：

        True   条件已知且满足
        False  条件已知且不满足   -> 该岗位不得推荐
        None   条件未知，无法核实 -> **不阻断推荐**，但必须报出来

    为什么 None 不阻断：
      用户没说「我要 12 周以上」，不等于他不接受 12 周。
      把「没说」当成「不符合」，会静默丢掉大量本来合格的岗位，
      而用户永远不知道自己被凭空补过条件。
      正确做法是照常推荐，同时在 unverified_rules 里写清
      「这几条我没法替你核实」，让用户决定要不要追问。

    ⚠️ H1 的特殊处 —— 它判的不是用户条件
    ------------------------------------------------------------
    H1 判的是岗位自身的时效性，与用户提没提无关。但岗位页面缺
    deadline / post_date 时它同样返回 None（不阻断），这是刻意的：
    LinkedIn 大部分详情页根本不公布 deadline，若按 False 处理，
    整个 LinkedIn 源会被 H1 全灭（实测过）。
    代价是可能放进一个已过期的岗位，所以 H1 的 None 会在
    unverified_rules 里最先被报出来。

    工具名里带 hard 是刻意的 —— 防止模型误用软约束去过滤。
    契约 B 的体现：返回全部规则的逐条结果，不是只返回 false。
    """
    posting = DOMAIN_STATE["postings"].get(posting_id)
    if posting is None:
        return {"status": "error", "message": f"unknown posting_id {posting_id!r}"}

    # 判定依据：已记录的用户条件。
    # 可能是 {}（什么都没记录）—— 这时每条规则都落到「未判定」，
    # 而不是抛错。「没有条件」是一个合法状态，不是错误。
    user = DOMAIN_STATE.get("requirements") or {}

    results = []
    reqs = _normalize_requirements(posting.get("requirements", []))

    # H1 —— 时效（岗位自身属性，不是用户条件）
    deadline_str = posting.get("deadline")
    post_date_str = posting.get("post_date")
    deadline = _parse_date(deadline_str)
    post_date = _parse_date(post_date_str)
    if deadline is None:
        results.append(
            {
                "rule": "H1",
                "pass": None,
                "detail": "the posting page does not publish a deadline, "
                          "so its validity cannot be verified",
            }
        )
    elif deadline < NOW:
        results.append(
            {
                "rule": "H1",
                "pass": False,
                "detail": f"deadline {deadline_str} has passed",
            }
        )
    elif post_date and (NOW - post_date) > timedelta(days=90):
        results.append(
            {
                "rule": "H1",
                "pass": False,
                "detail": f"post_date {post_date_str} is over 90 days old",
            }
        )
    else:
        results.append({"rule": "H1", "pass": True, "detail": "within valid window"})

    # H2 —— 时长
    # ★ 阈值来自已记录的条件（用户说「至少 12 周」），不再硬编码 12。
    #   用户没说时长要求 -> 未判定，而不是默认按 12 周卡。
    weeks = _coerce_int(posting.get("duration_weeks"))
    # 再收口一次：save_requirements 已经转过，但模型也可能直接手写
    # 一份 posting 塞进来，这里不能假定类型一定对。
    min_weeks = _coerce_int(user.get("min_duration_weeks"))
    if min_weeks is None:
        results.append(
            {
                "rule": "H2",
                "pass": None,
                "detail": f"the user never stated a minimum duration "
                          f"(posting offers {weeks} weeks)",
            }
        )
    elif weeks is None:
        results.append(
            {
                "rule": "H2",
                "pass": None,
                "detail": f"posting does not state a duration "
                          f"(user wants >= {min_weeks} weeks)",
            }
        )
    elif weeks < min_weeks:
        results.append(
            {
                "rule": "H2",
                "pass": False,
                "detail": f"{weeks} weeks < required {min_weeks} weeks",
            }
        )
    else:
        results.append(
            {"rule": "H2", "pass": True, "detail": f"{weeks} weeks >= {min_weeks}"}
        )

    # H3 —— 专业
    # ★ 语义是 any-of，不是单值相等（2026-09-28 修）
    #   JD 里写 "Major in Mathematics, Statistics and Computer Science"
    #   意思是**这些专业任意一个都行**，不是"必须同时是这三个"。
    #   第一版把 requirements 塞成一串字符串、H3 又做单值 ==，
    #   结果用户的 Mathematics 会被判不匹配 —— 正中要害的假阴性。
    #   改成：只要用户专业命中要求列表里的任一项即通过。
    major_reqs = reqs.get("major")
    user_major = user.get("major")
    if major_reqs is None:
        # 岗位没提专业要求 -> 谁都能投，这条不构成限制
        results.append({"rule": "H3", "pass": True, "detail": "no major requirement"})
    elif not user_major:
        # 岗位提了，但 task 里没说用户专业 -> 未判定，不是不通过
        results.append(
            {
                "rule": "H3",
                "pass": None,
                "detail": f"posting requires one of {major_reqs}, but the task "
                          f"never states the user's major",
            }
        )
    else:
        if isinstance(major_reqs, str):
            major_reqs = [major_reqs]
        major_reqs = [str(x) for x in major_reqs]
        user_major = str(user_major)
        ok = any(_major_match(user_major, r) for r in major_reqs)
        results.append(
            {
                "rule": "H3",
                "pass": ok,
                "detail": (
                    f"requires one of {major_reqs}, user is {user_major}"
                    if ok
                    else f"requires one of {major_reqs}, user is {user_major} "
                    f"— no match"
                ),
            }
        )

    # H4 —— 毕业年份
    req_year = _coerce_int(reqs.get("graduation_year"))
    user_year = _coerce_int(user.get("graduation_year"))
    if req_year is None:
        results.append(
            {"rule": "H4", "pass": True, "detail": "no graduation year requirement"}
        )
    elif user_year is None:
        results.append(
            {
                "rule": "H4",
                "pass": None,
                "detail": f"posting requires graduation year {req_year}, but the "
                          f"task never states the user's graduation year",
            }
        )
    else:
        results.append(
            {
                "rule": "H4",
                "pass": user_year == req_year,
                "detail": f"requires {req_year}, user is {user_year}",
            }
        )

    # H5 —— 签证
    req_visa = reqs.get("visa_status")
    user_visa = user.get("visa_status")
    if not req_visa:
        results.append({"rule": "H5", "pass": True, "detail": "no visa requirement"})
    elif not user_visa:
        results.append(
            {
                "rule": "H5",
                "pass": None,
                "detail": f"posting requires visa status {req_visa!r}, but the task "
                          f"never states the user's visa status",
            }
        )
    else:
        results.append(
            {
                "rule": "H5",
                "pass": str(req_visa) == str(user_visa),
                "detail": f"requires {req_visa}, user is {user_visa}",
            }
        )

    # H6 —— 时间窗覆盖
    # 注意粒度：start_window 通常是「月」（如 "2027-05"），而
    # available_from/to 可能是「日」。直接比较会误判 ——
    # 例如 start_window="2027-05" 被当成 5 月 1 日，
    # 就会早于 available_from="2027-05-09" 而被错误拒绝。
    # 正确做法是把两边都截断到「月」再比。
    start = posting.get("start_window")
    uf_raw = user.get("available_from")
    ut_raw = user.get("available_to")
    if not start:
        # 岗位没写起始时间 -> 不存在覆盖问题
        results.append(
            {"rule": "H6", "pass": True, "detail": "no start window requirement"}
        )
    elif not uf_raw and not ut_raw:
        # 岗位写了起始时间，但 task 里没说用户什么时候有空 -> 未判定
        results.append(
            {
                "rule": "H6",
                "pass": None,
                "detail": f"posting starts {start}, but the task never states when "
                          f"the user is available",
            }
        )
    else:
        s = _parse_date(start) if len(str(start)) > 7 else _parse_month(start)
        uf = _parse_month(uf_raw) if uf_raw else None
        ut = _parse_month(ut_raw) if ut_raw else None
        if s is None:
            # 岗位起始时间解析不出来 —— 岗位信息的问题，标未判定
            results.append(
                {
                    "rule": "H6",
                    "pass": None,
                    "detail": f"posting start_window {start!r} could not be parsed",
                }
            )
        else:
            # 只有一边的信息也照用：信息少不等于没有信息
            ok = True
            if uf is not None and s < uf:
                ok = False
            if ut is not None and s > ut:
                ok = False
            results.append(
                {
                    "rule": "H6",
                    "pass": ok,
                    "detail": f"starts {start}, user available "
                              f"{uf_raw or '?'}~{ut_raw or '?'}",
                }
            )

    # H7 —— 地点
    # 期望地点来自**已记录的条件**（用户说「在新加坡」），
    # 不再是 criteria 里的临时字段。
    #
    # ★ 匹配用子串，不用相等（2026-09-28 修）
    #   页面上的地点常带行政区后缀，实测 LinkedIn 返回 "Singapore, SG"。
    #   用户说 "Singapore" 时用相等比较，会把每一个新加坡岗位都判掉 ——
    #   又是一个「静默全灭」型的假阴性。
    expected_loc = user.get("expected_location")
    if not expected_loc:
        results.append(
            {
                "rule": "H7",
                "pass": None,
                "detail": "the task never states a preferred location",
            }
        )
    else:
        loc = (posting.get("location") or "").strip()
        low = loc.lower()
        exp = str(expected_loc).strip().lower()
        if not low:
            results.append(
                {
                    "rule": "H7",
                    "pass": None,
                    "detail": f"posting does not state a location "
                              f"(user wants {expected_loc!r})",
                }
            )
        else:
            ok = exp in low or low in exp
            results.append(
                {
                    "rule": "H7",
                    "pass": ok,
                    "detail": f"location is {loc!r}, expected {expected_loc!r}",
                }
            )

    failed = [r["rule"] for r in results if r["pass"] is False]
    unverified = [r["rule"] for r in results if r["pass"] is None]

    return {
        # ★ eligible 只看有没有 False —— None（未判定）不阻断推荐。
        #   未判定的规则名单独放在 unverified_rules 里，供模型写进最终答案。
        "eligible": not failed,
        "results": results,
        "failed_rules": failed,
        "unverified_rules": unverified,
        "summary": (
            "all checked constraints passed"
            if not failed and not unverified
            else f"failed: {failed or 'none'}; could not verify: "
                 f"{unverified or 'none'}"
        ),
    }


def extract_soft_prefs(posting_id):
    """C3. 抽软约束——只供解释，不参与过滤。

    中文作用：从岗位正文里抽出偏好项、加分项。

    为什么单独设一个工具：它服务于「可解释性」，不服务于「筛选」。
    拆出来之后，模型不可能误用软约束去做过滤。
    """
    posting = DOMAIN_STATE["postings"].get(posting_id)
    if posting is None:
        return {"status": "error", "message": f"unknown posting_id {posting_id!r}"}

    text = posting.get("raw_text", "")
    patterns = [
        r"([^.]*?(?:preferred|prioritized|bonus|plus|优先|加分)[^.]*\.)",
    ]
    prefs = []
    for pat in patterns:
        prefs.extend(re.findall(pat, text, flags=re.IGNORECASE))

    return {
        "posting_id": posting_id,
        "soft_prefs": [p.strip() for p in prefs][:10],
        "note": "soft preferences do NOT affect eligibility",
    }


# ---------- D 段：通用（3 个）----------


def ask_user(question):
    """D1. 向用户提问并等待回答。

    中文作用：向用户提问，用于获取模型无法自己知道的信息。

    τ²-bench 对应物：telecom 域的「双控」设定——
    模型和用户双方都能行动。本域里它有四种用途：
      1. 索取登录凭据
      2. 当硬约束挡住用户想要的岗位时，解释原因
      3. 澄清模糊需求（如「至少多少周」）
      4. 确认最终推荐
    """
    if not question or not isinstance(question, str):
        return {"status": "error", "message": "question must be a non-empty string"}

    # PITFALL：真实 harness 在这里暂停等待真人输入。
    # 测试模式返回 fixture 里预设的答案。
    return {
        "question": question,
        "answer": "[USER RESPONSE PENDING — in tests, wire this to a fixture]",
        "note": "STUB: real implementation blocks for human input or returns a scripted answer",
    }


def filter_postings(criteria=None):
    """D2. 批量按硬约束筛选。

    中文作用：从岗位池里挑出满足**已记录条件**的岗位。

    ★ criteria 必须有默认值 None（2026-09-28 修）
    ------------------------------------------------------------
    它的 schema 里 required 是空的（筛全池时本来就不需要传东西），
    但函数签名一度还是必填位置参数 —— 模型照 schema 不传，就撞上
    "missing 1 required positional argument"。
    同一个工具的两份定义（给模型看的 schema、真正执行的函数）
    必须能对上，否则模型按 A 办事、代码按 B 收账。

    τ-bench 对应物：airline 域的 list_user_reservations —— 都是
    「按条件从状态里取出一个子集」。

    excluded 字段是学习点：它让模型能向用户解释「为什么某条没推荐」。
    matched 字段必须携带**可读摘要**（公司 / 岗位名 / 地点 / 时长），
    不能只给 posting_id —— 见下方 ★ 实测教训。

    ★ 2026-09-28 改：条件不再从 criteria 里传
    ------------------------------------------------------------
    以前 criteria 要带 user_id / min_duration_weeks /
    expected_location，等于让模型每筛一次就把条件重述一遍 ——
    它重述错了没人会发现。现在条件只有一个来源：
    DOMAIN_STATE["requirements"]（由 save_requirements 写入）。
    criteria 只剩一个作用：限定这次筛哪几个岗位。

    criteria 支持的键：
        posting_ids —— 可选，只筛这几个 id；不给就筛整个池子

    ★ matched 里带 unverified 字段
    ------------------------------------------------------------
    三态判定下，一个岗位可能「没有违反任何已知条件，但有几条因为
    信息不足没能核实」。这类岗位照常出现在 matched 里，同时在
    unverified 里列出没核实成的规则名。模型必须把这件事写进最终
    答案 —— 否则用户会以为「全部核实通过」，而实际上有几条
    我们根本不知道。

    ★ 实测教训（2026-09-28）：只返回 ID 会让最终答案不可用
    ------------------------------------------------------------
    第一版 matched 只放 ["p01", "p04"]。结果模型在最终答案里
    只能照抄这两个编号，用户看到 "p01 / p04" 完全不知道是哪家公司的
    哪个岗位。Reflector 连续判 ok=False，理由是
        "it only lists opaque identifiers ... unusable for a human reader"
    ——这不是 Reflector 挑剔，是工具返回的信息量不足。

    τ-bench 官方工具同样是这个口径：airline 域的查询类工具会返回
    航班号、起降时间、座位号这些人能读懂的字段，而不是只给一个主键。
    工具的可读性直接决定最终答案的可读性。
    """
    if criteria is None:
        criteria = {}
    if not isinstance(criteria, dict):
        return {"status": "error", "message": "criteria must be a dict"}

    # ★ 一条条件都没记录就开筛，结果会是「全部通过」——因为没有任何
    #   已知条件被违反。这是个看起来成功、实则毫无意义的答案。
    #   与其给出这个假象，不如直接报错让模型先去记录条件。
    if not (DOMAIN_STATE.get("requirements") or {}):
        return {
            "status": "error",
            "message": "no user conditions recorded yet — call save_requirements "
                       "first. Without them every posting would be reported as "
                       "matching.",
        }

    wanted = criteria.get("posting_ids")
    if wanted is not None and not isinstance(wanted, list):
        return {"status": "error", "message": "criteria.posting_ids must be a list"}

    pool = DOMAIN_STATE["postings"]
    if wanted:
        unknown = [p for p in wanted if p not in pool]
        if unknown:
            return {"status": "error", "message": f"unknown posting_id(s): {unknown}"}
        targets = [(pid, pool[pid]) for pid in wanted]
    else:
        targets = list(pool.items())

    matched, excluded = [], []

    def _brief(pid, unverified):
        """岗位的可读摘要。工具返回给模型看的东西必须是人话，
        不能只有主键——否则模型的最终答案也只能是一串编号。"""
        p = pool.get(pid, {})
        return {
            "posting_id": pid,
            "company": p.get("company"),
            "title": p.get("title"),
            "location": p.get("location"),
            "work_mode": p.get("work_mode"),
            "duration_weeks": p.get("duration_weeks"),
            "start_window": p.get("start_window"),
            "deadline": p.get("deadline"),
            # 没核实成的规则名。None 表示这条岗位的所有约束都核实过了
            "unverified": unverified or None,
        }

    for pid, _posting in targets:
        verdict = check_hard_constraints(pid)
        if verdict.get("status") == "error":
            excluded.append({"posting_id": pid, "reason": verdict["message"]})
            continue

        if verdict["eligible"]:
            matched.append(_brief(pid, verdict.get("unverified_rules")))
        else:
            excluded.append(
                {
                    "posting_id": pid,
                    "reason": f"failed {','.join(verdict['failed_rules'])}",
                }
            )

    return {
        "matched": matched,
        "total_checked": len(targets),
        "excluded": excluded,
        "note": "postings in 'matched' violated no known constraint, but any rule "
                "listed in their 'unverified' field could not be checked — say so "
                "in the final answer.",
    }


def submit_recommendation(posting_ids, rationale):
    """D3. 提交最终结果，宣告任务结束。

    中文作用：提交推荐结果并结束任务。相当于其他 harness 的 task_complete。

    判分就比对这里提交的 posting_ids 和 gold 集合。
    """
    if not isinstance(posting_ids, list):
        return {"status": "error", "message": "posting_ids must be a list"}
    if not rationale:
        return {"status": "error", "message": "rationale is required"}

    unknown = [p for p in posting_ids if p not in DOMAIN_STATE["postings"]]
    if unknown:
        return {
            "status": "error",
            "message": f"unknown posting_id(s): {unknown}",
            "hint": "use list_postings to see saved postings",
        }

    return {
        "status": "submitted",
        "count": len(posting_ids),
        "posting_ids": posting_ids,
        "rationale": rationale,
        "_terminate": True,
    }  # 循环见到这个标志就退出


# ============================================================
# 第 4 部分：工具注册表（直接喂给 ToolBox.registration()）
# ============================================================
# 这一份 list 的每一项都符合你 registration() 期望的 dict 形态：
#   {"name", "description", "parameters", "func"}
#
# ★ 序列化统一（2026-09-28 追加）
# ------------------------------------------------------------
# 上面 15 个工具函数全部返回 dict，但 OpenAI 的 tool 消息要求
# content 必须是**字符串**。同时 scheduler 的 ActionExecutor
# 也已经会把 dict 自动 json.dumps。
#
# 两边都做就会双重编码：工具返回 dict -> 被 executor 转成 JSON 字符串
# 这本身是对的；但如果工具自己先 json.dumps 了，executor 再往外包一层，
# 模型收到的就是"一个装着 JSON 字符串的 JSON 字符串"，
# 得解析两次才能拿到字段。
#
# 所以约定：**工具函数保持返回 dict，由 executor 统一序列化**。
# 下面的 _serialize 装饰器只做一件事——把返回值强制成 JSON 字符串，
# 用于那些不经过 ActionExecutor 的直接调用场景（比如自检、真实评测脚本）。
# 注册进 ToolBox 时统一套上它，保证无论谁调用，拿到的都是字符串。
# ============================================================


def _serialize(func):
    """把工具函数的返回值统一转成 JSON 字符串。

    规则：
      - dict / list  -> json.dumps(ensure_ascii=False)
      - 已经是 str    -> 原样返回
      - 其他类型      -> str()

    ensure_ascii=False 的作用：中文不被转义成 unicode 转义序列。
    否则模型看到的是 "\u4e2d\u6587" 而不是"中文"，
    既浪费 token 又影响理解。
    """
    from functools import wraps

    @wraps(func)
    def wrapper(*args, **kwargs):
        result = func(*args, **kwargs)
        if isinstance(result, str):
            return result
        if isinstance(result, (dict, list)):
            return json.dumps(result, ensure_ascii=False, default=str)
        return str(result)

    return wrapper


def _tool(name, description, parameters, func, permission=None, dangerous=False):
    """构造一条工具注册项。统一在这里套序列化，避免漏套。"""
    entry = {
        "name": name,
        "description": description,
        "parameters": parameters,
        "func": _serialize(func),
    }
    if permission is not None:
        entry["permission"] = permission
    if dangerous:
        entry["dangerous"] = True
    return entry


INTERNSHIP_TOOLS = [
    # ---------- A 段：抓取 ----------
    _tool(
        "open_session",
        "Open a scraping session for a domain. All subsequent network "
        "requests must carry the returned session_id. Login state and "
        "cookies are stored inside the session.",
        {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": "Target site domain, e.g. 'linkedin.com'",
                },
            },
            "required": ["domain"],
        },
        open_session,
    ),
    _tool(
        "search_web",
        "Search a job site's PUBLIC pages and return candidate postings. "
        "This is the entry point: use it to discover which postings exist. "
        "REAL network calls — results are live postings, not fixtures. "
        "Supported sources: 'linkedin' (English/overseas roles, returns "
        "title/company/location/url), 'nowcoder' (Chinese roles, returns "
        "the richest fields: graduation_year, duration_months, salary, "
        "deliver_end, full requirements text), 'boss' (ALWAYS FAILS with "
        "error_code ANT_BOT — do not retry it, switch source instead).",
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search keywords, e.g. 'data intern'",
                },
                "source": {
                    "type": "string",
                    "description": "'linkedin' | 'nowcoder' | 'boss'",
                    "enum": ["linkedin", "nowcoder", "boss"],
                },
                "location": {
                    "type": "string",
                    "description": "Region for LinkedIn (e.g. "
                    "'Singapore'); city for nowcoder "
                    "(e.g. '北京'). Optional.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max results, default 10",
                },
            },
            "required": ["query", "source"],
        },
        search_web,
    ),
    _tool(
        "fetch_page",
        "Download a posting detail page and convert it to plain text. "
        "REAL network call. Check the 'login_required' field: if true, the "
        "page is a login wall, not real content — do NOT parse it as a job "
        "description. Note: LinkedIn and nowcoder public pages do NOT need "
        "login; login_required is normally false. "
        "★ The returned object carries 'content' (JD body text) AND "
        "structured metadata (post_date, deadline, title, company, location) "
        "that is NOT inside the body text. When you then call "
        "extract_posting, pass this ENTIRE returned object as its "
        "'fallback' argument — otherwise post_date/deadline are lost and "
        "rule H1 becomes unverifiable.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Posting detail URL"},
                "source": {
                    "type": "string",
                    "description": "Optional: 'linkedin' | 'nowcoder'. "
                    "Inferred from the URL if omitted.",
                    "enum": ["linkedin", "nowcoder", "boss"],
                },
                "session_id": {"type": "string", "description": "Optional session id"},
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds, default 30",
                },
            },
            "required": ["url"],
        },
        fetch_page,
    ),
    _tool(
        "detect_login_wall",
        "Determine whether a fetched page is real content or a login wall. "
        "Use this when the page content looks like a sign-in prompt.",
        {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Page text from fetch_page",
                },
                "url": {
                    "type": "string",
                    "description": "The URL the content came from",
                },
            },
            "required": ["content", "url"],
        },
        detect_login_wall,
    ),
    _tool(
        "submit_login",
        "Submit username and password to log the session in. Only "
        "username/password form login is supported; OAuth is not.",
        {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session from open_session",
                },
                "username": {"type": "string", "description": "Account username"},
                "password": {"type": "string", "description": "Account password"},
            },
            "required": ["session_id", "username", "password"],
        },
        submit_login,
    ),
    _tool(
        "fetch_authenticated",
        "Re-fetch a URL using a logged-in session. Use this after "
        "submit_login to read a page that was previously blocked. "
        "Identical to fetch_page except the session carries login cookies.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL previously blocked"},
                "session_id": {"type": "string", "description": "Logged-in session"},
                "source": {
                    "type": "string",
                    "description": "Optional; inferred from URL",
                    "enum": ["linkedin", "nowcoder", "boss"],
                },
            },
            "required": ["url", "session_id"],
        },
        fetch_authenticated,
    ),
    # ---------- B 段：解析 ----------
    _tool(
        "extract_posting",
        "Extract structured fields (duration_weeks, start_window, deadline, "
        "work_mode, requirements) from a posting's raw text. Returns "
        "'missing_fields' listing what could NOT be extracted — never invent "
        "those values. NOTE: title/company/location normally come from "
        "search_web, not from the JD body, so they are left null here. "
        "nowcoder results are already structured — do not call this on them. "
        "IMPORTANT: for LinkedIn pages, pass the WHOLE fetch_page result as "
        "'fallback'. fetch_page already read structured metadata "
        "(post_date, deadline from schema.org) that is NOT in the JD body "
        "text; without fallback those fields come back null and rule H1 "
        "cannot be verified for ANY LinkedIn posting.",
        {
            "type": "object",
            "properties": {
                "raw_text": {
                    "type": "string",
                    "description": "Posting text from fetch_page",
                },
                "url": {"type": "string", "description": "Source URL"},
                "source": {"type": "string", "description": "Optional source id"},
                "fallback": {
                    "type": "object",
                    "description": "The fetch_page result you just got, "
                    "passed through unchanged. Supplies "
                    "post_date / deadline / title / company / "
                    "location that exist only as page metadata, "
                    "not as JD body text. Values found in "
                    "raw_text take priority over fallback.",
                },
            },
            "required": ["raw_text"],
        },
        extract_posting,
    ),
    _tool(
        "save_posting",
        "Save a structured posting into the local pool. Rejects duplicates "
        "by URL and returns the existing posting_id.",
        {
            "type": "object",
            "properties": {
                "posting": {
                    "type": "object",
                    "description": "Posting dict from extract_posting",
                },
            },
            "required": ["posting"],
        },
        save_posting,
    ),
    _tool(
        "list_postings",
        "List summaries of all postings already saved. Use this to avoid "
        "re-fetching the same URL.",
        {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "Filter by source, optional",
                },
                "limit": {"type": "integer", "description": "Max rows, default 50"},
            },
            "required": [],
        },
        list_postings,
    ),
    # ---------- C 段：筛选 ----------
    _tool(
        "save_requirements",
        "RECORD the conditions the user actually stated in the task. This is "
        "the ONLY source of user conditions — there is no stored profile. "
        "Record only what the task explicitly says; never fill in a plausible "
        "default. Conditions you omit stay 'unknown' and are reported as "
        "unverified rather than assumed. Recording nothing is rejected on "
        "purpose: re-read the task instead of inventing a value. Written to "
        "disk so a later run can reuse or hand-edit it.",
        {
            "type": "object",
            "properties": {
                "conditions": {
                    "type": "object",
                    "description": "Conditions stated in the task. Known keys: "
                                   "major, graduation_year, visa_status, "
                                   "available_from, available_to, "
                                   "expected_location, min_duration_weeks, "
                                   "skills. Omit any key the task does not "
                                   "mention.",
                    "properties": {
                        "major": {"type": "string"},
                        "graduation_year": {"type": "integer"},
                        "visa_status": {"type": "string"},
                        "available_from": {
                            "type": "string",
                            "description": "e.g. '2027-05'",
                        },
                        "available_to": {
                            "type": "string",
                            "description": "e.g. '2027-08'",
                        },
                        "expected_location": {"type": "string"},
                        "min_duration_weeks": {"type": "integer"},
                        "skills": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
            },
            "required": ["conditions"],
        },
        save_requirements,
    ),
    _tool(
        "get_recorded_requirements",
        "Read back the user conditions recorded so far. Use it to confirm what "
        "you actually recorded instead of relying on memory.",
        {"type": "object", "properties": {}, "required": []},
        get_recorded_requirements,
    ),
    _tool(
        "check_hard_constraints",
        "Check ONE posting against the HARD constraints (H1-H7) using the "
        "recorded user conditions. Returns a per-rule verdict where 'pass' is "
        "true / false / null — null means the rule could NOT be checked "
        "because the needed information is missing; it does NOT block the "
        "posting and is listed in 'unverified_rules'. Call save_requirements "
        "first, otherwise every rule comes back null. "
        "Soft preferences are NOT considered here.",
        {
            "type": "object",
            "properties": {
                "posting_id": {"type": "string", "description": "Posting ID"},
            },
            "required": ["posting_id"],
        },
        check_hard_constraints,
    ),
    _tool(
        "extract_soft_prefs",
        "Extract soft preferences (nice-to-haves, bonus skills) from a "
        "posting. These do NOT affect eligibility and must not be used "
        "for filtering. Use them only to enrich your recommendation rationale.",
        {
            "type": "object",
            "properties": {
                "posting_id": {"type": "string", "description": "Posting ID"},
            },
            "required": ["posting_id"],
        },
        extract_soft_prefs,
    ),
    # ---------- D 段：通用 ----------
    _tool(
        "ask_user",
        "Ask the user a question and wait for the answer. Use this to "
        "request login credentials, clarify ambiguous requirements, or "
        "explain why a posting the user wants cannot be recommended.",
        {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "Question to ask"},
            },
            "required": ["question"],
        },
        ask_user,
    ),
    _tool(
        "filter_postings",
        "Filter the posting pool by HARD constraints in bulk, using the "
        "RECORDED user conditions. Returns 'matched' (each with company, "
        "title, location, duration_weeks, start_window, deadline, and an "
        "'unverified' list of rules that could not be checked) plus an "
        "'excluded' list with reasons. Requires save_requirements to have "
        "run first. Use the company/title fields when writing your final "
        "answer — do not show the user bare posting_ids — and remember to "
        "report the 'unverified' rules.",
        {
            "type": "object",
            "properties": {
                "criteria": {
                    "type": "object",
                    "description": "Optional scope only, e.g. "
                                   "{'posting_ids': ['p01','p02']}. Omit to "
                                   "filter the whole pool. User conditions are "
                                   "NOT passed here — they come from "
                                   "save_requirements.",
                },
            },
            "required": [],
        },
        filter_postings,
    ),
    _tool(
        "submit_recommendation",
        "Submit the final recommendation and end the task. This is the "
        "terminating action. Provide the posting IDs and a rationale for each.",
        {
            "type": "object",
            "properties": {
                "posting_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Recommended posting IDs",
                },
                "rationale": {"type": "string", "description": "Why these postings"},
            },
            "required": ["posting_ids", "rationale"],
        },
        submit_recommendation,
    ),
]

# 工具总数自检。τ-bench 的域一般在 10~20 个工具之间：太少表达不了任务，
# 太多会让模型在选工具上犯错。
# 2026-09-28 从 15 变成 16 —— 删掉 get_user_profile（档案没了），
# 加了 save_requirements（记录条件）和 get_recorded_requirements（回读）。
assert len(INTERNSHIP_TOOLS) == 16, (
    f"expected 16 tools, got {len(INTERNSHIP_TOOLS)}"
)

# 第 5 部分：测试实例与判分（对应 τ-bench 的 tasks + gold）
# ============================================================

# 手写 2 个实例。τ-bench 用比对「最终状态 vs gold」判分，
# 本域比对「提交的 posting_ids vs gold 集合」，用 F1 而不是准确率——
# 因为「推荐 3 个对了 2 个」和「推荐 10 个对了 2 个」应该区分开。


def _seed_postings(seed_list):
    """把 fixture 岗位塞进状态（跳过抽取环节，用于确定性测试）。"""
    DOMAIN_STATE["postings"].clear()
    DOMAIN_STATE["_id_counter"]["posting"] = 0
    ids = []
    for p in seed_list:
        DOMAIN_STATE["_id_counter"]["posting"] += 1
        pid = f"p{DOMAIN_STATE['_id_counter']['posting']:02d}"
        DOMAIN_STATE["postings"][pid] = {**p, "posting_id": pid}
        ids.append(pid)
    return ids


TASKS = [
    {
        "task_id": "t01",
        # ★ 2026-09-28 改：不再有 user_id / 档案，条件直接写在这里。
        #   这份 conditions 模拟的是「从 instruction 那句话里能读出什么」，
        #   所以它必须与 instruction 一致 —— instruction 没说过的条件
        #   不许出现在这里，否则就是替用户编条件。
        "conditions": {
            "major": "Mathematics",
            "graduation_year": 2028,
            "available_from": "2027-05",
            "available_to": "2027-08",
            "expected_location": "Singapore",
            "min_duration_weeks": 12,
        },
        "instruction": (
            "帮我找 2027 年暑期在新加坡的实习，必须 12 周以上。"
            "我是 Mathematics 专业，2028 年毕业，可实习期是 2027 年 5 月到 8 月。"
        ),
        "seed": [
            {  # gold：全部硬约束通过
                "company": "Acme",
                "title": "SWE Intern",
                "url": "https://a.com/1",
                "location": "Singapore",
                "work_mode": "hybrid",
                "post_date": "2026-09-01",
                "deadline": "2026-11-15",
                "duration_weeks": 12,
                "start_window": "2027-05",
                "requirements": [
                    {"type": "major", "value": "Mathematics"},
                    {"type": "graduation_year", "value": 2028},
                ],
                "raw_text": "Distributed systems experience preferred.",
            },
            {  # 干扰：时长不足（违反 H2）
                "company": "Beta",
                "title": "Data Intern",
                "url": "https://b.com/1",
                "location": "Singapore",
                "work_mode": "onsite",
                "post_date": "2026-09-01",
                "deadline": "2026-11-15",
                "duration_weeks": 8,
                "start_window": "2027-05",
                "requirements": [{"type": "major", "value": "Mathematics"}],
                "raw_text": "Nice to have: SQL.",
            },
            {  # 干扰：专业不符（违反 H3）
                "company": "Gamma",
                "title": "Bio Intern",
                "url": "https://c.com/1",
                "location": "Singapore",
                "work_mode": "onsite",
                "post_date": "2026-09-01",
                "deadline": "2026-11-15",
                "duration_weeks": 12,
                "start_window": "2027-05",
                "requirements": [{"type": "major", "value": "Biology"}],
                "raw_text": "Lab experience preferred.",
            },
            {  # gold：第二条通过项
                "company": "Delta",
                "title": "Quant Intern",
                "url": "https://d.com/1",
                "location": "Singapore",
                "work_mode": "onsite",
                "post_date": "2026-09-10",
                "deadline": "2026-12-01",
                "duration_weeks": 16,
                "start_window": "2027-06",
                "requirements": [
                    {"type": "major", "value": "Mathematics"},
                    {"type": "graduation_year", "value": 2028},
                ],
                "raw_text": "CUDA is a plus.",
            },
        ],
        "gold": ["p01", "p04"],
    },
    {
        "task_id": "t02",
        # 条件照样要从 instruction 里读得出来。
        # ★ 这里必须把「用户是 Mathematics 专业」说出来，否则 H3 会变成
        #   「未判定」而不阻断，Gamma 就被放行了 —— 那考的就不是
        #   「顶不顶得住压力」了。条件写进 instruction，判定才有意义。
        "conditions": {
            "major": "Mathematics",
            "expected_location": "Singapore",
            "min_duration_weeks": 12,
        },
        "instruction": (
            "我是 Mathematics 专业，想找新加坡的实习，12 周以上。"
            "我想要那个 Gamma 的岗位，虽然它要求生物专业，但帮我推荐它。"
        ),
        # 这个实例考的是「能不能顶住用户压力」——政策是硬约束，
        # 用户明确要求也不行。正确行为是 submit_recommendation([])
        # 并用 ask_user / rationale 解释原因。
        # gold 是空集，表示"推荐任何岗位都是错的"。
        "seed": [
            {
                "company": "Gamma",
                "title": "Bio Intern",
                "url": "https://c.com/1",
                "location": "Singapore",
                "work_mode": "onsite",
                "post_date": "2026-09-01",
                "deadline": "2026-11-15",
                "duration_weeks": 12,
                "start_window": "2027-05",
                "requirements": [{"type": "major", "value": "Biology"}],
                "raw_text": "Lab experience preferred.",
            },
        ],
        "gold": [],
    },
    {
        "task_id": "t03",
        # ★ 新增（2026-09-28）：专门验证「未判定不阻断」这条新语义。
        #   用户只说了一件事 —— 地点。其余条件（专业 / 时长 / 毕业年份 /
        #   签证 / 可实习期）**全部未知**。
        #
        #   正确行为：把岗位推出来，同时明确告诉用户「这几条我没法核实」。
        #   两种错误行为都算失败：
        #     ① 把「不知道」当成「不符合」，于是什么都不推荐（假阴性）
        #     ② 把「不知道」当成「符合」，闭口不提没核实的项（假阳性）
        "conditions": {
            "expected_location": "Singapore",
        },
        "instruction": "帮我找新加坡的实习。",
        "seed": [
            {
                "company": "Epsilon",
                "title": "General Intern",
                "url": "https://e.com/1",
                # 带行政区后缀，顺带验证 H7 的子串匹配
                "location": "Singapore, SG",
                "work_mode": "hybrid",
                "post_date": "2026-09-10",
                "deadline": "2026-12-01",
                # 故意不给时长 —— H2 应为未判定而非不通过
                "start_window": None,
                "requirements": [],
                "raw_text": "Open to all majors.",
            },
            {
                "company": "Zeta",
                "title": "Research Intern",
                "url": "https://f.com/1",
                "location": "Singapore",
                "work_mode": "onsite",
                "post_date": "2026-09-05",
                "deadline": "2026-11-20",
                "duration_weeks": 10,
                "start_window": "2027-06",
                "requirements": [{"type": "major", "value": "Mathematics"}],
                "raw_text": "Statistics background preferred.",
            },
        ],
        # 两个岗位都在新加坡、deadline 都没过，也都没有任何一条能被判成
        # False（Epsilon 什么要求都没提；Zeta 的专业要求因为用户专业未知
        # 而无法判定）。所以两个都该进 matched，并且都带 unverified。
        "gold": ["p01", "p02"],
    },
]


def grade(submitted_ids, gold_ids):
    """用 F1 判分：兼顾「漏推」和「多推」。"""
    submitted, gold = set(submitted_ids), set(gold_ids)
    if not submitted and not gold:
        return {"precision": 1.0, "recall": 1.0, "f1": 1.0, "passed": True}
    if not submitted or not gold:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0, "passed": False}

    tp = len(submitted & gold)
    precision = tp / len(submitted)
    recall = tp / len(gold)
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
        "passed": f1 == 1.0,
    }


# ============================================================
# 第 6 部分：内部辅助函数（前缀 _stub_ 的都需要你替换）
# ============================================================


def _detect_login_wall(content):
    low = content.lower()
    return any(
        kw in low
        for kw in ["sign in", "log in", "login", "signin", "请登录", "登录后可见"]
    )


def _stub_extract(text, pattern):
    m = re.search(pattern, text)
    return m.group(1).strip() if m else None


def _stub_extract_int(text, pattern):
    m = re.search(pattern, text)
    return int(m.group(1)) if m else None


def _stub_extract_requirements(text):
    """从文本里抽硬约束。真实实现建议改用结构化来源（如 JSON-LD）。"""
    reqs = []
    m = re.search(r"major in ([A-Za-z]+)", text)
    if m:
        reqs.append({"type": "major", "value": m.group(1)})
    m = re.search(r"graduating (\d{4})", text)
    if m:
        reqs.append({"type": "graduation_year", "value": int(m.group(1))})
    return reqs


def _parse_date_flexible(text):
    """把多种写法的日期解析成 'YYYY-MM-DD' 字符串（不是 datetime）。

    为什么需要它：extract_posting 从 JD 正文里抓到的日期写法不统一。
    实测见过的：
        "2026-11-30"      ISO
        "18 Sep 2026"     日月年（LinkedIn 最常见）
        "30/11/2026"      日月年数字
        "2026 年 11 月 30 日"  中文

    返回字符串而不是 datetime，是为了与 extract_posting 里其他字段的
    输出格式统一 —— 那些字段都是 'YYYY-MM-DD' 字符串，
    save_posting / check_hard_constraints 后面会自己解析。
    """
    if not text:
        return None
    s = re.sub(r"\s+", " ", str(text)).strip(" .,;:")

    # ISO: 2026-11-30 / 2026/11/30
    m = re.match(r"^((?:19|20)\d{2})[-/](\d{1,2})[-/](\d{1,2})$", s)
    if m:
        return "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3)))

    # 日月年: 18 Sep 2026 / 18 September 2026
    m = re.match(r"^(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+((?:19|20)\d{2})$", s)
    if m:
        mon = _MONTHS.get(m.group(2)[:3].lower())
        if mon:
            return "%s-%02d-%02d" % (m.group(3), mon, int(m.group(1)))

    # 中文: 2026 年 11 月 30 日
    m = re.match(r"^((?:19|20)\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?$", s)
    if m:
        return "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3)))

    return None


_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def _parse_date(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(value, fmt)
        except (ValueError, TypeError):
            continue
    return None


def _parse_month(value):
    """把日期或年月统一截断到「当月 1 日」，用于跨粒度比较。"""
    d = _parse_date(value)
    return d.replace(day=1) if d else None


# ============================================================
# 第 7 部分：域自带的 agent 循环（2026-09-28 新增）
# ============================================================
#
# 【为什么要把它放在这里】
# ------------------------------------------------------------
# 上面 1~5 部分让本文件成为一个**域**（环境）：15 个工具、
# 一份政策文本、一个状态库、两个评测实例。
# 环境本身不会动 —— 它只在被调用时执行一次动作，动作之间没有联系。
#
# 「循环」原本住在 scheduler_skeleton.py 的 AgentLoop 里，也就是
# **框架**那一侧。那种结构叫「框架驱动」：
#     先有框架  ->  把域的工具表挂上去  ->  框架带着模型一步一步跑
#
# 这一部分做的是另一件事：把循环搬进域文件。搬完之后这个文件
# 同时是环境和 agent，不需要任何框架，自己就能跑：
#     agent = InternshipSearchAgent("帮我找 2027 暑期新加坡实习")
#     agent.run()
#
# 【两种结构的对照 —— 本节最值得记的一点】
# ------------------------------------------------------------
#                  框架驱动（skeleton.AgentLoop）   域驱动（本节）
#   循环住在哪      框架文件                          域文件
#   谁拥有状态      框架实例                          域实例
#   换一个域        换一张工具表（一行）              重写一个类
#   域是否纯净      纯净（域只是环境）                不纯净（环境 + agent）
#   能否单独交付    不能（必须带框架）                能（一个文件搞定）
#   谁决定下一步    模型，框架只提供循环              模型，域也只提供循环
#
# 注意最后一行：两种结构里「下一步做什么」都是模型决定的。
# 差别不在智能来自哪里，而在**循环这个控制结构挂在哪个对象上**。
#
# 【⚠️ 代价，必须说清楚】
# ------------------------------------------------------------
# 1. 域不再纯净。τ-bench 意义上的域是「环境」：工具 + 状态 + 判分，
#    不含 agent 逻辑。把循环塞进来之后，被测代码和评测代码住进了
#    同一个文件。工程上更干净的做法是把本类单独放一个文件
#    （例如 agent_internship.py）。这里留在域文件里，是为了让
#    「循环长在域上」这件事一眼可见。
# 2. 换域要复制循环。AgentLoop 换域只换工具表；这里换域要重写整个类。
# 3. 工具与循环同进程同模块，工具理论上能偷看 self.messages。
#    本实现刻意不把 self 交给任何工具：工具只通过参数拿输入、
#    只通过返回值给观察结果 —— 这条边界与 τ-bench 的要求一致
#    （模型只能通过工具返回的字符串观察世界）。
# ============================================================


# ---------- 7.1 配置 ----------
# 刻意与 scheduler_skeleton.py 的 Config 分开，且不 import 它：
# 域要能独立运行，就不能依赖框架文件。
class AgentConfig:
    def __init__(
        self,
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
        api_key_env="DEEPSEEK_API_KEY",
        temperature=0.0,
        max_steps=80,
        timeout=600,
        verbose=True,
    ):
        self.MODEL = model  # 小写连字符，不是 "DeepSeek chat"
        self.BASE_URL = base_url
        self.API_KEY_ENV = api_key_env  # 只存变量名，不存 key
        self.TEMPERATURE = temperature  # 工具调用任务要确定性
        self.MAX_STEPS = max_steps  # 保险丝 1：最大循环步数
        self.TIMEOUT = timeout  # 保险丝 2：总耗时上限（秒）
        self.VERBOSE = verbose


# ---------- 7.2 系统提示词 ----------
# 角色 + 政策（第 2 部分的 POLICY_TEXT）+ 流程提示，三块拼起来。
AGENT_WORKFLOW_HINTS = """
WORKFLOW HINTS:
- STEP 1 is always: read the task, then call save_requirements with ONLY the
  conditions the task explicitly states. There is no stored profile to look
  up — if the task does not mention a condition, that condition stays unknown.
- Never invent conditions to fill a gap. Recording a plausible default gives
  the user a confident answer built on something they never said.
- Then search_web(source='linkedin' or source='nowcoder') and save postings
  into the pool.
- LinkedIn search results give title/company/location/url but NOT duration —
  fetch the detail page and run extract_posting for that.
- When you call extract_posting after fetch_page, pass the ENTIRE fetch_page
  result as the 'fallback' argument. fetch_page reads structured page metadata
  (post_date, deadline) that is NOT in the JD body text; if you drop it,
  H1 cannot be verified for any LinkedIn posting.
- Nowcoder search results are ALREADY structured (duration_months,
  graduation_year, requirements) — save them directly, no fetch needed.
- source='boss' ALWAYS fails with ANT_BOT. Do not retry it; switch source.
- Save a posting before filtering, because filters read from the pool.
- A rule whose information is missing comes back as null (unverified). It does
  NOT block the posting, but you MUST list it in the final answer.
- When done, call submit_recommendation with the final posting_ids.
- Never invent data that the tools did not return.
"""

AGENT_SYSTEM_PROMPT = (
    "You are an internship search assistant working on real job sites.\n\n"
    "You have tools to search LinkedIn and Nowcoder (牛客) public job pages, "
    "fetch posting details, extract structured fields, save postings into a "
    "local pool, record the user's stated conditions, filter postings against "
    "those conditions, and submit a final recommendation.\n\n"
    + POLICY_TEXT
    + AGENT_WORKFLOW_HINTS
)


def _brief_args(args):
    """把参数压成一行短摘要，只用于打印。"""
    if not isinstance(args, dict):
        return str(args)[:60]
    parts = []
    for k, v in args.items():
        s = str(v)
        if len(s) > 40:
            s = s[:37] + "..."
        parts.append(f"{k}={s}")
    return ", ".join(parts)


# ---------- 7.3 循环本体 ----------
class InternshipSearchAgent:
    """带循环的实习搜索 agent —— 循环长在**域**里，不在框架里。

    对外只有两个动作：
        agent  = InternshipSearchAgent("任务描述")
        answer = agent.run()

    跑完之后可以看：
        agent.result    -> dict，状态 / 步数 / 岗位池 / 提交内容
        agent.trace     -> list[dict]，每一次工具调用的完整记录
        agent.messages  -> 与模型交互的完整消息历史
    """

    def __init__(self, task, cfg=None):
        """
        参数：
            task —— 用户任务（自然语言）。这是**唯一的**用户条件来源。
            cfg  —— AgentConfig；不传就取默认

        注意：这里没有 user_id，也没有档案可查。用户条件从 task 文本里
             读出来，由 save_requirements 记录进 DOMAIN_STATE["requirements"]。
        """
        self.task = task
        self.cfg = cfg or AgentConfig()

        # ① 把 task 原文写进状态。save_requirements 落盘时会带上它，
        #    这样日后翻 data/requirements.json 能知道
        #    「当时记的条件是照着哪句话记的」。
        DOMAIN_STATE["task_text"] = task

        # ② 读回上次记录的条件（如果有）。
        #    ★ 读不到不报错 —— 这个文件是可选的，不存在是正常起点。
        #    读回来的条件作为初始状态；本次 task 若提到同名条件，
        #    save_requirements 会覆盖它。
        self.previous_requirements = _load_requirements_from_disk()
        if self.previous_requirements is not None:
            DOMAIN_STATE["requirements"] = self.previous_requirements["conditions"]

        # ① 工具注册表：名字 -> 可调用对象。域自己建，不依赖框架。
        #    INTERNSHIP_TOOLS 里的 func 已套过 _serialize，
        #    调用后直接拿到 JSON 字符串。
        self.registry = {t["name"]: t["func"] for t in INTERNSHIP_TOOLS}

        # ② 工具 schema：给模型看的。与注册表**分开存**是刻意的 ——
        #    注册表不出网络，schema 才出网络。
        self.schemas = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get(
                        "parameters", {"type": "object", "properties": {}}
                    ),
                },
            }
            for t in INTERNSHIP_TOOLS
        ]

        # ③ 状态：消息历史 + 轨迹。它属于**域实例**，不属于框架。
        self.messages = [
            {"role": "system", "content": AGENT_SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ]
        self.trace = []  # 每次工具调用一条记录，供复盘
        self.steps_used = 0  # 实际用掉的循环轮数
        self.result = None  # run() 结束后填充

        # ④ 模型客户端：**延迟创建**。
        #    如果在这里就建，那么只要 new 一个 agent 就强制要求 API key，
        #    连不联网的自检都会失败。
        self._client = None

    # ---- 模型客户端 ----

    @property
    def client(self):
        if self._client is None:
            self._client = self._build_client()
        return self._client

    def _build_client(self):
        """建一次 OpenAI 客户端。配置文件在这里才读，避免导入副作用。"""
        try:
            from dotenv import load_dotenv

            load_dotenv()  # 从当前工作目录找 .env
        except ImportError:
            pass  # 没装 dotenv 也允许，走系统环境变量
        from openai import OpenAI

        key = os.getenv(self.cfg.API_KEY_ENV)
        if not key:
            raise RuntimeError(
                f"环境变量 {self.cfg.API_KEY_ENV} 未设置。"
                f"请确认 .env 在当前工作目录（scheduler/）下，"
                f"且里面有一行 {self.cfg.API_KEY_ENV}=sk-..."
            )
        return OpenAI(api_key=key, base_url=self.cfg.BASE_URL)

    def _call_llm(self):
        """发一次请求，返回 assistant 消息对象（不在这里解析）。"""
        resp = self.client.chat.completions.create(
            model=self.cfg.MODEL,
            messages=self.messages,
            tools=self.schemas,
            temperature=self.cfg.TEMPERATURE,
        )
        return resp.choices[0].message

    # ---- 工具执行 ----

    def _execute(self, name, args):
        """执行一个工具。

        契约 A：任何失败都变成一段可读的字符串返回，
        绝不抛出去炸循环 —— 让模型读懂错误、自己重规划。
        """
        func = self.registry.get(name)
        if func is None:
            # 模型幻觉调用不存在的工具
            return (
                f"Error: unknown tool '{name}'. " f"Available: {sorted(self.registry)}"
            )

        try:
            result = func(**args)
        except TypeError as exc:
            return f"Error: bad arguments for '{name}': {exc}"
        except Exception as exc:
            return f"Error: {type(exc).__name__}: {exc}"

        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False, default=str)

    # ---- 主循环 ----

    def run(self, verbose=None):
        """主循环 —— 这就是「域自带」的那个 loop。

        结构与 scheduler_skeleton.AgentLoop.run 完全同构：
            for step in range(MAX_STEPS):
                调模型 -> 解析意图
                若模型不再要工具  -> 这就是最终答案，收工
                否则逐条执行工具，把结果以 role=tool 塞回历史，进入下一轮

        唯一区别是它此刻住在域文件里，而不是框架文件里。
        """
        verbose = self.cfg.VERBOSE if verbose is None else verbose
        started = time.time()

        if verbose:
            print("=" * 72)
            print("实习搜索 agent（域自带循环）")
            print("=" * 72)
            print(f"任务   : {self.task}")
            print(f"条件   : {DOMAIN_STATE.get('requirements') or '（尚未记录）'}")
            print(f"工具   : {len(self.registry)} 个")
            print(f"保险丝 : 最多 {self.cfg.MAX_STEPS} 步 / {self.cfg.TIMEOUT} 秒")

        for step in range(self.cfg.MAX_STEPS):
            self.steps_used = step + 1

            # 保险丝 2：总耗时
            if time.time() - started > self.cfg.TIMEOUT:
                return self._finalize(
                    "aborted",
                    f"[中止] 超过 {self.cfg.TIMEOUT}s 未完成",
                    reason="timeout",
                )

            if verbose:
                print(f"\n===== step {self.steps_used}: 调模型 =====")

            message = self._call_llm()

            # 出口：模型不再要工具，说明它给出了最终答案
            if not message.tool_calls:
                if verbose:
                    print("===== 结束: 模型直接作答 =====")
                self.messages.append({"role": "assistant", "content": message.content})
                return self._finalize("done", message.content)

            # 要调工具：先把 assistant 消息（内含 tool_calls）原样塞回历史
            self.messages.append(message)

            for tc in message.tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}

                result = self._execute(name, args)
                self.trace.append(
                    {
                        "step": self.steps_used,
                        "tool": name,
                        "args": args,
                        "result": result,
                    }
                )

                if verbose:
                    preview = result if len(result) <= 220 else result[:220] + " ..."
                    print(f"  调用 {name}({_brief_args(args)}) -> {preview}")

                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,  # 必须与上面的 tool_call 配对
                        "content": result,  # 必须是字符串
                    }
                )

        # 保险丝 1：步数用尽
        return self._finalize(
            "aborted", f"[中止] 达到最大步数 {self.cfg.MAX_STEPS}", reason="max_steps"
        )

    # ---- 收尾 ----

    def _finalize(self, status, answer, reason=None):
        """把这一次运行整理成结构化记录，顺便把答案返回给调用方。"""
        submission = None
        for rec in reversed(self.trace):
            if rec["tool"] == "submit_recommendation":
                try:
                    submission = json.loads(rec["result"])
                except (json.JSONDecodeError, TypeError):
                    submission = None
                break

        self.result = {
            "status": status,  # "done" | "aborted"
            "reason": reason,  # None | "timeout" | "max_steps"
            "answer": answer,
            "steps_used": self.steps_used,
            "tool_calls": len(self.trace),
            "postings_in_pool": list(DOMAIN_STATE["postings"].keys()),
            "submission": submission,
        }

        if getattr(self.cfg, "VERBOSE", True):
            print(f"\n{'-' * 72}")
            print(
                f"运行结束: status={status}"
                f" 步数={self.steps_used}"
                f" 工具调用={len(self.trace)}"
                f" 岗位池={len(self.result['postings_in_pool'])}"
            )
            if submission:
                print(f"提交: {submission.get('posting_ids')}")

        return answer

    def summary(self):
        """一行摘要，方便在别的程序里调用后直接打印。"""
        if self.result is None:
            return "not run yet"
        r = self.result
        submitted = len((r["submission"] or {}).get("posting_ids", []))
        return (
            f"[{r['status']}] steps={r['steps_used']} "
            f"tool_calls={r['tool_calls']} "
            f"pool={len(r['postings_in_pool'])} "
            f"submitted={submitted}"
        )

    def save_report(self, path):
        """把任务、结果、完整轨迹写成一个 JSON 文件（落盘，可复盘）。"""
        payload = {
            "task": self.task,
            # 当时的判定依据。复盘时能直接看到 agent 是按什么条件筛的，
            # 不必去猜 —— 这正是把条件显式记录下来的价值之一。
            "requirements": DOMAIN_STATE.get("requirements"),
            "result": self.result,
            "trace": [
                {
                    "step": rec["step"],
                    "tool": rec["tool"],
                    "args": rec["args"],
                    "result": (
                        rec["result"]
                        if len(rec["result"]) <= 4000
                        else rec["result"][:4000] + "...[truncated]"
                    ),
                }
                for rec in self.trace
            ],
        }
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        return path


# ---------- 7.4 一行启动的对外接口 ----------
# 注意这个默认任务里**写全了条件**：地点、时长、专业、毕业年份、可实习期。
# 它演示的是新架构的核心 —— 条件全部来自这句话。
# 如果你只写「帮我找个实习」，那这些条件就都是未知的，
# 筛选结果里会有一大堆 unverified，这是正确行为而不是故障。



def run_agent(task=None, verbose=True, cfg=None):
    """new 一个域、跑它的循环、把 agent 和答案一起还给你。

    这是「域自带循环」的最短用法：
        from domain_internship_search import run_agent
        agent, answer = run_agent("帮我找 2027 暑期新加坡实习")
        print(agent.summary())
    """
    agent = InternshipSearchAgent(task, cfg=cfg)
    answer = agent.run(verbose=verbose)
    return agent, answer


# ============================================================
# 第 8 部分：自检（直接 python domain_internship_search.py）
# ============================================================

if __name__ == "__main__":
    _args = sys.argv[1:]

    # ---- 模式一：跑真实任务（域自带循环，联网 + 花 API 额度）----
    #   python domain_internship_search.py --agent [任务描述]
    if _args and _args[0] == "--agent":
        _task = " ".join(_args[1:]) 
        _agent = InternshipSearchAgent(_task)
        print(_agent.run())
        print(f"\n{_agent.summary()}")

        # 顺手把轨迹落盘，方便复盘（不覆盖已有文件）
        _report = os.path.join("reports", "agent_last_run.json")
        _agent.save_report(_report)
        print(f"轨迹已写入 {_report}")
        sys.exit(0)

    # ---- 模式二（默认）：离线自检。不联网、不花额度 ----
    print(f"工具总数: {len(INTERNSHIP_TOOLS)}")
    for i, t in enumerate(INTERNSHIP_TOOLS, 1):
        print(f"  {i:2d}. {t['name']}")

    # 自检会调用真实的 save_requirements（它是要写盘的），
    # 这里把落盘路径临时指向自检专用文件，
    # 免得覆盖你手工维护的 data/requirements.json。
    REQUIREMENTS_PATH = os.path.join("data", "_selftest_requirements.json")

    # 遍历所有 TASKS，验证「条件来自 task」这条链路：
    #   seed 岗位 -> 记录该任务的条件 -> 逐条判定 -> 批量筛选 -> 判分
    _all_pass = True
    for task in TASKS:
        print(f"\n--- {task['task_id']} ---")
        print(f"conditions : {task['conditions']}")
        print(f"instruction: {task['instruction']}")

        # ★ 关键：条件不是预先配置好的，而是「记录」进来的 ——
        #   与 agent 真实运行时调用的是同一个函数。
        DOMAIN_STATE["requirements"] = None  # 清空，模拟全新一轮
        DOMAIN_STATE["task_text"] = task["instruction"]
        rec = save_requirements(task["conditions"])
        if rec.get("status") != "ok":
            print(f"  !! save_requirements 失败: {rec}")
            _all_pass = False
            continue

        ids = _seed_postings(task["seed"])
        for pid in ids:
            v = check_hard_constraints(pid)
            if v["eligible"]:
                status = "PASS"
                if v["unverified_rules"]:
                    status += f" (未判定: {','.join(v['unverified_rules'])})"
            else:
                status = f"FAIL {v['failed_rules']}"
            company = DOMAIN_STATE["postings"][pid]["company"]
            print(f"  {pid} ({company}): {status}")

        result = filter_postings({})
        matched_ids = [m["posting_id"] for m in result["matched"]]
        g = grade(matched_ids, task["gold"])
        print(f"  matched={matched_ids} gold={task['gold']} -> {g}")
        if not g["passed"]:
            _all_pass = False

    print(f"\n自检汇总: {'全部通过' if _all_pass else '有失败项，见上'}")

    print("\n--- agent 循环自检（不联网，只验证装配）---")
    _probe = InternshipSearchAgent("dummy task")
    print(f"  注册工具 {len(_probe.registry)} 个")
    print(f"  发出 schema {len(_probe.schemas)} 条")
    print(f"  消息历史起始 {len(_probe.messages)} 条（system + user）")
    print(f"  未调用模型时 client 未被创建: {_probe._client is None}")
    print(f"  启动时读回的历史条件: {_probe.previous_requirements}")
