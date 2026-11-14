-- Plan claims that pitfalls.sql does not exercise (found by the adversarial review). Scratch db `smoke`.
SET time_zone='+00:00';
SET system_versioning_insert_history=1;
DROP TABLE IF EXISTS px;
CREATE TABLE px (id INT PRIMARY KEY, v INT,
  row_start TIMESTAMP(6) GENERATED ALWAYS AS ROW START INVISIBLE,
  row_end   TIMESTAMP(6) GENERATED ALWAYS AS ROW END   INVISIBLE,
  PERIOD FOR SYSTEM_TIME (row_start, row_end)) WITH SYSTEM VERSIONING;
INSERT INTO px (id,v,row_start,row_end) VALUES (1,10,'2010-01-01 00:00:00','2012-01-01 00:00:00');
INSERT INTO px (id,v,row_start)         VALUES (1,11,'2012-01-01 00:00:00');
SET system_versioning_alter_history=ERROR;
ALTER TABLE px MODIFY COLUMN v BIGINT;
SET system_versioning_alter_history=KEEP;
ALTER TABLE px ADD COLUMN extra INT NULL;
SELECT 'KEEP: rows, non-null extra (expect 2,0)' t, CONCAT(COUNT(*), ',', COUNT(extra)) n FROM px FOR SYSTEM_TIME ALL;
SELECT 'KEEP: periods unchanged (expect 2010-01-01|2012-01-01)' t,
  CONCAT(DATE(MIN(row_start)), '|', DATE(MAX(row_start))) n FROM px FOR SYSTEM_TIME ALL;
SELECT 'secure_timestamp (insert_history for non-root needs NO)' t, @@secure_timestamp n;
