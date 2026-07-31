#!/bin/sh
set -eu
exec node scripts/run-tsx.cjs ./src/bridge.ts
