"""Current review decisions validate identities, obligations and source-local support."""

import copy

import pytest
from protocol_fixtures import contract, decision
from pydantic import ValidationError

from agent.runtime.context import RunContext, reset_run_context, set_run_context
from agent.runtime.policies import DecisionRejected, load_runtime_policy
from agent.runtime.requirements import RequirementSpec, register
from agent.runtime.review_contract import ContractViolation, Finding, ReviewDecision, _required_quote
from agent.runtime.review_state import deliverable


@pytest.fixture
def context(sandbox_dir):
    value = RunContext(
        "review", "session", sandbox_dir, policy=load_runtime_policy(), user_query="核实 Agent A 的工具调用"
    )
    marker = set_run_context(value)
    try:
        yield value
    finally:
        reset_run_context(marker)


def test_per_fact_review_returns_only_checked_facts(context):
    value = contract(context).resolve(ReviewDecision.model_validate(decision()))
    assert value["verdict"] == "passed" and deliverable(value["state"])
    assert len(value["accepted_report"]["facts"]) == 1


def test_foreign_or_missing_fact_judgment_is_rejected(context):
    scope = contract(context)
    for changed in (dict(decision(), facts=[]), decision(fact_id="foreign")):
        with pytest.raises(ContractViolation, match="exactly once"):
            scope.resolve(ReviewDecision.model_validate(changed))


def test_legacy_review_output_is_rejected_instead_of_silently_accepted():
    with pytest.raises(ValidationError):
        ReviewDecision.model_validate({"verdict": "passed", "findings": []})
    with pytest.raises(ValidationError):
        Finding.model_validate({"kind": "unsupported_claim", "claim_quote": "old protocol"})


def test_source_quotes_cannot_bind_a_foreign_object(context):
    with pytest.raises(ContractViolation, match="identity"):
        contract(context).resolve(ReviewDecision.model_validate(decision(object_id="foreign")))


def test_unverified_fact_cannot_satisfy_a_required_obligation(context):
    req = register(context, None, [RequirementSpec(anchor="工具调用", label="工具调用", kind="fact")], owner="main")[0]
    scope = contract(context, requirements=[req])
    value = decision(
        verdict="insufficient", coverage={req["rid"]: {"status": "covered", "fact_ids": ["f1"], "claim_ids": []}}
    )
    with pytest.raises(ContractViolation, match="support contract|ineligible"):
        scope.resolve(ReviewDecision.model_validate(value))


def test_covered_requires_real_nonempty_support(context):
    req = register(context, None, [RequirementSpec(anchor="工具调用", label="工具调用", kind="fact")], owner="main")[0]
    scope = contract(context, requirements=[req])
    invalid = decision(coverage={req["rid"]: {"status": "covered", "fact_ids": [], "claim_ids": []}})
    with pytest.raises(ContractViolation, match="nonempty"):
        scope.resolve(ReviewDecision.model_validate(invalid))


def test_coverage_cannot_borrow_another_requirement(context):
    with pytest.raises(ContractViolation, match="coverage"):
        contract(context).resolve(
            ReviewDecision.model_validate(
                decision(coverage={"foreign": {"status": "missing", "fact_ids": [], "claim_ids": []}})
            )
        )


def test_checking_failure_does_not_discard_other_valid_facts(context):
    scope = contract(context)
    value = scope.resolve(ReviewDecision.model_validate(decision(verdict="insufficient")))
    assert value["accepted_report"]["facts"] == [] and not deliverable(value["state"])


@pytest.mark.parametrize(
    "source,quote",
    [
        ("核实工具调用，优先补充英文Glossary", "Glossary"),
        ("比较价格，不要求发布日期", "发布日期"),
    ],
)
def test_method_preferences_do_not_become_required_obligations(source, quote):
    assert not _required_quote(quote, source)


def test_register_rejects_unanchored_and_duplicate_obligations(context):
    item = RequirementSpec(anchor="工具调用", label="工具调用", kind="fact")
    register(context, None, [item], owner="main")
    with pytest.raises(DecisionRejected, match="Duplicate"):
        register(context, None, [item], owner="main")
    with pytest.raises(DecisionRejected, match="exact required"):
        register(context, None, [RequirementSpec(anchor="额外性能指标", label="额外", kind="fact")], owner="main")


def test_parallel_contracts_keep_their_own_candidate_snapshots(context):
    first, second = contract(context), contract(context)
    value = copy.deepcopy(decision())
    first.resolve(ReviewDecision.model_validate(value))
    assert first is not second and second.report["facts"][0]["id"] == "f1"
    assert decision() == value


def test_generated_finding_schema_requires_fact_id_for_nonmissing_findings(context):
    from test_framework_contract import ScriptedModel

    from agent.reflection import ResultReviewer
    from agent.subagent_registry import ReviewConfig

    scope = contract(context)
    reviewer = ResultReviewer(ReviewConfig(enabled=True, model_check=True), ScriptedModel(responses=[]))
    schema = reviewer._model_kwargs(30, scope)["response_format"]["json_schema"]["schema"]
    variants = schema["properties"]["findings"]["items"]["anyOf"]
    assert all("" not in v["properties"]["fact_id"]["enum"] for v in variants)
    assert all("missing_requirement" not in v["properties"]["kind"]["enum"] for v in variants)
