"""仅用于虚构案例的离线流程演示；不提供真实语义分析能力。"""
import json
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from .errors import RadarError
from .evidence import dump

TOPIC = "AI Agent 在企业客服中的应用"
OLD_TEXT = "常见咨询试点值得继续评估推广。"
NEW_TEXT = "内部试点尚未计入人工复核成本，当前证据不足以支持全面推广。"
AFTER_TEXT = "常见咨询试点值得继续评估推广，但需要先验证人工复核成本，当前证据不足以支持全面推广。"
DEMO_MARKDOWN = f"""# {TOPIC}

## 管理摘要

建议继续评估推广条件。

## 推广条件

{OLD_TEXT}

## 权限控制

权限配置方案仍在讨论中。
"""


def reference(doc_id, text):
    return {"document_id": doc_id, "segment_id": "p1", "quote": text}


def document(doc_id, text):
    return {"document_id": doc_id, "title": "虚构内部试点材料", "source_type": "internal",
            "segments": [{"segment_id": "p1", "text": text}], "metadata": {"fictional": True}}


def module(title, question, ref, module_id=None, gap=False):
    result = {
        "title": title, "core_question": question, "leadership_value": "支持试点范围及推进条件的判断",
        "scope_in": [question], "scope_out": ["未提供的公司事实"], "sub_questions": [question],
        "evidence_refs": [] if gap else [ref], "evidence_status": "gap" if gap else "limited",
        "missing_information": ["需要补充完整验证数据"], "priority_reason": "影响试点是否继续和如何扩大",
    }
    if module_id:
        result["module_id"] = module_id
    return result


def sample_decompose_request():
    from .schemas import DecomposeRequest
    dec = DecomposeRequest(topic=TOPIC, leadership_question="是否值得扩大试点？",
                           documents=[document("I_NEW", NEW_TEXT)])
    return dec


def sample_markdown_request():
    from .schemas import MarkdownUpdateRequest
    return MarkdownUpdateRequest.model_validate({
        "request_id": "markdown-update-demo-1", "report_path": "customer-service.md",
        "expected_version": 1,
        "adoption": {"adoption_id": "markdown-adoption-demo-1", "turn_id": "t2",
                     "answer_revision": "a1", "accepted_segment_ids": ["accepted"]},
        "current_turn": {"turn_id": "t2", "question": "那是不是可以推广到全部客服场景？",
                         "answer_revision": "a1", "answer_segments": [
                             {"segment_id": "accepted", "text": AFTER_TEXT},
                             {"segment_id": "not_accepted", "text": "另外可以重新设计权限配置方案。"}],
                         "documents": [document("I_NEW", NEW_TEXT)]},
        "previous_turn": {"question": "先从哪类咨询试点？", "answer": "先评估常见咨询场景。"},
    })


class DemoModel:
    mode = "demo"

    def generate(self, task, payload, schema):
        return schema.model_validate(self.response(task, deepcopy(payload)))

    def response(self, task, p):
        if p.get("topic", TOPIC) != TOPIC:
            raise RadarError("demo_only", "demo 模式只支持随附虚构案例，请配置真实模型处理其他 Topic")
        if task == "rank_documents":
            return {"assessments": [{
                "document_id": doc["document_id"], "relevance": 2, "decision_value": 2,
                "directness": 2, "applicability": 2, "freshness": 1,
                "evidence_role": "qualifies", "include": True,
                "reason": "内部试点材料直接限定推广判断",
            } for doc in p["documents"]]}
        if task == "extract_evidence":
            doc = p["document"]
            segment = doc["segments"][0]
            return {"units": [{"evidence_id": "e1", "proposition": segment["text"],
                               "proposition_type": "observation", "subject": "常见咨询试点",
                               "applicability": ["内部试点"], "qualifiers": ["缺少完整成本数据"],
                               "source_ref": {"document_id": doc["document_id"],
                                              "segment_id": segment["segment_id"], "quote": segment["text"]}}]}
        if task == "candidate_modules":
            ref = p["evidence_units"][0]["source_ref"]
            candidates = [
                module("推广条件", "客服试点是否值得推广及验证成本", ref),
                module("成本结构", "人工复核成本是否纳入测算", ref),
                module("质量验证", "还需要什么质量验证数据", ref, gap=True),
            ]
            for c in candidates:
                c.update(relevance=2, decision_value=2, evidence_quality=1, uniqueness=2)
            return {"topic_interpretation": TOPIC, "selected_lens": "新技术或能力",
                    "assumptions": ["虚构案例，仅演示流程"], "candidates": candidates}
        if task == "review_structure":
            fields = {"relevance", "decision_value", "evidence_quality", "uniqueness"}
            return {"modules": [{k: v for k, v in c.items() if k not in fields} for c in p["candidates"]],
                    "pending_questions": ["完整试点成本和质量结果是什么"],
                    "supplemental_search_requests": [{"query": "内部试点 人工复核 完整成本 质量数据",
                                                       "source_type": "internal", "reason": "补充推广判断依据"}],
                    "review_notes": ["按试点推广判断组织模块，保留质量数据缺口"]}
        if task == "resolve_scope":
            return {"resolved_question": "常见咨询客服试点的推广条件及人工复核成本", "constraints": ["常见咨询"], "ambiguities": []}
        if task == "extract_deltas":
            return {"deltas": [{"delta_id": "d_cost", "accepted_segment_ids": ["accepted"],
                                "proposition": "推广条件需先验证人工复核成本", "kind": "assessment",
                                "applicability": ["常见咨询"], "evidence_links": [
                                    {"ref": reference("I_NEW", NEW_TEXT), "relation": "qualifies"}],
                                "support_status": "supported", "reason": "内部材料说明成本测算存在缺口"}],
                    "unresolved_items": []}
        if task == "plan_update":
            if p.get("report_format") == "markdown":
                old = next(b for b in p["allowed_blocks"] if OLD_TEXT in b["text"] or b["text"] == AFTER_TEXT)
                if old["text"] == AFTER_TEXT:
                    return {"operations": [], "dispositions": [{
                        "delta_id": "d_cost", "outcome": "duplicate",
                        "existing_claim_ids": [old["block_id"]],
                        "reason": "报告已经包含相同推广限定",
                    }], "unresolved_items": []}
                return {"operations": [{
                    "op": "revise_block", "operation_id": "op_cost",
                    "target_block_id": old["block_id"], "expected_text_hash": old["text_hash"],
                    "after_text": AFTER_TEXT, "delta_ids": ["d_cost"],
                    "evidence_refs": [reference("I_NEW", NEW_TEXT)],
                    "reason": "内部材料说明人工复核成本尚未纳入，推广判断需要增加这一限定",
                    "location_reason": "该段正在回答试点是否值得推广，与本轮增量属于同一管理问题",
                    "decision_impact": "全面推广判断调整为先补充成本验证",
                }], "dispositions": [{"delta_id": "d_cost", "outcome": "change",
                                       "existing_claim_ids": [old["block_id"]],
                                       "reason": "旧段落缺少成本验证条件"}], "unresolved_items": []}
        if task == "review_dependencies":
            if p.get("report_format") == "markdown":
                return {"decisions": [{"target_block_id": item["block_id"],
                                        "expected_text_hash": item["text_hash"],
                                        "caused_by_operation_ids": p["allowed_operation_ids"],
                                        "action": "keep", "reason": "继续评估的摘要仍成立",
                                        "decision_impact": "管理摘要无需变化"}
                                       for item in p["targets"]]}
        if task == "review_update":
            if p.get("report_format") == "markdown":
                return {"verdict": "pass", "issues": [], "change_assessments": [
                    {"operation_id": change["operation_id"], "verdict": "reasonable",
                     "evidence_reason": "内部材料直接说明成本测算缺口，修改保留了限定语气",
                     "location_reason": "修改位于推广条件章节，和新增认识回答同一问题",
                     "limitations": ["仍需补充完整成本数据"]} for change in p["changes"]
                ]}
        raise ValueError(task)


def main():
    from fastapi.testclient import TestClient
    from .api import create_app
    from .store import Store
    decompose_request = sample_decompose_request()
    update_request = sample_markdown_request()
    with TemporaryDirectory() as directory:
        root = Path(directory) / "reports"
        root.mkdir()
        (root / update_request.report_path).write_text(DEMO_MARKDOWN, encoding="utf-8")
        with TestClient(create_app(DemoModel(), Store(Path(directory) / "demo.sqlite3"),
                                   root, Path(directory) / "versions")) as client:
            a = client.post("/decompose", json=dump(decompose_request))
            b = client.post("/update", json=dump(update_request))
            a.raise_for_status()
            b.raise_for_status()
            repeated = client.post("/update", json=dump(update_request))
            assert repeated.json() == b.json()
            output = {"mode": "demo", "notice": "虚构案例和预设模型输出，仅验证程序流程",
                      "decompose": a.json(), "update": b.json(), "idempotent_replay": True}
            Path("demo-output.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
            print("Demo passed: /decompose and Markdown /update; version 1 -> 2; replay unchanged.")
            print("Saved demo-output.json (fictional demo; no model service called).")


if __name__ == "__main__":
    main()
