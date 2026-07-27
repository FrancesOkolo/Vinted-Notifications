BEGIN TRANSACTION;

UPDATE parameters
SET value = '1.1.1'
WHERE key = 'version';

COMMIT;
