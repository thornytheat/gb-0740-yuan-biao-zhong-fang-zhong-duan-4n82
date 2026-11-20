from app.config import MAX_BYTES
from app.keys import ReplayError
from app.modules.m8_continuation.service import build_continuation
from app.web import fixture, record, repository
from flask import Blueprint, jsonify, request, url_for

api = Blueprint("continuation", __name__)


@api.get("/api/continuation/<plan>")
def inspection(plan):
    result = record(plan)
    # 普通原批没有续批块时回空结构，页面显示续批入口而不是报错。
    return jsonify({"续批": result.get("续批")})


@api.post("/api/continuation")
def issue():
    args = request.get_json(silent=True) if request.is_json else request.form
    if not hasattr(args, "get"):
        raise ReplayError("JSON", "请选择原批与迟到修订")
    parent_key = args.get("parent")
    if not isinstance(parent_key, str) or not parent_key:
        raise ReplayError("PARENT", "请指定已签发的原批")
    store = repository()
    parent = store.get(parent_key)
    if not parent:
        raise ReplayError("PARENT_NOT_FOUND", "原批记录不存在，无法回查，不能签发续批")

    source = request.files.get("bundle")
    raw = source.read(MAX_BYTES + 1) if source and source.filename else fixture(args.get("sample", "dawn"))

    key, document = build_continuation(parent, parent_key, raw)
    # 构建全部成功之后才落库；重试时按编号收敛，旧版续批原样保留且不会被覆盖。
    saved, reused = store.save_continuation(key, document)
    return jsonify(
        id=key,
        reused=reused,
        parent=parent_key,
        disposition=saved["续批"],
        summary=saved["分区勾稽"],
        next=url_for("console", section="continuation", plan=key, sample=args.get("sample", "dawn")),
    ), 200 if reused else 201
