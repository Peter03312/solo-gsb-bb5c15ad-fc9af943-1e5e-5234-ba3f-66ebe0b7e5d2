"""野外终端离线整包汇入正式库与整包勘误。

野外终端在无网络条件下用**地层短号**记录“同一期 / 早于”断言，回站后整包汇入：

- 请求携带采集站点、终端编号、包号、预期正式修订号、短号→全库地层编号对照、
  以及带包内唯一序号的断言；**断言端点只能引用本包短号**。
- 对照目标已登记时站点须一致（`SITE_CONFLICT`），未登记时随包建立；
  同一终端的同一短号跨包不可改绑（`SHORT_CODE_REBOUND`）。
- 正式证据编号由 (终端编号, 包号, 序号) **稳定确定**（`evidence_id_for`），
  且不得占用已有编号（`FIELD_EVIDENCE_TAKEN`）。
- 整包沿用正式批次的最终关系核验（组内早于 / 早于闭环，冲突附证据路径）：
  任一错误都不落地层、绑定或断言；成功只递增一次正式修订，与变化记录、
  包台账同事务提交。
- 相同终端 + 包号 + 内容（内容指纹不含 expected_revision）重试原样返回
  首次结果，即使正式修订已前进（`replayed=true`）；异内容复用拒绝
  （`FIELD_PACKAGE_CONTENT_MISMATCH`）。

整包勘误（correction）：已成功汇入的包若需更正，以**新包号**汇入替代内容：

- 新包号须未使用且不同于原包号；原包须存在、未被替换、其证据全部仍生效。
- 一次事务内撤回原包全部证据、按整包汇入的全部规则（短号 / 序号 / 端点 /
  站点 / 跨包绑定 / 稳定证据编号）汇入新包，并对最终断言集核验组内早于与
  早于闭环；任一错误或冲突都不改变地层、绑定、断言、台账与修订。
- 成功只递增一次正式修订，关系变化以 `field_package_correction` 来源记录，
  替换链（原包 → 新包）落勘误台账；同一原包只被替换一次，新包可再被勘误。
- 相同（终端, 原包号, 新包号）+ 相同内容重试原样返回首次结果，即使正式
  修订已前进（`replayed=true`）；异内容复用拒绝
  （`FIELD_CORRECTION_CONTENT_MISMATCH`）。
- 原包台账行不修改不删除：原包原请求重放仍返回首次汇入结果；历史定版与
  推演冻结结果不受影响。
"""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple
from psycopg2.extras import Json

from .changes import insert_revision_changes, relation_diffs
from .db import db_conn
from .derivation import Assertion, Derivation
from .errors import ApiError
from .models import FieldAssertionIn, FieldCorrectionIn, FieldMappingIn, FieldPackageIn

SEQ_WIDTH = 12  # 序号补零宽度：稳定且字典序友好（上限 10**12-1，由模型 ge=1 与上层约束共同保证）


# ------------------------------------------------------------------ 稳定标识
def _b64url(text: str) -> str:
    """UTF-8 字节的 URL 安全 base64（去填充）。结果只含 [A-Za-z0-9_-]，
    与证据编号的 ':' 分隔符互不冲突，且对任意终端编号 / 包号双射可逆。"""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def evidence_id_for(terminal_id: str, package_no: str, seq: int) -> str:
    """证据编号由终端、包号、包内序号稳定确定：F:<终端>:<包号>:<序号>。

    终端 / 包号段做无歧义的 base64url 编码；同输入永远得到同一编号，
    不同输入永不碰撞（编码双射 + 固定分隔符）。
    """
    return f"F:{_b64url(terminal_id)}:{_b64url(package_no)}:{seq:0{SEQ_WIDTH}d}"


def canonical_content(
    site: str,
    mappings: List[FieldMappingIn],
    assertions: List[FieldAssertionIn],
) -> Dict[str, Any]:
    """参与幂等指纹的规范化内容（**不含 expected_revision**）。

    mappings 按短号排序去重（重复短号在结构校验阶段拒绝），
    assertions 按序号排序；指纹只依赖语义内容，与数组输入顺序无关。
    """
    normalized_mappings = sorted(
        ({"short_code": m.short_code, "stratum_id": m.stratum_id} for m in mappings),
        key=lambda m: m["short_code"],
    )
    normalized_assertions = sorted(
        (
            {
                "seq": a.seq,
                "stratum_a": a.stratum_a,
                "stratum_b": a.stratum_b,
                "relation": a.relation,
            }
            for a in assertions
        ),
        key=lambda a: a["seq"],
    )
    return {"site": site, "mappings": normalized_mappings, "assertions": normalized_assertions}


def canonical_correction_content(body: FieldCorrectionIn) -> Dict[str, Any]:
    """勘误请求的规范化内容：新包内容 + 原包号 / 新包号（不含 expected_revision）。"""
    content = canonical_content(body.site, body.mappings, body.assertions)
    content["original_package_no"] = body.original_package_no
    content["new_package_no"] = body.new_package_no
    return content


def content_hash_of(content: Dict[str, Any]) -> str:
    blob = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# ------------------------------------------------------------------ 结构校验（事务外）
def validate_structure(
    terminal_id: str,
    package_no: str,
    mappings: List[FieldMappingIn],
    assertions: List[FieldAssertionIn],
) -> Tuple[Dict[str, str], Dict[int, Dict[str, Any]]]:
    """包内自洽性校验（不访问数据库）：短号不重复、序号不重复、端点只引本包短号。

    返回 (短号→全库编号, 序号→断言) 供汇入使用。
    """
    short_to_full: Dict[str, str] = {}
    for m in mappings:
        if m.short_code in short_to_full:
            raise ApiError(
                400,
                "DUPLICATE_SHORT_CODE",
                f"包内短号重复: {m.short_code}",
                {"terminal_id": terminal_id, "package_no": package_no, "short_code": m.short_code},
            )
        short_to_full[m.short_code] = m.stratum_id

    by_seq: Dict[int, Dict[str, Any]] = {}
    for a in assertions:
        if a.seq in by_seq:
            raise ApiError(
                400,
                "DUPLICATE_SEQUENCE",
                f"包内序号重复: {a.seq}",
                {"terminal_id": terminal_id, "package_no": package_no, "seq": a.seq},
            )
        for endpoint in (a.stratum_a, a.stratum_b):
            if endpoint not in short_to_full:
                raise ApiError(
                    400,
                    "UNKNOWN_SHORT_CODE",
                    f"断言端点引用了本包未对照的短号: {endpoint}（序号 {a.seq}）",
                    {
                        "terminal_id": terminal_id,
                        "package_no": package_no,
                        "seq": a.seq,
                        "short_code": endpoint,
                    },
                )
        by_seq[a.seq] = {
            "seq": a.seq,
            "short_a": a.stratum_a,
            "short_b": a.stratum_b,
            "full_a": short_to_full[a.stratum_a],
            "full_b": short_to_full[a.stratum_b],
            "relation": a.relation,
        }
    return short_to_full, by_seq


# ------------------------------------------------------------------ 事务内汇入主体（整包汇入与勘误共用）
def _ingest_apply(
    cur,
    terminal_id: str,
    package_no: str,
    site: str,
    short_to_full: Dict[str, str],
    by_seq: Dict[int, Dict[str, Any]],
    new_revision: int,
) -> Tuple[List[str], Dict[int, str]]:
    """在调用方事务内把一个包的内容落到正式库（不含修订号递增与核验）。

    依次执行：既有地层站点一致性、跨包绑定不可改绑、稳定证据编号占用校验，
    然后落地层（未登记随包建立）、落绑定、落断言（added_revision=new_revision）。
    返回 (随包新建的地层编号, 序号→证据编号) 供响应与台账使用。
    """
    ordered_short_codes = sorted(short_to_full)

    # ---- 既有地层站点一致性 ----
    cur.execute("SELECT stratum_id, site FROM strata")
    registered_site: Dict[str, Optional[str]] = {r[0]: r[1] for r in cur.fetchall()}
    new_strata: List[str] = []
    for short in ordered_short_codes:
        full = short_to_full[short]
        if full in registered_site:
            registered = registered_site[full]
            # 已登记目标的站点（含未知 NULL）与本包站点必须一致
            if registered != site:
                raise ApiError(
                    409,
                    "SITE_CONFLICT",
                    f"对照目标地层 {full} 已登记站点 {registered if registered is not None else '（未登记站点）'}，与本包站点 {site} 不一致",
                    {
                        "terminal_id": terminal_id,
                        "package_no": package_no,
                        "short_code": short,
                        "stratum_id": full,
                        "registered_site": registered,
                        "request_site": site,
                    },
                )
        elif full not in new_strata:
            new_strata.append(full)

    # ---- 同一终端同一短号跨包不可改绑 ----
    cur.execute(
        "SELECT short_code, stratum_id, package_no FROM field_short_bindings "
        "WHERE terminal_id = %s AND short_code = ANY(%s) FOR UPDATE",
        (terminal_id, ordered_short_codes),
    )
    bound: Dict[str, Tuple[str, str]] = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    for short in ordered_short_codes:
        full = short_to_full[short]
        if short in bound and bound[short][0] != full:
            prior_full, prior_pkg = bound[short]
            raise ApiError(
                409,
                "SHORT_CODE_REBOUND",
                f"终端 {terminal_id} 的短号 {short} 已在包 {prior_pkg} 绑定 {prior_full}，不可改绑为 {full}",
                {
                    "terminal_id": terminal_id,
                    "short_code": short,
                    "bound_stratum_id": prior_full,
                    "bound_package_no": prior_pkg,
                    "requested_stratum_id": full,
                    "request_package_no": package_no,
                },
            )

    # ---- 稳定证据编号不得占用已有编号（含已撤回，永久唯一）----
    evidence_by_seq: Dict[int, str] = {}
    for seq in sorted(by_seq):
        evidence_by_seq[seq] = evidence_id_for(terminal_id, package_no, seq)
    cur.execute(
        "SELECT evidence_id FROM assertions WHERE evidence_id = ANY(%s)",
        (list(evidence_by_seq.values()),),
    )
    occupied = [r[0] for r in cur.fetchall()]
    if occupied:
        raise ApiError(
            409,
            "FIELD_EVIDENCE_TAKEN",
            f"稳定证据编号已被占用（含已撤回）: {', '.join(sorted(occupied))}",
            {
                "terminal_id": terminal_id,
                "package_no": package_no,
                "occupied_evidence_ids": sorted(occupied),
            },
        )

    # ---- 落地层（未登记随包建立；防御并发登记后再核站点）----
    for full in new_strata:
        cur.execute(
            "INSERT INTO strata (stratum_id, site) VALUES (%s, %s) ON CONFLICT (stratum_id) DO NOTHING",
            (full, site),
        )
    if new_strata:
        cur.execute(
            "SELECT stratum_id, site FROM strata WHERE stratum_id = ANY(%s)",
            (new_strata,),
        )
        rechecked = {r[0]: r[1] for r in cur.fetchall()}
        for full in new_strata:
            if rechecked.get(full) != site:
                raise ApiError(
                    409,
                    "SITE_CONFLICT",
                    f"对照目标地层 {full} 已登记站点 {rechecked.get(full)}，与本包站点 {site} 不一致",
                    {
                        "terminal_id": terminal_id,
                        "package_no": package_no,
                        "stratum_id": full,
                        "registered_site": rechecked.get(full),
                        "request_site": site,
                    },
                )

    # ---- 落绑定（首次建立的短号）----
    for short in ordered_short_codes:
        if short not in bound:
            cur.execute(
                "INSERT INTO field_short_bindings (terminal_id, short_code, stratum_id, site, package_no) "
                "VALUES (%s, %s, %s, %s, %s)",
                (terminal_id, short, short_to_full[short], site, package_no),
            )

    # ---- 落断言（证据编号稳定、端点已展开为全库地层编号）----
    for seq in sorted(by_seq):
        item = by_seq[seq]
        cur.execute(
            "INSERT INTO assertions (evidence_id, stratum_a, stratum_b, relation, status, added_revision) "
            "VALUES (%s, %s, %s, %s, 'active', %s)",
            (evidence_by_seq[seq], item["full_a"], item["full_b"], item["relation"], new_revision),
        )
    return new_strata, evidence_by_seq


def _package_payloads(
    site: str,
    short_to_full: Dict[str, str],
    by_seq: Dict[int, Dict[str, Any]],
    evidence_by_seq: Dict[int, str],
    new_strata: List[str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """成功响应与台账共用的短号映射 / 证据清单。"""
    mappings_payload = [
        {
            "short_code": short,
            "stratum_id": short_to_full[short],
            "site": site,
            "already_registered": short_to_full[short] not in new_strata,
        }
        for short in sorted(short_to_full)
    ]
    evidence_payload = [
        {
            "seq": seq,
            "evidence_id": evidence_by_seq[seq],
            "stratum_a": by_seq[seq]["full_a"],
            "stratum_b": by_seq[seq]["full_b"],
            "relation": by_seq[seq]["relation"],
        }
        for seq in sorted(by_seq)
    ]
    return mappings_payload, evidence_payload


def _audit_operations(by_seq: Dict[int, Dict[str, Any]], evidence_by_seq: Dict[int, str]) -> List[Dict[str, Any]]:
    """revisions 审计中的操作清单（端点为全库地层编号）。"""
    return [
        {
            "seq": item["seq"],
            "evidence_id": evidence_by_seq[seq],
            "stratum_a": item["full_a"],
            "stratum_b": item["full_b"],
            "relation": item["relation"],
        }
        for seq, item in sorted(by_seq.items())
    ]


# ------------------------------------------------------------------ 汇入
def ingest_field_package(body: FieldPackageIn) -> Dict[str, Any]:
    """整包汇入正式库（语义见模块文档）。成功响应的 replayed 字段由路由层标注。"""
    short_to_full, by_seq = validate_structure(body.terminal_id, body.package_no, body.mappings, body.assertions)
    content = canonical_content(body.site, body.mappings, body.assertions)
    content_hash = content_hash_of(content)

    with db_conn() as conn:
        with conn.cursor() as cur:
            # 同一 (终端, 包号) 的请求在事务级咨询锁上串行，包台账写入无竞争。
            # hashtextextended 返回 bigint，对应单键版 pg_advisory_xact_lock(bigint)。
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 9173))",
                (f"{body.terminal_id}\x1f{body.package_no}",),
            )

            # ---- 幂等判定：相同终端 + 包号只成功一次 ----
            cur.execute(
                "SELECT content_hash, stored_response FROM field_packages "
                "WHERE terminal_id = %s AND package_no = %s",
                (body.terminal_id, body.package_no),
            )
            existing = cur.fetchone()
            if existing is not None:
                if existing[0] != content_hash:
                    raise ApiError(
                        409,
                        "FIELD_PACKAGE_CONTENT_MISMATCH",
                        f"终端 {body.terminal_id} 的包号 {body.package_no} 已以不同内容汇入，异内容复用拒绝",
                        {
                            "terminal_id": body.terminal_id,
                            "package_no": body.package_no,
                            "original_content_hash": existing[0],
                            "request_content_hash": content_hash,
                        },
                    )
                response = dict(existing[1])
                response["replayed"] = True
                return response

            # ---- 过期修订（首次汇入才检查；幂等重试即使修订已前进也返回原结果）----
            cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE FOR UPDATE")
            current = cur.fetchone()[0]
            if body.expected_revision != current:
                raise ApiError(
                    409,
                    "STALE_REVISION",
                    f"修订号过期：请求携带 {body.expected_revision}，当前为 {current}",
                    {"current_revision": current, "expected_revision": body.expected_revision},
                )
            new_revision = current + 1

            before_assertions = _load_active(cur)

            # ---- 落地层 / 绑定 / 断言（站点、改绑、编号占用校验在内）----
            new_strata, evidence_by_seq = _ingest_apply(
                cur, body.terminal_id, body.package_no, body.site, short_to_full, by_seq, new_revision
            )

            # ---- 整包应用后的最终关系核验（沿用正式批次口径）----
            after_assertions = _load_active(cur)
            conflicts = Derivation(after_assertions).validate()
            if conflicts:
                raise ApiError(
                    409,
                    "FIELD_CONFLICT",
                    "野外整包与现有断言矛盾，整批拒绝，地层 / 绑定 / 断言均不落地",
                    {
                        "terminal_id": body.terminal_id,
                        "package_no": body.package_no,
                        "current_revision": current,
                        "conflicts": conflicts,
                    },
                )

            # ---- 变化记录：与修订号递增同事务 ----
            cur.execute("SELECT stratum_id FROM strata ORDER BY stratum_id")
            all_strata = [r[0] for r in cur.fetchall()]
            diffs = relation_diffs(all_strata, before_assertions, after_assertions)

            cur.execute("UPDATE revision_counter SET revision = %s WHERE id = TRUE", (new_revision,))
            cur.execute(
                "INSERT INTO revisions (revision, batch) VALUES (%s, %s)",
                (
                    new_revision,
                    Json(
                        {
                            "source": "field_package",
                            "site": body.site,
                            "terminal_id": body.terminal_id,
                            "package_no": body.package_no,
                            "content_hash": content_hash,
                            "operations": _audit_operations(by_seq, evidence_by_seq),
                        }
                    ),
                ),
            )
            insert_revision_changes(cur, new_revision, diffs, "field_package")

            mappings_payload, evidence_payload = _package_payloads(
                body.site, short_to_full, by_seq, evidence_by_seq, new_strata
            )
            response = {
                "site": body.site,
                "terminal_id": body.terminal_id,
                "package_no": body.package_no,
                "revision": new_revision,
                "mappings": mappings_payload,
                "evidence": evidence_payload,
                "applied": len(by_seq),
                "relation_changes": len(diffs),
                "replayed": False,
            }

            # ---- 包台账（重试据此原样返回；唯一约束 + 咨询锁保证不重复）----
            cur.execute(
                "INSERT INTO field_packages "
                "(terminal_id, package_no, site, content_hash, expected_revision, applied_revision, "
                " mappings, evidence, request, stored_response) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    body.terminal_id,
                    body.package_no,
                    body.site,
                    content_hash,
                    body.expected_revision,
                    new_revision,
                    Json(mappings_payload),
                    Json(evidence_payload),
                    Json(content),
                    Json(response),
                ),
            )
            return response


# ------------------------------------------------------------------ 勘误
def correct_field_package(body: FieldCorrectionIn) -> Dict[str, Any]:
    """整包勘误：单事务撤回原包全部证据并以新包号汇入替代内容（语义见模块文档）。"""
    # ---- 结构校验（事务外）：新包号须不同于原包号；新包内容沿用整包汇入结构规则 ----
    if body.new_package_no == body.original_package_no:
        raise ApiError(
            400,
            "FIELD_CORRECTION_SAME_PACKAGE",
            f"新包号须不同于原包号: {body.original_package_no}",
            {
                "terminal_id": body.terminal_id,
                "original_package_no": body.original_package_no,
                "new_package_no": body.new_package_no,
            },
        )
    short_to_full, by_seq = validate_structure(body.terminal_id, body.new_package_no, body.mappings, body.assertions)
    content = canonical_correction_content(body)
    content_hash = content_hash_of(content)
    # 新包台账指纹与直接汇入同口径：同内容经 /field-packages 重放可命中
    pkg_content = canonical_content(body.site, body.mappings, body.assertions)
    pkg_content_hash = content_hash_of(pkg_content)

    with db_conn() as conn:
        with conn.cursor() as cur:
            # 同一终端的原包与新包在事务级咨询锁上串行（按包号排序加锁避免死锁），
            # 与整包汇入使用同一锁键空间，勘误与并发汇入 / 勘误互斥。
            for pkg_no in sorted({body.original_package_no, body.new_package_no}):
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 9173))",
                    (f"{body.terminal_id}\x1f{pkg_no}",),
                )

            # ---- 幂等判定：相同（终端, 原包号, 新包号）只成功一次 ----
            cur.execute(
                "SELECT content_hash, stored_response FROM field_package_corrections "
                "WHERE terminal_id = %s AND original_package_no = %s AND new_package_no = %s",
                (body.terminal_id, body.original_package_no, body.new_package_no),
            )
            existing = cur.fetchone()
            if existing is not None:
                if existing[0] != content_hash:
                    raise ApiError(
                        409,
                        "FIELD_CORRECTION_CONTENT_MISMATCH",
                        f"终端 {body.terminal_id} 的原包 {body.original_package_no} 已以不同内容勘误为 "
                        f"{body.new_package_no}，异内容复用拒绝",
                        {
                            "terminal_id": body.terminal_id,
                            "original_package_no": body.original_package_no,
                            "new_package_no": body.new_package_no,
                            "original_content_hash": existing[0],
                            "request_content_hash": content_hash,
                        },
                    )
                response = dict(existing[1])
                response["replayed"] = True
                return response

            # ---- 原包必须存在 ----
            cur.execute(
                "SELECT evidence FROM field_packages WHERE terminal_id = %s AND package_no = %s",
                (body.terminal_id, body.original_package_no),
            )
            row = cur.fetchone()
            if row is None:
                raise ApiError(
                    404,
                    "FIELD_PACKAGE_NOT_FOUND",
                    f"野外包不存在: 终端 {body.terminal_id} 包号 {body.original_package_no}",
                    {"terminal_id": body.terminal_id, "package_no": body.original_package_no},
                )
            original_evidence_ids = sorted(item["evidence_id"] for item in row[0])

            # ---- 原包未被替换（同一原包只被勘误一次）----
            cur.execute(
                "SELECT new_package_no, applied_revision FROM field_package_corrections "
                "WHERE terminal_id = %s AND original_package_no = %s",
                (body.terminal_id, body.original_package_no),
            )
            prior = cur.fetchone()
            if prior is not None:
                raise ApiError(
                    409,
                    "FIELD_PACKAGE_ALREADY_REPLACED",
                    f"终端 {body.terminal_id} 的原包 {body.original_package_no} 已被包 {prior[0]} 替换，不可再次勘误",
                    {
                        "terminal_id": body.terminal_id,
                        "original_package_no": body.original_package_no,
                        "replaced_by_package_no": prior[0],
                        "replaced_at_revision": prior[1],
                    },
                )

            # ---- 新包号须未使用 ----
            cur.execute(
                "SELECT 1 FROM field_packages WHERE terminal_id = %s AND package_no = %s",
                (body.terminal_id, body.new_package_no),
            )
            if cur.fetchone() is not None:
                raise ApiError(
                    409,
                    "FIELD_PACKAGE_NO_TAKEN",
                    f"终端 {body.terminal_id} 的新包号 {body.new_package_no} 已被使用",
                    {"terminal_id": body.terminal_id, "new_package_no": body.new_package_no},
                )

            # ---- 过期修订（首次勘误才检查；幂等重试即使修订已前进也返回原结果）----
            cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE FOR UPDATE")
            current = cur.fetchone()[0]
            if body.expected_revision != current:
                raise ApiError(
                    409,
                    "STALE_REVISION",
                    f"修订号过期：请求携带 {body.expected_revision}，当前为 {current}",
                    {"current_revision": current, "expected_revision": body.expected_revision},
                )
            new_revision = current + 1

            # ---- 原包证据须全部仍生效 ----
            cur.execute(
                "SELECT evidence_id, status FROM assertions WHERE evidence_id = ANY(%s) FOR UPDATE",
                (original_evidence_ids,),
            )
            states = {r[0]: r[1] for r in cur.fetchall()}
            inactive = [eid for eid in original_evidence_ids if states.get(eid) != "active"]
            if inactive:
                raise ApiError(
                    409,
                    "FIELD_PACKAGE_EVIDENCE_INACTIVE",
                    f"原包 {body.original_package_no} 的证据未全部生效，不可整体勘误: {', '.join(inactive)}",
                    {
                        "terminal_id": body.terminal_id,
                        "original_package_no": body.original_package_no,
                        "inactive_evidence_ids": inactive,
                    },
                )

            before_assertions = _load_active(cur)

            # ---- 撤回原包全部证据（只置状态，历史可查）----
            cur.execute(
                "UPDATE assertions SET status = 'retracted', retracted_revision = %s, retracted_at = now() "
                "WHERE evidence_id = ANY(%s)",
                (new_revision, original_evidence_ids),
            )

            # ---- 汇入新包（沿用整包汇入的站点 / 绑定 / 编号规则）----
            new_strata, evidence_by_seq = _ingest_apply(
                cur, body.terminal_id, body.new_package_no, body.site, short_to_full, by_seq, new_revision
            )

            # ---- 撤回 + 汇入后的最终断言集核验（沿用正式批次口径）----
            after_assertions = _load_active(cur)
            conflicts = Derivation(after_assertions).validate()
            if conflicts:
                raise ApiError(
                    409,
                    "FIELD_CORRECTION_CONFLICT",
                    "野外整包勘误后的最终断言集矛盾，整批拒绝，地层 / 绑定 / 断言 / 台账 / 修订均不变",
                    {
                        "terminal_id": body.terminal_id,
                        "original_package_no": body.original_package_no,
                        "new_package_no": body.new_package_no,
                        "current_revision": current,
                        "conflicts": conflicts,
                    },
                )

            # ---- 变化记录：与修订号递增同事务 ----
            cur.execute("SELECT stratum_id FROM strata ORDER BY stratum_id")
            all_strata = [r[0] for r in cur.fetchall()]
            diffs = relation_diffs(all_strata, before_assertions, after_assertions)

            cur.execute("UPDATE revision_counter SET revision = %s WHERE id = TRUE", (new_revision,))
            cur.execute(
                "INSERT INTO revisions (revision, batch) VALUES (%s, %s)",
                (
                    new_revision,
                    Json(
                        {
                            "source": "field_package_correction",
                            "site": body.site,
                            "terminal_id": body.terminal_id,
                            "original_package_no": body.original_package_no,
                            "new_package_no": body.new_package_no,
                            "content_hash": content_hash,
                            "retracted_evidence": original_evidence_ids,
                            "operations": _audit_operations(by_seq, evidence_by_seq),
                        }
                    ),
                ),
            )
            insert_revision_changes(cur, new_revision, diffs, "field_package_correction")

            mappings_payload, evidence_payload = _package_payloads(
                body.site, short_to_full, by_seq, evidence_by_seq, new_strata
            )
            response = {
                "site": body.site,
                "terminal_id": body.terminal_id,
                "original_package_no": body.original_package_no,
                "new_package_no": body.new_package_no,
                "revision": new_revision,
                "retracted_evidence": original_evidence_ids,
                "mappings": mappings_payload,
                "evidence": evidence_payload,
                "applied": len(by_seq),
                "retracted": len(original_evidence_ids),
                "relation_changes": len(diffs),
                "replayed": False,
            }
            # 新包台账的首次响应与直接汇入同构：经 /field-packages 同内容重放可命中
            package_response = {
                "site": body.site,
                "terminal_id": body.terminal_id,
                "package_no": body.new_package_no,
                "revision": new_revision,
                "mappings": mappings_payload,
                "evidence": evidence_payload,
                "applied": len(by_seq),
                "relation_changes": len(diffs),
                "replayed": False,
            }

            # ---- 新包台账（与直接汇入同结构；原包台账行不修改不删除）----
            cur.execute(
                "INSERT INTO field_packages "
                "(terminal_id, package_no, site, content_hash, expected_revision, applied_revision, "
                " mappings, evidence, request, stored_response) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    body.terminal_id,
                    body.new_package_no,
                    body.site,
                    pkg_content_hash,
                    body.expected_revision,
                    new_revision,
                    Json(mappings_payload),
                    Json(evidence_payload),
                    Json(pkg_content),
                    Json(package_response),
                ),
            )
            # ---- 勘误台账（替换链；重试据此原样返回）----
            cur.execute(
                "INSERT INTO field_package_corrections "
                "(terminal_id, original_package_no, new_package_no, content_hash, expected_revision, "
                " applied_revision, retracted_evidence, stored_response) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    body.terminal_id,
                    body.original_package_no,
                    body.new_package_no,
                    content_hash,
                    body.expected_revision,
                    new_revision,
                    Json(original_evidence_ids),
                    Json(response),
                ),
            )
            return response


# ------------------------------------------------------------------ 查询
def _load_active(cur) -> List[Assertion]:
    cur.execute(
        "SELECT evidence_id, stratum_a, stratum_b, relation "
        "FROM assertions WHERE status = 'active' ORDER BY evidence_id"
    )
    return [Assertion(evidence_id=r[0], a=r[1], b=r[2], relation=r[3]) for r in cur.fetchall()]


# 台账列表 / 详情联出替换链：c_orig 为本包被哪次勘误替换，c_new 为本包替换了哪个原包
_LEDGER_JOIN = (
    " FROM field_packages fp "
    " LEFT JOIN field_package_corrections c_orig "
    "   ON c_orig.terminal_id = fp.terminal_id AND c_orig.original_package_no = fp.package_no "
    " LEFT JOIN field_package_corrections c_new "
    "   ON c_new.terminal_id = fp.terminal_id AND c_new.new_package_no = fp.package_no "
)


def list_field_packages() -> List[Dict[str, Any]]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT fp.terminal_id, fp.package_no, fp.site, fp.content_hash, fp.expected_revision, "
                "fp.applied_revision, fp.created_at, c_orig.new_package_no, c_new.original_package_no"
                + _LEDGER_JOIN
                + "ORDER BY fp.terminal_id, fp.package_no"
            )
            return [
                {
                    "terminal_id": r[0],
                    "package_no": r[1],
                    "site": r[2],
                    "content_hash": r[3],
                    "expected_revision": r[4],
                    "revision": r[5],
                    "created_at": r[6].isoformat(),
                    "replaced_by_package_no": r[7],
                    "replaces_package_no": r[8],
                }
                for r in cur.fetchall()
            ]


def get_field_package(terminal_id: str, package_no: str) -> Dict[str, Any]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT fp.site, fp.content_hash, fp.expected_revision, fp.applied_revision, fp.mappings, "
                "fp.evidence, fp.stored_response, fp.created_at, "
                "c_orig.new_package_no, c_new.original_package_no"
                + _LEDGER_JOIN
                + "WHERE fp.terminal_id = %s AND fp.package_no = %s",
                (terminal_id, package_no),
            )
            row = cur.fetchone()
            if row is None:
                raise ApiError(
                    404,
                    "FIELD_PACKAGE_NOT_FOUND",
                    f"野外包不存在: 终端 {terminal_id} 包号 {package_no}",
                    {"terminal_id": terminal_id, "package_no": package_no},
                )
            return {
                "terminal_id": terminal_id,
                "package_no": package_no,
                "site": row[0],
                "content_hash": row[1],
                "expected_revision": row[2],
                "revision": row[3],
                "mappings": row[4],
                "evidence": row[5],
                "response": row[6],
                "created_at": row[7].isoformat(),
                "replaced_by_package_no": row[8],
                "replaces_package_no": row[9],
            }


def list_field_package_corrections() -> List[Dict[str, Any]]:
    """野外整包勘误台账（替换链逐环记录，含首次成功响应）。"""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT terminal_id, original_package_no, new_package_no, content_hash, expected_revision, "
                "applied_revision, retracted_evidence, stored_response, created_at "
                "FROM field_package_corrections ORDER BY terminal_id, original_package_no, new_package_no"
            )
            return [
                {
                    "terminal_id": r[0],
                    "original_package_no": r[1],
                    "new_package_no": r[2],
                    "content_hash": r[3],
                    "expected_revision": r[4],
                    "revision": r[5],
                    "retracted_evidence": r[6],
                    "response": r[7],
                    "created_at": r[8].isoformat(),
                }
                for r in cur.fetchall()
            ]
