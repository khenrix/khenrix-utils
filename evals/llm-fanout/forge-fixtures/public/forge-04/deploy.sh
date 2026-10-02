#!/bin/sh
# Public synthetic scratch-directory cleanup, deliberately flawed.
root=$1
target=$2
rm -rf -- "$target"
