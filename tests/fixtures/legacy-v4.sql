BEGIN IMMEDIATE;
                        CREATE TABLE results (
                            result_id TEXT PRIMARY KEY,
                            run_id TEXT NOT NULL REFERENCES runs(run_id),
                            thread_id TEXT NOT NULL,
                            call_id TEXT NOT NULL REFERENCES subtasks(call_id),
                            attempt INTEGER NOT NULL CHECK(attempt >= 0),
                            source TEXT NOT NULL CHECK(source IN ('db','knowledge','search')),
                            created_at TEXT NOT NULL,
                            query_object TEXT NOT NULL,
                            time_range TEXT NOT NULL,
                            statistical_scope TEXT NOT NULL,
                            raw_text TEXT,
                            raw_path TEXT,
                            raw_sha256 TEXT NOT NULL,
                            summary TEXT,
                            check_status TEXT NOT NULL,
                            review TEXT NOT NULL,
                            published INTEGER NOT NULL CHECK(published IN (0,1)),
                            UNIQUE(call_id,attempt),
                            CHECK((raw_text IS NULL) != (raw_path IS NULL)),
                            CHECK(published=0 OR (check_status='passed' AND summary IS NOT NULL))
                        );
                        CREATE INDEX results_by_run ON results(run_id,source,created_at);
                        PRAGMA user_version = 4;
                        COMMIT;
