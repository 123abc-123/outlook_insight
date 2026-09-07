import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from threading import Barrier

import httpx
import pytest
from fastapi.testclient import TestClient

from radar.api import create_app
from radar.config import RadarConfig, load_config
from radar.demo import (AFTER_TEXT, DEMO_MARKDOWN, NEW_TEXT, DemoModel, sample_decompose_request,
                        reference, sample_markdown_request)
from radar.errors import RadarError
from radar.evidence import dump
from radar.model import HTTPModel, UnconfiguredModel, model_from_config
from radar.schemas import MarkdownReview, ModuleDraft
from radar.store import Store


class ControlledModel(DemoModel):
    mode = "test"

    def __init__(self, overrides=None):
        self.overrides = overrides or {}
        self.calls = []

    def response(self, task, payload):
        self.calls.append((task, deepcopy(payload)))
        value = super().response(task, payload)
        if task in self.overrides:
            return self.overrides[task](value, payload)
        return value


@pytest.fixture
def markdown_setup(tmp_path):
    report_root = tmp_path / "reports"
    history_root = tmp_path / "versions"
    report_root.mkdir()
    source = report_root / "customer-service.md"
    source.write_text(DEMO_MARKDOWN, encoding="utf-8")
    model = ControlledModel()
    store = Store(tmp_path / "radar.sqlite3")
    app = create_app(model, store, report_root, history_root)
    return model, store, app, source, history_root, sample_markdown_request()


def test_exactly_two_business_routes(markdown_setup):
    _, _, app, _, _, _ = markdown_setup
    assert set(app.openapi()["paths"]) == {"/decompose", "/update"}


def test_decompose_deduplicates_documents_and_marks_gaps(markdown_setup):
    model, _, app, _, _, _ = markdown_setup
    req = sample_decompose_request()
    duplicate = req.documents[0].model_copy(deep=True)
    duplicate.document_id = "copy"
    req.documents.append(duplicate)
    with TestClient(app) as client:
        response = client.post("/decompose", json=dump(req))
    assert response.status_code == 200
    assert response.json()["status"] == "provisional"
    assert response.json()["themes"] == [item["title"] for item in response.json()["modules"]]
    assert all(len(title) <= 24 for title in response.json()["themes"])
    assert len([task for task, _ in model.calls if task == "extract_evidence"]) == 1
    assert any(module["evidence_status"] == "gap" for module in response.json()["modules"])


def test_decompose_without_documents_does_not_call_model(markdown_setup):
    model, _, app, _, _, _ = markdown_setup
    with TestClient(app) as client:
        response = client.post("/decompose", json={"topic": "测试", "documents": []})
    assert response.json()["status"] == "insufficient_input"
    assert model.calls == []


def test_decompose_uses_family_deduplication_and_stable_module_ids(markdown_setup):
    model, _, app, _, _, _ = markdown_setup
    req = sample_decompose_request()
    req.documents[0].family_id = "family-1"
    family_copy = req.documents[0].model_copy(deep=True)
    family_copy.document_id = "family-copy"
    family_copy.segments[0].text += "转载页面增加的编辑说明。"
    req.documents.append(family_copy)
    with TestClient(app) as client:
        first = client.post("/decompose", json=dump(req)).json()
        second = client.post("/decompose", json=dump(req)).json()
    assert len([task for task, _ in model.calls if task == "extract_evidence"]) == 2
    assert [item["module_id"] for item in first["modules"]] == [
        item["module_id"] for item in second["modules"]
    ]
    assert first["structure_version"] == second["structure_version"]


def test_decompose_receives_company_context_and_source_policy(markdown_setup):
    model, store, _, source, history_root, _ = markdown_setup
    app = create_app(model, store, source.parent, history_root, organization_context={
        "organization_name": "长鑫存储", "industry": "DRAM",
        "business_priorities": ["量产成熟度"],
        "leadership_focus_by_topic_type": {"新技术或能力": ["技术成熟度与量产窗口"]},
    })
    with TestClient(app) as client:
        assert client.post("/decompose", json=dump(sample_decompose_request())).status_code == 200
    payload = next(payload for task, payload in model.calls if task == "candidate_modules")
    assert payload["organization_context"]["organization_name"] == "长鑫存储"
    assert payload["organization_context"]["leadership_focus_by_topic_type"]["新技术或能力"]
    assert "hard_gate" in payload["source_policy"]
    assert ModuleDraft.model_json_schema()["properties"]["title"]["maxLength"] == 24


def test_decompose_with_only_evidence_gaps_is_insufficient(markdown_setup):
    _, store, _, source, history_root, _ = markdown_setup

    def gaps_only(value, payload):
        value["modules"] = value["modules"][:2]
        for item in value["modules"]:
            item.update(evidence_refs=[], evidence_status="gap",
                        missing_information=["缺少可用于形成正式主题的直接材料"])
        return value

    app = create_app(ControlledModel({"review_structure": gaps_only}), store,
                     source.parent, history_root)
    with TestClient(app) as client:
        result = client.post("/decompose", json=dump(sample_decompose_request())).json()
    assert result["status"] == "insufficient_input"


def test_markdown_update_creates_versions_and_explainable_change(markdown_setup):
    model, _, app, source, history_root, req = markdown_setup
    with TestClient(app) as client:
        response = client.post("/update", json=dump(req))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "updated" and body["base_version"] == 1 and body["new_version"] == 2
    before, after, change_log = map(Path, (
        body["before_report_path"], body["after_report_path"], body["change_log_path"]
    ))
    assert before.name == "v000001.md" and after.name == "v000002.md"
    assert before.read_text(encoding="utf-8") == DEMO_MARKDOWN
    assert AFTER_TEXT in after.read_text(encoding="utf-8")
    assert source.read_text(encoding="utf-8") == DEMO_MARKDOWN
    assert {path.name for path in history_root.rglob("v*.md")} == {
        "v000001.md", "v000002.md", "v000002_changes.md"
    }
    change = body["changes"][0]
    assert change["before"] in DEMO_MARKDOWN and change["after"] == AFTER_TEXT
    assert change["location_reason"] and change["decision_impact"]
    assert change["evidence_refs"][0]["quote"]
    assert change["review"]["verdict"] == "reasonable"
    log = change_log.read_text(encoding="utf-8")
    assert all(label in log for label in ("为什么修改这里", "合理性审查", "支持证据", "保留限制"))
    assert all("not_accepted" not in str(payload) for _, payload in model.calls)


def test_next_update_reads_latest_file_and_preserves_history(markdown_setup):
    _, _, app, _, history_root, req = markdown_setup
    with TestClient(app) as client:
        first = client.post("/update", json=dump(req)).json()
        next_req = req.model_copy(deep=True)
        next_req.request_id = "update-2"
        next_req.adoption.adoption_id = "adoption-2"
        next_req.expected_version = 2
        second = client.post("/update", json=dump(next_req)).json()
    assert first["new_version"] == 2 and second["status"] == "no_change"
    assert Path(second["before_report_path"]).name == "v000002.md"
    assert Path(second["after_report_path"]).name == "v000002.md"
    assert {path.name for path in history_root.rglob("v*.md")} == {
        "v000001.md", "v000002.md", "v000002_changes.md"
    }


def test_sensitive_update_previews_full_report_then_commits_exact_candidate(markdown_setup):
    _, store, _, source, history_root, req = markdown_setup

    def revise_summary(value, payload):
        for decision in value["decisions"]:
            decision.update(action="revise", after_text="建议先验证人工复核成本，再判断推广范围。")
        return value

    app = create_app(ControlledModel({"review_dependencies": revise_summary}), store,
                     source.parent, history_root)
    with TestClient(app) as client:
        preview = client.post("/update", json=dump(req)).json()
        assert preview["status"] == "preview_ready"
        assert preview["confirmation_required"] is True
        assert AFTER_TEXT in preview["candidate_report"]
        assert not list(history_root.rglob("v000002.md"))
        commit = client.post("/update", json={
            "action": "commit", "request_id": "commit-preview-1",
            "report_path": req.report_path, "preview_id": preview["preview_id"],
            "confirmed": True,
        }).json()
    assert commit["status"] == "updated" and commit["new_version"] == 2
    assert Path(commit["after_report_path"]).read_text(encoding="utf-8") == preview["candidate_report"]
    assert source.read_text(encoding="utf-8") == DEMO_MARKDOWN


def test_historical_documents_are_separate_evidence_input(markdown_setup):
    model, _, app, _, _, req = markdown_setup
    historical = req.current_turn.documents[0].model_copy(deep=True)
    historical.document_id = "I_HISTORY"
    historical.segments[0].text = "历史试点曾假设人工复核成本可以忽略。"
    req.historical_documents = [historical]
    with TestClient(app) as client:
        assert client.post("/update", json=dump(req)).status_code == 200
    payload = next(payload for task, payload in model.calls if task == "extract_deltas")
    assert [doc["document_id"] for doc in payload["new_documents"]] == ["I_NEW"]
    assert [doc["document_id"] for doc in payload["historical_documents"]] == ["I_HISTORY"]
    assert payload["source_policy"]["independence_rule"]


def test_structure_change_is_preview_only_until_confirmed(markdown_setup):
    _, store, _, source, history_root, req = markdown_setup

    def add_section(value, payload):
        root = next(item for item in payload["allowed_headings"] if item["heading_level"] == 1)
        return {"operations": [{
            "op": "append_section", "operation_id": "op_new_module",
            "target_heading_id": root["block_id"], "expected_text_hash": root["text_hash"],
            "heading_title": "成本验证与观察信号", "heading_level": 2,
            "after_text": "人工复核成本需要在扩大推广前完成验证。",
            "delta_ids": ["d_cost"], "evidence_refs": [reference("I_NEW", NEW_TEXT)],
            "reason": "新增认识无法由现有章节完整承载",
            "location_reason": "该章节与推广条件同属报告一级管理议题",
            "decision_impact": "将成本验证提升为独立的管理判断模块",
        }], "dispositions": [{"delta_id": "d_cost", "outcome": "change",
                               "existing_claim_ids": [], "reason": "需要新增管理议题"}],
                "unresolved_items": []}

    req.allow_structure_change = True
    app = create_app(ControlledModel({"plan_update": add_section}), store,
                     source.parent, history_root)
    with TestClient(app) as client:
        preview = client.post("/update", json=dump(req)).json()
        assert preview["status"] == "preview_ready"
        assert preview["changes"][0]["change_type"] == "append_section"
        assert "## 成本验证与观察信号" in preview["candidate_report"]
        assert not list(history_root.rglob("v000002.md"))


def test_stale_preview_cannot_be_committed(markdown_setup):
    _, _, app, _, _, req = markdown_setup
    req.confirmation_policy = "always"
    other = req.model_copy(deep=True)
    other.request_id = "preview-other"
    other.adoption.adoption_id = "preview-adoption-other"
    with TestClient(app) as client:
        first = client.post("/update", json=dump(req)).json()
        second = client.post("/update", json=dump(other)).json()
        committed = client.post("/update", json={
            "action": "commit", "request_id": "commit-first", "report_path": req.report_path,
            "preview_id": first["preview_id"], "confirmed": True,
        })
        stale = client.post("/update", json={
            "action": "commit", "request_id": "commit-stale", "report_path": req.report_path,
            "preview_id": second["preview_id"], "confirmed": True,
        })
    assert committed.status_code == 200 and committed.json()["status"] == "updated"
    assert stale.status_code == 409 and stale.json()["status"] == "version_conflict"


def test_repeated_request_and_adoption_are_idempotent(markdown_setup):
    model, _, app, _, _, req = markdown_setup
    with TestClient(app) as client:
        first = client.post("/update", json=dump(req)).json()
        calls = len(model.calls)
        assert client.post("/update", json=dump(req)).json() == first
        retry = req.model_copy(update={"request_id": "new-request-key"})
        assert client.post("/update", json=dump(retry)).json() == first
    assert len(model.calls) == calls


def test_stale_version_cannot_overwrite_latest(markdown_setup):
    _, _, app, _, _, req = markdown_setup
    with TestClient(app) as client:
        assert client.post("/update", json=dump(req)).status_code == 200
        stale = req.model_copy(deep=True)
        stale.request_id = "stale-request"
        stale.adoption.adoption_id = "stale-adoption"
        response = client.post("/update", json=dump(stale))
    assert response.status_code == 409
    assert response.json()["status"] == "version_conflict"


def test_concurrent_updates_only_create_one_next_version(markdown_setup):
    _, store, _, source, history_root, req = markdown_setup
    barrier = Barrier(2)

    def wait_at_review(value, payload):
        barrier.wait(timeout=10)
        return value

    def run(index):
        local = req.model_copy(deep=True)
        local.request_id = f"parallel-{index}"
        local.adoption.adoption_id = f"parallel-adoption-{index}"
        app = create_app(ControlledModel({"review_update": wait_at_review}), store,
                         source.parent, history_root)
        with TestClient(app) as client:
            return client.post("/update", json=dump(local)).json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, [1, 2]))
    assert {result["status"] for result in results} == {"updated", "version_conflict"}
    assert len(list(history_root.rglob("v000002.md"))) == 1


@pytest.mark.parametrize("path", ["../secret.md", "C:/secret.md", "report.txt"])
def test_report_path_is_confined_to_configured_directory(markdown_setup, path):
    _, _, app, _, _, req = markdown_setup
    body = dump(req)
    body["report_path"] = path
    with TestClient(app) as client:
        response = client.post("/update", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_report_path"


@pytest.mark.parametrize("problem", ["quote", "number", "target", "hash", "heading"])
def test_invalid_markdown_patch_never_creates_new_version(markdown_setup, problem):
    _, store, _, source, history_root, req = markdown_setup

    def corrupt(value, payload):
        operation = value["operations"][0]
        if problem == "quote":
            operation["evidence_refs"][0]["quote"] = "伪造引文"
        elif problem == "number":
            operation["after_text"] += "成本下降99%。"
        elif problem == "target":
            operation["target_block_id"] = "b_unrelated"
        elif problem == "hash":
            operation["expected_text_hash"] = "0" * 64
        else:
            operation["after_text"] += "\n## 越界增加的章节"
        return value

    app = create_app(ControlledModel({"plan_update": corrupt}), store, source.parent, history_root)
    with TestClient(app) as client:
        response = client.post("/update", json=dump(req))
    assert response.status_code == 422 and response.json()["status"] == "failed"
    assert {path.name for path in history_root.rglob("v*.md")} == {"v000001.md"}


def test_unreasonable_change_does_not_create_new_version(markdown_setup):
    _, store, _, source, history_root, req = markdown_setup

    def reject(value, payload):
        return {"verdict": "reject", "change_assessments": [],
                "issues": ["该证据不足以支持修改后的推广判断"]}

    app = create_app(ControlledModel({"review_update": reject}), store, source.parent, history_root)
    with TestClient(app) as client:
        response = client.post("/update", json=dump(req))
    assert response.status_code == 422
    assert response.json()["status"] == "evidence_insufficient"
    assert {path.name for path in history_root.rglob("v*.md")} == {"v000001.md"}


def test_invalid_adoption_is_rejected_before_model(markdown_setup):
    model, _, app, _, _, req = markdown_setup
    body = dump(req)
    body["adoption"]["answer_revision"] = "different"
    with TestClient(app) as client:
        assert client.post("/update", json=body).status_code == 422
    assert model.calls == []


def test_unconfigured_model_fails_explicitly(markdown_setup):
    _, store, _, source, history_root, _ = markdown_setup
    app = create_app(UnconfiguredModel(), store, source.parent, history_root)
    with TestClient(app) as client:
        response = client.post("/decompose", json=dump(sample_decompose_request()))
    assert response.status_code == 503


def test_repository_config_is_safe_and_complete():
    config = load_config("config/radar.toml")
    assert config.app.model_mode == "unconfigured"
    assert config.llm_profiles["default"].api_key == ""
    assert config.reports.root_dir == "reports"
    assert config.organization.organization_name == "长鑫存储"
    assert set(config.organization.leadership_focus_by_topic_type) == {
        "新技术或能力", "行业市场变化", "政策变化", "竞争对象", "经营问题",
    }
    assert set(config.tasks) == {
        "extract_evidence", "candidate_modules", "review_structure", "resolve_scope",
        "extract_deltas", "plan_update", "review_dependencies", "review_update",
    }


def live_config():
    raw = {
        "app": {"model_mode": "live", "database_path": "data/test.sqlite3"},
        "llm_profiles": {"default": {
            "url": "http://internal.test/v1/chat/completions", "model": "model-a",
            "api_key_env": "COMPANY_MODEL_KEY", "temperature": 0.4, "max_tokens": 1000,
        }},
        "tasks": {task: {"profile": "default"} for task in (
            "extract_evidence", "candidate_modules", "review_structure", "resolve_scope",
            "extract_deltas", "plan_update", "review_dependencies", "review_update")},
    }
    raw["tasks"]["review_update"].update({"temperature": 0.0, "max_tokens": 321})
    return RadarConfig.model_validate(raw)


def test_model_uses_task_parameters_and_environment_key(monkeypatch):
    monkeypatch.setenv("COMPANY_MODEL_KEY", "secret-for-test")
    model = model_from_config(live_config())
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content":
            '{"verdict":"pass","change_assessments":[],"issues":[]}'}}]})

    model.transport = httpx.MockTransport(handler)
    model.generate("review_update", {"test": True}, MarkdownReview)
    body = json.loads(seen[0].content)
    assert body["model"] == "model-a" and body["temperature"] == 0.0 and body["max_tokens"] == 321
    assert seen[0].headers["authorization"] == "Bearer secret-for-test"


def test_http_model_repairs_schema_once_and_never_truncates():
    seen = []

    def handler(request):
        seen.append(request)
        content = '{"invalid":true}' if len(seen) == 1 else \
            '{"verdict":"pass","change_assessments":[],"issues":[]}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    model = HTTPModel("http://internal.test", "internal", transport=httpx.MockTransport(handler))
    assert model.generate("review_update", {"test": True}, MarkdownReview).verdict == "pass"
    assert len(seen) == 2
    with pytest.raises(RadarError, match="上下文超限"):
        HTTPModel("http://internal.test", "internal", max_context_chars=1).generate(
            "review_update", {"source": "full source"}, MarkdownReview
        )
