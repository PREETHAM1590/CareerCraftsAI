#!/bin/sh
set -eu
# SANDBOX_SSH_TARGET: user@host of the computers host (relay on its loopback).
# SANDBOX_TUNNEL_BIND: 127.0.0.1 next to host-network services, 0.0.0.0 inside
# a private Docker network.
: "${SANDBOX_SSH_TARGET:?set SANDBOX_SSH_TARGET, e.g. ubuntu@203.0.113.10}"
cp /secrets/sandbox.key /tmp/sandbox.key
chmod 600 /tmp/sandbox.key
exec ssh -N -T -i /tmp/sandbox.key -o IdentitiesOnly=yes -o BatchMode=yes   -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/secrets/known_hosts   -o ExitOnForwardFailure=yes -o ServerAliveInterval=20 -o ServerAliveCountMax=3   -L "${SANDBOX_TUNNEL_BIND:-127.0.0.1}:14312:127.0.0.1:14312" "$SANDBOX_SSH_TARGET"
