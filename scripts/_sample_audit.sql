.headers on
.mode column

-- 1. How many (city, model, regime, lead, season) buckets meet n>=30?
SELECT 'BUCKETS_BY_SAMPLE_SIZE' as label;
SELECT
  CASE
    WHEN n_samples >= 150 THEN '4_ULTRA_n>=150'
    WHEN n_samples >= 60  THEN '3_REAL_n>=60'
    WHEN n_samples >= 30  THEN '2_TRADEABLE_n>=30'
    WHEN n_samples >= 10  THEN '1_THIN_n10-29'
    ELSE '0_GARBAGE_n<10'
  END as tier,
  COUNT(1) n_buckets,
  COUNT(DISTINCT city) n_distinct_cities,
  ROUND(AVG(sigma),2) avg_sigma
FROM error_distributions
GROUP BY tier
ORDER BY tier;

SELECT '---' as separator;

-- 2. Per-city tradability — is this city's STABLE_HIGH 24h pooled fit n>=30?
SELECT 'PER_CITY_PRIMARY_BUCKETS' as label;
SELECT
  city,
  MAX(CASE WHEN regime='stable_high' AND lead_hours=24 AND season=-1 THEN n_samples END) sh_24h_pooled,
  MAX(CASE WHEN regime='stable_high' AND lead_hours=48 AND season=-1 THEN n_samples END) sh_48h_pooled,
  MAX(CASE WHEN regime='frontal_passage' AND lead_hours=24 AND season=-1 THEN n_samples END) fp_24h_pooled,
  MAX(CASE WHEN regime='transition' AND lead_hours=24 AND season=-1 THEN n_samples END) tr_24h_pooled
FROM error_distributions
GROUP BY city
ORDER BY city;

SELECT '---' as separator;

-- 3. Per-(city, season) coverage at 24h stable_high
SELECT 'PER_CITY_SEASON_24H_STABLE' as label;
SELECT
  city,
  MAX(CASE WHEN season=-1 THEN n_samples END) pooled,
  MAX(CASE WHEN season=0  THEN n_samples END) winter,
  MAX(CASE WHEN season=1  THEN n_samples END) spring,
  MAX(CASE WHEN season=2  THEN n_samples END) summer,
  MAX(CASE WHEN season=3  THEN n_samples END) fall
FROM error_distributions
WHERE regime='stable_high' AND lead_hours=24
GROUP BY city
ORDER BY city;
