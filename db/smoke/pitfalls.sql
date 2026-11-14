-- pitfalls.sql: behaviours the plan depends on. Run as an ordinary user in a scratch database,
--   mariadb -u USER -pPASS DB --force --table < pitfalls.sql
-- Observed on MariaDB 10.11.14 (the sandbox). Re-run on the real server (12.3.x) and compare:
--   T1: old / new / old / 0                       AS OF boundaries: row_start inclusive, row_end exclusive
--   LIFE: 7,8 / 8 / 7,8 / 7:222,8:333 / 7 / 0      deletion and revival with the same primary key (gap in validity)
--   ZERO-LEN history row: ERROR 1525 (Incorrect row_start value)  -> the 1 microsecond bump is mandatory
--   ALIAS after the FOR SYSTEM_TIME clause: works; alias before it: syntax error 1064
--   CTE + AS OF + IN (subquery) + join: works
--   LIMIT directly inside IN (subquery): ERROR 1235; wrapped in a derived table: works
--   FLIPS: claim 20 -> 3, claim 21 -> 1;  CHAIN broken: 0
--   PREPARE with a bare date string: accepted (n = 2) on 10.11 (the starter reports a refusal with its client)
--   UPDATE on a versioned row: adds a history row stamped with the real clock
--   CREATE TEMPORARY TABLE ... WITH SYSTEM VERSIONING: ERROR 4137
--   ALTER ADD COLUMN / MODIFY COLUMN: ERROR 4119 unless system_versioning_alter_history=KEEP; ADD KEY works
SET time_zone='+00:00';
SET system_versioning_insert_history=1;

-- T1: backdated history rows as a normal user
DROP TABLE IF EXISTS t1;
CREATE TABLE t1 (id INT PRIMARY KEY, v VARCHAR(9),
  row_start TIMESTAMP(6) GENERATED ALWAYS AS ROW START INVISIBLE,
  row_end   TIMESTAMP(6) GENERATED ALWAYS AS ROW END   INVISIBLE,
  PERIOD FOR SYSTEM_TIME (row_start, row_end)) WITH SYSTEM VERSIONING;
INSERT INTO t1 (id,v,row_start,row_end) VALUES (1,'old','2010-01-01 00:00:00','2012-06-01 00:00:00');
INSERT INTO t1 (id,v,row_start)         VALUES (1,'new','2012-06-01 00:00:00');
SELECT 'T1 mid (expect old)' t, v FROM t1 FOR SYSTEM_TIME AS OF TIMESTAMP '2011-01-01 00:00:00';
SELECT 'T1 at boundary (expect new)' t, v FROM t1 FOR SYSTEM_TIME AS OF TIMESTAMP '2012-06-01 00:00:00';
SELECT 'T1 boundary-1us (expect old)' t, v FROM t1 FOR SYSTEM_TIME AS OF TIMESTAMP '2012-05-31 23:59:59.999999';
SELECT 'T1 before first (expect 0)' t, COUNT(*) n FROM t1 FOR SYSTEM_TIME AS OF TIMESTAMP '2009-01-01 00:00:00';

-- claim table: lifecycle with deletion and revival (same PK, gap in validity)
DROP TABLE IF EXISTS c;
CREATE TABLE c (page_id INT UNSIGNED NOT NULL, claim_id INT UNSIGNED NOT NULL, text_hash BIGINT UNSIGNED NOT NULL, rev_id INT UNSIGNED NOT NULL,
  row_start TIMESTAMP(6) GENERATED ALWAYS AS ROW START INVISIBLE,
  row_end   TIMESTAMP(6) GENERATED ALWAYS AS ROW END   INVISIBLE,
  PERIOD FOR SYSTEM_TIME (row_start, row_end),
  PRIMARY KEY (page_id, claim_id), KEY (page_id, row_start), KEY (text_hash)) WITH SYSTEM VERSIONING;
INSERT INTO c (page_id,claim_id,text_hash,rev_id,row_start,row_end) VALUES (1,7,111,1,'2011-01-01 00:00:00','2012-01-01 00:00:00');
INSERT INTO c (page_id,claim_id,text_hash,rev_id,row_start,row_end) VALUES (1,7,111,3,'2013-01-01 00:00:00','2014-01-01 00:00:00');
INSERT INTO c (page_id,claim_id,text_hash,rev_id,row_start)         VALUES (1,7,222,4,'2014-01-01 00:00:00');
INSERT INTO c (page_id,claim_id,text_hash,rev_id,row_start,row_end) VALUES (1,8,333,1,'2011-01-01 00:00:00','2015-01-01 00:00:00');
SELECT 'LIFE 2011-06 (expect 7,8)' t, GROUP_CONCAT(claim_id ORDER BY claim_id) ids FROM c FOR SYSTEM_TIME AS OF TIMESTAMP '2011-06-01 00:00:00';
SELECT 'LIFE 2012-06 gap (expect 8)' t, GROUP_CONCAT(claim_id ORDER BY claim_id) ids FROM c FOR SYSTEM_TIME AS OF TIMESTAMP '2012-06-01 00:00:00';
SELECT 'LIFE 2013-06 revived (expect 7,8)' t, GROUP_CONCAT(claim_id ORDER BY claim_id) ids FROM c FOR SYSTEM_TIME AS OF TIMESTAMP '2013-06-01 00:00:00';
SELECT 'LIFE 2014-06 (expect 7,8; 7 has 222)' t, GROUP_CONCAT(CONCAT(claim_id,':',text_hash) ORDER BY claim_id) ids FROM c FOR SYSTEM_TIME AS OF TIMESTAMP '2014-06-01 00:00:00';
SELECT 'LIFE 2016 (expect 7)' t, GROUP_CONCAT(claim_id ORDER BY claim_id) ids FROM c FOR SYSTEM_TIME AS OF TIMESTAMP '2016-01-01 00:00:00';
SELECT 'LIFE current rows per PK > 1 (expect 0)' t, COUNT(*) n FROM (SELECT page_id, claim_id FROM c GROUP BY page_id, claim_id HAVING COUNT(*) > 1) x;

-- zero-length history row (what same-second edits would produce without the 1 us bump)
INSERT INTO c (page_id,claim_id,text_hash,rev_id,row_start,row_end) VALUES (1,9,444,1,'2012-01-01 00:00:00','2012-01-01 00:00:00');
SELECT 'ZERO-LEN accepted? rows for claim 9' t, COUNT(*) n FROM c FOR SYSTEM_TIME ALL WHERE claim_id=9;

-- alias placement with FOR SYSTEM_TIME
SELECT 'ALIAS after clause' t, k.claim_id FROM c FOR SYSTEM_TIME AS OF TIMESTAMP '2011-06-01 00:00:00' AS k WHERE k.claim_id=8;
SELECT 'ALIAS before clause' t, k.claim_id FROM c AS k FOR SYSTEM_TIME AS OF TIMESTAMP '2011-06-01 00:00:00' WHERE k.claim_id=8;

-- CTE + AS OF + IN (subquery) + join (shape of the retrieval query)
CREATE TABLE ch (text_hash BIGINT UNSIGNED PRIMARY KEY, txt TEXT, score INT);
INSERT INTO ch VALUES (111,'a',1),(222,'b',2),(333,'c',3),(444,'d',4);
WITH routed AS (SELECT 1 AS page_id),
snap AS (SELECT page_id, claim_id, text_hash FROM c FOR SYSTEM_TIME AS OF TIMESTAMP '2014-06-01 00:00:00' WHERE page_id IN (SELECT page_id FROM routed))
SELECT 'CTE ok' t, s.claim_id, k.txt FROM snap s JOIN ch k ON k.text_hash = s.text_hash ORDER BY k.score DESC LIMIT 8;

-- LIMIT inside IN (subquery): direct vs wrapped in a derived table
SELECT 'LIMIT direct' t, claim_id FROM c FOR SYSTEM_TIME ALL WHERE text_hash IN (SELECT text_hash FROM ch ORDER BY score LIMIT 2);
SELECT 'LIMIT wrapped (R2 shape)' t, GROUP_CONCAT(DISTINCT page_id) pages FROM c FOR SYSTEM_TIME ALL WHERE text_hash IN (SELECT text_hash FROM (SELECT text_hash FROM ch ORDER BY score LIMIT 2) AS n);

-- flips via LAG on FOR SYSTEM_TIME ALL: A,B,A,B,A on claim 20 and A,B,A on claim 21
INSERT INTO c (page_id,claim_id,text_hash,rev_id,row_start,row_end) VALUES
 (2,20,1,1,'2011-01-01','2011-02-01'),(2,20,2,2,'2011-02-01','2011-03-01'),(2,20,1,3,'2011-03-01','2011-04-01'),(2,20,2,4,'2011-04-01','2011-05-01'),
 (2,21,1,1,'2011-01-01','2011-02-01'),(2,21,2,2,'2011-02-01','2011-03-01');
INSERT INTO c (page_id,claim_id,text_hash,rev_id,row_start) VALUES (2,20,1,5,'2011-05-01'),(2,21,1,3,'2011-03-01');
SELECT 'FLIPS (expect 20->3, 21->1)' t, claim_id, SUM(text_hash = h2 AND text_hash <> h1) AS flips
FROM (SELECT page_id, claim_id, text_hash, LAG(text_hash,1) OVER w AS h1, LAG(text_hash,2) OVER w AS h2
      FROM c FOR SYSTEM_TIME ALL WHERE page_id=2
      WINDOW w AS (PARTITION BY page_id, claim_id ORDER BY row_start)) x GROUP BY claim_id;

-- chain integrity query (F2) on claim 20/21 histories
SELECT 'CHAIN broken (expect 0)' t, COUNT(*) broken FROM (
  SELECT page_id, claim_id, row_start, row_end, LEAD(row_start) OVER (PARTITION BY page_id, claim_id ORDER BY row_start) AS next_start
  FROM c FOR SYSTEM_TIME ALL WHERE page_id=2) h
WHERE row_end <= row_start OR (next_start IS NOT NULL AND row_end <> next_start);

-- server-side prepared statement: bare date vs full datetime
PREPARE s FROM 'SELECT COUNT(*) n FROM c FOR SYSTEM_TIME AS OF TIMESTAMP ? WHERE page_id=1';
SET @t='2011-06-01'; EXECUTE s USING @t;
SET @t='2011-06-01 00:00:00'; EXECUTE s USING @t;

-- UPDATE on a versioned row writes a history row stamped with the real clock
SELECT 'UPD before: history rows claim 8' t, COUNT(*) n FROM c FOR SYSTEM_TIME ALL WHERE claim_id=7 AND page_id=1;
UPDATE c SET rev_id = 99 WHERE page_id=1 AND claim_id=7;
SELECT 'UPD after: history rows claim 7' t, COUNT(*) n, MAX(row_start) latest_start FROM c FOR SYSTEM_TIME ALL WHERE claim_id=7 AND page_id=1;

-- temporary + versioned, ALTER variants
CREATE TEMPORARY TABLE tt (id INT) WITH SYSTEM VERSIONING;
ALTER TABLE c ADD COLUMN extra INT NULL;
ALTER TABLE c MODIFY COLUMN extra BIGINT NULL;
ALTER TABLE c ADD KEY (rev_id);
SELECT 'ALTER done' t, @@system_versioning_alter_history ah;
