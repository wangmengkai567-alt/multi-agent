-- Provisioning example for a DBA; never executed by the Agent or this step.
-- Replace the password placeholder before manually running on your MySQL 8 server.
CREATE USER 'multiagent_reader'@'%' IDENTIFIED BY 'REPLACE_WITH_A_STRONG_PASSWORD';
GRANT SELECT ON deep_search.orders TO 'multiagent_reader'@'%';
-- Do not grant INSERT/UPDATE/DELETE/EXECUTE/FILE, roles, global SELECT or GRANT OPTION.
-- Configure MYSQL_READONLY_USER and MYSQL_READONLY_PASSWORD in your local environment.
