#!/bin/bash
set -e

echo 'load pg_denormalized'
time parallel --jobs 10 --halt soon,fail=1 \
    sh ./load_denormalized.sh {} ::: data/*.zip

echo 'load pg_normalized'
time parallel --jobs 10 --halt soon,fail=1 \
    python3 -u load_tweets.py \
    --db 'postgresql+psycopg2://postgres:pass@localhost:21139/postgres' \
    --inputs {} ::: data/*.zip

psql 'postgresql://postgres:pass@localhost:21139/postgres' -v ON_ERROR_STOP=1 \
    -c 'REFRESH MATERIALIZED VIEW tweet_tags_total;' \
    -c 'REFRESH MATERIALIZED VIEW tweet_tags_cooccurrence;'

echo 'load pg_normalized_batch'
time parallel --jobs 10 --halt soon,fail=1 \
    python3 -u load_tweets_batch.py \
    --db 'postgresql://postgres:pass@localhost:21140/postgres' \
    --inputs {} ::: data/*.zip
