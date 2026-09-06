from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Text = Annotated[str, Field(min_length=1, max_length=12000)]
Id = Annotated[str, Field(min_length=1, max_length=120, pattern=r"^[\w.:-]+$")]
ClaimKind = Literal["fact", "assessment", "recommendation", "user_constraint"]


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Segment(Schema):
    segment_id: Id
    text: Text


class Document(Schema):
    document_id: Id
    title: Text
    source_type: Literal["internal", "external"]
    segments: list[Segment] = Field(min_length=1, max_length=200)
    family_id: str | None = None
    metadata: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_segments(self):
        unique([s.segment_id for s in self.segments], "文献片段")
        return self


class EvidenceRef(Schema):
    document_id: Id
    segment_id: Id
    quote: Text


class EvidenceLink(Schema):
    ref: EvidenceRef
    relation: Literal["supports", "contradicts", "qualifies"] = "supports"


class ModuleDraft(Schema):
    title: Text
    core_question: Text
    leadership_value: Text
    scope_in: list[Text] = Field(min_length=1)
    scope_out: list[Text] = Field(default_factory=list)
    sub_questions: list[Text] = Field(min_length=1)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    evidence_status: Literal["supported", "limited", "gap"]
    missing_information: list[Text] = Field(default_factory=list)
    priority_reason: Text

    @model_validator(mode="after")
    def support_consistency(self):
        if self.evidence_status != "gap" and not self.evidence_refs:
            raise ValueError("非 gap 模块必须提供证据")
        if self.evidence_status == "gap" and not self.missing_information:
            raise ValueError("gap 模块必须说明缺少什么信息")
        return self


class Module(ModuleDraft):
    module_id: Id


class OrganizationContext(Schema):
    industry: str | None = None
    business_priorities: list[str] = Field(default_factory=list)
    known_constraints: list[str] = Field(default_factory=list)


class ResearchScope(Schema):
    time_range: str | None = None
    geography: str | None = None
    business_scope: str | None = None


class DecomposeRequest(Schema):
    topic: Text
    documents: list[Document] = Field(default_factory=list, max_length=80)
    leadership_question: str | None = None
    organization_context: OrganizationContext | None = None
    research_scope: ResearchScope | None = None


class EvidenceUnit(Schema):
    evidence_id: Id
    proposition: Text
    proposition_type: Literal["observation", "interpretation", "forecast"]
    subject: Text
    applicability: list[Text]
    qualifiers: list[Text]
    source_ref: EvidenceRef


class ExtractedEvidence(Schema):
    units: list[EvidenceUnit]


class CandidateModule(ModuleDraft):
    relevance: int = Field(ge=0, le=2)
    decision_value: int = Field(ge=0, le=2)
    evidence_quality: int = Field(ge=0, le=2)
    uniqueness: int = Field(ge=0, le=2)


class CandidateStructure(Schema):
    topic_interpretation: Text
    selected_lens: Text
    assumptions: list[Text]
    candidates: list[CandidateModule] = Field(max_length=12)


class SearchRequest(Schema):
    query: Text
    source_type: Literal["internal", "external", "both"]
    reason: Text


class ReviewedStructure(Schema):
    modules: list[ModuleDraft] = Field(max_length=8)
    pending_questions: list[Text]
    supplemental_search_requests: list[SearchRequest]
    review_notes: list[Text]


class DecomposeResult(Schema):
    status: Literal["ready", "provisional", "insufficient_input"]
    topic_interpretation: str
    selected_lens: str
    assumptions: list[str]
    modules: list[Module]
    pending_questions: list[str]
    supplemental_search_requests: list[SearchRequest]
    structure_version: str
    review_notes: list[str]


class Turn(Schema):
    turn_id: Id
    question: Text
    answer_revision: Id
    answer_segments: list[Segment] = Field(min_length=1, max_length=80)
    documents: list[Document] = Field(default_factory=list, max_length=80)

    @model_validator(mode="after")
    def identifiers(self):
        unique([s.segment_id for s in self.answer_segments], "回答片段")
        unique([d.document_id for d in self.documents], "本轮文献")
        return self


class PreviousTurn(Schema):
    question: Text
    answer: Text


class Adoption(Schema):
    adoption_id: Id
    turn_id: Id
    answer_revision: Id
    accepted_segment_ids: list[Id] = Field(min_length=1)


class ScopeResult(Schema):
    resolved_question: Text
    constraints: list[Text]
    ambiguities: list[Text]


class Delta(Schema):
    delta_id: Id
    accepted_segment_ids: list[Id] = Field(min_length=1)
    proposition: Text
    kind: ClaimKind
    applicability: list[Text]
    evidence_links: list[EvidenceLink]
    support_status: Literal["supported", "partial", "unsupported"]
    reason: Text


class DeltaResult(Schema):
    deltas: list[Delta]
    unresolved_items: list[Text]


class DeltaDisposition(Schema):
    delta_id: Id
    outcome: Literal["change", "duplicate"]
    existing_claim_ids: list[Id]
    reason: Text


class MarkdownUpdateRequest(Schema):
    request_id: Id
    report_path: Annotated[str, Field(min_length=1, max_length=500)]
    expected_version: int | None = Field(default=None, ge=1)
    adoption: Adoption
    current_turn: Turn
    previous_turn: PreviousTurn | None = None

    @model_validator(mode="after")
    def adopted_revision(self):
        a, t = self.adoption, self.current_turn
        if (a.turn_id, a.answer_revision) != (t.turn_id, t.answer_revision):
            raise ValueError("采纳记录与本轮回答修订不一致")
        unique(a.accepted_segment_ids, "采纳片段")
        if not set(a.accepted_segment_ids) <= {s.segment_id for s in t.answer_segments}:
            raise ValueError("被采纳片段不存在")
        return self


class MarkdownBlock(Schema):
    block_id: Id
    kind: Literal["heading", "body", "code"]
    text: Text
    text_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    heading_level: int | None = Field(default=None, ge=1, le=6)
    section_path: list[str] = Field(default_factory=list)


class MarkdownOperationBase(Schema):
    operation_id: Id
    delta_ids: list[Id] = Field(min_length=1)
    after_text: Text
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    reason: Text
    location_reason: Text
    decision_impact: Text


class ReviseMarkdownBlock(MarkdownOperationBase):
    op: Literal["revise_block"]
    target_block_id: Id
    expected_text_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class AppendMarkdownBlock(MarkdownOperationBase):
    op: Literal["append_to_section"]
    target_heading_id: Id
    expected_text_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


MarkdownOperation = Annotated[
    ReviseMarkdownBlock | AppendMarkdownBlock, Field(discriminator="op")
]


class MarkdownPlan(Schema):
    operations: list[MarkdownOperation]
    dispositions: list[DeltaDisposition]
    unresolved_items: list[Text]


class MarkdownDependencyDecision(Schema):
    target_block_id: Id
    expected_text_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    caused_by_operation_ids: list[Id] = Field(min_length=1)
    action: Literal["keep", "revise"]
    after_text: str | None = None
    reason: Text
    decision_impact: Text

    @model_validator(mode="after")
    def valid_action(self):
        if self.action == "revise" and not (self.after_text or "").strip():
            raise ValueError("revise 必须提供 after_text")
        if self.action == "keep" and self.after_text is not None:
            raise ValueError("keep 不能包含 after_text")
        return self


class MarkdownDependencyReview(Schema):
    decisions: list[MarkdownDependencyDecision]


class ChangeAssessment(Schema):
    operation_id: Id
    verdict: Literal["reasonable", "unreasonable"]
    evidence_reason: Text
    location_reason: Text
    limitations: list[Text] = Field(default_factory=list)


class MarkdownReview(Schema):
    verdict: Literal["pass", "reject"]
    change_assessments: list[ChangeAssessment]
    issues: list[Text]


class MarkdownChange(Schema):
    operation_id: Id
    change_type: Literal["revise", "append", "dependency"]
    section_path: list[str]
    line_before: int | None = None
    before: str | None = None
    after: Text
    reason: Text
    location_reason: Text
    decision_impact: Text
    accepted_segment_ids: list[Id] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    validation_checks: list[Text] = Field(default_factory=list)
    review: ChangeAssessment | None = None


class MarkdownUpdateResult(Schema):
    status: Literal["updated", "no_change", "needs_clarification", "evidence_insufficient",
                    "version_conflict", "failed"]
    request_id: str
    report_id: str
    base_version: int
    new_version: int | None = None
    before_report_path: str | None = None
    after_report_path: str | None = None
    change_log_path: str | None = None
    changes: list[MarkdownChange] = Field(default_factory=list)
    unresolved_items: list[str] = Field(default_factory=list)


def unique(values, label):
    if len(values) != len(set(values)):
        raise ValueError(f"{label} ID 不得重复")
