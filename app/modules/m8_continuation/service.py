import hashlib

from app.config import VERSION
from app.keys import ReplayError
from app.modules.m7_release.service import compute

# 续批对每个受影响分区只允许一个处置：可继续使用 / 需要重做 / 证据不足。
DISPOSITIONS = (
    ("reuse", "可继续使用"),
    ("write", "需要重做"),
    ("blocked", "证据不足"),
)


def continuation_key(parent_key, raw):
    # 同一份修订对同一个原批重试，得到同一个续批编号，重试不会产生第二份续批。
    digest = hashlib.sha256(f"{parent_key}|".encode() + raw).hexdigest()[:26]
    return f"cont-{digest}"


def single_target_rows(tasks):
    """同一分区不得同时出现两个互相矛盾的补数目标行数。"""
    targets = {}
    conflicts = []
    for item in tasks:
        plan = item["计划"]
        if not plan:
            continue
        rows = plan["rows"]
        previous = targets.get(item["分区"])
        if previous is None:
            targets[item["分区"]] = rows
        elif previous != rows:
            conflicts.append({"分区": item["分区"], "目标行数": sorted({previous, rows})})
    return conflicts


def build_continuation(parent, parent_key, raw):
    """在原批记录只读保留的前提下，按迟到修订复算一版可交接的续批。"""
    if not parent:
        raise ReplayError("PARENT_NOT_FOUND", "原批记录不存在，无法回查，不能签发续批")
    if "续批" in parent:
        raise ReplayError("LINEAGE", "续批不能再作为原批续开，链路只允许一层")
    if parent["批次"].get("规则") != VERSION:
        raise ReplayError("RULE_DRIFT", f"原批规则 {parent['批次'].get('规则')} 与当前 {VERSION} 不一致")

    # 失败发生在任何写入之前：迟到修订解析或复算抛错时，原批与旧版指针都保持原样。
    late = compute(raw)

    conflicts = single_target_rows(late["排程"])
    if conflicts:
        raise ReplayError("TARGET_ROW_CONFLICT", f"补数目标行数互相矛盾：{conflicts[0]['分区']}")

    buckets = {state: [] for state, _ in DISPOSITIONS}
    for item in late["排程"]:
        buckets[item["状态"]].append(item["分区"])
    for bucket in buckets.values():
        bucket.sort()

    key = continuation_key(parent_key, raw)
    result = dict(late)
    result["批次"] = {
        **late["批次"],
        "编号": key,
        "种类": "续批",
        "原批编号": parent_key,
        "原批文件摘要": parent["批次"]["文件摘要"],
        "修订文件摘要": late["批次"]["文件摘要"],
    }
    result["续批"] = {
        "原批编号": parent_key,
        "原批文件摘要": parent["批次"]["文件摘要"],
        "修订文件摘要": late["批次"]["文件摘要"],
        "原批规则": parent["批次"]["规则"],
        "原批截止分区日": parent["批次"]["截止分区日"],
        "可继续使用": buckets["reuse"],
        "需要重做": buckets["write"],
        "证据不足": buckets["blocked"],
        "重复留证": late["重复"],
        "隔离留证": late["隔离"],
    }
    return key, result
