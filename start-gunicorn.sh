#!/bin/bash

# This script launches Gunicorn and redirects both its stdout and stderr (2>&1)
# into a pipe. The 'awk' command reads from that pipe, line by line.
#
# For each line, awk prints:
#   - The prefix "gunicorn  "
#   - The current timestamp
#   - The original log line from Gunicorn ($0)
#
# fflush() ensures that the output is not buffered, so you see logs immediately.

while read -r line; do
    echo -e "gunicorn   ::   $(date "+%Y-%m-%d %H:%M:%S")   ::   ${line}"
done < <(exec gunicorn \
            --workers 2 \
            --bind 0.0.0.0:12345 \
            'app:app' 2>&1)

