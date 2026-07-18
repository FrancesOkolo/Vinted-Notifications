BEGIN TRANSACTION;

UPDATE parameters
SET value = '1.1.0'
WHERE key = 'version';

COMMIT;
