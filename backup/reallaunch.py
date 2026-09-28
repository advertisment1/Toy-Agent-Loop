"""在域文件之外独立跑一遍自检。

原本是把 domain_internship_search.py 的自检块复制出来做的试验。

★ 2026-09-28 同步（域删掉用户档案那次改动）
  改动前有两处问题：
    ① 只 `import domain_internship_search`，没把名字取进来 ——
       下面用到的 INTERNSHIP_TOOLS / TASKS 等其实都会 NameError。
    ② 引用了 check_hard_constraints(pid, user_id) 和
       filter_postings({"user_id": ...}) —— 这两个接口已经不存在了。

  现在条件不再来自档案，而是「从 task 里读出来、记录进去」。
  所以流程变成：
      save_requirements(task["conditions"])
      -> check_hard_constraints(pid)     # 不再传 user_id
      -> filter_postings({})             # criteria 只剩限定范围的作用

  另外把落盘路径指到自检专用文件，免得覆盖 data/requirements.json。
"""

import os

import domain_internship_search as dom
from domain_internship_search import (
    DOMAIN_STATE,
    INTERNSHIP_TOOLS,
    TASKS,
    _seed_postings,
    check_hard_constraints,
    filter_postings,
    grade,
    save_requirements,
)


if __name__ == "__main__":
    # 别覆盖正式的条件文件
    dom.REQUIREMENTS_PATH = os.path.join("data", "_reallaunch_requirements.json")

    print(f"工具总数: {len(INTERNSHIP_TOOLS)}")
    for i, t in enumerate(INTERNSHIP_TOOLS, 1):
        print(f"  {i:2d}. {t['name']}")

    print("\n--- 冒烟测试：t01 ---")
    task = TASKS[0]
    print(f"conditions : {task['conditions']}")
    print(f"instruction: {task['instruction']}")

    # ★ 条件必须先从 task 记录进来，否则所有规则都会判成「未判定」
    rec = save_requirements(task["conditions"])
    print(f"save_requirements -> {rec.get('status')}")

    ids = _seed_postings(task["seed"])
    print(f"seed 了 {len(ids)} 个岗位: {ids}")

    for pid in ids:
        v = check_hard_constraints(pid)
        status = "PASS" if v["eligible"] else f"FAIL {v['failed_rules']}"
        if v["eligible"] and v["unverified_rules"]:
            status += f" (未判定: {','.join(v['unverified_rules'])})"
        print(f"  {pid} ({DOMAIN_STATE['postings'][pid]['company']}): {status}")

    result = filter_postings({})
    print(f"\nfilter 结果: matched={result['matched']}")
    for e in result["excluded"]:
        print(f"  excluded {e['posting_id']}: {e['reason']}")

    # matched 是可读摘要（dict 列表），判分要先取出 posting_id
    matched_ids = [m["posting_id"] for m in result["matched"]]
    print(f"\nmatched_ids={matched_ids}")
    print(f"判分: {grade(matched_ids, task['gold'])}")
