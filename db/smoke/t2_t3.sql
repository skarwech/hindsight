-- T2 and T3: the vector smoke tests the plan could not run on 10.11. Run in the scratch db `smoke`.
SET time_zone='+00:00';

-- T2: HNSW + FULLTEXT on one plain table (the shape of wikihistory.chunk)
DROP TABLE IF EXISTS t2;
CREATE TABLE t2 (h BIGINT UNSIGNED PRIMARY KEY, txt TEXT, e VECTOR(4) NOT NULL,
  VECTOR INDEX (e) M=16 DISTANCE=cosine, FULLTEXT (txt));
INSERT INTO t2 VALUES (1,'Mannerheim johti puolustusvoimia', VEC_FromText('[1,0,0,0]')),
                      (2,'Mannerheimin elämä ja ura',        VEC_FromText('[0.9,0.1,0,0]')),
                      (3,'Kekkonen oli presidentti',         VEC_FromText('[0,1,0,0]')),
                      (4,'Talvisota alkoi marraskuussa',     VEC_FromText('[0,0,1,0]'));
SELECT 'T2 MATCH mannerheim* (expect 1,2)' t, GROUP_CONCAT(h ORDER BY h) ids
  FROM t2 WHERE MATCH(txt) AGAINST ('mannerheim*' IN BOOLEAN MODE);
SELECT 'T2 VECTOR top2 near [1,0,0,0] (expect 1,2)' t, GROUP_CONCAT(h ORDER BY d) ids FROM (
  SELECT h, VEC_DISTANCE_COSINE(e, VEC_FromText('[1,0,0,0]')) d FROM t2 ORDER BY d LIMIT 2) x;
EXPLAIN SELECT h FROM t2 ORDER BY VEC_DISTANCE_COSINE(e, VEC_FromText('[1,0,0,0]')) LIMIT 2;

-- T3: HNSW on a system-versioned table. Does it create? After UPDATEs and DELETEs, does
-- ORDER BY distance LIMIT 5 still return 5 rows, now and AS OF before the changes? (MDEV-35246)
DROP TABLE IF EXISTS t3;
CREATE TABLE t3 (id INT PRIMARY KEY, e VECTOR(4) NOT NULL,
  VECTOR INDEX (e) DISTANCE=cosine) WITH SYSTEM VERSIONING;
INSERT INTO t3 (id, e) SELECT seq, VEC_FromText(CONCAT('[', 1 + seq % 7, ',', 1 + seq % 5, ',', 1 + seq % 3, ',', seq, ']'))
  FROM seq_1_to_20;
DO SLEEP(0.05);
SET @before = NOW(6);
DO SLEEP(0.05);
UPDATE t3 SET e = VEC_FromText(CONCAT('[', id, ',1,1,1]')) WHERE id <= 8;      -- 8 rows get a new vector
DELETE FROM t3 WHERE id BETWEEN 9 AND 13;                                       -- 5 rows deleted -> 15 current
SELECT 'T3 all row-versions (expect 28 = 20 + 8 updated)' t, COUNT(*) n FROM t3 FOR SYSTEM_TIME ALL;
SELECT 'T3 current, ANN LIMIT 5 (expect 5)' t, COUNT(*) n FROM (
  SELECT id FROM t3 ORDER BY VEC_DISTANCE_COSINE(e, VEC_FromText('[1,1,1,1]')) LIMIT 5) x;
SELECT 'T3 current, ANN LIMIT 15 (expect 15)' t, COUNT(*) n FROM (
  SELECT id FROM t3 ORDER BY VEC_DISTANCE_COSINE(e, VEC_FromText('[1,1,1,1]')) LIMIT 15) x;
SELECT 'T3 AS OF before, LIMIT 5 (expect 5)' t, COUNT(*) n FROM (
  SELECT id FROM t3 FOR SYSTEM_TIME AS OF TIMESTAMP @before
  ORDER BY VEC_DISTANCE_COSINE(e, VEC_FromText('[1,1,1,1]')) LIMIT 5) x;
SELECT 'T3 AS OF before, LIMIT 20 (expect 20)' t, COUNT(*) n FROM (
  SELECT id FROM t3 FOR SYSTEM_TIME AS OF TIMESTAMP @before
  ORDER BY VEC_DISTANCE_COSINE(e, VEC_FromText('[1,1,1,1]')) LIMIT 20) x;
SELECT 'T3 AS OF before, exact top5 vs ANN top5 equal? (expect 1)' t,
  (SELECT GROUP_CONCAT(id ORDER BY d, id) FROM (SELECT id, VEC_DISTANCE_COSINE(e, VEC_FromText('[1,1,1,1]')) d
     FROM t3 FOR SYSTEM_TIME AS OF TIMESTAMP @before ORDER BY d LIMIT 5) a) =
  (SELECT GROUP_CONCAT(id ORDER BY d, id) FROM (SELECT id, VEC_DISTANCE_COSINE(e, VEC_FromText('[1,1,1,1]')) d
     FROM t3 FOR SYSTEM_TIME AS OF TIMESTAMP @before IGNORE INDEX (e) ORDER BY d LIMIT 5) b) same;
EXPLAIN SELECT id FROM t3 ORDER BY VEC_DISTANCE_COSINE(e, VEC_FromText('[1,1,1,1]')) LIMIT 5;
EXPLAIN SELECT id FROM t3 FOR SYSTEM_TIME AS OF TIMESTAMP @before ORDER BY VEC_DISTANCE_COSINE(e, VEC_FromText('[1,1,1,1]')) LIMIT 5;
