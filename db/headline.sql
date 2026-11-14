SET mhnsw_ef_search = 80;
WITH r1 AS (                                   -- article-level ANN on S
  SELECT page_id FROM (
    SELECT page_id, VEC_DISTANCE_COSINE(embedding, VEC_FromText(?)) AS d
    FROM wikirag.article_vector WHERE lang = 'fi' ORDER BY d LIMIT 12) AS a),
r2 AS (                                        -- pages that ever held a text close to the question
  SELECT DISTINCT page_id FROM wikihistory.claim FOR SYSTEM_TIME ALL
  WHERE text_hash IN (SELECT text_hash FROM (
        SELECT text_hash FROM wikihistory.chunk
        ORDER BY VEC_DISTANCE_COSINE(emb, VEC_FromText(?)) LIMIT 100) AS n)),
r3 AS (                                        -- lexical prefix match for names and years
  SELECT DISTINCT page_id FROM wikihistory.claim FOR SYSTEM_TIME ALL
  WHERE text_hash IN (SELECT text_hash FROM (
        SELECT text_hash FROM wikihistory.chunk
        WHERE MATCH(text) AGAINST (? IN BOOLEAN MODE) LIMIT 200) AS m)),   -- e.g. 'mannerheim*'
routed AS (SELECT page_id FROM (
        SELECT page_id FROM r1 UNION SELECT page_id FROM r2 UNION SELECT page_id FROM r3) u LIMIT 25),
snap AS (                                      -- claims alive on that date
  SELECT page_id, claim_id, text_hash, section, rev_id, editor
  FROM wikihistory.claim FOR SYSTEM_TIME AS OF TIMESTAMP '{as_of}'
  WHERE page_id IN (SELECT page_id FROM routed))
SELECT s.page_id, s.claim_id, s.section, s.rev_id, s.editor, c.text,
       VEC_DISTANCE_COSINE(c.emb, VEC_FromText(?)) AS d
FROM snap s JOIN wikihistory.chunk c ON c.text_hash = s.text_hash
ORDER BY d LIMIT 8;
