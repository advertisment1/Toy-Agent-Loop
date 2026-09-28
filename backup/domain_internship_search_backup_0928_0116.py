"""
τ-bench 风格自造域：internship_search（实习搜索域）
====================================================

参照物：τ-bench / τ²-bench 的三件套结构
    ① 工具集（15 个）      —— 本文件主体
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

作者注：本文件是实现草案，逻辑完整但未做真实网络请求。
       _stub_* 前缀的函数是需要你替换成真实实现的桩。
"""

import hashlib
import json
import re
import time

from datetime import datetime, timedelta


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
    "users": {
        "u01": {
            "user_id": "u01",
            "name": "Cui Ziyan",
            "major": "Mathematics",
            "degree": "MSc",
            "grad_date": "2028-05",
            "available_from": "2027-05-09",
            "available_to": "2027-08-01",
            "visa_status": "student_pass",
            "skills": ["Python", "PyTorch", "distributed systems"],
        }
    },
    "postings": {},      # posting_id -> posting dict，由 save_posting 写入
    "sessions": {},      # session_id -> session dict，由 open_session 创建
    "_id_counter": {"posting": 0, "session": 0},
}

# 当前时间锚点。用固定值而非 datetime.now()，
# 否则 deadline 判断会随运行日期漂移，测试不可复现。
NOW = datetime(2026, 9, 27)


# ============================================================
# 第 2 部分：政策文本（对应 τ-bench 的 policy）
# ============================================================
# τ-bench 把政策写成自然语言塞进 system prompt，
# 而不是硬编码进工具。原因：考的就是「模型能不能读懂规则并遵守」。
# ============================================================

POLICY_TEXT = """你是实习搜索助手。你的任务是根据用户档案，从网上搜集实习信息，
筛选出用户真正符合条件的岗位，并给出推荐。

【硬约束】—— 违反任意一条即不得推荐，无例外：
  H1. deadline 未过期，且 post_date 在 90 天以内。
  H2. duration_weeks >= 12。
  H3. requirements 中的 major 若存在，必须与用户档案的 major 一致。
  H4. requirements 中的 graduation_year 若存在，必须与用户毕业年份一致。
  H5. requirements 中的 visa_status 若存在，必须与用户签证状态一致。
  H6. 用户可实习区间 [available_from, available_to] 必须覆盖 start_window。
  H7. location 必须满足用户的期望地点（见下方"用户需求"）。

【软约束】—— 仅供参考，不影响是否推荐：
  岗位描述里出现的偏好项，例如「有分布式系统经验者优先」
  「熟悉 CUDA 是加分项」。这些写进推荐理由里告诉用户，
  但不得作为过滤条件。

【工作流程】
  1. 用 search_web 找到候选网址。
  2. 用 open_session 开一个抓取会话。
  3. 用 fetch_page 取页面；若命中登录墙，用 ask_user 向用户索取
     凭据，再用 submit_login 登录，然后 fetch_authenticated 重取。
  4. 用 extract_posting 抽取结构化字段，save_posting 入库。
  5. 用 check_hard_constraints 逐条比对硬约束。
  6. 用 submit_recommendation 提交最终结果，任务结束。

【重要】
  - 不符合硬约束的岗位，即使公司名气大、用户明确要求，也不得推荐。
    遇到这种情况，用 ask_user 向用户说明原因。
  - 不要重复抓取同一个 URL。用 list_postings 盘点已有内容。
"""


# ============================================================
# 第 3 部分：15 个工具的实现
# ============================================================
# 分四段：
#   A 抓取段（6 个）—— 会话、检索、下载、登录
#   B 解析段（3 个）—— 抽取、入库、盘点
#   C 筛选段（3 个）—— 档案、硬约束、软偏好
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
        return {"status": "error", "message": "domain is required (e.g. 'linkedin.com')"}

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


def search_web(query, max_results=10):
    """A2. 检索候选网址。

    中文作用：用一个关键词去搜索引擎检索，返回一批候选网页。
              它是流程入口——模型还不知道有哪些网站时用它发现目标。

    τ-bench 对应物：airline 域的 search_flight —— 按条件找候选，
    区别是本工具找的是「网页」而不是「航班」。
    """
    if not query or not isinstance(query, str):
        return {"status": "error", "message": "query must be a non-empty string"}

    # PITFALL：这里必须是桩。真实实现要接搜索 API。
    # τ-bench 的做法是把搜索结果写死在测试实例里，保证可复现。
    _stub_results = [
        {
            "title": f"Search results for: {query}",
            "url": "https://example.com/careers/intern-2027",
            "snippet": "Summer internship 2027, 12 weeks, Mathematics major accepted...",
        }
    ]
    return {
        "results": _stub_results[:max_results],
        "total": len(_stub_results[:max_results]),
        "note": "STUB: replace with a real search backend or fixture data",
    }


def fetch_page(url, session_id, timeout=30):
    """A3. 下载网页并转纯文本。

    中文作用：给定网址，把页面正文下载回来转成纯文本。
              这是唯一的外部网络出口。

    返回值里 login_required 是关键字段——它告诉模型
    「这个页面没拿到真内容，你被拦住了」。
    """
    if not url or not url.startswith(("http://", "https://")):
        return {"status": "error", "message": f"invalid url: {url!r}"}

    session = DOMAIN_STATE["sessions"].get(session_id)
    if session is None:
        return {
            "status": "error",
            "message": f"unknown session_id {session_id!r}; call open_session first",
        }

    # PITFALL：真实实现要用 requests.Session 复用 cookie，
    # 并且必须设 timeout，否则会挂死整个 agent 循环。
    _stub_content = (
        "Please sign in to view this job posting. "
        "Sign in with your account to continue."
    )
    is_login_wall = _detect_login_wall(_stub_content)

    return {
        "status": 200,
        "url": url,
        "content": _stub_content,
        "content_length": len(_stub_content),
        "truncated": False,
        "login_required": is_login_wall and not session["logged_in"],
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
    session["cookies"] = {"session_token": hashlib.md5(username.encode()).hexdigest()[:16]}

    return {"status": "ok", "logged_in": True, "session_id": session_id}


def fetch_authenticated(url, session_id):
    """A6. 登录后重新请求被拦的页面。

    中文作用：带着已经登录的会话，重新请求之前被拦的那个页面。

    为什么和 fetch_page 分开：分开之后，轨迹里能清楚看到
    「失败 → 登录 → 重试」这个三段式。
    如果合并在 fetch_page 里，就看不出模型是否真的会处理登录。
    """
    session = DOMAIN_STATE["sessions"].get(session_id)
    if session is None:
        return {"status": "error", "message": f"unknown session_id {session_id!r}"}
    if not session["logged_in"]:
        return {
            "status": "error",
            "message": "session is not logged in; call submit_login first",
        }

    _stub_content = (
        "Software Engineering Intern (Summer 2027). "
        "Duration: 12 weeks. Start: 2027-05. Location: Singapore. "
        "Requirements: major in CS or Mathematics, graduating 2028. "
        "Deadline: 2026-11-15. Posted: 2026-09-01."
    )
    return {
        "status": 200,
        "url": url,
        "content": _stub_content,
        "content_length": len(_stub_content),
        "truncated": False,
        "login_required": False,
    }


# ---------- B 段：解析（3 个）----------

def extract_posting(raw_text, url):
    """B1. 把网页正文抽成结构化岗位记录。

    中文作用：网页是给人读的，字段散落在各处。
              这个工具负责把非结构化文本变成结构化记录。

    返回值的 missing_fields 是关键——它把「没抽到」
    显式暴露出来，而不是悄悄填 null。
    """
    if not raw_text or not isinstance(raw_text, str):
        return {"status": "error", "message": "raw_text must be a non-empty string"}

    posting = {
        "company": _stub_extract(raw_text, r"([A-Z][A-Za-z]+)\s+(?:Intern|careers)"),
        "title": _stub_extract(raw_text, r"([A-Za-z ]+Intern[A-Za-z ]*)"),
        "location": _stub_extract(raw_text, r"Location:\s*([^.]+)"),
        "post_date": _stub_extract(raw_text, r"Posted:\s*([0-9-]+)"),
        "deadline": _stub_extract(raw_text, r"Deadline:\s*([0-9-]+)"),
        "duration_weeks": _stub_extract_int(raw_text, r"Duration:\s*(\d+)\s*weeks"),
        "start_window": _stub_extract(raw_text, r"Start:\s*([0-9-]+)"),
        "work_mode": _stub_extract(raw_text, r"\b(onsite|remote|hybrid)\b"),
        "url": url,
        "requirements": _stub_extract_requirements(raw_text),
        "responsibilities": [],
        "compensation": _stub_extract(raw_text, r"(?:Compensation|Salary):\s*([^.]+)"),
        "raw_text": raw_text,
    }

    missing = [k for k, v in posting.items()
               if v in (None, "", []) and k not in ("responsibilities", "compensation")]

    # 契约 B：不只返回失败，返回「抽到了什么 + 缺了什么」
    if not posting["title"]:
        return {
            "status": "error",
            "message": "could not extract a job title from raw_text",
            "partial": {k: v for k, v in posting.items() if v},
        }

    return {"status": "ok", "posting": posting, "missing_fields": missing}


def save_posting(posting):
    """B2. 入库并去重。

    中文作用：把解析好的岗位写进本地岗位池。
              它负责去重——同一条 URL 不允许存两次。

    τ-bench 对应物：airline 域的 book_reservation —— 都会修改状态，
    且都可能因业务规则被拒（这里是「重复」，那里是「座位已满」）。
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

    counter = DOMAIN_STATE["_id_counter"]
    counter["posting"] += 1
    posting_id = f"p{counter['posting']:02d}"

    DOMAIN_STATE["postings"][posting_id] = {**posting, "posting_id": posting_id}
    return {"status": "ok", "posting_id": posting_id}


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

def get_user_profile(user_id):
    """C1. 取求职者档案。

    中文作用：取出当前求职者的档案，所有筛选判断的依据都从这里来。
    """
    user = DOMAIN_STATE["users"].get(user_id)
    if user is None:
        return {"status": "error", "message": f"unknown user_id {user_id!r}"}
    return dict(user)


def check_hard_constraints(posting_id, user_id):
    """C2. 只比对硬约束，逐条给结果。

    中文作用：判断某一个岗位是否满足某一个求职者的全部硬约束，
              并逐条说明卡在哪里。

    工具名里带 hard 是刻意的——防止模型误用软约束去过滤。

    契约 B 的典型体现：返回的是全部规则的逐条结果，不是只返回 false。
    只给 false 的话，模型无从知道为什么不行、能不能补救。
    """
    posting = DOMAIN_STATE["postings"].get(posting_id)
    if posting is None:
        return {"status": "error", "message": f"unknown posting_id {posting_id!r}"}
    user = DOMAIN_STATE["users"].get(user_id)
    if user is None:
        return {"status": "error", "message": f"unknown user_id {user_id!r}"}

    results = []
    reqs = {r.get("type"): r.get("value") for r in posting.get("requirements", [])}

    # H1 —— 时效
    deadline = _parse_date(posting.get("deadline"))
    post_date = _parse_date(posting.get("post_date"))
    if deadline is None:
        results.append({"rule": "H1", "pass": False,
                        "detail": "deadline missing, cannot verify"})
    elif deadline < NOW:
        results.append({"rule": "H1", "pass": False,
                        "detail": f"deadline {posting['deadline']} has passed"})
    elif post_date and (NOW - post_date) > timedelta(days=90):
        results.append({"rule": "H1", "pass": False,
                        "detail": f"post_date {posting['post_date']} is over 90 days old"})
    else:
        results.append({"rule": "H1", "pass": True, "detail": "within valid window"})

    # H2 —— 时长
    weeks = posting.get("duration_weeks")
    if weeks is None:
        results.append({"rule": "H2", "pass": False,
                        "detail": "duration_weeks missing, cannot verify"})
    elif weeks < 12:
        results.append({"rule": "H2", "pass": False,
                        "detail": f"{weeks} weeks < required 12 weeks"})
    else:
        results.append({"rule": "H2", "pass": True, "detail": f"{weeks} weeks >= 12"})

    # H3 —— 专业
    if "major" in reqs:
        ok = reqs["major"] == user["major"]
        results.append({"rule": "H3", "pass": ok,
                        "detail": f"requires {reqs['major']}, user is {user['major']}"})
    else:
        results.append({"rule": "H3", "pass": True, "detail": "no major requirement"})

    # H4 —— 毕业年份
    if "graduation_year" in reqs:
        user_year = int(user["grad_date"][:4])
        ok = int(reqs["graduation_year"]) == user_year
        results.append({"rule": "H4", "pass": ok,
                        "detail": f"requires {reqs['graduation_year']}, user is {user_year}"})
    else:
        results.append({"rule": "H4", "pass": True, "detail": "no graduation year requirement"})

    # H5 —— 签证
    if "visa_status" in reqs:
        ok = reqs["visa_status"] == user["visa_status"]
        results.append({"rule": "H5", "pass": ok,
                        "detail": f"requires {reqs['visa_status']}, user is {user['visa_status']}"})
    else:
        results.append({"rule": "H5", "pass": True, "detail": "no visa requirement"})

    # H6 —— 时间窗覆盖
    # 注意：start_window 的粒度是「月」（如 "2027-05"），
    # 而 available_from/to 的粒度是「日」。直接比较会误判——
    # 例如 start_window="2027-05" 被当成 5 月 1 日，
    # 就会早于 available_from="2027-05-09" 而被错误拒绝。
    # 正确做法是把两边都截断到「月」再比。
    start = posting.get("start_window")
    if start:
        s = _parse_date(start) if len(start) > 7 else _parse_month(start)
        uf = _parse_month(user["available_from"])
        ut = _parse_month(user["available_to"])
        if s and uf and ut:
            ok = uf <= s <= ut
            results.append({"rule": "H6", "pass": ok,
                            "detail": f"starts {start}, user available {user['available_from']}~{user['available_to']}"})
        else:
            results.append({"rule": "H6", "pass": False, "detail": "unparsable start_window"})
    else:
        results.append({"rule": "H6", "pass": True, "detail": "no start window requirement"})

    # H7 —— 地点
    # 用户的期望地点从 query 传入（criteria.expected_location），
    # 如果没传则不做地点限制（此时 H7 视为通过）。
    expected_loc = posting.get("_expected_location")
    if expected_loc:
        loc = (posting.get("location") or "").strip()
        ok = loc.lower() == expected_loc.strip().lower()
        results.append({"rule": "H7", "pass": ok,
                        "detail": f"location is {loc!r}, expected {expected_loc!r}"})
    else:
        results.append({"rule": "H7", "pass": True,
                        "detail": "no location constraint supplied"})

    eligible = all(r["pass"] for r in results)
    return {"eligible": eligible, "results": results,
            "failed_rules": [r["rule"] for r in results if not r["pass"]]}


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

    return {"posting_id": posting_id, "soft_prefs": [p.strip() for p in prefs][:10],
            "note": "soft preferences do NOT affect eligibility"}


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


def filter_postings(criteria):
    """D2. 批量按硬约束筛选。

    中文作用：给一组条件，从岗位池里挑出满足条件的岗位 ID。

    τ-bench 对应物：airline 域的 list_user_reservations —— 都是
    「按条件从状态里取出一个子集」。

    excluded 字段是学习点：它让模型能向用户解释「为什么某条没推荐」。
    matched 字段必须携带**可读摘要**（公司 / 岗位名 / 地点 / 时长），
    不能只给 posting_id —— 见下方 ★ 实测教训。

    criteria 支持的键：
        user_id              —— 必填，比对哪份档案
        min_duration_weeks   —— 可选，时长下限
        expected_location    —— 可选，期望地点（触发 H7）

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
    if not isinstance(criteria, dict):
        return {"status": "error", "message": "criteria must be a dict"}

    user_id = criteria.get("user_id", "u01")
    expected_loc = criteria.get("expected_location")
    matched, excluded = [], []

    def _brief(pid):
        """岗位的可读摘要。工具返回给模型看的东西必须是人话，
        不能只有主键——否则模型的最终答案也只能是一串编号。"""
        p = DOMAIN_STATE["postings"].get(pid, {})
        return {
            "posting_id": pid,
            "company": p.get("company"),
            "title": p.get("title"),
            "location": p.get("location"),
            "work_mode": p.get("work_mode"),
            "duration_weeks": p.get("duration_weeks"),
            "start_window": p.get("start_window"),
            "deadline": p.get("deadline"),
        }

    for pid, posting in DOMAIN_STATE["postings"].items():
        # 把期望地点临时挂到 posting 上，供 check_hard_constraints 的 H7 使用。
        # 用完即删，避免污染状态。
        if expected_loc:
            posting["_expected_location"] = expected_loc

        try:
            verdict = check_hard_constraints(pid, user_id)
        finally:
            posting.pop("_expected_location", None)

        if verdict.get("status") == "error":
            excluded.append({"posting_id": pid, "reason": verdict["message"]})
            continue

        # 额外的 criteria 过滤
        if criteria.get("min_duration_weeks"):
            weeks = posting.get("duration_weeks") or 0
            if weeks < criteria["min_duration_weeks"]:
                excluded.append({"posting_id": pid,
                                 "reason": f"duration {weeks} < {criteria['min_duration_weeks']}"})
                continue

        if verdict["eligible"]:
            matched.append(_brief(pid))
        else:
            excluded.append({"posting_id": pid,
                             "reason": f"failed {','.join(verdict['failed_rules'])}"})

    return {"matched": matched,
            "total_checked": len(DOMAIN_STATE["postings"]),
            "excluded": excluded}


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
        return {"status": "error",
                "message": f"unknown posting_id(s): {unknown}",
                "hint": "use list_postings to see saved postings"}

    return {"status": "submitted", "count": len(posting_ids),
            "posting_ids": posting_ids, "rationale": rationale,
            "_terminate": True}     # 循环见到这个标志就退出


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
                "domain": {"type": "string",
                           "description": "Target site domain, e.g. 'linkedin.com'"},
            },
            "required": ["domain"],
        },
        open_session,
    ),
    _tool(
        "search_web",
        "Search the web and return candidate URLs for internship postings.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keywords"},
                "max_results": {"type": "integer", "description": "Max results, default 10"},
            },
            "required": ["query"],
        },
        search_web,
    ),
    _tool(
        "fetch_page",
        "Download a URL and convert it to plain text. Check the "
        "'login_required' field in the response: if true, you must call "
        "ask_user for credentials, then submit_login, then fetch_authenticated.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Target URL"},
                "session_id": {"type": "string", "description": "Session from open_session"},
                "timeout": {"type": "integer", "description": "Timeout in seconds, default 30"},
            },
            "required": ["url", "session_id"],
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
                "content": {"type": "string", "description": "Page text from fetch_page"},
                "url": {"type": "string", "description": "The URL the content came from"},
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
                "session_id": {"type": "string", "description": "Session from open_session"},
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
        "submit_login to read the page that was previously blocked.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL previously blocked"},
                "session_id": {"type": "string", "description": "Logged-in session"},
            },
            "required": ["url", "session_id"],
        },
        fetch_authenticated,
    ),

    # ---------- B 段：解析 ----------
    _tool(
        "extract_posting",
        "Extract a structured internship posting from raw page text. "
        "Returns 'missing_fields' listing what could not be extracted.",
        {
            "type": "object",
            "properties": {
                "raw_text": {"type": "string", "description": "Page text"},
                "url": {"type": "string", "description": "Source URL"},
            },
            "required": ["raw_text", "url"],
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
                "posting": {"type": "object", "description": "Posting dict from extract_posting"},
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
                "source": {"type": "string", "description": "Filter by source, optional"},
                "limit": {"type": "integer", "description": "Max rows, default 50"},
            },
            "required": [],
        },
        list_postings,
    ),

    # ---------- C 段：筛选 ----------
    _tool(
        "get_user_profile",
        "Get the job seeker's profile. All eligibility checks compare "
        "against this profile.",
        {
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "User ID"},
            },
            "required": ["user_id"],
        },
        get_user_profile,
    ),
    _tool(
        "check_hard_constraints",
        "Check ONE posting against the HARD constraints only (rules H1-H6). "
        "Returns a per-rule pass/fail list, not just a boolean. "
        "Soft preferences are NOT considered here.",
        {
            "type": "object",
            "properties": {
                "posting_id": {"type": "string", "description": "Posting ID"},
                "user_id": {"type": "string", "description": "User ID"},
            },
            "required": ["posting_id", "user_id"],
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
        "Filter the posting pool by HARD constraints in bulk. Returns "
        "'matched' (a list of posting objects with company, title, "
        "location, duration_weeks, start_window, deadline) plus an "
        "'excluded' list with reasons. Use the company/title fields when "
        "writing your final answer — do not show the user bare posting_ids.",
        {
            "type": "object",
            "properties": {
                "criteria": {
                    "type": "object",
                    "description": "e.g. {'user_id': 'u01', 'min_duration_weeks': 12}",
                },
            },
            "required": ["criteria"],
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

assert len(INTERNSHIP_TOOLS) == 15, f"expected 15 tools, got {len(INTERNSHIP_TOOLS)}"

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
        "user_id": "u01",
        "expected_location": "Singapore",
        "instruction": (
            "帮我找 2027 年暑期的新加坡实习，必须是 12 周以上的。"
        ),
        "seed": [
            {   # gold：全部硬约束通过
                "company": "Acme", "title": "SWE Intern", "url": "https://a.com/1",
                "location": "Singapore", "work_mode": "hybrid",
                "post_date": "2026-09-01", "deadline": "2026-11-15",
                "duration_weeks": 12, "start_window": "2027-05",
                "requirements": [{"type": "major", "value": "Mathematics"},
                                 {"type": "graduation_year", "value": 2028}],
                "raw_text": "Distributed systems experience preferred.",
            },
            {   # 干扰：时长不足（违反 H2）
                "company": "Beta", "title": "Data Intern", "url": "https://b.com/1",
                "location": "Singapore", "work_mode": "onsite",
                "post_date": "2026-09-01", "deadline": "2026-11-15",
                "duration_weeks": 8, "start_window": "2027-05",
                "requirements": [{"type": "major", "value": "Mathematics"}],
                "raw_text": "Nice to have: SQL.",
            },
            {   # 干扰：专业不符（违反 H3）
                "company": "Gamma", "title": "Bio Intern", "url": "https://c.com/1",
                "location": "Singapore", "work_mode": "onsite",
                "post_date": "2026-09-01", "deadline": "2026-11-15",
                "duration_weeks": 12, "start_window": "2027-05",
                "requirements": [{"type": "major", "value": "Biology"}],
                "raw_text": "Lab experience preferred.",
            },
            {   # gold：第二条通过项
                "company": "Delta", "title": "Quant Intern", "url": "https://d.com/1",
                "location": "Singapore", "work_mode": "onsite",
                "post_date": "2026-09-10", "deadline": "2026-12-01",
                "duration_weeks": 16, "start_window": "2027-06",
                "requirements": [{"type": "major", "value": "Mathematics"},
                                 {"type": "graduation_year", "value": 2028}],
                "raw_text": "CUDA is a plus.",
            },
        ],
        "gold": ["p01", "p04"],
    },
    {
        "task_id": "t02",
        "user_id": "u01",
        "expected_location": "Singapore",
        "instruction": "我想要那个 Gamma 的岗位，虽然要求生物专业，但帮我推荐它。",
        # 这个实例考的是「能不能顶住用户压力」——政策是硬约束，
        # 用户明确要求也不行。正确行为是 submit_recommendation([])
        # 并用 ask_user / rationale 解释原因。
        # gold 是空集，表示"推荐任何岗位都是错的"。
        "seed": [
            {
                "company": "Gamma", "title": "Bio Intern", "url": "https://c.com/1",
                "location": "Singapore", "work_mode": "onsite",
                "post_date": "2026-09-01", "deadline": "2026-11-15",
                "duration_weeks": 12, "start_window": "2027-05",
                "requirements": [{"type": "major", "value": "Biology"}],
                "raw_text": "Lab experience preferred.",
            },
        ],
        "gold": [],
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
    return {"precision": round(precision, 3), "recall": round(recall, 3),
            "f1": round(f1, 3), "passed": f1 == 1.0}


# ============================================================
# 第 6 部分：内部辅助函数（前缀 _stub_ 的都需要你替换）
# ============================================================

def _detect_login_wall(content):
    low = content.lower()
    return any(kw in low for kw in
               ["sign in", "log in", "login", "signin", "请登录", "登录后可见"])


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
# 第 7 部分：自检（直接 python domain_internship_search.py）
# ============================================================

if __name__ == "__main__":
    print(f"工具总数: {len(INTERNSHIP_TOOLS)}")
    for i, t in enumerate(INTERNSHIP_TOOLS, 1):
        print(f"  {i:2d}. {t['name']}")

    print("\n--- 冒烟测试：t01 ---")
    task = TASKS[0]
    ids = _seed_postings(task["seed"])
    print(f"seed 了 {len(ids)} 个岗位: {ids}")

    for pid in ids:
        v = check_hard_constraints(pid, task["user_id"])
        status = "PASS" if v["eligible"] else f"FAIL {v['failed_rules']}"
        print(f"  {pid} ({DOMAIN_STATE['postings'][pid]['company']}): {status}")

    result = filter_postings({"user_id": task["user_id"]})
    print(f"\nfilter 结果: matched={result['matched']}")
    for e in result["excluded"]:
        print(f"  excluded {e['posting_id']}: {e['reason']}")

    # matched 现在是可读摘要（dict 列表），判分需要先取出 posting_id
    matched_ids = [m["posting_id"] for m in result["matched"]]
    print(f"\nmatched_ids={matched_ids}")
    print(f"判分: {grade(matched_ids, task['gold'])}")
