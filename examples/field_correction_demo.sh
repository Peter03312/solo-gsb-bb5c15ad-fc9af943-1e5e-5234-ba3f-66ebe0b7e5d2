#!/usr/bin/env bash
# 考古地层关系核验服务 —— 野外终端整包勘误端到端示例
# 前提：服务已启动（docker compose up --build），监听 http://localhost:8000
#
# 演示：
#   1) 终端整包汇入后发现记录有误，先定版留底；
#   2) 整包勘误：单事务撤回原包全部证据、以新包号汇入替代内容，正式修订只 +1；
#   3) 相同（原包, 新包）+ 相同内容重试幂等返回首次结果（即使修订已前进、预期号过期）；
#   4) 异内容复用 / 原包已被替换 / 新包号已使用 / 原包不存在 / 新包号同原包号 / 过期修订；
#   5) 原包证据被其他批次撤回后不可再整体勘误（FIELD_PACKAGE_EVIDENCE_INACTIVE）；
#   6) 勘误后最终断言集冲突整批拒绝（FIELD_CORRECTION_CONFLICT），状态不变；
#   7) 新包沿用跨包绑定规则（改绑拒绝 SHORT_CODE_REBOUND）与稳定证据编号规则；
#   8) 替换链：原包 -> 新包 -> 再勘误；台账与勘误台账可查；
#   9) 原包原请求重放仍返回首次汇入结果；勘误前定版结论保持不变。
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pp() { python3 -m json.tool 2>/dev/null || cat; }
T="FC-DEMO-T11"

cur_rev() { curl -s "$BASE/revision" | python3 -c 'import sys,json;print(json.load(sys.stdin)["revision"])'; }

echo "== 0. 健康检查 / 当前修订号 =="
curl -s "$BASE/health"; echo
REV="$(cur_rev)"
echo "当前正式修订号：$REV"

echo; echo "== 1. 整包汇入：站点=北岗，终端 $T，包号 2026-09-05-01 =="
echo "   短号 g1~g2 同一期，g2 早于 g3（事后发现方向记反，将勘误）。"
curl -s -X POST "$BASE/field-packages" -H 'Content-Type: application/json' -d "{
  \"site\": \"北岗\",
  \"terminal_id\": \"$T\",
  \"package_no\": \"2026-09-05-01\",
  \"expected_revision\": $REV,
  \"mappings\": [
    {\"short_code\": \"g1\", \"stratum_id\": \"BG-01\"},
    {\"short_code\": \"g2\", \"stratum_id\": \"BG-02\"},
    {\"short_code\": \"g3\", \"stratum_id\": \"BG-03\"}
  ],
  \"assertions\": [
    {\"seq\": 1, \"stratum_a\": \"g1\", \"stratum_b\": \"g2\", \"relation\": \"same_period\"},
    {\"seq\": 2, \"stratum_a\": \"g2\", \"stratum_b\": \"g3\", \"relation\": \"earlier_than\"}
  ]}" | pp

echo; echo "== 2. 勘误前定版留底（历史定版在勘误后保持可查、结论不变） =="
curl -s -X POST "$BASE/snapshots" -H 'Content-Type: application/json' -d '{"label": "北岗勘误前定版"}' | pp

echo; echo "== 3. 整包勘误：原包 2026-09-05-01 -> 新包 2026-09-05-01-c1 =="
echo "   单事务撤回原包 2 条证据、以新包号汇入更正内容（g3 早于 g2），正式修订只 +1。"
REV="$(cur_rev)"
curl -s -X POST "$BASE/field-packages/corrections" -H 'Content-Type: application/json' -d "{
  \"terminal_id\": \"$T\",
  \"original_package_no\": \"2026-09-05-01\",
  \"new_package_no\": \"2026-09-05-01-c1\",
  \"expected_revision\": $REV,
  \"site\": \"北岗\",
  \"mappings\": [
    {\"short_code\": \"g1\", \"stratum_id\": \"BG-01\"},
    {\"short_code\": \"g2\", \"stratum_id\": \"BG-02\"},
    {\"short_code\": \"g3\", \"stratum_id\": \"BG-03\"}
  ],
  \"assertions\": [
    {\"seq\": 1, \"stratum_a\": \"g1\", \"stratum_b\": \"g2\", \"relation\": \"same_period\"},
    {\"seq\": 2, \"stratum_a\": \"g3\", \"stratum_b\": \"g2\", \"relation\": \"earlier_than\"}
  ]}" | pp

echo; echo "== 4. 勘误生效：BG-03 与 BG-02 现关系（应：早于，证据为新包稳定编号） =="
curl -s "$BASE/relationship?a=BG-03&b=BG-02" | pp
echo "-- 原包证据已撤回（retracted），新包证据生效（active）："
curl -s "$BASE/assertions" | python3 -c '
import sys, json
for r in json.load(sys.stdin):
    if r["stratum_a"].startswith("BG-"):
        print("  %s  %s->%s %s  %s" % (r["evidence_id"], r["stratum_a"], r["stratum_b"], r["relation"], r["status"]))'

echo; echo "== 5. 幂等重试：同（原包, 新包）+ 同内容，expected_revision 故意写 0（已过期） =="
echo "   返回首次勘误结果且 replayed=true，修订不再前进。"
curl -s -X POST "$BASE/field-packages/corrections" -H 'Content-Type: application/json' -d "{
  \"terminal_id\": \"$T\",
  \"original_package_no\": \"2026-09-05-01\",
  \"new_package_no\": \"2026-09-05-01-c1\",
  \"expected_revision\": 0,
  \"site\": \"北岗\",
  \"mappings\": [
    {\"short_code\": \"g1\", \"stratum_id\": \"BG-01\"},
    {\"short_code\": \"g2\", \"stratum_id\": \"BG-02\"},
    {\"short_code\": \"g3\", \"stratum_id\": \"BG-03\"}
  ],
  \"assertions\": [
    {\"seq\": 1, \"stratum_a\": \"g1\", \"stratum_b\": \"g2\", \"relation\": \"same_period\"},
    {\"seq\": 2, \"stratum_a\": \"g3\", \"stratum_b\": \"g2\", \"relation\": \"earlier_than\"}
  ]}" | pp

echo; echo "== 6. 拒绝分支 =="
echo "-- 6.1 同（原包, 新包）异内容复用：FIELD_CORRECTION_CONTENT_MISMATCH（409）"
curl -s -X POST "$BASE/field-packages/corrections" -H 'Content-Type: application/json' -d "{
  \"terminal_id\": \"$T\",
  \"original_package_no\": \"2026-09-05-01\",
  \"new_package_no\": \"2026-09-05-01-c1\",
  \"expected_revision\": 0,
  \"site\": \"北岗\",
  \"mappings\": [
    {\"short_code\": \"g1\", \"stratum_id\": \"BG-01\"},
    {\"short_code\": \"g2\", \"stratum_id\": \"BG-02\"}
  ],
  \"assertions\": [
    {\"seq\": 1, \"stratum_a\": \"g1\", \"stratum_b\": \"g2\", \"relation\": \"earlier_than\"}
  ]}" | pp || true

echo "-- 6.2 原包已被替换（再勘误原包 2026-09-05-01）：FIELD_PACKAGE_ALREADY_REPLACED（409）"
curl -s -X POST "$BASE/field-packages/corrections" -H 'Content-Type: application/json' -d "{
  \"terminal_id\": \"$T\",
  \"original_package_no\": \"2026-09-05-01\",
  \"new_package_no\": \"2026-09-05-01-c9\",
  \"expected_revision\": 0,
  \"site\": \"北岗\",
  \"mappings\": [
    {\"short_code\": \"g1\", \"stratum_id\": \"BG-01\"},
    {\"short_code\": \"g2\", \"stratum_id\": \"BG-02\"}
  ],
  \"assertions\": [
    {\"seq\": 1, \"stratum_a\": \"g1\", \"stratum_b\": \"g2\", \"relation\": \"same_period\"}
  ]}" | pp || true

echo "-- 6.3 原包不存在：FIELD_PACKAGE_NOT_FOUND（404）"
curl -s -X POST "$BASE/field-packages/corrections" -H 'Content-Type: application/json' -d "{
  \"terminal_id\": \"$T\",
  \"original_package_no\": \"2099-01-01-99\",
  \"new_package_no\": \"2099-01-01-99-c1\",
  \"expected_revision\": 0,
  \"site\": \"北岗\",
  \"mappings\": [{\"short_code\": \"x1\", \"stratum_id\": \"BG-X1\"}, {\"short_code\": \"x2\", \"stratum_id\": \"BG-X2\"}],
  \"assertions\": [{\"seq\": 1, \"stratum_a\": \"x1\", \"stratum_b\": \"x2\", \"relation\": \"same_period\"}]
  }" | pp || true

echo "-- 6.4 新包号与原包号相同：FIELD_CORRECTION_SAME_PACKAGE（400）"
curl -s -X POST "$BASE/field-packages/corrections" -H 'Content-Type: application/json' -d "{
  \"terminal_id\": \"$T\",
  \"original_package_no\": \"2026-09-05-01-c1\",
  \"new_package_no\": \"2026-09-05-01-c1\",
  \"expected_revision\": 0,
  \"site\": \"北岗\",
  \"mappings\": [{\"short_code\": \"g1\", \"stratum_id\": \"BG-01\"}, {\"short_code\": \"g2\", \"stratum_id\": \"BG-02\"}],
  \"assertions\": [{\"seq\": 1, \"stratum_a\": \"g1\", \"stratum_b\": \"g2\", \"relation\": \"same_period\"}]
  }" | pp || true

echo "-- 6.5 新包沿用跨包绑定规则：短号 g1 已绑 BG-01，请求改绑 BG-99 -> SHORT_CODE_REBOUND（409）"
REV="$(cur_rev)"
curl -s -X POST "$BASE/field-packages/corrections" -H 'Content-Type: application/json' -d "{
  \"terminal_id\": \"$T\",
  \"original_package_no\": \"2026-09-05-01-c1\",
  \"new_package_no\": \"2026-09-05-01-c2\",
  \"expected_revision\": $REV,
  \"site\": \"北岗\",
  \"mappings\": [
    {\"short_code\": \"g1\", \"stratum_id\": \"BG-99\"},
    {\"short_code\": \"g2\", \"stratum_id\": \"BG-02\"}
  ],
  \"assertions\": [{\"seq\": 1, \"stratum_a\": \"g1\", \"stratum_b\": \"g2\", \"relation\": \"same_period\"}]
  }" | pp || true

echo; echo "== 7. 第二个包：先汇入，再演示新包号占用 / 过期修订 / 证据失效三类拒绝 =="
REV="$(cur_rev)"
curl -s -X POST "$BASE/field-packages" -H 'Content-Type: application/json' -d "{
  \"site\": \"北岗\",
  \"terminal_id\": \"$T\",
  \"package_no\": \"2026-09-06-01\",
  \"expected_revision\": $REV,
  \"mappings\": [
    {\"short_code\": \"h1\", \"stratum_id\": \"BG-10\"},
    {\"short_code\": \"h2\", \"stratum_id\": \"BG-11\"}
  ],
  \"assertions\": [
    {\"seq\": 1, \"stratum_a\": \"h1\", \"stratum_b\": \"h2\", \"relation\": \"same_period\"}
  ]}" | pp

echo "-- 7.1 新包号已被使用（2026-09-05-01-c1 是既有包号）：FIELD_PACKAGE_NO_TAKEN（409）"
REV="$(cur_rev)"
curl -s -X POST "$BASE/field-packages/corrections" -H 'Content-Type: application/json' -d "{
  \"terminal_id\": \"$T\",
  \"original_package_no\": \"2026-09-06-01\",
  \"new_package_no\": \"2026-09-05-01-c1\",
  \"expected_revision\": $REV,
  \"site\": \"北岗\",
  \"mappings\": [{\"short_code\": \"h1\", \"stratum_id\": \"BG-10\"}],
  \"assertions\": [{\"seq\": 1, \"stratum_a\": \"h1\", \"stratum_b\": \"h1\", \"relation\": \"same_period\"}]
  }" | pp || true

echo "-- 7.2 首次勘误携带过期修订号：STALE_REVISION（409，幂等重试不受此限，见第 5 步）"
curl -s -X POST "$BASE/field-packages/corrections" -H 'Content-Type: application/json' -d "{
  \"terminal_id\": \"$T\",
  \"original_package_no\": \"2026-09-06-01\",
  \"new_package_no\": \"2026-09-06-01-c1\",
  \"expected_revision\": 0,
  \"site\": \"北岗\",
  \"mappings\": [{\"short_code\": \"h1\", \"stratum_id\": \"BG-10\"}],
  \"assertions\": [{\"seq\": 1, \"stratum_a\": \"h1\", \"stratum_b\": \"h1\", \"relation\": \"same_period\"}]
  }" | pp || true

echo "-- 7.3 原包证据被正式批次撤回一条后，不可再整体勘误：FIELD_PACKAGE_EVIDENCE_INACTIVE（409）"
EVID="$(curl -s "$BASE/assertions?status=active" | python3 -c 'import sys,json;ids=[a["evidence_id"] for a in json.load(sys.stdin) if a["stratum_a"]=="BG-10"];print(ids[0] if ids else "")')"
if [ -n "$EVID" ]; then
  echo "   先经正式批次撤回 $EVID："
  REV="$(cur_rev)"
  curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d "{
    \"expected_revision\": $REV,
    \"operations\": [{\"op\": \"retract\", \"evidence_id\": \"$EVID\"}]}" | pp
else
  echo "   （该证据已在之前的演示运行中撤回，直接重试勘误）"
fi
REV="$(cur_rev)"
curl -s -X POST "$BASE/field-packages/corrections" -H 'Content-Type: application/json' -d "{
  \"terminal_id\": \"$T\",
  \"original_package_no\": \"2026-09-06-01\",
  \"new_package_no\": \"2026-09-06-01-c1\",
  \"expected_revision\": $REV,
  \"site\": \"北岗\",
  \"mappings\": [{\"short_code\": \"h1\", \"stratum_id\": \"BG-10\"}],
  \"assertions\": [{\"seq\": 1, \"stratum_a\": \"h1\", \"stratum_b\": \"h1\", \"relation\": \"same_period\"}]
  }" | pp || true

echo; echo "== 8. 勘误后最终断言集冲突：整批拒绝，地层 / 绑定 / 断言 / 台账 / 修订均不变 =="
REV="$(cur_rev)"
curl -s -X POST "$BASE/field-packages" -H 'Content-Type: application/json' -d "{
  \"site\": \"北岗\",
  \"terminal_id\": \"$T\",
  \"package_no\": \"2026-09-07-01\",
  \"expected_revision\": $REV,
  \"mappings\": [
    {\"short_code\": \"k1\", \"stratum_id\": \"BG-20\"},
    {\"short_code\": \"k2\", \"stratum_id\": \"BG-21\"}
  ],
  \"assertions\": [
    {\"seq\": 1, \"stratum_a\": \"k1\", \"stratum_b\": \"k2\", \"relation\": \"same_period\"}
  ]}" | pp
echo "   现关系 BG-03 早于 BG-01（BG-03<BG-02~BG-01）。勘误新包若补 BG-01 早于 BG-03 将成环："
REV="$(cur_rev)"
curl -s -X POST "$BASE/field-packages/corrections" -H 'Content-Type: application/json' -d "{
  \"terminal_id\": \"$T\",
  \"original_package_no\": \"2026-09-07-01\",
  \"new_package_no\": \"2026-09-07-01-c1\",
  \"expected_revision\": $REV,
  \"site\": \"北岗\",
  \"mappings\": [
    {\"short_code\": \"k1\", \"stratum_id\": \"BG-20\"},
    {\"short_code\": \"k2\", \"stratum_id\": \"BG-21\"},
    {\"short_code\": \"g1\", \"stratum_id\": \"BG-01\"},
    {\"short_code\": \"g3\", \"stratum_id\": \"BG-03\"}
  ],
  \"assertions\": [
    {\"seq\": 1, \"stratum_a\": \"k1\", \"stratum_b\": \"k2\", \"relation\": \"same_period\"},
    {\"seq\": 2, \"stratum_a\": \"g1\", \"stratum_b\": \"g3\", \"relation\": \"earlier_than\"}
  ]}" | pp || true
echo "   修订号未变（拒绝前 $REV，拒绝后 $(cur_rev)），原包证据仍全部生效："
curl -s "$BASE/assertions?status=active" | python3 -c '
import sys, json
print("  active:", [a["evidence_id"] for a in json.load(sys.stdin) if a["stratum_a"] == "BG-20"])'

echo; echo "== 9. 替换链：新包 2026-09-05-01-c1 再被勘误为 2026-09-05-01-c2 =="
REV="$(cur_rev)"
curl -s -X POST "$BASE/field-packages/corrections" -H 'Content-Type: application/json' -d "{
  \"terminal_id\": \"$T\",
  \"original_package_no\": \"2026-09-05-01-c1\",
  \"new_package_no\": \"2026-09-05-01-c2\",
  \"expected_revision\": $REV,
  \"site\": \"北岗\",
  \"mappings\": [
    {\"short_code\": \"g1\", \"stratum_id\": \"BG-01\"},
    {\"short_code\": \"g2\", \"stratum_id\": \"BG-02\"},
    {\"short_code\": \"g3\", \"stratum_id\": \"BG-03\"}
  ],
  \"assertions\": [
    {\"seq\": 1, \"stratum_a\": \"g1\", \"stratum_b\": \"g2\", \"relation\": \"same_period\"},
    {\"seq\": 2, \"stratum_a\": \"g2\", \"stratum_b\": \"g3\", \"relation\": \"same_period\"}
  ]}" | pp

echo; echo "== 10. 原包原请求重放：仍返回首次汇入结果（replayed=true，原修订号） =="
curl -s -X POST "$BASE/field-packages" -H 'Content-Type: application/json' -d "{
  \"site\": \"北岗\",
  \"terminal_id\": \"$T\",
  \"package_no\": \"2026-09-05-01\",
  \"expected_revision\": 0,
  \"mappings\": [
    {\"short_code\": \"g1\", \"stratum_id\": \"BG-01\"},
    {\"short_code\": \"g2\", \"stratum_id\": \"BG-02\"},
    {\"short_code\": \"g3\", \"stratum_id\": \"BG-03\"}
  ],
  \"assertions\": [
    {\"seq\": 1, \"stratum_a\": \"g1\", \"stratum_b\": \"g2\", \"relation\": \"same_period\"},
    {\"seq\": 2, \"stratum_a\": \"g2\", \"stratum_b\": \"g3\", \"relation\": \"earlier_than\"}
  ]}" | pp

echo; echo "== 11. 历史定版保持可查且结论不变：定版 1 中 BG-02 与 BG-03 仍为 早于 =="
echo "   （现库经 2026-09-05-01-c2 已改为同一期，旧定版不受后续勘误影响）"
curl -s "$BASE/snapshots/1/relationship?a=BG-02&b=BG-03" | pp
curl -s "$BASE/relationship?a=BG-02&b=BG-03" | pp

echo; echo "== 12. 台账与替换链 =="
echo "-- 包台账（replaces_package_no / replaced_by_package_no 串起替换链）："
curl -s "$BASE/field-packages" | python3 -c '
import sys, json
for p in json.load(sys.stdin):
    if p["terminal_id"] == "FC-DEMO-T11":
        print("  %-22s replaces=%-22s replaced_by=%s" % (p["package_no"], p["replaces_package_no"], p["replaced_by_package_no"]))'
echo "-- 勘误台账（GET /field-packages/corrections）："
curl -s "$BASE/field-packages/corrections" | pp
echo "-- 原包详情（被替换标记仍在，首次响应保留）："
curl -s "$BASE/field-packages/$T/2026-09-05-01" | pp

echo; echo "== 13. 变化追踪：勘误产生的修订 source=field_package_correction =="
curl -s "$BASE/revision-changes?limit=1000" | python3 -c '
import sys, json
rows = [c for c in json.load(sys.stdin)["changes"] if c["source"] == "field_package_correction"]
for c in rows[:6]:
    print("  rev %s: %s ~ %s  %s -> %s" % (c["revision"], c["stratum_a"], c["stratum_b"], c["old_relation"], c["new_relation"]))
print("  … 共 %d 条勘误变化记录" % len(rows))'

echo; echo "全部野外整包勘误示例执行完毕。"
