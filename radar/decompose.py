from uuid import uuid4

from .errors import RadarError
from .evidence import deduplicate, digest, dump, validate_ref
from .prompts import LENSES
from .schemas import (CandidateStructure, DecomposeResult, ExtractedEvidence, Module,
                      ReviewedStructure, unique)


def decompose(request, model):
    documents = deduplicate(request.documents)
    if not documents:
        return DecomposeResult(
            status="insufficient_input", topic_interpretation=request.topic, selected_lens="待确定",
            assumptions=[], modules=[], pending_questions=["缺少文献，无法形成有证据支持的模块结构"],
            supplemental_search_requests=[], structure_version="v1-empty", review_notes=[],
        )
    units = []
    for doc in documents:
        extracted = model.generate("extract_evidence", {"document": dump(doc)}, ExtractedEvidence)
        unique([u.evidence_id for u in extracted.units], "证据单元")
        for unit in extracted.units:
            validate_ref(unit.source_ref, [doc])
            unit.evidence_id = f"e_{uuid4().hex}"
            units.append(dump(unit))
    context = {
        "topic": request.topic, "leadership_question": request.leadership_question,
        "organization_context": dump(request.organization_context),
        "research_scope": dump(request.research_scope), "lenses": LENSES,
        "evidence_units": units, "documents": [dump(d) for d in documents],
    }
    candidates = model.generate("candidate_modules", context, CandidateStructure)
    valid = []
    for candidate in candidates.candidates:
        for ref in candidate.evidence_refs:
            validate_ref(ref, documents)
        if candidate.relevance and candidate.decision_value and candidate.uniqueness:
            valid.append(candidate)
    valid.sort(key=lambda c: 2*c.relevance + 3*c.decision_value + c.evidence_quality + c.uniqueness, reverse=True)
    review = model.generate("review_structure", {
        **context, "selected_lens": candidates.selected_lens,
        "candidates": [dump(c) for c in valid],
    }, ReviewedStructure)
    unique([m.core_question for m in review.modules], "模块核心问题")
    candidate_refs = {digest(ref) for c in valid for ref in c.evidence_refs}
    for module in review.modules:
        for ref in module.evidence_refs:
            validate_ref(ref, documents)
            if digest(ref) not in candidate_refs:
                raise RadarError("structure_out_of_scope", "结构审查引入了候选模块之外的证据")
    modules = [Module(module_id="m_" + digest({
        "topic": candidates.topic_interpretation,
        "core_question": m.core_question,
        "scope_in": m.scope_in,
    })[:24], **dump(m)) for m in review.modules]
    unique([module.module_id for module in modules], "模块")
    assumptions = list(candidates.assumptions)
    if request.organization_context is None:
        assumptions.append("未提供公司背景；采用一般管理视角，公司适用性需验证")
    return DecomposeResult(
        status=("insufficient_input" if not modules else "provisional"
                if any(m.evidence_status != "supported" for m in modules) else "ready"),
        topic_interpretation=candidates.topic_interpretation, selected_lens=candidates.selected_lens,
        assumptions=list(dict.fromkeys(assumptions)), modules=modules,
        pending_questions=review.pending_questions,
        supplemental_search_requests=review.supplemental_search_requests,
        structure_version=f"v1-{digest([dump(m) for m in modules])[:16]}", review_notes=review.review_notes,
    )
