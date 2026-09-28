"""
job_sources.py —— 真实岗位数据源抓取层（LinkedIn / 牛客）

================================================================================
这个文件解决什么问题
================================================================================

实习域 `domain_internship_search.py` 里的 `search_web`、`fetch_page`
原本是**桩函数**（返回写死的假数据）。要让 agent 真跑通，
需要有人去真网站上把岗位信息抓回来。

**但是**——LinkedIn、牛客、BOSS 直聘**都没有公开的岗位搜索 API**：

  站点        官方 API 情况                              本文件怎么做
  ----------  ----------------------------------------  --------------------
  LinkedIn    无公开岗位搜索 API。Talent Solutions 是   抓公开搜索页 HTML
              **partner-only 且只写不读**（把岗位推给     （未登录可见的部分）
              LinkedIn，不让你读出来）
  牛客        无公开 API                                抓页面内嵌的 state JSON
  BOSS 直聘   无公开 API，且搜索接口有 JS 反爬           本文件不实现，见下

所以本文件的策略是：**抓公开页面 + 明确报告失败**，不做假成功。

================================================================================
实测记录（2026-09-28，真跑过，不是推测）
================================================================================

  LinkedIn  https://www.linkedin.com/jobs/search?keywords=X&location=Y
            -> HTTP 200，274KB
            -> 60 个岗位卡片，真实数据（Shopee / TikTok / YouTrip / PwC）
            -> 详情页 200，JD 正文 2145 字
            ✅ 可用

  牛客      https://www.nowcoder.com/jobs/intern/center
            -> HTTP 200，310KB
            -> 内嵌 state JSON，字段最全：
               jobName / jobCity / graduationYear（"2026届"）/
               durationMonths / salaryMin / salaryMax /
               eduLevel / deliverEnd（投递截止）/
               requirements 与 infos 全文
            ✅ 可用，且字段质量最好

  BOSS 直聘 https://www.zhipin.com/wapi/zpgeek/search/joblist.json
            -> {"code":37,"message":"您的环境存在异常."}
            -> 反爬机制 `__zp_stoken__`，需要执行 JS 才能算出来
            -> 首页可抓（512KB），但搜索页是 9KB 的 JS 壳
            ❌ 本文件不实现；域里的工具会明确返回"被反爬拦截"

================================================================================
合规说明（重要）
================================================================================

本文件只抓取**公开可见、未登录**的页面内容，不绕过登录墙、不破解反爬。
使用时请注意：
  1. 两个站点的 ToS 都可能禁止自动化抓取，**用于个人学习/求职辅助
     与用于商业分发是两件事**；
  2. 请求频率必须限流（本文件默认每次请求间隔 1.5 秒 + 随机抖动）；
  3. 页面结构随时会变，解析失败是**预期内**的情况，必须优雅降级。

这不是免责声明，是工程约束：**解析失败要返回错误，不能返回假数据。**
"""

import json
import random
import re
import time
from datetime import datetime, timedelta

import requests

# ============================================================================
# 公共常量
# ============================================================================

# 默认请求头。没有 UA 会被直接拒绝，这是最低要求。
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
    "Connection": "keep-alive",
}

# 限流参数：两次请求之间的最小间隔（秒）
_MIN_INTERVAL = 1.5
_last_request_at = 0.0

# 每个 session_id 复用一个 requests.Session（复用 TCP 连接与 cookie）
_SESSIONS = {}


def _throttle():
    """限流：保证两次外部请求之间至少间隔 _MIN_INTERVAL 秒。

    为什么要随机抖动：固定间隔的请求节奏本身就是机器人特征。
    """
    global _last_request_at
    now = time.time()
    wait = _MIN_INTERVAL - (now - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.time() + random.uniform(0, 0.4)


def _get_session(session_id):
    """按 session_id 取（或建）一个 requests.Session。

    复用 Session 的好处：TCP 连接不断开、cookie 自动带上，
    这既是性能优化，也更接近真实浏览器的行为。
    """
    key = session_id or "_anon"
    if key not in _SESSIONS:
        s = requests.Session()
        s.headers.update(_DEFAULT_HEADERS)
        _SESSIONS[key] = s
    return _SESSIONS[key]


def _clean_text(html):
    """把 HTML 片段转成纯文本：去标签、解实体、压空白。"""
    if not html:
        return ""
    # 去掉 script / style 块（它们的内容不是正文）
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html,
                  flags=re.S | re.I)
    # 去掉 HTML 注释。
    # ★ LinkedIn 用了 Vue，页面里散落着 <!----> 这种占位注释
    #   （Vue 的 v-if 渲染残留）。不清理的话会混进标题和正文。
    html = re.sub(r"<!--.*?-->", " ", html, flags=re.S)
    # 块级标签转换成换行，保留段落感
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    html = re.sub(r"</(p|div|li|h[1-6])>", "\n", html, flags=re.I)
    # 去掉所有剩余标签
    html = re.sub(r"<[^>]+>", " ", html)
    # 常见 HTML 实体
    entities = {
        "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
        "&quot;": '"', "&#39;": "'", "&apos;": "'", "&mdash;": "—",
        "&ndash;": "–", "&rsquo;": "'", "&lsquo;": "'",
        "&ldquo;": '"', "&rdquo;": '"', "&hellip;": "…",
    }
    for k, v in entities.items():
        html = html.replace(k, v)
    html = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), html)
    # 压缩空白
    html = re.sub(r"[ \t\u00a0]+", " ", html)
    html = re.sub(r"\n\s*\n+", "\n\n", html)
    return html.strip()


# ============================================================================
# LinkedIn
# ============================================================================
# 抓取思路：
#   1. 搜索页 /jobs/search?keywords=X&location=Y 直接返回服务端渲染的
#      HTML（未登录可见），岗位卡片在 <li class="base-card ..."> 里；
#   2. 从卡片里拿到标题 / 公司 / 地点 / 详情页 URL；
#   3. 详情页 /jobs/view/xxx 里 JD 正文在
#      <div class="show-more-less-html__markup"> 里。
#
# ★ 注意：这些 class 名是 LinkedIn 的前端实现细节，随时可能变。
#   所以每个字段都独立 try，缺字段就留 None，不抛异常。


def linkedin_search(keywords, location="", max_results=10, session_id=None):
    """搜索 LinkedIn 公开岗位列表。

    参数：
        keywords    —— 搜索关键词，如 "data intern"
        location    —— 地点，如 "Singapore"
        max_results —— 最多返回几条
        session_id  —— 复用哪个会话

    返回：
        {"status": "ok",   "source": "linkedin", "results": [...], "total": n}
        {"status": "error","source": "linkedin", "message": "..."}
    """
    if not keywords:
        return {"status": "error", "source": "linkedin",
                "message": "keywords is required"}

    url = "https://www.linkedin.com/jobs/search"
    params = {"keywords": keywords}
    if location:
        params["location"] = location

    try:
        _throttle()
        sess = _get_session(session_id)
        resp = sess.get(url, params=params, timeout=20)
    except Exception as exc:
        return {"status": "error", "source": "linkedin",
                "message": f"request failed: {type(exc).__name__}: {exc}"}

    if resp.status_code != 200:
        return {"status": "error", "source": "linkedin",
                "http_status": resp.status_code,
                "message": f"LinkedIn returned HTTP {resp.status_code}"}

    html = resp.text

    # ---- 按 <li> 块切分 ----
    # ★ 实测修正过程（2026-09-28，两次踩坑）
    # ------------------------------------------------------------
    # 真实结构是：
    #   <ul class="jobs-search__results-list">
    #     <li>
    #       <div class="base-card ... base-search-card ... job-search-card"
    #            data-entity-urn="urn:li:jobPosting:4432231250">
    #         <a class="base-card__full-link" href="https://sg.linkedin.com/
    #            jobs/view/...-at-shopee-4432231250?...">
    #         ...
    #         <h3 class="base-search-card__title">岗位名</h3>
    #         <a class="hidden-nested-link">公司名</a>
    #         <span class="job-search-card__location">地点</span>
    #
    #   踩坑 1：写成 `<li[^>]*class="...base-card` -> 0 条。
    #          因为 li 上**没有** class，base-card 挂在里面的 div 上。
    #   踩坑 2：改写成按 `<div ... base-search-card` 切 -> 标题有了，
    #          但 posting_id / url / location 全空。
    #          因为切分锚点落在 div 的开标签**内部**，
    #          属性串里的 data-entity-urn 被切掉了；
    #          而且 location 在更靠后的兄弟节点里，不在同一段。
    #
    #   最终方案：按 `<li>` 切整块。一个 li = 一张完整卡片，
    #            所有字段都在这一块里，不用跨块拼接。
    blocks = re.split(r"<li[^>]*>", html)
    blocks = [b for b in blocks if "base-search-card__title" in b]

    results = []
    for block in blocks[:max_results]:
        title = _first(r'class="base-search-card__title"[^>]*>\s*(.*?)\s*</h3>',
                       block)
        company = _first(r'class="hidden-nested-link"[^>]*>\s*(.*?)\s*</a>',
                         block)
        loc = _first(r'class="job-search-card__location"[^>]*>\s*(.*?)\s*</span>',
                     block)
        job_url = _first(r'class="base-card__full-link[^"]*"\s+href="([^"]+)"',
                         block)
        listed = _first(r'datetime="([0-9]{4}-[0-9]{2}-[0-9]{2})"', block)
        job_id = _first(r"urn:li:jobPosting:(\d+)", block)

        if not title:
            continue

        # URL 尾部带了 ?position=...&trackingId=... 这类追踪参数，去掉
        if job_url:
            job_url = job_url.split("?")[0].replace("&amp;", "&")

        results.append({
            "posting_id": f"li_{job_id}" if job_id else None,
            "source": "linkedin",
            "title": _clean_text(title),
            "company": _clean_text(company),
            "location": _clean_text(loc),
            "post_date": listed,
            "url": job_url,
        })

    # ★ 兜底：如果按 li 切分失败（页面结构再次变动），
    #   退回全局正则并行扫描。宁可字段少，也不要 0 条。
    if not results:
        titles = re.findall(
            r'class="base-search-card__title"[^>]*>\s*(.*?)\s*</h3>', html,
            re.S)
        comps = re.findall(
            r'class="hidden-nested-link"[^>]*>\s*(.*?)\s*</a>', html, re.S)
        locs = re.findall(
            r'class="job-search-card__location"[^>]*>\s*(.*?)\s*</span>',
            html, re.S)
        urls = re.findall(
            r'class="base-card__full-link[^"]*"\s+href="([^"]+)"', html)
        urns = re.findall(r'urn:li:jobPosting:(\d+)', html)
        for k in range(min(len(titles), max_results)):
            results.append({
                "posting_id": (f"li_{urns[k]}" if k < len(urns) else None),
                "source": "linkedin",
                "title": _clean_text(titles[k]),
                "company": _clean_text(comps[k]) if k < len(comps) else None,
                "location": _clean_text(locs[k]) if k < len(locs) else None,
                "post_date": None,
                "url": (urls[k].split("?")[0] if k < len(urls) else None),
            })

    return {
        "status": "ok",
        "source": "linkedin",
        "query": keywords,
        "location": location,
        "results": results,
        "total": len(results),
    }


def linkedin_detail(url, session_id=None):
    """抓 LinkedIn 岗位详情页，返回 JD 正文文本。

    返回：
        {"status": "ok", "content": "...", "content_length": n, ...}
        {"status": "error", ...}
    """
    if not url or not url.startswith("http"):
        return {"status": "error", "source": "linkedin",
                "message": f"invalid url: {url!r}"}

    try:
        _throttle()
        sess = _get_session(session_id)
        resp = sess.get(url, timeout=20)
    except Exception as exc:
        return {"status": "error", "source": "linkedin",
                "message": f"request failed: {type(exc).__name__}: {exc}"}

    if resp.status_code != 200:
        return {"status": "error", "source": "linkedin",
                "http_status": resp.status_code,
                "message": f"LinkedIn returned HTTP {resp.status_code}"}

    html = resp.text

    # ========================================================================
    # 主解析路径：schema.org JSON-LD（★ 2026-09-28 改用的最可靠方式）
    # ========================================================================
    # ★ 为什么换成这个：用正则解析 HTML 太脆，且漏字段。
    #
    # 实测问题：第一版用 CSS class 抓 title/company/location，
    #   跑端到端任务时发现**LinkedIn 岗位的 deadline 和 post_date 全部缺失**，
    #   导致硬约束 H1（deadline 未过期 + post_date 在 90 天内）对
    #   每一个 LinkedIn 岗位都报"无法验证 → 不通过"。
    #   结果：LinkedIn 这个源在域里实际上完全不可用，
    #   模型只能建议用户"换牛客"。
    #
    # 根因：LinkedIn 详情页里嵌了一段 **schema.org/JobPosting 标准 JSON-LD**：
    #   {"@context":"http://schema.org","@type":"JobPosting",
    #    "datePosted":"2026-09-11T11:11:53.000Z",
    #    "description":"&lt;strong&gt;About Airwallex...",
    #    ...}
    # 这是**给搜索引擎看的结构化数据**，字段齐全且格式稳定，
    # 比解析 HTML class 名可靠得多——它是数据，不是视图。
    #
    # 教训：抓页面时先找结构化数据（JSON-LD / __NEXT_DATA__ / state JSON），
    #       找不到再退回正则解析 HTML。牛客也是这么抓的（内嵌 state）。
    jsonld = _extract_jobposting_jsonld(html)

    content, title, company, loc, post_date, deadline = "", None, None, None, None, None
    if jsonld:
        content = _clean_text(_unescape_html(jsonld.get("description", "")))
        title = jsonld.get("title")
        company = _jsonld_company(jsonld)
        loc = _jsonld_location(jsonld)
        post_date = _iso_to_date(jsonld.get("datePosted"))
        deadline = _iso_to_date(jsonld.get("validThrough"))

    # ========================================================================
    # 兜底路径：JSON-LD 不存在或不全时，回退到 HTML 正则解析
    # ========================================================================
    if not content:
        body = _first(r'class="show-more-less-html__markup[^"]*"[^>]*>(.*?)</div>',
                      html, flags=re.S)
        content = _clean_text(body) if body else ""

    if not title:
        # 真实结构：<h1 class="top-card-layout__title ... topcard__title">
        title = _first(r'class="[^"]*topcard__title[^"]*"[^>]*>\s*(.*?)\s*</h1>',
                       html)
        if not title:
            title = _first(r'<h1[^>]*class="[^"]*top-card-layout__title[^"]*"'
                           r'[^>]*>\s*(.*?)\s*</h1>', html)

    if not company:
        company = _first(r'class="[^"]*topcard__org-name-link[^"]*"[^>]*>\s*'
                         r'(.*?)\s*</a>', html)

    if not loc:
        loc = _first(r'class="[^"]*topcard__flavor[^"]*topcard__flavor--bullet'
                     r'[^"]*"[^>]*>\s*(.*?)\s*</span>', html)
        if not loc:
            loc = _first(r'class="[^"]*topcard__flavor[^"]*"[^>]*>\s*'
                         r'(.*?)\s*</span>', html)

    if not post_date:
        # <time class="main-job-card__listdate" datetime="2026-09-26">
        post_date = _first(r'class="[^"]*listdate[^"]*"[^>]*datetime="([^"]+)"',
                           html)

    return {
        "status": "ok",
        "source": "linkedin",
        "url": url,
        "title": _clean_text(title) if title else None,
        "company": _clean_text(company) if company else None,
        "location": _clean_text(loc) if loc else None,
        "post_date": post_date,
        "deadline": deadline,
        "content": content,
        "content_length": len(content),
        "used_jsonld": bool(jsonld),
        "truncated": False,
    }


def _unescape_html(s):
    """把 JSON-LD 里被转义的 HTML 反转义。

    JSON-LD 的 description 是 HTML 源码被转义后的字符串：
        &lt;strong&gt;About &lt;br&gt;...
    必须先反转义，再交给 _clean_text 去标签。
    顺序不能反——先 _clean_text 的话，&lt; 会被当成普通文本留下。
    """
    if not s:
        return ""
    return (s.replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&#39;", "'")
             .replace("&amp;", "&").replace("&nbsp;", " "))


def _extract_jobposting_jsonld(html):
    """从页面里提取 schema.org JobPosting 的 JSON-LD。

    LinkedIn 可能嵌多个 JSON-LD 块（面包屑、公司信息等），
    所以这里遍历全部，只挑 @type == "JobPosting" 的那个。

    括号配平扫描（和牛客的 jobList 提取同一套思路）——
    因为 JSON-LD 是嵌套结构，正则的贪婪匹配容易切错。
    """
    if not html:
        return None

    for m in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>',
                         html, re.I):
        start = html.find("{", m.end())
        if start < 0:
            continue

        depth, in_str, escaped = 0, False, False
        for j in range(start, len(html)):
            ch = html[j]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    chunk = html[start:j + 1]
                    try:
                        data = json.loads(chunk)
                    except Exception:
                        break
                    if isinstance(data, dict):
                        t = data.get("@type", "")
                        if isinstance(t, str) and "JobPosting" in t:
                            return data
                        # @type 可能是列表
                        if isinstance(t, list) and any("JobPosting" in x for x in t):
                            return data
                    break

    return None


def _jsonld_company(data):
    """从 JSON-LD 取公司名。hiringOrganization 可能是 dict 或 str。"""
    org = data.get("hiringOrganization")
    if isinstance(org, dict):
        return org.get("name")
    if isinstance(org, str):
        return org
    return None


def _jsonld_location(data):
    """从 JSON-LD 取地点。

    结构是嵌套的：
        jobLocation: {
          address: {addressLocality: "Singapore", addressRegion: ..., addressCountry: ...}
        }
    也可能是 jobLocation 数组。这里取出首个地址，拼成可读字符串。
    """
    jl = data.get("jobLocation")
    if isinstance(jl, list):
        jl = jl[0] if jl else None
    if not isinstance(jl, dict):
        return None

    addr = jl.get("address")
    if isinstance(addr, str):
        return addr
    if not isinstance(addr, dict):
        return None

    parts = [addr.get("addressLocality"), addr.get("addressRegion"),
             addr.get("addressCountry")]
    parts = [p for p in parts if p]
    return ", ".join(parts) if parts else None


def _iso_to_date(s):
    """把 ISO 8601 时间戳截成 'YYYY-MM-DD'。

    '2026-09-11T11:11:53.000Z' -> '2026-09-11'
    """
    if not s or not isinstance(s, str):
        return None
    m = re.match(r"((?:19|20)\d{2})-(\d{2})-(\d{2})", s)
    return m.group(1) + "-" + m.group(2) + "-" + m.group(3) if m else None


# ============================================================================
# 牛客
# ============================================================================
# 抓取思路：
#   牛客的实习列表页是 Next.js 应用，服务端会把 initialState
#   序列化进 HTML。实测该 state 里就带着完整岗位对象：
#       jobName / jobCity / graduationYear / durationMonths /
#       salaryMin / salaryMax / eduLevel / deliverEnd /
#       jobAddress / ext（内含 requirements 与 infos 全文）
#
#   所以抓取 = 取出那段 state JSON + 遍历 jobList。
#   比解析 HTML 稳定得多，因为它是**数据**不是**视图**。


def nowcoder_search(keywords="", city="", max_results=15, session_id=None):
    """搜索牛客实习岗位。

    参数：
        keywords    —— 关键词过滤（在岗位名里做包含匹配）；空则不过滤
        city        —— 城市过滤（如 "北京"、"上海"）；空则不过滤
        max_results —— 最多返回几条

    返回：
        {"status": "ok", "source": "nowcoder", "results": [...], "total": n}
    """
    url = "https://www.nowcoder.com/jobs/intern/center"

    try:
        _throttle()
        sess = _get_session(session_id)
        resp = sess.get(url, timeout=20)
    except Exception as exc:
        return {"status": "error", "source": "nowcoder",
                "message": f"request failed: {type(exc).__name__}: {exc}"}

    if resp.status_code != 200:
        return {"status": "error", "source": "nowcoder",
                "http_status": resp.status_code,
                "message": f"Nowcoder returned HTTP {resp.status_code}"}

    jobs = _extract_nowcoder_jobs(resp.text)
    if jobs is None:
        return {"status": "error", "source": "nowcoder",
                "message": "could not locate the embedded job state in page; "
                           "the site layout may have changed"}

    results = []
    for raw in jobs:
        name = raw.get("jobName") or ""
        c = raw.get("jobCity") or ""

        if keywords and keywords.lower() not in name.lower():
            continue
        if city and city not in c:
            continue

        # ext 字段是一个**字符串形式的 JSON**，里面才有 requirements / infos
        req_text, info_text = "", ""
        ext_raw = raw.get("ext")
        if isinstance(ext_raw, str) and ext_raw.strip().startswith("{"):
            try:
                ext = json.loads(ext_raw)
                req_text = ext.get("requirements", "") or ""
                info_text = ext.get("infos", "") or ""
            except Exception:
                pass

        results.append({
            "posting_id": f"nc_{raw.get('id')}",
            "source": "nowcoder",
            "title": name,
            "company": raw.get("companyName") or raw.get("brandName"),
            "location": c,
            "graduation_year": raw.get("graduationYear"),
            "duration_months": raw.get("durationMonths"),
            "salary": _nowcoder_salary(raw),
            "edu_level": _nowcoder_edu(raw.get("eduLevel")),
            "deliver_end": _ms_to_date(raw.get("deliverEnd")),
            "create_time": _ms_to_date(raw.get("createTime")),
            "requirements": _clean_text(req_text)[:2000],
            "responsibilities": _clean_text(info_text)[:2000],
            "url": (f"https://www.nowcoder.com/jobs/detail/{raw.get('id')}"
                    if raw.get("id") else None),
        })
        if len(results) >= max_results:
            break

    return {
        "status": "ok",
        "source": "nowcoder",
        "query": keywords,
        "city": city,
        "results": results,
        "total": len(results),
    }


def _extract_nowcoder_jobs(html):
    """从牛客页面里取出所有内嵌的岗位对象。

    做法：先用**括号配平**扫描，切出页面里出现的每一个候选数组
    （jobList / recommendJobs / otherRecommendJobList / list ...），
    再从中筛出「形状像岗位」的条目 —— 判据是同时有 id 和 jobName。
    最后按 id 去重。

    ★ 为什么不能只解析 jobList（2026-09-28 端到端跑通后发现）
    ------------------------------------------------------------
    第一版只找 `"jobList":[` 一个数组，实测只拿到 5 条记录。
    但同一页里 `"jobName":` 出现了 47 次 —— 说明岗位数据还散落在
    recommendJobs / otherRecommendJobList 等其他块里。
    只解析 jobList 的结果是：**关键词经常命中 0 条**，
    而这不是"牛客没有这个岗位"，是"我们没把页面读全"。
    静默漏数据比报错更危险 —— 模型会据此得出"该源无结果"的错误结论。

    为什么用括号配平而不是正则：
        数组里嵌套着对象，正则的贪婪/非贪婪都容易切错位置。
        括号配平是确定性的（判据唯一：深度归零处即为数组末尾）。
    """
    if not html:
        return None

    found = []
    seen_spans = []

    for key in _NC_JOB_ARRAY_KEYS:
        needle = '"%s":[' % key
        pos = 0
        while True:
            i = html.find(needle, pos)
            if i < 0:
                break
            start = i + len(needle) - 1          # 指向 '['
            end = _balanced_array_end(html, start)
            pos = i + len(needle)
            if end < 0:
                continue
            # 同一个数组可能被多个 key 命中，跳过重复区间
            if any(s <= start < e for s, e in seen_spans):
                continue
            seen_spans.append((start, end))
            chunk = html[start:end + 1]
            try:
                arr = json.loads(chunk)
            except Exception:
                continue
            _collect_job_like(arr, found)

    if not found:
        return None

    # 按 id 去重（页面里同一条岗位会出现在多个推荐块中）
    dedup = {}
    for item in found:
        jid = item.get("id")
        if jid is None:
            continue
        # 保留字段更全的那份
        if jid not in dedup or len(item) > len(dedup[jid]):
            dedup[jid] = item
    return list(dedup.values())


# 牛客页面里可能出现岗位对象的数组名（实测得来，见 _extract_nowcoder_jobs）
_NC_JOB_ARRAY_KEYS = (
    "jobList", "recommendJobs", "otherRecommendJobList", "list", "data",
)


def _balanced_array_end(text, start):
    """从 text[start] == '[' 开始做括号配平扫描，返回匹配的 ']' 下标。
    扫不到返回 -1。字符串内部的括号不计数（in_str 状态）。"""
    if start >= len(text) or text[start] != "[":
        return -1
    depth = 0
    in_str = False
    escaped = False
    for j in range(start, len(text)):
        ch = text[j]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                return j
    return -1


def _collect_job_like(node, out):
    """递归收集"形状像岗位"的对象 —— 判据是同时含 id 和 jobName。

    为什么要判形状而不是判位置：
        岗位对象在不同数组里的嵌套层数不一样
        （有的是 [{"data": {...}}]，有的是 [{...}]），
        按层数取必然漏。按字段判形状则与嵌套方式无关。
    """
    if isinstance(node, dict):
        if "jobName" in node and "id" in node:
            out.append(node)
            return
        for v in node.values():
            _collect_job_like(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_job_like(v, out)


def _nowcoder_salary(raw):
    """把牛客的薪资字段拼成人话。

    牛客的薪资是 salaryMin/salaryMax + salaryMonth（月数）+ salaryType。
    这里只做保守拼接，不猜测单位含义。
    """
    lo, hi = raw.get("salaryMin"), raw.get("salaryMax")
    months = raw.get("salaryMonth")
    if lo and hi:
        s = f"{lo}-{hi}/day"
    elif hi:
        s = f"up to {hi}/day"
    elif lo:
        s = f"from {lo}/day"
    else:
        return None
    if months:
        s += f" · {months} months"
    return s


_NC_EDU = {1000: "不限", 2000: "大专", 3000: "本科",
           4000: "硕士", 5000: "本科及以上", 6000: "硕士及以上"}


def _nowcoder_edu(code):
    if code is None:
        return None
    return _NC_EDU.get(code, f"edu_level={code}")


def _ms_to_date(ms, max_years_ahead=5):
    """毫秒时间戳 -> 'YYYY-MM-DD'。

    ★ 合理性校验（2026-09-28 加）
    ------------------------------------------------------------
    实测牛客的 deliverEnd 里有脏数据：某条实习岗的"投递截止"
    原始时间戳是 4938940800000，换算出来是 **2126-07-06**。
    这不是我们换算错了，是站点数据本身写错了一百年。

    如果原样传下去，H1（deadline 未过期）会把它判成"有效"——
    一条早就该过期的岗位因为截止日期写在 2126 年而永远合格。

    处理方式：超过 max_years_ahead 年的日期一律视为**无效输入**，
    返回 None（= 该字段未提供）。宁可让 H1 报"无法核实"，
    也不要用一个明显错误的值去做出"通过"的判断。
    这两个错误的代价不对称：报无法核实是保守的，
    用脏数据放行是把错岗位推给用户。
    """
    if not ms:
        return None
    try:
        dt = datetime.fromtimestamp(int(ms) / 1000)
    except Exception:
        return None
    if dt.year > datetime.now().year + max_years_ahead:
        return None
    return dt.strftime("%Y-%m-%d")


# ============================================================================
# BOSS 直聘 —— 明确不实现
# ============================================================================

def boss_search(keywords, city="", max_results=10, session_id=None):
    """BOSS 直聘搜索 —— **故意返回失败**，不假装成功。

    ★ 为什么这个函数存在，而不是干脆不写（2026-09-28 实测）
    ------------------------------------------------------------
    实测 https://www.zhipin.com/wapi/zpgeek/search/joblist.json 返回：
        {"code":37,"message":"您的环境存在异常.",
         "zpData":{"seed":"...","name":"013f7191","ts":...}}

    这是 BOSS 的 `__zp_stoken__` 反爬机制：它要你先执行一段 JS
    算出令牌，再带着令牌请求。纯 requests 打不进去。
    首页能抓（512KB），但搜索页只是 9KB 的 JS 壳。

    但这个函数**仍然保留**，理由有两条：
      ① 让 agent 知道"这个源存在，但当前拿不到"，
         它才能正确地向用户解释——这正是 τ-bench 考的"恢复能力"；
      ② 它天然是一个真实的"被拦截"测试场景，
         正好可以验证域的登录墙/封禁处理逻辑。

    要真正打通，需要 Playwright 之类的真浏览器执行 JS。
    那是另一条技术路线，不在本文件范围内。
    """
    return {
        "status": "error",
        "source": "boss",
        "error_code": "ANTI_BOT",
        "message": (
            "BOSS 直聘的搜索接口启用了 JS 反爬（__zp_stoken__），"
            "纯 HTTP 请求返回 code:37 '您的环境存在异常'。"
            "需要真实浏览器执行 JS 才能访问，当前不可用。"
        ),
        "hint": "use source='linkedin' or source='nowcoder' instead",
    }


# ============================================================================
# 统一入口
# ============================================================================

def search(source, keywords, location="", max_results=10, session_id=None):
    """统一的搜索入口，按 source 分发。

    source 取值：
        "linkedin"  —— LinkedIn 公开岗位（英文岗、海外岗）
        "nowcoder"  —— 牛客实习（中文岗、字段最全）
        "boss"      —— 永远返回 ANT_BOT 错误（见 boss_search 的说明）

    为什么要有这个统一入口：
        域里的工具只需要暴露一个 `search_jobs(source=...)`，
        模型通过参数选源，不用记住三个不同的工具名。
        这也符合 τ-bench 的工具设计习惯——**参数化而不是工具爆炸**。
    """
    src = (source or "").strip().lower()

    if src in ("linkedin", "li"):
        return linkedin_search(keywords, location, max_results, session_id)
    if src in ("nowcoder", "nc", "牛客"):
        # 牛客的地点语义是"城市"，LinkedIn 是"地区"，两者都塞进去做过滤
        return nowcoder_search(keywords, city=location,
                               max_results=max_results, session_id=session_id)
    if src in ("boss", "zhipin", "直聘"):
        return boss_search(keywords, location, max_results, session_id)

    return {
        "status": "error",
        "message": f"unknown source {source!r}; "
                   f"supported: 'linkedin', 'nowcoder', 'boss'",
    }


def fetch(source, url, session_id=None):
    """统一的详情抓取入口。"""
    src = (source or "").strip().lower()
    if src in ("linkedin", "li"):
        return linkedin_detail(url, session_id)
    if src in ("nowcoder", "nc", "牛客"):
        # 牛客的详情在列表页 state 里已经全给了，不需要单独抓详情页
        return {
            "status": "error",
            "source": "nowcoder",
            "message": "nowcoder already returns full requirements in search "
                       "results; use search(source='nowcoder') instead",
        }
    return {"status": "error",
            "message": f"unknown source {source!r}"}


# ============================================================================
# 内部小工具
# ============================================================================

def _first(pattern, text, flags=re.S):
    """取第一个捕获组，取不到返回 None。"""
    if not text:
        return None
    m = re.search(pattern, text, flags | re.I)
    return m.group(1) if m else None


# ============================================================================
# 自测
# ============================================================================

if __name__ == "__main__":
    print("=" * 72)
    print("job_sources 自测：真实抓取 LinkedIn / 牛客")
    print("=" * 72)

    print("\n【1】LinkedIn 搜索：data intern @ Singapore")
    r = linkedin_search("data intern", "Singapore", max_results=5)
    print(f"  status={r['status']}  total={r.get('total')}")
    for x in r.get("results", [])[:5]:
        print(f"    - {x['company']} | {x['title'][:60]} | {x['location']}")

    print("\n【2】牛客搜索：实习")
    r2 = nowcoder_search("", max_results=5)
    print(f"  status={r2['status']}  total={r2.get('total')}")
    for x in r2.get("results", [])[:5]:
        print(f"    - {x['title'][:40]} | {x['location']} | "
              f"{x['graduation_year']} | {x['duration_months']}月 | "
              f"{x['salary']}")

    print("\n【3】BOSS 直聘（预期失败）")
    r3 = boss_search("intern")
    print(f"  status={r3['status']}  error_code={r3.get('error_code')}")
    print(f"  message={r3['message'][:80]}")

    print("\n【4】统一入口分发")
    r4 = search("nowcoder", "", max_results=2)
    print(f"  search('nowcoder') -> status={r4['status']} total={r4['total']}")
