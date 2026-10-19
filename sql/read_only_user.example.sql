-- Optional provisioning template for a DBA; the application never executes it.
-- Use an existing business database; this template does not create data or tables.
-- Replace YOUR_DATABASE, YOUR_TABLE and the password before manually running it.
-- Set sql.allowed_tables in config/runtime.yml to the same approved table names.
CREATE USER 'multiagent_reader'@'%' IDENTIFIED BY 'REPLACE_WITH_A_STRONG_PASSWORD';
GRANT SELECT ON `YOUR_DATABASE`.`YOUR_TABLE` TO 'multiagent_reader'@'%';
-- Repeat the table-level GRANT only for other explicitly approved business tables.
-- Do not grant INSERT/UPDATE/DELETE/EXECUTE/FILE, roles, global SELECT or GRANT OPTION.
-- Configure MYSQL_READONLY_USER, MYSQL_READONLY_PASSWORD and MYSQL_DATABASE in .env.
