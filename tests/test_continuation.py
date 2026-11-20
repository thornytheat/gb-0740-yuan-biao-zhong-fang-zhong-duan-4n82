import json
from io import BytesIO

import pytest

from app.keys import ReplayError
from app.modules.m1_inventory.reader import read_bundle
from app.modules.m7_release.service import compute
from app.modules.m8_continuation.service import (
    build_continuation,
    continuation_key,
    single_target_rows,
)


@pytest.fixture(scope="session")
def dawn_raw():
    from app.config import ROOT

    return (ROOT / "sample_data" / "dawn.json").read_bytes()


@pytest.fixture(scope="session")
def parent_doc(night):
    return compute(night)


@pytest.fixture(scope="session")
def issued(parent_doc, night, dawn_raw):
    key, document = build_continuation(parent_doc, "parent-1", dawn_raw)
    return key, document


def test_three_buckets_partition_dispositions(issued):
    _, document = issued
    cont = document["续批"]
    # 下游已领走、现行指针与修订一致的分区：可继续使用，不中断补数。
    assert cont["可继续使用"] == ["orders@2026-11-17"]
    # 依赖新口径、需要重放的分区进入重做档。
    assert "orders@2026-11-18" in cont["需要重做"]
    assert "facts@2026-11-18" in cont["需要重做"]
    # 证据不足（陈旧 previous、缺上游快照）的分区不能进入续批。
    assert "orders@2026-11-19" in cont["证据不足"]
    assert "facts@2026-11-19" in cont["证据不足"]
    dispositioned = set(cont["可继续使用"] + cont["需要重做"] + cont["证据不足"])
    assert dispositioned == {task["分区"] for task in document["排程"]}


def test_unprocessed_duplicates_and_conflicts_kept_as_evidence(issued):
    _, document = issued
    cont = document["续批"]
    assert cont["重复留证"] == [
        {"源行": 2, "分区": "orders@2026-11-17", "原因码": "DUPLICATE"}
    ]
    conflict_lines = [row["源行"] for row in cont["隔离留证"] if row["原因码"] == "CONFLICT"]
    assert conflict_lines == [5, 6]
    reason_codes = {row["原因码"] for row in cont["隔离留证"]}
    assert {"CONFLICT", "INVALID_CHANGE", "NOT_SOURCE", "DATE_LIMIT"} <= reason_codes
    # 留证行不得混入任何处置档。
    bucketed = set(cont["可继续使用"] + cont["需要重做"] + cont["证据不足"])
    assert "orders@2026-11-20" not in bucketed


def test_continuation_retry_is_idempotent_and_creates_one_record(client, night, dawn_raw):
    parent = client.post("/api/plans", json={"sample": "nightly"})
    payload = {"sample": "dawn", "parent": parent.json["id"]}
    first = client.post("/api/continuation", json=payload)
    second = client.post("/api/continuation", json=payload)
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json["id"] == second.json["id"] and second.json["reused"]
    # 普通批次列表里仍只有原批，续批不冒名顶替。
    assert client.get("/api/plans").json == [parent.json["summary"]] or len(
        client.get("/api/plans").json
    ) == 1


def test_continuation_key_is_deterministic(night, dawn_raw):
    key_a = continuation_key("parent-1", dawn_raw)
    key_b = continuation_key("parent-1", dawn_raw)
    key_other_parent = continuation_key("parent-2", dawn_raw)
    assert key_a == key_b and key_a.startswith("cont-")
    assert key_other_parent != key_a


def test_failed_issue_keeps_parent_and_old_version(client, night):
    parent = client.post("/api/plans", json={"sample": "nightly"})
    before = client.get(f"/api/export/{parent.json['id']}").data
    # 破损修订：在任何落库之前失败。
    response = client.post(
        "/api/continuation",
        data={"parent": parent.json["id"], "bundle": (BytesIO(b"{"), "broken.json")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 422 and response.json["code"] == "JSON"
    # 原批记录字节不变，仍可回查。
    assert client.get(f"/api/export/{parent.json['id']}").data == before
    # 没有任何续批残留。
    assert client.get("/api/plans").json and not client.get("/api/plans").json[0].get("续批")


def test_missing_parent_is_refused(client, dawn_raw):
    response = client.post(
        "/api/continuation",
        data={"parent": "ghost", "bundle": (BytesIO(dawn_raw), "dawn.json")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 422 and response.json["code"] == "PARENT_NOT_FOUND"


def test_continuation_cannot_be_recontinued(client, night, dawn_raw):
    parent = client.post("/api/plans", json={"sample": "nightly"}).json
    cont = client.post(
        "/api/continuation", json={"sample": "dawn", "parent": parent["id"]}
    ).json
    again = client.post(
        "/api/continuation", json={"sample": "dawn", "parent": cont["id"]}
    )
    assert again.status_code == 422 and again.json["code"] == "LINEAGE"


def test_rule_drift_rejected(parent_doc, dawn_raw):
    drifted = json.loads(json.dumps(parent_doc, ensure_ascii=False))
    drifted["批次"]["规则"] = "replay-0.9"
    with pytest.raises(ReplayError) as exc:
        build_continuation(drifted, "parent-1", dawn_raw)
    assert exc.value.code == "RULE_DRIFT"


def test_conflicting_target_rows_for_one_partition_rejected(parent_doc, dawn_raw):
    tasks = [
        {"分区": "facts@2026-11-18", "状态": "write", "计划": {"version": "v1", "rows": 100}},
        {"分区": "facts@2026-11-18", "状态": "write", "计划": {"version": "v2", "rows": 120}},
    ]
    assert single_target_rows(tasks) == [
        {"分区": "facts@2026-11-18", "目标行数": [100, 120]}
    ]

    # 同一源分区登记两条互相矛盾的有效修订，整组隔离：冲突分区不进续批，
    # 没有任何分区被写入，自然不会出现两个互相矛盾的补数目标行数。
    data = read_bundle(dawn_raw)
    data["changes"] = [
        {"dataset": "orders", "day": "2026-11-18", "version": "dawn18", "rows": 24, "previous": "s18"},
        {"dataset": "orders", "day": "2026-11-18", "version": "dawn19", "rows": 25, "previous": "s18"},
    ]
    _, document = build_continuation(parent_doc, "parent-1", json.dumps(data).encode())
    assert document["分区勾稽"]["待写"] == 0
    assert single_target_rows(document["排程"]) == []
    assert [row["源行"] for row in document["续批"]["隔离留证"]] == [1, 2]
    assert all(row["原因码"] == "CONFLICT" for row in document["续批"]["隔离留证"])


def test_guard_blocks_document_with_two_contradictory_targets(parent_doc, dawn_raw, monkeypatch):
    # 纵深防御：即便上游漏过矛盾目标，签发前也要整单拒绝，不产生半截续批。
    from app.modules.m8_continuation import service

    def broken(raw):
        result = compute(raw)
        result["排程"].append(
            {"分区": "ghost@2026-11-18", "状态": "write", "计划": {"version": "gx", "rows": 7}}
        )
        result["排程"].append(
            {"分区": "ghost@2026-11-18", "状态": "write", "计划": {"version": "gy", "rows": 9}}
        )
        return result

    monkeypatch.setattr(service, "compute", broken)
    with pytest.raises(ReplayError) as exc:
        service.build_continuation(parent_doc, "parent-1", dawn_raw)
    assert exc.value.code == "TARGET_ROW_CONFLICT"


def test_identical_target_rows_are_not_conflicts():
    tasks = [
        {"分区": "a@2026-11-18", "状态": "write", "计划": {"version": "v1", "rows": 50}},
        {"分区": "a@2026-11-18", "状态": "write", "计划": {"version": "v2", "rows": 50}},
    ]
    assert single_target_rows(tasks) == []


def test_rollback_evidence_preserves_old_version(issued):
    _, document = issued
    evidence = document["回滚"]
    assert evidence["恢复一致"]
    assert evidence["演练前摘要"] == evidence["回滚摘要"] != evidence["演练后摘要"]
    assert evidence["影响外变动"] == []


def test_lineage_attestation_points_back_to_parent(issued):
    key, document = issued
    cont = document["续批"]
    assert document["批次"]["编号"] == key
    assert document["批次"]["种类"] == "续批"
    assert cont["原批编号"] == "parent-1"
    assert cont["原批文件摘要"] == document["续批"]["原批文件摘要"]
    assert document["批次"]["原批编号"] == "parent-1"


def test_continuation_view_and_api_render(client, night, dawn_raw):
    from app.modules import VIEWS

    parent = client.post("/api/plans", json={"sample": "nightly"}).json
    cont = client.post(
        "/api/continuation", json={"sample": "dawn", "parent": parent["id"]}
    ).json
    assert "continuation" in VIEWS
    api = client.get(f"/api/continuation/{cont['id']}")
    assert api.status_code == 200 and set(api.json) == {"续批"}
    page = client.get(f"/console/continuation?plan={cont['id']}")
    assert page.status_code == 200 and "可继续使用".encode() in page.data
    # 原批视角显示入口说明，而不是处置表。
    entry = client.get(f"/console/continuation?plan={parent['id']}")
    assert entry.status_code == 200 and "续开一批".encode() in entry.data


def test_uploaded_revision_converges_with_sample(client, night, dawn_raw):
    parent = client.post("/api/plans", json={"sample": "nightly"}).json
    via_sample = client.post(
        "/api/continuation", json={"sample": "dawn", "parent": parent["id"]}
    ).json
    via_upload = client.post(
        "/api/continuation",
        data={"parent": parent["id"], "bundle": (BytesIO(dawn_raw), "dawn.json")},
        content_type="multipart/form-data",
    ).json
    assert via_sample["id"] == via_upload["id"] and via_upload["reused"]


def test_parent_record_bytes_are_never_rewritten(client, tmp_path, night, dawn_raw):
    from app.modules.m7_release.store import Catalog

    store = Catalog(tmp_path / "lineage.sqlite")
    parent, _ = store.save("parent-x", compute(night))
    import sqlite3

    before = sqlite3.connect(store.path).execute(
        "SELECT document FROM plans WHERE id=?", ("parent-x",)
    ).fetchone()[0]
    key, document = build_continuation(parent, "parent-x", dawn_raw)
    store.save_continuation(key, document)
    # 原批被误删就无法回查：续批只新增自己的行，原批行的字节保持不变。
    after = sqlite3.connect(store.path).execute(
        "SELECT document FROM plans WHERE id=?", ("parent-x",)
    ).fetchone()[0]
    assert before == after
    reloaded = store.get("parent-x")
    assert reloaded == parent and "续批" not in reloaded
    assert store.get(key)["续批"]["原批编号"] == "parent-x"


def test_continuation_kept_out_of_release_listing(client, night, dawn_raw):
    parent = client.post("/api/plans", json={"sample": "nightly"}).json
    client.post("/api/continuation", json={"sample": "dawn", "parent": parent["id"]})
    listing = client.get("/api/plans").json
    assert [item["编号"] for item in listing] == [parent["id"]]
    assert all(item.get("种类") != "续批" for item in listing)


def test_over_limit_revision_rejected_without_side_effect(client, night):
    parent = client.post("/api/plans", json={"sample": "nightly"}).json
    big = b"{ " + b", ".join(b'"a%d": 1' % i for i in range(40000)) + b"}"
    response = client.post(
        "/api/continuation",
        data={"parent": parent["id"], "bundle": (BytesIO(big), "big.json")},
        content_type="multipart/form-data",
    )
    assert response.status_code in (413, 422)
    assert [item["编号"] for item in client.get("/api/plans").json] == [parent["id"]]


def test_parallel_continuation_converges_to_one_record(tmp_path, parent_doc, dawn_raw):
    from concurrent.futures import ThreadPoolExecutor

    from app.modules.m7_release.store import Catalog

    store = Catalog(tmp_path / "parallel.sqlite")
    store.save("parent-1", parent_doc)
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(
            pool.map(
                lambda _: (
                    lambda built: store.save_continuation(*built)
                )(build_continuation(parent_doc, "parent-1", dawn_raw)),
                range(4),
            )
        )
    keys = {document["批次"]["编号"] for document, _ in outcomes}
    assert keys == {continuation_key("parent-1", dawn_raw)}
    assert sum(not reused for _, reused in outcomes) == 1
    rows = store.connection().execute("SELECT COUNT(*) FROM plans").fetchone()[0]
    assert rows == 2  # 原批一行 + 续批一行
