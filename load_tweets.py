#!/usr/bin/python3

# imports
import sqlalchemy
import os
import datetime
import zipfile
import io
import json

################################################################################
# helper functions
################################################################################


def remove_nulls(s):
    r'''
    Postgres doesn't support strings with the null character \x00 in them, but twitter does.
    This helper function replaces the null characters with an escaped version so that they can be loaded into postgres.
    Technically, this means the data in postgres won't be an exact match of the data in twitter,
    and there is no way to get the original twitter data back from the data in postgres.

    The null character is extremely rarely used in real world text (approx. 1 in 1 billion tweets),
    and so this isn't too big of a deal.
    A more correct implementation, however, would be to *escape* the null characters rather than remove them.
    This isn't hard to do in python, but it is a bit of a pain to do with the JSON/COPY commands for the denormalized data.
    Since our goal is for the normalized/denormalized versions of the data to match exactly,
    we're not going to escape the strings for the normalized data.

    >>> remove_nulls('\x00')
    ''
    >>> remove_nulls('hello\x00 world')
    'hello world'
    '''
    if s is None:
        return None
    else:
        return s.replace('\x00','')


def get_id_urls(url, connection):
    '''
    Given a url, return the corresponding id in the urls table.
    If no row exists for the url, then one is inserted automatically.

    NOTE:
    This function cannot be tested with standard python testing tools because it interacts with the db.
    '''
    sql = sqlalchemy.sql.text('''
    insert into urls
        (url)
        values
        (:url)
    on conflict do nothing
    returning id_urls
    ;
    ''')
    res = connection.execute(sql,{'url':url}).first()

    # when no conflict occurs, then the query above inserts a new row in the url table and returns id_urls in res[0];
    # when a conflict occurs, then the query above does not insert or return anything;
    # we need to run a select statement to put the already existing id_urls into res[0]
    if res is None:
        sql = sqlalchemy.sql.text('''
        select id_urls
        from urls
        where
            url=:url
        ''')
        res = connection.execute(sql,{'url':url}).first()

    id_urls = res[0]
    return id_urls


def insert_tweet(connection,tweet):
    '''
    Insert the tweet into the database.

    Args:
        connection: a sqlalchemy connection to the postgresql db
        tweet: a dictionary representing the json tweet object

    NOTE:
    This function cannot be tested with standard python testing tools because it interacts with the db.

    FIXME:
    This function is only partially implemented.
    You'll need to add appropriate SQL insert statements to get it to work.
    '''

    # Keep the duplicate check and all inserts in one transaction.
    with connection.begin():
        sql = sqlalchemy.sql.text('''
            SELECT id_tweets
            FROM tweets
            WHERE id_tweets = :id_tweets
        ''')
        res = connection.execute(sql, {'id_tweets': tweet['id']})
        if res.first() is not None:
            return

        ########################################
        # insert into the users table
        ########################################
        if tweet['user']['url'] is None:
            user_id_urls = None
        else:
            user_id_urls = get_id_urls(tweet['user']['url'], connection)

        # Create the author, or update them with a newer observation.
        sql = sqlalchemy.sql.text('''
            INSERT INTO users (
                id_users, created_at, updated_at, id_urls,
                friends_count, listed_count, favourites_count,
                statuses_count, protected, verified,
                screen_name, name, location, description,
                withheld_in_countries
            )
            VALUES (
                :id_users, :created_at, :updated_at, :id_urls,
                :friends_count, :listed_count, :favourites_count,
                :statuses_count, :protected, :verified,
                :screen_name, :name, :location, :description,
                :withheld_in_countries
            )
            ON CONFLICT (id_users) DO UPDATE SET
                created_at = EXCLUDED.created_at,
                updated_at = EXCLUDED.updated_at,
                id_urls = EXCLUDED.id_urls,
                friends_count = EXCLUDED.friends_count,
                listed_count = EXCLUDED.listed_count,
                favourites_count = EXCLUDED.favourites_count,
                statuses_count = EXCLUDED.statuses_count,
                protected = EXCLUDED.protected,
                verified = EXCLUDED.verified,
                screen_name = EXCLUDED.screen_name,
                name = EXCLUDED.name,
                location = EXCLUDED.location,
                description = EXCLUDED.description,
                withheld_in_countries = EXCLUDED.withheld_in_countries
            WHERE users.updated_at IS NULL
               OR EXCLUDED.updated_at > users.updated_at
        ''')

        user = tweet['user']
        fields = (
            'created_at', 'friends_count', 'listed_count',
            'favourites_count', 'statuses_count', 'protected',
            'verified', 'screen_name', 'name', 'location',
            'description', 'withheld_in_countries'
        )
        params = {field: user.get(field) for field in fields}
        for field in ('screen_name', 'name', 'location', 'description'):
            params[field] = remove_nulls(params[field])

        params.update({
            'id_users': user['id'],
            'updated_at': tweet['created_at'],
            'id_urls': user_id_urls,
        })
        connection.execute(sql, params)

        ########################################
        # insert into the tweets table
        ########################################

        # Default to unknown location.
        geo_str = None
        geo_coords = None

        geo = tweet.get('geo')
        place = tweet.get('place')
        bounding_box = (place or {}).get('bounding_box')

        if geo and geo.get('coordinates'):
            # Twitter's geo field uses latitude, longitude.
            # Geometry points use x, y: longitude, latitude.
            latitude, longitude = geo['coordinates']
            geo_str = 'POINT'
            geo_coords = f'{longitude} {latitude}'

        elif bounding_box and bounding_box.get('coordinates'):
            rings = []
            for ring in bounding_box['coordinates']:
                points = list(ring)
                if not points:
                    continue
                if points[0] != points[-1]:
                    points.append(points[0])
                rings.append(
                    '(' + ','.join(
                        f'{point[0]} {point[1]}' for point in points
                    ) + ')'
                )

            if rings:
                geo_str = 'MULTIPOLYGON'
                geo_coords = '(' + ','.join(rings) + ')'

        try:
            text = tweet['extended_tweet']['full_text']
        except:
            text = tweet['text']

        try:
            country_code = tweet['place']['country_code'].lower()
        except TypeError:
            country_code = None

        if country_code == 'us':
            state_code = tweet['place']['full_name'].split(',')[-1].strip().lower()
            if len(state_code)>2:
                state_code = None
        else:
            state_code = None

        try:
            place_name = tweet['place']['full_name']
        except TypeError:
            place_name = None

        # NOTE:
        # The tweets table has the following foreign key:
        # > FOREIGN KEY (in_reply_to_user_id) REFERENCES users(id_users)
        #
        # This means that every "in_reply_to_user_id" field must reference a valid entry in the users table.
        # If the id is not in the users table, then you'll need to add it in an "unhydrated" form.
        # Ensure the reply recipient exists before referencing them.
        if tweet.get('in_reply_to_user_id') is not None:
            sql = sqlalchemy.sql.text('''
                INSERT INTO users (id_users)
                VALUES (:id_users)
                ON CONFLICT (id_users) DO NOTHING
            ''')
            connection.execute(sql, {
                'id_users': tweet['in_reply_to_user_id'],
            })

        # Insert the tweet using parameter values.
        sql = sqlalchemy.sql.text('''
            INSERT INTO tweets (
                id_tweets, id_users, created_at,
                in_reply_to_status_id, in_reply_to_user_id,
                quoted_status_id, retweet_count, favorite_count,
                quote_count, withheld_copyright, withheld_in_countries,
                source, text, country_code, state_code, lang,
                place_name, geo
            )
            VALUES (
                :id_tweets, :id_users, :created_at,
                :in_reply_to_status_id, :in_reply_to_user_id,
                :quoted_status_id, :retweet_count, :favorite_count,
                :quote_count, :withheld_copyright, :withheld_in_countries,
                :source, :text, :country_code, :state_code, :lang,
                :place_name, ST_GeomFromText(:geo)
            )
        ''')

        fields = (
            'created_at', 'in_reply_to_status_id', 'in_reply_to_user_id',
            'quoted_status_id', 'retweet_count', 'favorite_count',
            'quote_count', 'withheld_copyright', 'withheld_in_countries',
            'source', 'lang'
        )
        params = {field: tweet.get(field) for field in fields}
        params.update({
            'id_tweets': tweet['id'],
            'id_users': tweet['user']['id'],
            'text': remove_nulls(text),
            'source': remove_nulls(tweet.get('source')),
            'country_code': country_code,
            'state_code': state_code,
            'place_name': remove_nulls(place_name),
            'geo': (
                geo_str + '(' + geo_coords + ')'
                if geo_str is not None else None
            ),
        })
        connection.execute(sql, params)

        ########################################
        # insert into the tweet_urls table
        ########################################

        try:
            urls = tweet['extended_tweet']['entities']['urls']
        except KeyError:
            urls = tweet['entities']['urls']

        for url in urls:
            id_urls = get_id_urls(url['expanded_url'], connection)

            sql = sqlalchemy.sql.text('''
                INSERT INTO tweet_urls (id_tweets, id_urls)
                VALUES (:id_tweets, :id_urls)
                ON CONFLICT DO NOTHING
            ''')
            connection.execute(sql, {
                'id_tweets': tweet['id'],
                'id_urls': id_urls,
            })

        ########################################
        # insert into the tweet_mentions table
        ########################################

        try:
            mentions = tweet['extended_tweet']['entities']['user_mentions']
        except KeyError:
            mentions = tweet['entities']['user_mentions']

        for mention in mentions:
            # Create a partial user without overwriting an existing one.
            sql = sqlalchemy.sql.text('''
                INSERT INTO users (id_users, screen_name, name)
                VALUES (:id_users, :screen_name, :name)
                ON CONFLICT (id_users) DO NOTHING
            ''')
            connection.execute(sql, {
                'id_users': mention['id'],
                'screen_name': remove_nulls(mention.get('screen_name')),
                'name': remove_nulls(mention.get('name')),
            })

            # Link the mentioned user to this tweet.
            sql = sqlalchemy.sql.text('''
                INSERT INTO tweet_mentions (id_tweets, id_users)
                VALUES (:id_tweets, :id_users)
                ON CONFLICT DO NOTHING
            ''')
            connection.execute(sql, {
                'id_tweets': tweet['id'],
                'id_users': mention['id'],
            })

        ########################################
        # insert into the tweet_tags table
        ########################################

        try:
            hashtags = tweet['extended_tweet']['entities']['hashtags']
            cashtags = tweet['extended_tweet']['entities']['symbols']
        except KeyError:
            hashtags = tweet['entities']['hashtags']
            cashtags = tweet['entities']['symbols']

        tags = [ '#'+hashtag['text'] for hashtag in hashtags ] + [ '$'+cashtag['text'] for cashtag in cashtags ]

        for tag in tags:
            sql = sqlalchemy.sql.text('''
                INSERT INTO tweet_tags (id_tweets, tag)
                VALUES (:id_tweets, :tag)
                ON CONFLICT DO NOTHING
            ''')
            connection.execute(sql, {
                'id_tweets': tweet['id'],
                'tag': remove_nulls(tag),
            })

        ########################################
        # insert into the tweet_media table
        ########################################

        try:
            media = tweet['extended_tweet']['extended_entities']['media']
        except KeyError:
            try:
                media = tweet['extended_entities']['media']
            except KeyError:
                media = []

        for medium in media:
            id_urls = get_id_urls(medium['media_url'], connection)

            sql = sqlalchemy.sql.text('''
                INSERT INTO tweet_media (id_tweets, id_urls, type)
                VALUES (:id_tweets, :id_urls, :type)
                ON CONFLICT DO NOTHING
            ''')
            connection.execute(sql, {
                'id_tweets': tweet['id'],
                'id_urls': id_urls,
                'type': medium['type'],
            })


################################################################################
# main functions
################################################################################

if __name__ == '__main__':

    # process command line args
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--db',required=True)
    parser.add_argument('--inputs',nargs='+',required=True)
    parser.add_argument('--print_every',type=int,default=1000)
    args = parser.parse_args()

    # create database connection
    engine = sqlalchemy.create_engine(args.db, connect_args={
        'application_name': 'load_tweets.py',
        })
    connection = engine.connect()

    # loop through the input file
    # NOTE:
    # we reverse sort the filenames because this results in fewer updates to the users table,
    # which prevents excessive dead tuples and autovacuums
    for filename in sorted(args.inputs, reverse=True):
        with zipfile.ZipFile(filename, 'r') as archive:
            print(datetime.datetime.now(),filename)
            for subfilename in sorted(archive.namelist(), reverse=True):
                with io.TextIOWrapper(archive.open(subfilename)) as f:
                    for i,line in enumerate(f):

                        # load and insert the tweet
                        tweet = json.loads(line)
                        insert_tweet(connection,tweet)

                        # print message
                        if i%args.print_every==0:
                            print(datetime.datetime.now(),filename,subfilename,'i=',i,'id=',tweet['id'])
