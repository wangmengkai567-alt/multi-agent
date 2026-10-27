BEGIN IMMEDIATE;
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
                        PRAGMA user_version = 2;
                        COMMIT;
