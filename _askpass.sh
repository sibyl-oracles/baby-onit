#!/bin/sh
# GIT_ASKPASS helper: git calls this for credentials; the token rides in env.
case "$1" in
  Username*) echo "x-access-token" ;;
  Password*) echo "${GITHUB_TOKEN}" ;;
  *) echo "" ;;
esac
