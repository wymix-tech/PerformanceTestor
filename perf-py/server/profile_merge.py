"""折叠栈（Folded Stacks）合并与聚合树工具（P2-1）。

agent 端 JfrProfiler 周期上报标准折叠栈（每行 "frame1;frame2;...;frameN count"），
多批快照必须在服务端按栈路径合并累加后返回（严禁前端逐批拼接渲染）。
供 /api/profiles 与 AI profile_flamegraph_summary 工具共用。
"""

from __future__ import annotations

# 聚合树最大节点数（防深调用树撑爆前端渲染与响应体）
DEFAULT_MAX_NODES = 512
MAX_NODES = 4000


def parse_folded(folded_text: str) -> dict[str, int]:
    """解析折叠栈文本：{"栈路径": 计数}。坏行跳过（容忍单批数据损坏）。"""
    stacks: dict[str, int] = {}
    if not folded_text:
        return stacks
    for line in folded_text.splitlines():
        line = line.strip()
        if not line:
            continue
        idx = line.rfind(" ")
        if idx <= 0:
            continue
        path, cnt = line[:idx].strip(), line[idx + 1:].strip()
        if not path or not cnt.isdigit():
            continue
        stacks[path] = stacks.get(path, 0) + int(cnt)
    return stacks


def merge_folded(folded_texts: list[str]) -> dict:
    """多批折叠栈按栈路径合并累加。

    返回 {"stacks": {路径: 计数}, "totalSamples": 总量}；
    样本口径与 agent 上报一致（cpu=次数，alloc=分配字节，lock=阻塞毫秒）。
    """
    merged: dict[str, int] = {}
    total = 0
    for text in folded_texts:
        for path, cnt in parse_folded(text).items():
            merged[path] = merged.get(path, 0) + cnt
            total += cnt
    return {"stacks": merged, "totalSamples": total}


def folded_to_tree(merged: dict[str, int], max_nodes: int = DEFAULT_MAX_NODES) -> dict:
    """合并后的折叠栈 → 聚合树 JSON（根为 "root"，超 max_nodes 聚入 *overflow* 桶）。"""
    limit = max(1, min(max_nodes, MAX_NODES))
    used = 1  # root 占一个

    root: dict = {"name": "root", "value": 0, "self": 0, "children": {}}

    def node_get(children: dict, name: str) -> dict:
        nonlocal used
        n = children.get(name)
        if n is not None:
            return n
        if used >= limit:
            # 超限：聚入当前父节点的 *overflow* 桶（桶不计入上限，仅承载溢出路径，
            # 防前端卡顿的同时不丢样本计数）
            n = children.get("*overflow*")
            if n is None:
                n = {"name": "*overflow*", "value": 0, "self": 0, "children": {}}
                children["*overflow*"] = n
            return n
        n = {"name": name, "value": 0, "self": 0, "children": {}}
        children[name] = n
        used += 1
        return n

    for path, cnt in merged.items():
        frames = path.split(";") if path else []
        if not frames:
            root["self"] += cnt
            continue
        node = root
        for i, frame in enumerate(frames):
            node = node_get(node["children"], frame)
            node["value"] += cnt
            if i == len(frames) - 1:
                node["self"] += cnt
        root["value"] += cnt

    def to_json(n: dict) -> dict:
        out: dict = {"name": n["name"], "value": n["value"], "self": n["self"]}
        if n["children"]:
            out["children"] = [to_json(c) for c in n["children"].values()]
        return out

    return to_json(root)


def top_call_paths(merged: dict[str, int], top_n: int = 10) -> list[dict]:
    """热点调用路径 Top N（按完整路径样本量降序，含叶帧与占比）。"""
    total = sum(merged.values())
    ordered = sorted(merged.items(), key=lambda kv: kv[1], reverse=True)
    items: list[dict] = []
    for path, cnt in ordered[:max(1, top_n)]:
        leaf = path.rsplit(";", 1)[-1] if path else ""
        items.append({
            "path": path,
            "leaf": leaf,
            "samples": cnt,
            "pct": round(cnt * 100.0 / total, 1) if total else 0.0,
        })
    return items
