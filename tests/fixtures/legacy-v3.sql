BEGIN IMMEDIATE;
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
                            check_status TEXT NOT NULL,
                            CHECK((status IN ('queued','running') AND ended_at IS NULL)
                                OR (status NOT IN ('queued','running') AND ended_at IS NOT NULL))
                        );
                        CREATE INDEX subtasks_by_run ON subtasks(run_id,created_at);
                        PRAGMA user_version = 3;
                        COMMIT;
