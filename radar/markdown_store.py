from __future__ import annotations

from pathlib import Path

from .errors import RadarError
from .evidence import canonical, digest
from .schemas import MarkdownUpdateResult


def markdown_adoption_fingerprint(req):
    return digest({
        "adoption": req.adoption.model_dump(mode="json"),
        "question": req.current_turn.question,
        "answer_segments": [s.model_dump(mode="json") for s in req.current_turn.answer_segments],
        "report_path": req.report_path,
    })


def _quote(text):
    return "\n".join("> " + line for line in (text or "").splitlines()) or "> （无）"


def render_change_log(result: MarkdownUpdateResult):
    lines = [
        f"# 认知报告变更说明 v{result.new_version}", "",
        f"- 更新前：`{result.before_report_path}`",
        f"- 更新后：`{result.after_report_path}`",
        f"- 修改数量：{len(result.changes)}", "",
    ]
    for index, change in enumerate(result.changes, 1):
        section = " / ".join(change.section_path) or "文档正文"
        lines.extend([
            f"## {index} {section}", "",
            f"**修改类型**：{change.change_type}", "",
            f"**为什么修改这里**：{change.location_reason}", "",
            f"**修改依据**：{change.reason}", "",
            f"**对领导判断的影响**：{change.decision_impact}", "",
            "**修改前**", "", _quote(change.before), "", "**修改后**", "", _quote(change.after), "",
        ])
        if change.accepted_segment_ids:
            lines.extend([f"**对应采纳片段**：{', '.join(change.accepted_segment_ids)}", ""])
        if change.evidence_refs:
            lines.extend(["**支持证据**", ""])
            for ref in change.evidence_refs:
                lines.extend([f"- `{ref.document_id}/{ref.segment_id}`", "", _quote(ref.quote), ""])
        if change.validation_checks:
            lines.extend(["**程序校验**", ""])
            lines.extend(f"- {check}" for check in change.validation_checks)
            lines.append("")
        if change.review:
            lines.extend([
                "**合理性审查**：通过", "",
                f"- 证据判断：{change.review.evidence_reason}",
                f"- 位置判断：{change.review.location_reason}",
            ])
            if change.review.limitations:
                lines.append(f"- 保留限制：{'；'.join(change.review.limitations)}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


class MarkdownVersionStore:
    def __init__(self, store, report_root="reports", history_root="data/report_versions"):
        self.store = store
        self.report_root = Path(report_root).resolve()
        self.history_root = Path(history_root).resolve()
        self.report_root.mkdir(parents=True, exist_ok=True)
        self.history_root.mkdir(parents=True, exist_ok=True)
        with self.store.connection() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS markdown_reports (
                    report_id TEXT PRIMARY KEY, source_path TEXT NOT NULL UNIQUE,
                    latest_version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS markdown_snapshots (
                    report_id TEXT NOT NULL, version INTEGER NOT NULL,
                    file_path TEXT NOT NULL, content_hash TEXT NOT NULL,
                    PRIMARY KEY (report_id, version)
                );
                CREATE TABLE IF NOT EXISTS markdown_attempts (
                    report_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL, result TEXT NOT NULL,
                    PRIMARY KEY (report_id, request_id)
                );
                CREATE TABLE IF NOT EXISTS markdown_adoptions (
                    report_id TEXT NOT NULL, adoption_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL, result TEXT,
                    PRIMARY KEY (report_id, adoption_id)
                );
            """)

    def resolve_source(self, relative_path):
        relative = Path(relative_path)
        if relative.is_absolute() or relative.suffix.lower() != ".md":
            raise RadarError("invalid_report_path", "report_path 必须是报告目录下的相对 .md 路径")
        resolved = (self.report_root / relative).resolve()
        try:
            resolved.relative_to(self.report_root)
        except ValueError as exc:
            raise RadarError("invalid_report_path", "report_path 不能离开配置的报告目录") from exc
        return resolved, relative.as_posix()

    def report_id_for(self, relative_path):
        return "md_" + digest(relative_path)[:24]

    def _version_path(self, report_id, version):
        return (self.history_root / report_id / f"v{version:06d}.md").resolve()

    def _change_path(self, report_id, version):
        return (self.history_root / report_id / f"v{version:06d}_changes.md").resolve()

    def lookup(self, req):
        _, relative = self.resolve_source(req.report_path)
        report_id = self.report_id_for(relative)
        with self.store.connection() as con:
            row = con.execute("SELECT * FROM markdown_attempts WHERE report_id=? AND request_id=?",
                              (report_id, req.request_id)).fetchone()
            if row:
                if row["request_hash"] != digest(req):
                    raise RadarError("idempotency_conflict", "相同 request_id 对应了不同请求内容", 409)
                return MarkdownUpdateResult.model_validate_json(row["result"])
            adopted = con.execute("SELECT * FROM markdown_adoptions WHERE report_id=? AND adoption_id=?",
                                  (report_id, req.adoption.adoption_id)).fetchone()
            if adopted:
                if adopted["content_hash"] != markdown_adoption_fingerprint(req):
                    raise RadarError("adoption_conflict", "同一 adoption_id 的采纳内容不能改变", 409)
                if adopted["result"]:
                    return MarkdownUpdateResult.model_validate_json(adopted["result"])
        return None

    def load_base(self, req):
        source, relative = self.resolve_source(req.report_path)
        report_id = self.report_id_for(relative)
        with self.store.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM markdown_reports WHERE report_id=?", (report_id,)).fetchone()
            if not row:
                if not source.is_file():
                    raise RadarError("report_not_found", f"找不到认知报告：{relative}", 404)
                content = source.read_text(encoding="utf-8-sig")
                if not content.strip():
                    raise RadarError("empty_report", "认知报告为空")
                version = 1
                version_path = self._version_path(report_id, version)
                version_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with version_path.open("x", encoding="utf-8", newline="") as stream:
                        stream.write(content)
                except FileExistsError:
                    existing = version_path.read_text(encoding="utf-8")
                    if digest(existing) != digest(content):
                        raise RadarError("history_collision", "初始版本文件已存在且内容不同", 409)
                con.execute("INSERT INTO markdown_reports VALUES (?,?,?)", (report_id, relative, version))
                con.execute("INSERT INTO markdown_snapshots VALUES (?,?,?,?)",
                            (report_id, version, str(version_path), digest(content)))
            else:
                version = row["latest_version"]
                snap = con.execute("SELECT * FROM markdown_snapshots WHERE report_id=? AND version=?",
                                   (report_id, version)).fetchone()
                version_path = Path(snap["file_path"])
                content = version_path.read_text(encoding="utf-8")
                if digest(content) != snap["content_hash"]:
                    raise RadarError("history_tampered", "已保存的报告版本内容发生变化", 409)
            if req.expected_version is not None and req.expected_version != version:
                raise RadarError("version_conflict", f"当前最新版本是 v{version}，请求基于 v{req.expected_version}", 409)
            return report_id, version, version_path, content

    def finish(self, req, result, base_hash, after_content=None):
        report_id = result.report_id
        created = []
        with self.store.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute("SELECT * FROM markdown_attempts WHERE report_id=? AND request_id=?",
                                   (report_id, req.request_id)).fetchone()
            if existing:
                return MarkdownUpdateResult.model_validate_json(existing["result"])
            row = con.execute("SELECT latest_version FROM markdown_reports WHERE report_id=?", (report_id,)).fetchone()
            snap = con.execute("SELECT content_hash FROM markdown_snapshots WHERE report_id=? AND version=?",
                               (report_id, result.base_version)).fetchone()
            if not row or row["latest_version"] != result.base_version or not snap or snap["content_hash"] != base_hash:
                result = MarkdownUpdateResult(
                    status="version_conflict", request_id=req.request_id, report_id=report_id,
                    base_version=result.base_version, unresolved_items=["报告已产生新版本，请重新执行更新"],
                )
                after_content = None
            complete = result.status in {"updated", "no_change"}
            try:
                if result.status == "updated":
                    if after_content is None or result.new_version != result.base_version + 1:
                        raise RadarError("invalid_commit", "缺少可提交的新报告内容")
                    after_path = self._version_path(report_id, result.new_version)
                    change_path = self._change_path(report_id, result.new_version)
                    after_path.parent.mkdir(parents=True, exist_ok=True)
                    with after_path.open("x", encoding="utf-8", newline="") as stream:
                        stream.write(after_content)
                    created.append(after_path)
                    result.after_report_path = str(after_path)
                    result.change_log_path = str(change_path)
                    with change_path.open("x", encoding="utf-8", newline="") as stream:
                        stream.write(render_change_log(result))
                    created.append(change_path)
                    con.execute("INSERT INTO markdown_snapshots VALUES (?,?,?,?)",
                                (report_id, result.new_version, str(after_path), digest(after_content)))
                    updated = con.execute("UPDATE markdown_reports SET latest_version=? WHERE report_id=? AND latest_version=?",
                                          (result.new_version, report_id, result.base_version))
                    if updated.rowcount != 1:
                        raise RadarError("version_conflict", "报告已产生新版本", 409)
                con.execute("INSERT OR IGNORE INTO markdown_adoptions VALUES (?,?,?,NULL)",
                            (report_id, req.adoption.adoption_id, markdown_adoption_fingerprint(req)))
                if complete:
                    con.execute("UPDATE markdown_adoptions SET result=? WHERE report_id=? AND adoption_id=?",
                                (canonical(result), report_id, req.adoption.adoption_id))
                con.execute("INSERT INTO markdown_attempts VALUES (?,?,?,?)",
                            (report_id, req.request_id, digest(req), canonical(result)))
            except Exception:
                for path in reversed(created):
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                raise
            return result
