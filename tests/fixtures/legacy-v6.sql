-- Pre-security version 6 schema, frozen for migration verification.
CREATE TABLE events (
                            run_id TEXT NOT NULL REFERENCES runs(run_id),
                            event_seq INTEGER NOT NULL,
                            thread_id TEXT NOT NULL,
                            event TEXT NOT NULL,
                            call_id TEXT NOT NULL,
                            parent_call_id TEXT,
                            actor TEXT NOT NULL,
                            status TEXT NOT NULL,
                            timestamp TEXT NOT NULL,
                            message TEXT NOT NULL,
                            data TEXT NOT NULL,
                            PRIMARY KEY(run_id,event_seq)
                        );
CREATE TABLE subtasks (
                            call_id TEXT PRIMARY KEY,
                            run_id TEXT NOT NULL REFERENCES runs(run_id),
                            thread_id TEXT NOT NULL,
                            request_call_id TEXT,
                            target TEXT NOT NULL,
                            display_name TEXT NOT NULL,
                            description TEXT NOT NULL,
                            description_length INTEGER NOT NULL,
                            description_sha256 TEXT NOT NULL,
                            status TEXT NOT NULL CHECK(status IN
                                ('queued','running','completed','failed','rejected','cancelled','interrupted')),
                            deadline TEXT,
                            created_at TEXT NOT NULL,
                            ended_at TEXT,
                            result TEXT,
                            error TEXT,
                            check_status TEXT NOT NULL, current_result_id TEXT REFERENCES results(result_id),
                            CHECK((status IN ('queued','running') AND ended_at IS NULL)
                                OR (status NOT IN ('queued','running') AND ended_at IS NOT NULL))
                        );
CREATE TABLE "runs" (
                            run_id TEXT PRIMARY KEY,
                            thread_id TEXT NOT NULL,
                            question TEXT NOT NULL,
                            status TEXT NOT NULL CHECK(status IN
                                ('queued','running','completed','failed','cancelled','interrupted','timed_out')),
                            answer TEXT,
                            error TEXT,
                            created_at TEXT NOT NULL,
                            ended_at TEXT, coverage TEXT NOT NULL DEFAULT 'unknown' CHECK(coverage IN ('complete','partial','unknown')),
                            CHECK((status IN ('queued','running') AND ended_at IS NULL)
                                OR (status NOT IN ('queued','running') AND ended_at IS NOT NULL))
                        );
CREATE TABLE "results" (
                result_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
                thread_id TEXT NOT NULL, call_id TEXT NOT NULL REFERENCES subtasks(call_id),
                attempt INTEGER NOT NULL CHECK(attempt>=0), source TEXT NOT NULL CHECK(source IN ('db','knowledge','search')),
                created_at TEXT NOT NULL, query_object TEXT NOT NULL, time_range TEXT NOT NULL,
                statistical_scope TEXT NOT NULL, raw_text TEXT, raw_path TEXT, raw_sha256 TEXT NOT NULL,
                summary TEXT, check_status TEXT NOT NULL, review TEXT NOT NULL,
                published INTEGER NOT NULL CHECK(published IN (0,1)), judged INTEGER NOT NULL DEFAULT 0,
                facts_status TEXT CHECK(facts_status IN ('all_passed','some_passed','none_passed')),
                coverage_status TEXT CHECK(coverage_status IN ('covered','missing','unknown')),
                UNIQUE(call_id,attempt), CHECK((raw_text IS NULL)!=(raw_path IS NULL)), CHECK(judged IN (0,1)),
        CHECK((judged=0 AND facts_status IS NULL AND coverage_status IS NULL)
           OR (judged=1 AND facts_status IS NOT NULL AND coverage_status IS NOT NULL)),
        CHECK((judged=0 AND check_status IN ('not_checked','tool_failed','dependency_unavailable','timed_out','check_incomplete','budget_exhausted')) OR (judged=1 AND check_status=CASE WHEN facts_status='all_passed' AND coverage_status='covered' THEN 'passed' WHEN facts_status='all_passed' AND coverage_status='missing' THEN 'passed_with_gap' WHEN facts_status='all_passed' AND coverage_status='unknown' THEN 'passed_coverage_unknown' WHEN facts_status='some_passed' AND coverage_status='covered' THEN 'partial_passed' WHEN facts_status='some_passed' AND coverage_status='missing' THEN 'partial_passed_with_gap' WHEN facts_status='some_passed' AND coverage_status='unknown' THEN 'partial_coverage_unknown' WHEN facts_status='none_passed' AND coverage_status='covered' THEN 'insufficient' WHEN facts_status='none_passed' AND coverage_status='missing' THEN 'insufficient' WHEN facts_status='none_passed' AND coverage_status='unknown' THEN 'insufficient' END)),
        CHECK(published=0 OR (judged=1 AND facts_status IN ('all_passed','some_passed')
          AND coverage_status IN ('covered','missing') AND summary IS NOT NULL AND length(trim(summary))>0)));
CREATE INDEX subtasks_by_run ON subtasks(run_id,created_at);
CREATE UNIQUE INDEX one_active_run_per_thread
                            ON runs(thread_id) WHERE status IN ('queued','running');
CREATE INDEX results_by_run ON results(run_id,source,created_at);
PRAGMA user_version=6;
