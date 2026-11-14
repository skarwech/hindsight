-- Schemas and one ordinary (non-root) user for everything. Safe to run again.
-- ponytail: dev password in the clear on a localhost-only server; use env secrets before anything is exposed.
CREATE DATABASE IF NOT EXISTS wikirag     CHARACTER SET utf8mb4;
CREATE DATABASE IF NOT EXISTS wikihistory CHARACTER SET utf8mb4;
CREATE DATABASE IF NOT EXISTS smoke       CHARACTER SET utf8mb4;   -- scratch: smoke tests and pitfalls.sql only
CREATE USER IF NOT EXISTS 'hindsight'@'127.0.0.1' IDENTIFIED BY 'hindsight';
CREATE USER IF NOT EXISTS 'hindsight'@'localhost' IDENTIFIED BY 'hindsight';
GRANT ALL PRIVILEGES ON wikirag.*     TO 'hindsight'@'127.0.0.1', 'hindsight'@'localhost';
GRANT ALL PRIVILEGES ON wikihistory.* TO 'hindsight'@'127.0.0.1', 'hindsight'@'localhost';
GRANT ALL PRIVILEGES ON smoke.*       TO 'hindsight'@'127.0.0.1', 'hindsight'@'localhost';
