#!/bin/bash
set -e

echo 'load denormalized'
time for file in data/*.zip; do
    sh ./load_denormalized.sh "$file"
done

echo 'load pg_normalized'
time for file in data/*.zip; do
    python3 -u load_tweets.py \
        --db 'postgresql+psycopg2://postgres:pass@localhost:21139/postgres' \
        --inputs "$file"
done

psql 'postgresql://postgres:pass@localhost:21139/postgres' -v ON_ERROR_STOP=1 \
    -c 'REFRESH MATERIALIZED VIEW tweet_tags_total;' \
    -c 'REFRESH MATERIALIZED VIEW tweet_tags_cooccurrence;'

echo 'load pg_normalized_batch'
time for file in data/*.zip; do
    python3 -u load_tweets_batch.py \
        --db 'postgresql://postgres:pass@localhost:21140/postgres' \
        --inputs "$file"
done
