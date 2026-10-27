# 步骤 1：保留的失败清单

后续实现与当前验证结果见 [基本多智能体修复记录](39-basic-multi-agent-repair.md)；本文保留步骤 1 当时的执行记录，不能代表当前源码仍存在相同失败。

日期：2026-10-27。结果来自完整 625 项执行及最后 5 项引用夹具的定向复验，未伪装成第二次全量运行。

汇总：442 通过、183 失败、0 个夹具错误；没有 skip、xfail 或失败豁免。原有 220 项通过用例没有退化。

分类：125 项匹配既有旧契约阻塞规则；2 项已确认实现缺陷；56 项待复核。分类不改变失败状态，也不证明被提前阻塞的下游行为。

完整原始报告：[全量报告](../../output/verification/step1-final/report.md)；[5 项夹具复验](../../output/verification/step1-answer-review.xml)；[按用例合并并保留来源的报告](../../output/verification/step1-reviewed/report.json)。

| 用例 | 状态 | 归因 | 原因/处理义务 |
| --- | --- | --- | --- |
| `tests.test_acceptance14::test_model_timeout_with_cancellation_cause_is_dependency_failure_without_content_retry` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_acceptance14::test_output_instructions_and_markdown_work_with_isolated_storage` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_call_budget::test_actual_task_model_tool_and_total_counts` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_delegation::test_before_delegate_is_at_actual_executor_boundary` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_delegation::test_empty_descriptions_rejected_before_route[   ]` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_delegation::test_empty_descriptions_rejected_before_route[]` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_delegation::test_executor_failure_cancellation_and_invalid_return_saved[bad_result]` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_delegation::test_executor_failure_cancellation_and_invalid_return_saved[cancelled]` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_delegation::test_executor_failure_cancellation_and_invalid_return_saved[failed]` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_delegation::test_expired_deadline_stops_executor` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_delegation::test_extra_task_prompt_cannot_silently_drop_constraints_and_corrected_description_reaches_child[False]` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_delegation::test_extra_task_prompt_cannot_silently_drop_constraints_and_corrected_description_reaches_child[True]` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_delegation::test_factory_child_cannot_delegate_to_unregistered_generic_agent` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_delegation::test_factory_child_tool_rejection_prevents_actual_business_function` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_delegation::test_factory_rejects_unbudgeted_sdk_retries` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_delegation::test_http_history_contains_subtasks_and_chinese_display_names` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_delegation::test_late_result_cannot_overwrite_cancelled_parent_or_terminal_subtask` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_delegation::test_parallel_sessions_and_rounds_never_mix` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_delegation::test_parallel_task_calls_are_linked_without_duplicate_hooks` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_delegation::test_registry_validation_and_import_contract` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_delegation::test_reused_model_call_id_gets_distinct_server_ids` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_delegation::test_same_call_id_cannot_be_moved_to_another_run` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_delegation::test_task_row_and_event_roll_back_together` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_delegation::test_two_real_task_routes_and_durable_server_ids[False]` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_delegation::test_two_real_task_routes_and_durable_server_ids[True]` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_delegation::test_unknown_and_default_targets_rejected_before_routing[\u8054\u7f51\u641c\u7d22\u52a9\u624b]` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_delegation::test_unknown_and_default_targets_rejected_before_routing[general-purpose]` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_delegation::test_unknown_and_default_targets_rejected_before_routing[knowledge]` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_delegation::test_unknown_and_default_targets_rejected_before_routing[missing]` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_execution_governance::test_accepted_plan_with_only_a_promise_cannot_pass_final_source_gate` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_execution_governance::test_final_citation_and_fact_repairs_each_have_one_slot` | failure | 旧契约待迁移 | Raw legacy evidence dictionaries do not satisfy the versioned evidence contract. |
| `tests.test_execution_governance::test_rate_limit_uses_header_and_will_not_sleep_past_deadline` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_execution_governance::test_recovery_retains_tool_turn_and_provider_identity` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_execution_governance::test_registered_scope_cannot_be_rewritten_to_refresh_budget` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_execution_governance::test_registered_source_whitespace_does_not_consume_a_revision[[SOURCE: {upper}]]` | failure | 旧契约待迁移 | Raw legacy evidence dictionaries do not satisfy the versioned evidence contract. |
| `tests.test_execution_governance::test_registered_source_whitespace_does_not_consume_a_revision[[source:  {id}  ]]` | failure | 旧契约待迁移 | Raw legacy evidence dictionaries do not satisfy the versioned evidence contract. |
| `tests.test_execution_governance::test_registered_source_whitespace_does_not_consume_a_revision[[source: {id}]]` | failure | 旧契约待迁移 | Raw legacy evidence dictionaries do not satisfy the versioned evidence contract. |
| `tests.test_execution_governance::test_same_model_turn_has_a_separate_tool_cap` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_execution_governance::test_source_ids_render_exact_addresses_and_bad_links_get_precise_feedback` | failure | 旧契约待迁移 | Raw legacy evidence dictionaries do not satisfy the versioned evidence contract. |
| `tests.test_execution_governance::test_tool_timeout_is_independent_of_run_deadline` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_execution_governance::test_transients_have_exponential_jitter_and_attempt_budget[408]` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_execution_governance::test_transients_have_exponential_jitter_and_attempt_budget[500]` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_execution_governance::test_transients_have_exponential_jitter_and_attempt_budget[501]` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_execution_governance::test_transients_have_exponential_jitter_and_attempt_budget[502]` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_execution_governance::test_transients_have_exponential_jitter_and_attempt_budget[503]` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_execution_governance::test_transients_have_exponential_jitter_and_attempt_budget[504]` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_execution_governance::test_unknown_source_id_is_not_satisfied_by_another_valid_url` | failure | 旧契约待迁移 | Raw legacy evidence dictionaries do not satisfy the versioned evidence contract. |
| `tests.test_execution_governance::test_unknown_tool_is_never_executed_and_does_not_erase_real_evidence` | failure | 旧契约待迁移 | Raw legacy evidence dictionaries do not satisfy the versioned evidence contract. |
| `tests.test_fact_reports::test_approved_quote_survives_compression_without_mutating_evidence` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_fact_reports::test_duplicate_fact_ids_and_excess_quote_budget_are_rejected` | failure | 旧契约待迁移 | Report fixtures must use runtime-owned quote IDs and the current evidence protocol. |
| `tests.test_fact_reports::test_empty_report_requires_a_disclosed_gap` | failure | 旧契约待迁移 | Fixture must establish its actual delegated task owner before report parsing. |
| `tests.test_fact_reports::test_failed_or_unexecuted_calls_do_not_support_a_fact[completed-False]` | failure | 旧契约待迁移 | Report fixtures must use runtime-owned quote IDs and the current evidence protocol. |
| `tests.test_fact_reports::test_failed_or_unexecuted_calls_do_not_support_a_fact[failed-True]` | failure | 旧契约待迁移 | Report fixtures must use runtime-owned quote IDs and the current evidence protocol. |
| `tests.test_fact_reports::test_failed_or_unexecuted_calls_do_not_support_a_fact[rejected-False]` | failure | 旧契约待迁移 | Report fixtures must use runtime-owned quote IDs and the current evidence protocol. |
| `tests.test_fact_reports::test_incomplete_or_invalid_research_never_enters_report_generation[False]` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_fact_reports::test_incomplete_or_invalid_research_never_enters_report_generation[True]` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_fact_reports::test_invalid_quote_is_recorded_for_its_own_fact[evidence]` | failure | 旧契约待迁移 | Report fixtures must use runtime-owned quote IDs and the current evidence protocol. |
| `tests.test_fact_reports::test_invalid_quote_is_recorded_for_its_own_fact[identity_evidence]` | failure | 旧契约待迁移 | Report fixtures must use runtime-owned quote IDs and the current evidence protocol. |
| `tests.test_fact_reports::test_missing_or_unproved_effective_date_is_fact_local` | failure | 旧契约待迁移 | Report fixtures must use runtime-owned quote IDs and the current evidence protocol. |
| `tests.test_fact_reports::test_model_phase_records_sizes_usage_and_failures_without_prompt_bodies[None]` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_fact_reports::test_model_phase_records_sizes_usage_and_failures_without_prompt_bodies[cancelled]` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_fact_reports::test_one_invalid_fact_does_not_erase_valid_sibling_evidence` | failure | 旧契约待迁移 | Report fixtures must use runtime-owned quote IDs and the current evidence protocol. |
| `tests.test_fact_reports::test_report_has_verbatim_local_value_and_identity_evidence` | failure | 旧契约待迁移 | Report fixtures must use runtime-owned quote IDs and the current evidence protocol. |
| `tests.test_fact_reports::test_report_revision_has_schema_but_no_research_history_or_tools[False]` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_fact_reports::test_report_revision_has_schema_but_no_research_history_or_tools[True]` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_fact_reports::test_report_type_error_explains_array_requirement_without_echoing_input` | failure | 旧契约待迁移 | Report fixtures must use runtime-owned quote IDs and the current evidence protocol. |
| `tests.test_fact_reports::test_undated_conflicting_values_do_not_get_ranked_by_retrieval_time` | failure | 旧契约待迁移 | Report fixtures must use runtime-owned quote IDs and the current evidence protocol. |
| `tests.test_hook_pipeline::test_actual_graph_default_tools_and_custom_file_tool_cannot_bypass` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_hook_pipeline::test_production_builder_and_persisted_child_correlation` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_hook_pipeline::test_real_task_routes_child_tools_and_returns_once[False]` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_hook_pipeline::test_real_task_routes_child_tools_and_returns_once[True]` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_output_boundary::test_failed_attempts_never_become_sources_and_valid_siblings_remain_usable[execution]` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_output_boundary::test_failed_attempts_never_become_sources_and_valid_siblings_remain_usable[invalid_argument]` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_output_boundary::test_failed_attempts_never_become_sources_and_valid_siblings_remain_usable[permission]` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_output_boundary::test_one_genuine_reference_cannot_hide_an_invented_reference` | failure | 旧契约待迁移 | Raw legacy evidence dictionaries do not satisfy the versioned evidence contract. |
| `tests.test_result_handoff::test_ended_parent_keeps_late_original_without_publishing` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_result_handoff::test_handoff_does_not_mutate_inputs_and_preserves_summary_on_refinement` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_result_handoff::test_http_history_keeps_internal_and_external_results_separate` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_result_handoff::test_large_original_is_saved_in_run_directory_and_reopens` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_result_handoff::test_real_framework_routes_db_summary_into_search[False]` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_result_handoff::test_real_framework_routes_db_summary_into_search[True]` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_result_handoff::test_refinement_keeps_failed_original_and_hands_off_only_passed_snapshot` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_result_handoff::test_result_and_event_are_atomic_on_storage_failure` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_result_handoff::test_result_identity_and_file_path_are_validated` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_result_handoff::test_result_metadata_unknown_is_not_guessed_and_secrets_are_redacted` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_result_handoff::test_retired_source_history_is_preserved_but_never_handed_to_search` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_result_handoff::test_same_thread_next_run_and_concurrent_sessions_do_not_share` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_result_handoff::test_search_cannot_forge_server_result_and_external_is_not_internal` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_result_handoff::test_unqualified_results_are_traceable_but_never_handed_off[check_incomplete]` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_result_handoff::test_unqualified_results_are_traceable_but_never_handed_off[dependency_unavailable]` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_result_handoff::test_unqualified_results_are_traceable_but_never_handed_off[insufficient]` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_result_handoff::test_unqualified_results_are_traceable_but_never_handed_off[not_checked]` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_result_handoff::test_v3_migration_preserves_existing_runs_events_and_subtasks` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_result_handoff::test_v4_migration_preserves_checked_result_foreign_keys` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_review_contract::test_checking_failure_does_not_discard_other_valid_facts` | failure | 旧契约待迁移 | Report fixtures must use runtime-owned quote IDs and the current evidence protocol. |
| `tests.test_review_contract::test_coverage_cannot_borrow_another_requirement` | failure | 旧契约待迁移 | Report fixtures must use runtime-owned quote IDs and the current evidence protocol. |
| `tests.test_review_contract::test_covered_requires_real_nonempty_support` | failure | 旧契约待迁移 | Report fixtures must use runtime-owned quote IDs and the current evidence protocol. |
| `tests.test_review_contract::test_foreign_or_missing_fact_judgment_is_rejected` | failure | 旧契约待迁移 | Report fixtures must use runtime-owned quote IDs and the current evidence protocol. |
| `tests.test_review_contract::test_generated_finding_schema_requires_fact_id_for_nonmissing_findings` | failure | 旧契约待迁移 | Report fixtures must use runtime-owned quote IDs and the current evidence protocol. |
| `tests.test_review_contract::test_parallel_contracts_keep_their_own_candidate_snapshots` | failure | 旧契约待迁移 | Report fixtures must use runtime-owned quote IDs and the current evidence protocol. |
| `tests.test_review_contract::test_per_fact_review_returns_only_checked_facts` | failure | 旧契约待迁移 | Report fixtures must use runtime-owned quote IDs and the current evidence protocol. |
| `tests.test_review_contract::test_source_quotes_cannot_bind_a_foreign_object` | failure | 旧契约待迁移 | Report fixtures must use runtime-owned quote IDs and the current evidence protocol. |
| `tests.test_review_contract::test_unverified_fact_cannot_satisfy_a_required_obligation` | failure | 旧契约待迁移 | Report fixtures must use runtime-owned quote IDs and the current evidence protocol. |
| `tests.test_search_budgets::test_checkpoint_does_not_refresh_used_budget` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_search_evidence_context::test_context_uses_relevant_middle_from_original_not_an_already_cut_excerpt` | failure | 旧契约待迁移 | Raw legacy evidence dictionaries do not satisfy the versioned evidence contract. |
| `tests.test_search_evidence_context::test_excerpt_budget_covers_all_queries_and_does_not_change_originals` | failure | 旧契约待迁移 | Raw legacy evidence dictionaries do not satisfy the versioned evidence contract. |
| `tests.test_search_evidence_context::test_synthesis_has_no_tool_history_or_foreign_task_guidance` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_search_recovery::test_invalid_review_rechecks_same_candidate_without_research[False]` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_search_recovery::test_invalid_review_rechecks_same_candidate_without_research[True]` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_search_recovery::test_repeated_legacy_review_is_rejected_without_compatibility` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_search_recovery::test_review_budget_exhaustion_keeps_the_report_unapproved` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_search_runtime::test_bad_domain_argument_is_recoverable_in_same_task_without_searching_or_borrowing_evidence[False]` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_search_runtime::test_bad_domain_argument_is_recoverable_in_same_task_without_searching_or_borrowing_evidence[True]` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_search_runtime::test_domain_filter_rejects_invalid_shape_before_network[domains2]` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_search_runtime::test_intentional_search_limit_preserves_successful_source_evidence` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_search_runtime::test_model_timeout_is_identified_and_does_not_restart_same_search` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_search_runtime::test_review_timeout_is_bounded_and_named` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_search_runtime::test_search_allowance_is_atomic_and_retries_keep_the_same_slot` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_search_runtime::test_search_excerpts_keep_urls_and_full_original_evidence[False-False]` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_search_runtime::test_search_excerpts_keep_urls_and_full_original_evidence[False-True]` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_search_runtime::test_search_excerpts_keep_urls_and_full_original_evidence[True-False]` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_search_runtime::test_search_excerpts_keep_urls_and_full_original_evidence[True-True]` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_search_runtime::test_search_limit_is_enforced_before_tool_execution_and_forces_final_answer` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_skill_loading::test_business_file_reader_tracks_real_skill_read_but_upload_is_not_skill` | failure | 已确认实现缺陷 | read_file_content passes display content with extraction metadata to stage_read; parse_metadata therefore rejects a valid Skill header. |
| `tests.test_stage2::test_changed_source_identity_policy_cannot_reuse_a_checkpoint` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_stage2::test_dependencies_consume_no_slots_and_checkpoint_retains_pending_plan` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_stage2::test_four_real_delegations_queue_without_dropping_results` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_stage2::test_rate_limited_transport_attempts_are_charged_once_each` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_stage2::test_search_scope_reaches_provider_and_rejects_outside_results_without_losing_raw_evidence` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_stage2::test_source_tiers_come_from_original_scope_not_from_plan_output` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_subagent_review::test_foreign_fact_judgment_never_publishes_a_fact` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_subagent_review::test_model_review_accepts_only_complete_per_fact_judgments[False]` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_subagent_review::test_model_review_accepts_only_complete_per_fact_judgments[True]` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_task_contract::test_checkpoint_restores_the_same_contract_and_source` | failure | 旧契约待迁移 | Fixture must provide the complete current intent/material/permission contract. |
| `tests.test_task_contract::test_code_evidence_request_cannot_be_routed_around_review[calculation]` | failure | 旧契约待迁移 | Fixture must provide the complete current intent/material/permission contract. |
| `tests.test_task_contract::test_code_evidence_request_cannot_be_routed_around_review[conversation]` | failure | 旧契约待迁移 | Fixture must provide the complete current intent/material/permission contract. |
| `tests.test_task_contract::test_code_evidence_request_cannot_be_routed_around_review[explanation]` | failure | 旧契约待迁移 | Fixture must provide the complete current intent/material/permission contract. |
| `tests.test_task_contract::test_code_evidence_request_cannot_be_routed_around_review[rewrite]` | failure | 旧契约待迁移 | Fixture must provide the complete current intent/material/permission contract. |
| `tests.test_task_contract::test_contract_binds_original_task_and_reviewed_source_and_cannot_be_replaced` | failure | 旧契约待迁移 | Fixture must provide the complete current intent/material/permission contract. |
| `tests.test_task_contract::test_direct_contract_blocks_invented_tool_calls_before_execution` | failure | 旧契约待迁移 | Fixture must provide the complete current intent/material/permission contract. |
| `tests.test_task_contract::test_direct_finalization_has_no_tools_and_only_the_authoritative_source` | failure | 旧契约待迁移 | Fixture must provide the complete current intent/material/permission contract. |
| `tests.test_task_contract::test_direct_schema_does_not_accept_grounded_or_arbitrary_extra_fields` | failure | 旧契约待迁移 | Fixture must provide the complete current intent/material/permission contract. |
| `tests.test_task_contract::test_explicit_data_requirements_cannot_be_downgraded` | failure | 旧契约待迁移 | Fixture must provide the complete current intent/material/permission contract. |
| `tests.test_task_contract::test_invented_claim_is_revised_once_and_still_rejected` | failure | 旧契约待迁移 | Fixture must provide the complete current intent/material/permission contract. |
| `tests.test_task_contract::test_planning_schema_does_not_invent_an_official_source_obligation` | failure | 旧契约待迁移 | Fixture must provide the complete current intent/material/permission contract. |
| `tests.test_task_contract::test_real_graph_rewrites_bound_delivery_without_research_or_fact_pool[False]` | failure | 旧契约待迁移 | Fixture must provide the complete current intent/material/permission contract. |
| `tests.test_task_contract::test_real_graph_rewrites_bound_delivery_without_research_or_fact_pool[True]` | failure | 旧契约待迁移 | Fixture must provide the complete current intent/material/permission contract. |
| `tests.test_task_contract::test_research_without_network_permission_cannot_call_search` | failure | 旧契约待迁移 | Fixture must provide the complete current intent/material/permission contract. |
| `tests.test_task_contract::test_rewrite_cannot_invent_a_previous_source` | failure | 旧契约待迁移 | Fixture must provide the complete current intent/material/permission contract. |
| `tests.test_task_contract::test_source_mutation_and_foreign_source_identity_are_rejected` | failure | 旧契约待迁移 | Fixture must provide the complete current intent/material/permission contract. |
| `tests.test_tool_policies::test_non_transient_errors_never_retry[error1]` | failure | 旧契约待迁移 | The simulated executed tool call omits its mandatory evidence binding. |
| `tests.test_tool_policies::test_non_transient_errors_never_retry[error2]` | failure | 旧契约待迁移 | The simulated executed tool call omits its mandatory evidence binding. |
| `tests.test_tool_policies::test_non_transient_errors_never_retry[error3]` | failure | 旧契约待迁移 | The simulated executed tool call omits its mandatory evidence binding. |
| `tests.test_tool_policies::test_unsafe_sql_rejected_before_database_connect[SELECT]` | failure | 已确认实现缺陷 | SQL validation admits bare SELECT and reaches the fake database adapter; no real connection is made. |
| `tests.test_web_page_evidence::test_failed_page_does_not_create_a_citable_source` | failure | 旧契约待迁移 | Raw legacy evidence dictionaries do not satisfy the versioned evidence contract. |
| `tests.test_web_page_evidence::test_parallel_children_cannot_borrow_each_others_origin` | failure | 旧契约待迁移 | Raw legacy evidence dictionaries do not satisfy the versioned evidence contract. |
| `tests.test_web_page_evidence::test_raw_body_reaches_worker_and_checker_instead_of_discovery_snippet` | failure | 旧契约待迁移 | Raw legacy evidence dictionaries do not satisfy the versioned evidence contract. |
| `tests.test_web_page_evidence::test_reader_preserves_real_artifact_and_exact_version_source` | failure | 旧契约待迁移 | Tavily clients are owned by the managed provider process. |
| `tests.test_web_page_evidence::test_schema_normalization_and_policy_use_same_actual_search_arguments` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_web_page_evidence::test_search_exhaustion_allows_only_bounded_page_reads_and_cannot_reset` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_workflow_order::test_all_report_write_entrypoints_block_before_business_function[convert_md_to_pdf-args3]` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_workflow_order::test_api_initializes_mode_and_blocks_premature_final_answer` | failure | 待复核 | The full suite reached this failure after environment repair; no claim that this is a production defect or a harmless old assertion. |
| `tests.test_workflow_order::test_default_framework_write_file_is_also_gated` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_workflow_order::test_degraded_pdf_requires_controlled_markdown_and_edit_cannot_restore_invented_metrics` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_workflow_order::test_dependency_failure_is_persisted_and_degraded_output_cannot_claim_root_invented_metrics` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_workflow_order::test_model_claims_and_mutated_step_flags_cannot_replace_server_results` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_workflow_order::test_next_round_and_other_session_cannot_use_prior_db_result` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_workflow_order::test_ordinary_report_only_requires_explicit_sources_and_full_requested_upload_read` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_workflow_order::test_ordinary_search_and_text_report_do_not_require_db_even_with_ecommerce_skill` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_workflow_order::test_out_of_order_search_has_zero_executions_then_db_handoff_unblocks_report[False]` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_workflow_order::test_out_of_order_search_has_zero_executions_then_db_handoff_unblocks_report[True]` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_workflow_order::test_parallel_search_waits_for_actual_db_completion` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_workflow_order::test_repeated_out_of_order_calls_consume_main_and_tool_budgets[main_model]` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_workflow_order::test_repeated_out_of_order_calls_consume_main_and_tool_budgets[total]` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_workflow_order::test_storage_unavailable_and_corrupt_raw_source_fail_closed` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_workflow_order::test_unlinked_search_cannot_retroactively_satisfy_comparison_report` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_workflow_order::test_unqualified_db_prose_cannot_satisfy_order_or_trigger_dependency_degradation[budget_exhausted]` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_workflow_order::test_unqualified_db_prose_cannot_satisfy_order_or_trigger_dependency_degradation[check_incomplete]` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_workflow_order::test_unqualified_db_prose_cannot_satisfy_order_or_trigger_dependency_degradation[insufficient]` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_workflow_order::test_unqualified_db_prose_cannot_satisfy_order_or_trigger_dependency_degradation[not_checked]` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_workflow_order::test_unqualified_db_prose_cannot_satisfy_order_or_trigger_dependency_degradation[tool_failed]` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_workflow_order::test_workflow_requires_existing_uploads_and_business_reader_proof` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
| `tests.test_workflow_order::test_workflow_without_durable_store_does_not_execute_expert` | failure | 旧契约待迁移 | Expert definitions must declare the mandatory semantic review policy. |
