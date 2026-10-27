BEGIN IMMEDIATE;
                        CREATE TABLE runs (
                            run_id TEXT PRIMARY KEY,
                            thread_id TEXT NOT NULL,
                            question TEXT NOT NULL,
                            status TEXT NOT NULL CHECK(status IN
                                ('queued','running','completed','failed','cancelled','interrupted')),
                            answer TEXT,
                            error TEXT,
                            created_at TEXT NOT NULL,
                            ended_at TEXT,
                            CHECK((status IN ('queued','running') AND ended_at IS NULL)
                                OR (status NOT IN ('queued','running') AND ended_at IS NOT NULL))
                        );
                        CREATE UNIQUE INDEX one_active_run_per_thread
                            ON runs(thread_id) WHERE status IN ('queued','running');
                        PRAGMA user_version = 1;
                        COMMIT;
