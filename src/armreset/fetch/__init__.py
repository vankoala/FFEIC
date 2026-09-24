"""Downloaders for the raw sources (CDR bulk zips, HMDA LAR CSVs, HMDA panel).

Every fetcher runs one request at a time, waits at least ``cdr.request_delay_s`` seconds
between requests, sends ``Settings.user_agent``, and skips files the manifest already has.
"""
