SELECT MOD(k, 100) AS bucket, COUNT(*) AS n, AVG(id) AS avg_id
FROM sbtest1
GROUP BY bucket
ORDER BY n DESC, bucket;
