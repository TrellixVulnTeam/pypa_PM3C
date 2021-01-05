
"""
modified from https://gist.githubusercontent.com/kwarrick/4247343/raw/ca961ac3429777350a0256e39944135987012834/fncache.py

  - Built an object rather than pure function
  - Modified to use redis pipelining
  - Modified to allow purging

credit: Kevin Warrick <kwarrick@uga.edu>

"""

import os
import json
import redis

from functools import wraps

from perfmetrics import statsd_client
from perfmetrics import set_statsd_client

from dogadapter import dogstatsd

import config

root = os.path.dirname(os.path.abspath(__file__))
conf = config.Config(os.path.join(root, "config.ini"))

STATSD_URI = "statsd://127.0.0.1:8125?prefix=%s" % (conf.database_name)
set_statsd_client(STATSD_URI)


class RedisLru(object):
    """
    Redis backed LRU cache for functions which return an object which
    can survive json.dumps() and json.loads() intact
    """

    def __init__(self, conn, expires=86400, capacity=5000, prefix="lru", tag=None, arg_index=None, kwarg_name=None, slice_obj=slice(None)):
        """
        conn:      Redis Connection Object
        expires:   Default key expiration time
        capacity:  Approximate Maximum size of caching set
        prefix:    Prefix for all keys in the cache
        tag:       String (formattable optional) to tag keys with for purging
            arg_index/kwarg_name: Choose One, the tag string will be formatted with that argument
        slice_obj: Slice object to cut out un picklable thingz
        """
        self.conn = conn
        self.expires = expires
        self.capacity = capacity
        self.prefix = prefix
        self.tag = tag
        self.arg_index = arg_index
        self.kwarg_name = kwarg_name
        self.slice = slice_obj
        self.statsd = statsd_client()
        self.dogstatsd = dogstatsd

    def format_key(self, func_name, tag):
        if tag is not None:
            return ':'.join([self.prefix, tag, func_name])
        return ':'.join([self.prefix, 'tag', func_name])

    def eject(self, func_name):
        self.statsd.incr('rpc-lru.eject')
        self.dogstatsd.increment('xmlrpc.lru.eject')
        count = min((self.capacity / 10) or 1, 1000)
        cache_keys = self.format_key(func_name, '*')
        if self.conn.zcard(cache_keys) >= self.capacity:
            eject = self.conn.zrange(cache_keys, 0, count)
            pipeline = self.conn.pipeline()
            pipeline.zremrangebyrank(cache_keys, 0, count)
            pipeline.hdel(cache_vals, *eject)
            pipeline.execute()

    def get(self, func_name, key, tag):
        value = self.conn.hget(self.format_key(func_name, tag), key)
        if value:
            self.statsd.incr('rpc-lru.hit')
            self.dogstatsd.increment('xmlrpc.lru.hit')
            value = json.loads(value)
        else:
            self.statsd.incr('rpc-lru.miss')
            self.dogstatsd.increment('xmlrpc.lru.miss')
        return value

    def add(self, func_name, key, value, tag):
        self.statsd.incr('rpc-lru.add')
        self.dogstatsd.increment('xmlrpc.lru.add')
        self.eject(func_name)
        pipeline = self.conn.pipeline()
        pipeline.hset(self.format_key(func_name, tag), key, json.dumps(value))
        pipeline.expire(self.format_key(func_name, tag), self.expires)
        pipeline.execute()
        return value

    def purge(self, tag):
        self.statsd.incr('rpc-lru.purge')
        self.dogstatsd.increment('xmlrpc.lru.purge')
        keys = self.conn.scan_iter(":".join([self.prefix, tag, '*']))
        pipeline = self.conn.pipeline()
        for key in keys:
            pipeline.delete(key)
        pipeline.execute()

    def decorator(self, func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if self.conn is None:
                return func(*args, **kwargs)
            else:
                try:
                    items = args + tuple(sorted(kwargs.items()))
                    key = json.dumps(items[self.slice])
                    tag = None
                    if self.arg_index is not None and self.kwarg_name is not None:
                        raise ValueError('only one of arg_index or kwarg_name may be specified')
                    if self.arg_index is not None:
                        tag = self.tag % (args[self.arg_index])
                    if self.kwarg_name is not None:
                        tag = self.tag % (kwargs[self.kwarg_name])
                    return self.get(func.__name__, key, tag) or self.add(func.__name__, key, func(*args, **kwargs), tag)
                except redis.exceptions.RedisError:
                    return func(*args, **kwargs)
        return wrapper
