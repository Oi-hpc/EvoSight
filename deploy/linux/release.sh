#!/bin/sh
export LD_LIBRARY_PATH=/opt/evosight/runtime/lib
exec /opt/evosight/runtime/bin/python /opt/evosight/bin/release.py "$@"
