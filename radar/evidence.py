import hashlib
import json
import re

from .errors import RadarError
from .schemas import Document, EvidenceRef


def dump(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def canonical(value):
    return json.dumps(dump(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def merge_documents(*groups):
    result = {}
    for docs in groups:
        for doc in docs:
            prior = result.get(doc.document_id)
            if prior and prior != doc:
                raise RadarError("document_collision", "同一文献 ID 的内容或元信息发生变化，请使用新的版本 ID")
            result[doc.document_id] = doc
    return list(result.values())


def deduplicate(documents):
    documents = merge_documents(documents)
    seen, seen_families, result = set(), set(), []
    for doc in documents:
        if doc.family_id and doc.family_id in seen_families:
            continue
        body = re.sub(r"\s+", "", "\n".join(s.text for s in doc.segments))
        key = text_hash(body)
        if key not in seen:
            result.append(doc)
            seen.add(key)
            if doc.family_id:
                seen_families.add(doc.family_id)
    return result


def validate_ref(ref: EvidenceRef, documents: list[Document]):
    for doc in documents:
        if doc.document_id == ref.document_id:
            for segment in doc.segments:
                if segment.segment_id == ref.segment_id and ref.quote in segment.text:
                    return
    raise RadarError("invalid_evidence", f"引用不存在或引文不匹配：{ref.document_id}/{ref.segment_id}")


def numeric_tokens(text):
    # 保留单位，避免把 70 次直接当作 70%。中文数词及复杂口径交给语义审查。
    return set(re.findall(r"\d+(?:\.\d+)?\s*(?:%|％|万元|亿元|元|次|天|年|月|人|小时|分钟)?", text))


def check_numbers(text, source_text):
    normalize = lambda values: {re.sub(r"\s+", "", v).replace("％", "%") for v in values}
    if not normalize(numeric_tokens(text)) <= normalize(numeric_tokens(source_text)):
        raise RadarError("unsupported_number", "修改包含来源未直接支持的数字或单位；请补充明确数据或可复核的计算结果")


def check_added_numbers(before, after, source_text):
    normalize = lambda values: {re.sub(r"\s+", "", v).replace("％", "%") for v in values}
    added = normalize(numeric_tokens(after)) - normalize(numeric_tokens(before or ""))
    if not added <= normalize(numeric_tokens(source_text)):
        raise RadarError("unsupported_number", "修改新增了来源未直接支持的数字或单位")


def tokens(text):
    text = text.lower()
    english = re.findall(r"[a-z0-9]+", text)
    chinese = re.findall(r"[\u4e00-\u9fff]+", text)
    return set(english + [s[i:i + 2] for s in chinese for i in range(max(1, len(s) - 1))])


def overlap(a, b):
    left, right = tokens(a), tokens(b)
    return len(left & right) / max(1, min(len(left), len(right)))
