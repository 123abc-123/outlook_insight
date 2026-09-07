from .errors import RadarError
from .evidence import digest, dump, merge_documents, rank_documents, validate_ref
from .markdown import (apply_dependency_decisions, apply_operations, attach_reviews,
                       is_sensitive_change, parse_markdown, report_topic, retrieve_blocks, summary_targets,
                       validate_plan)
from .prompts import SOURCE_POLICY
from .schemas import (DeltaResult, MarkdownDependencyReview, MarkdownPlan, MarkdownReview,
                      MarkdownUpdateResult, ScopeResult, unique)


def validate_deltas(extracted, accepted_ids, documents):
    unique([delta.delta_id for delta in extracted.deltas], "认知增量")
    covered = set()
    for delta in extracted.deltas:
        if not set(delta.accepted_segment_ids) <= accepted_ids:
            raise RadarError("invalid_delta", "认知增量引用了未采纳的回答片段")
        covered.update(delta.accepted_segment_ids)
        for link in delta.evidence_links:
            validate_ref(link.ref, documents)
        if delta.kind != "user_constraint" and delta.support_status == "supported":
            if not any(link.relation in {"supports", "qualifies"} for link in delta.evidence_links):
                raise RadarError("invalid_delta", "声称已有支持的认知增量缺少有效证据")
    if extracted.deltas and covered != accepted_ids:
        raise RadarError("invalid_delta", "认知增量提取遗漏了被采纳片段")


def commit_markdown_report_update(req, versions):
    """提交已经展示并由用户确认的候选报告；本函数不会调用模型重新生成内容。"""
    return versions.commit_preview(req)


def prepare_markdown_report_update(req, model, versions, organization_context=None):
    """生成更新；普通正文可自动提交，核心结论、建议或结构变化必须返回待确认版本。"""
    previous = versions.lookup(req)
    if previous:
        return previous
    try:
        report_id, base_version, before_path, content = versions.load_base(req)
    except RadarError as exc:
        if exc.code == "version_conflict":
            # 尚未加载内容，不登记一次不可复用的旧版本请求。
            relative = versions.resolve_source(req.report_path)[1]
            return MarkdownUpdateResult(
                status="version_conflict", request_id=req.request_id,
                report_id=versions.report_id_for(relative), base_version=req.expected_version or 0,
                unresolved_items=[exc.message],
            )
        raise
    base_hash = digest(content)

    def result(status, **kwargs):
        return MarkdownUpdateResult(
            status=status, request_id=req.request_id, report_id=report_id,
            base_version=base_version, before_report_path=str(before_path), **kwargs,
        )

    accepted_ids = set(req.adoption.accepted_segment_ids)
    accepted = [dump(s) for s in req.current_turn.answer_segments if s.segment_id in accepted_ids]
    blocks = parse_markdown(content)
    if not blocks:
        return versions.finish(req, result("failed", unresolved_items=["报告中没有可更新的 Markdown 内容块"]), base_hash)
    topic = report_topic(blocks, versions.resolve_source(req.report_path)[0].stem)
    new_documents = merge_documents(req.current_turn.documents)
    historical_documents = merge_documents(req.historical_documents)
    documents = merge_documents(historical_documents, new_documents)
    context = {
        "topic": topic, "question": req.current_turn.question,
        "accepted_segments": accepted, "previous_turn_context_only": dump(req.previous_turn),
        "organization_context": dump(organization_context),
    }
    try:
        scope = model.generate("resolve_scope", context, ScopeResult)
        if scope.ambiguities:
            return versions.finish(req, result("needs_clarification", unresolved_items=scope.ambiguities), base_hash)
        documents, document_assessments = rank_documents(model, documents, {
            **context,
            "task_context": "markdown_report_update",
            "resolved_scope": dump(scope),
            "source_policy": SOURCE_POLICY,
        })
        selected_ids = {document.document_id for document in documents}
        new_documents = [document for document in new_documents if document.document_id in selected_ids]
        historical_documents = [
            document for document in historical_documents if document.document_id in selected_ids
        ]
        extracted = model.generate("extract_deltas", {
            "scope": dump(scope), "accepted_segments": accepted,
            "new_documents": [dump(d) for d in new_documents],
            "historical_documents": [dump(d) for d in historical_documents],
            "document_assessments": document_assessments,
            "source_policy": SOURCE_POLICY,
            "note": "历史文献用于复核旧观点基础；本轮采纳内容仍是产生更新意图的唯一来源",
        }, DeltaResult)
        validate_deltas(extracted, accepted_ids, documents)
        unresolved = list(extracted.unresolved_items)
        unresolved.extend(delta.reason for delta in extracted.deltas if delta.support_status != "supported")
        if unresolved:
            return versions.finish(req, result("evidence_insufficient", unresolved_items=unresolved), base_hash)
        allowed_blocks, allowed_headings = retrieve_blocks(blocks, scope, extracted.deltas)
        if req.allow_structure_change:
            known = {heading.block_id for heading in allowed_headings}
            allowed_headings.extend(
                heading for heading in blocks
                if heading.kind == "heading" and heading.heading_level == 1
                and heading.block_id not in known
            )
        if extracted.deltas and not allowed_blocks and not allowed_headings:
            return versions.finish(req, result("needs_clarification", unresolved_items=[
                "没有找到与本轮认知增量对应的报告章节，请明确希望更新的主题范围"
            ]), base_hash)
        plan = model.generate("plan_update", {
            "report_format": "markdown", "scope": dump(scope),
            "accepted_segments": accepted, "deltas": [dump(d) for d in extracted.deltas],
            "allowed_blocks": [dump(b) for b in allowed_blocks],
            "allowed_headings": [dump(b) for b in allowed_headings],
            "documents": [dump(d) for d in documents],
            "document_assessments": document_assessments,
            "source_policy": SOURCE_POLICY,
            "allow_structure_change": req.allow_structure_change,
            "operation_rules": {
                "revise_block": "修改一个允许的正文块，保留不受影响的原意",
                "append_to_section": "在允许的现有章节末尾增加一个正文块",
                "append_section": ("仅在 allow_structure_change=true 时，在允许的父章节下新增直接子章节；"
                                   "该操作必须由用户确认后才能提交"),
                "rename_section": ("仅在 allow_structure_change=true 时修改允许章节的名称；"
                                   "该操作必须由用户确认后才能提交"),
                "forbidden": ["删除章节", "移动章节", "修改范围外的正文"],
            },
        }, MarkdownPlan)
        if plan.unresolved_items:
            return versions.finish(req, result("evidence_insufficient", unresolved_items=plan.unresolved_items), base_hash)
        validate_plan(plan, extracted.deltas, allowed_blocks, allowed_headings, documents,
                      req.allow_structure_change)
        candidate, changes = apply_operations(content, blocks, plan.operations, extracted.deltas)
        direct_ids = {op.operation_id for op in plan.operations}
        dependency_review = MarkdownDependencyReview(decisions=[])
        targets = summary_targets(parse_markdown(candidate)) if direct_ids else []
        if targets:
            dependency_review = model.generate("review_dependencies", {
                "report_format": "markdown", "direct_changes": [dump(c) for c in changes],
                "changed_report": candidate, "targets": [dump(b) for b in targets],
                "allowed_operation_ids": sorted(direct_ids),
                "instruction": "逐项判断摘要或建议是否仍成立；只在正文判断改变导致不一致时修改",
            }, MarkdownDependencyReview)
            candidate, changes = apply_dependency_decisions(
                candidate, targets, dependency_review, direct_ids, changes,
            )
        review = model.generate("review_update", {
            **context, "report_format": "markdown", "before": content, "after": candidate,
            "deltas": [dump(d) for d in extracted.deltas], "changes": [dump(c) for c in changes],
            "documents": [dump(d) for d in documents],
            "document_assessments": document_assessments,
            "source_policy": SOURCE_POLICY,
            "required_assessment": [
                "证据是否支持修改后的表述", "修改是否放在回答同一管理问题的章节",
                "是否扩大适用范围或丢失限定", "是否误改无关内容",
            ],
        }, MarkdownReview)
        if review.verdict != "pass" or review.issues:
            return versions.finish(req, result("evidence_insufficient", unresolved_items=review.issues or [
                "最终合理性审查未通过"
            ]), base_hash)
        changes = attach_reviews(changes, review.change_assessments)
        if not changes:
            return versions.finish(req, result(
                "no_change", after_report_path=str(before_path), changes=[]
            ), base_hash)
        prepared = result("updated", new_version=base_version + 1, changes=changes)
        confirmation_required = (
            req.confirmation_policy == "always"
            or any(is_sensitive_change(change) for change in changes)
        )
        if confirmation_required:
            return versions.save_preview(req, prepared, base_hash, candidate)
        return versions.finish(req, prepared, base_hash, candidate)
    except RadarError as exc:
        if exc.http_status in {413, 502, 503}:
            raise
        return versions.finish(req, result(
            "failed", unresolved_items=[f"{exc.code}: {exc.message}"]
        ), base_hash)


def update_markdown_report(req, model, versions, organization_context=None):
    if req.action == "commit":
        return commit_markdown_report_update(req, versions)
    return prepare_markdown_report_update(req, model, versions, organization_context)
