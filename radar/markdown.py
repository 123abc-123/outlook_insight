from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .errors import RadarError
from .evidence import check_added_numbers, digest, overlap, text_hash, validate_ref
from .schemas import (AppendMarkdownBlock, ChangeAssessment, Delta, MarkdownBlock,
                      MarkdownChange, MarkdownDependencyReview, MarkdownOperation,
                      MarkdownPlan, ReviseMarkdownBlock, unique)

SUMMARY_PATTERN = re.compile(r"摘要|结论|建议|管理层|决策|执行概要|核心判断")
HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def parse_markdown(content: str):
    lines = content.splitlines(keepends=True)
    blocks, stack, ordinal = [], [], {}
    i = 0
    while i < len(lines):
        raw = lines[i]
        if not raw.strip():
            i += 1
            continue
        match = HEADING_PATTERN.match(raw.rstrip("\r\n"))
        kind, level = "body", None
        start = i
        if match:
            kind, level = "heading", len(match.group(1))
            title = match.group(2).strip()
            stack = stack[:level - 1] + [title]
            i += 1
        elif raw.lstrip().startswith(("```", "~~~")):
            kind = "code"
            fence = raw.lstrip()[:3]
            i += 1
            while i < len(lines):
                closing = lines[i].lstrip().startswith(fence)
                i += 1
                if closing:
                    break
        else:
            i += 1
            while i < len(lines):
                candidate = lines[i]
                if not candidate.strip() or HEADING_PATTERN.match(candidate.rstrip("\r\n")):
                    break
                if candidate.lstrip().startswith(("```", "~~~")):
                    break
                i += 1
        text = "".join(lines[start:i]).rstrip("\r\n")
        section = list(stack)
        key = (kind, tuple(section))
        ordinal[key] = ordinal.get(key, 0) + 1
        raw_id = f"{kind}|{'/'.join(section)}|{ordinal[key]}|{text}"
        block_id = "b_" + hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:24]
        blocks.append(MarkdownBlock(block_id=block_id, kind=kind, text=text,
                                    text_hash=text_hash(text), line_start=start + 1, line_end=i,
                                    heading_level=level, section_path=section))
    return blocks


def report_topic(blocks, fallback):
    for block in blocks:
        if block.kind == "heading" and block.heading_level == 1:
            return HEADING_PATTERN.match(block.text).group(2).strip()
    return fallback


def is_summary(block):
    return block.kind == "body" and any(SUMMARY_PATTERN.search(section) for section in block.section_path)


def retrieve_blocks(blocks, scope, deltas):
    body = [b for b in blocks if b.kind == "body" and not is_summary(b)]
    headings = [b for b in blocks if b.kind == "heading" and not any(
        SUMMARY_PATTERN.search(section) for section in b.section_path)]
    queries = [scope.resolved_question, *(d.proposition for d in deltas)]
    scored_body = sorted(((max(overlap(q, b.text + " ".join(b.section_path)) for q in queries), b)
                          for b in body), key=lambda x: x[0], reverse=True)
    scored_headings = sorted(((max(overlap(q, " ".join(b.section_path)) for q in queries), b)
                              for b in headings), key=lambda x: x[0], reverse=True)
    selected_body = [b for score, b in scored_body[:30] if score >= 0.05]
    selected_headings = [b for score, b in scored_headings[:8] if score >= 0.05]
    return selected_body, selected_headings


def validate_plan(plan: MarkdownPlan, deltas: list[Delta], allowed_blocks, allowed_headings, documents):
    unique([op.operation_id for op in plan.operations], "修改操作")
    targets = [getattr(op, "target_block_id", None) or getattr(op, "target_heading_id")
               for op in plan.operations]
    unique(targets, "修改目标")
    delta_map = {d.delta_id: d for d in deltas}
    unique([d.delta_id for d in plan.dispositions], "增量处置")
    if {d.delta_id for d in plan.dispositions} != set(delta_map):
        raise RadarError("invalid_patch", "修改计划没有逐项处置全部增量")
    used = {delta_id for op in plan.operations for delta_id in op.delta_ids}
    for disposition in plan.dispositions:
        if disposition.outcome == "change" and disposition.delta_id not in used:
            raise RadarError("invalid_patch", "需要修改的增量缺少操作")
        if disposition.outcome == "duplicate" and disposition.delta_id in used:
            raise RadarError("invalid_patch", "重复增量不应生成修改操作")
    allowed_block_ids = {b.block_id for b in allowed_blocks}
    allowed_heading_ids = {b.block_id for b in allowed_headings}
    for op in plan.operations:
        if not set(op.delta_ids) <= set(delta_map):
            raise RadarError("invalid_patch", "修改使用了未知增量")
        if isinstance(op, ReviseMarkdownBlock) and op.target_block_id not in allowed_block_ids:
            raise RadarError("invalid_patch", "修改了召回范围外的正文块")
        if isinstance(op, AppendMarkdownBlock) and op.target_heading_id not in allowed_heading_ids:
            raise RadarError("invalid_patch", "向召回范围外的章节增加了内容")
        if any(HEADING_PATTERN.match(line) for line in op.after_text.splitlines()):
            raise RadarError("invalid_patch", "第一版不允许通过正文操作增加或改变报告章节")
        allowed_refs = {digest(link.ref) for delta_id in op.delta_ids
                        for link in delta_map[delta_id].evidence_links}
        if not op.evidence_refs and any(delta_map[d].kind != "user_constraint" for d in op.delta_ids):
            raise RadarError("invalid_patch", "报告修改必须关联本轮证据")
        for ref in op.evidence_refs:
            validate_ref(ref, documents)
            if digest(ref) not in allowed_refs:
                raise RadarError("invalid_patch", "修改引用超出本轮已核实增量")


def _replacement_lines(text, newline):
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    return [line + newline for line in normalized.split("\n")]


def apply_operations(content: str, blocks, operations: list[MarkdownOperation], deltas):
    newline = "\r\n" if "\r\n" in content else "\n"
    lines = content.splitlines(keepends=True)
    block_map = {b.block_id: b for b in blocks}
    delta_map = {d.delta_id: d for d in deltas}
    prepared, changes = [], []
    for op in operations:
        if isinstance(op, ReviseMarkdownBlock):
            target = block_map[op.target_block_id]
            if target.text_hash != op.expected_text_hash:
                raise RadarError("invalid_patch", "正文块哈希不匹配")
            start, end, kind, before = target.line_start - 1, target.line_end, "revise", target.text
            section = target.section_path
        else:
            target = block_map[op.target_heading_id]
            if target.kind != "heading" or target.text_hash != op.expected_text_hash:
                raise RadarError("invalid_patch", "目标章节哈希不匹配")
            start = len(lines)
            for candidate in blocks:
                if (candidate.line_start > target.line_start and candidate.kind == "heading"
                        and candidate.heading_level <= target.heading_level):
                    start = candidate.line_start - 1
                    break
            end, kind, before, section = start, "append", None, target.section_path
        source = "\n".join(ref.quote for ref in op.evidence_refs)
        check_added_numbers(before, op.after_text, source)
        replacement = _replacement_lines(op.after_text, newline)
        if kind == "append":
            if start and lines[start - 1].strip():
                replacement.insert(0, newline)
            replacement.append(newline)
        prepared.append((start, end, replacement))
        accepted = sorted({segment for did in op.delta_ids for segment in delta_map[did].accepted_segment_ids})
        changes.append(MarkdownChange(
            operation_id=op.operation_id, change_type=kind, section_path=section,
            line_before=target.line_start, before=before, after=op.after_text,
            reason=op.reason, location_reason=op.location_reason, decision_impact=op.decision_impact,
            accepted_segment_ids=accepted, evidence_refs=op.evidence_refs,
            validation_checks=[
                "修改只使用了被采纳回答对应的认知增量",
                "所有引用均已与输入文献 ID、片段 ID 和原文逐字核对",
                "新增数字和单位已在支持证据中核对",
                "目标正文块属于本轮召回范围，且旧文本哈希与最新版本一致",
                "除明确目标块外的 Markdown 内容由程序原样保留",
            ],
        ))
    for start, end, replacement in sorted(prepared, reverse=True):
        lines[start:end] = replacement
    return "".join(lines), changes


def summary_targets(blocks):
    targets = [b for b in blocks if is_summary(b)]
    if len(targets) > 40:
        raise RadarError("report_too_broad", "摘要和建议相关段落超过第一版联动检查上限", 413)
    return targets


def apply_dependency_decisions(content, targets, review: MarkdownDependencyReview, causes, changes):
    unique([d.target_block_id for d in review.decisions], "联动审查目标")
    if {d.target_block_id for d in review.decisions} != {b.block_id for b in targets}:
        raise RadarError("invalid_patch", "摘要和建议联动审查遗漏目标或引入无关目标")
    operations = []
    for decision in review.decisions:
        target = next(b for b in targets if b.block_id == decision.target_block_id)
        if decision.expected_text_hash != target.text_hash:
            raise RadarError("invalid_patch", "摘要或建议正文哈希不匹配")
        if not set(decision.caused_by_operation_ids) <= causes:
            raise RadarError("invalid_patch", "摘要联动没有关联实际正文修改")
        if decision.action == "revise":
            if any(HEADING_PATTERN.match(line) for line in decision.after_text.splitlines()):
                raise RadarError("invalid_patch", "联动修改不能改变报告章节结构")
            operation_id = "dep_" + hashlib.sha256(
                (decision.target_block_id + "|" + "|".join(decision.caused_by_operation_ids)).encode()
            ).hexdigest()[:20]
            # 联动内容中的新数字必须已经出现在直接修改后的正文中。
            check_added_numbers(target.text, decision.after_text, content)
            operations.append((target, decision, operation_id))
    newline = "\r\n" if "\r\n" in content else "\n"
    lines = content.splitlines(keepends=True)
    for target, decision, operation_id in sorted(operations, key=lambda x: x[0].line_start, reverse=True):
        lines[target.line_start - 1:target.line_end] = _replacement_lines(decision.after_text, newline)
        changes.append(MarkdownChange(
            operation_id=operation_id, change_type="dependency", section_path=target.section_path,
            line_before=target.line_start, before=target.text, after=decision.after_text,
            reason=decision.reason, location_reason="该摘要或建议依赖本轮已修改的正文判断",
            decision_impact=decision.decision_impact,
            validation_checks=[
                "联动目标属于管理摘要、结论或建议章节",
                "联动修改已关联导致变化的正文操作",
                "目标正文块哈希与当前候选版本一致",
                "联动内容未引入正文中不存在的新数字",
            ],
        ))
    return "".join(lines), changes


def attach_reviews(changes, assessments: list[ChangeAssessment]):
    by_id = {a.operation_id: a for a in assessments}
    if set(by_id) != {c.operation_id for c in changes}:
        raise RadarError("invalid_review", "最终审查没有逐项覆盖全部修改")
    if any(a.verdict != "reasonable" for a in assessments):
        raise RadarError("invalid_review", "存在被判定为不合理的修改")
    return [c.model_copy(update={
        "review": by_id[c.operation_id],
        "validation_checks": [*c.validation_checks, "最终语义审查逐项判定为合理"],
    }) for c in changes]
