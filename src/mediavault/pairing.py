"""Shared constants and the proof-of-possession used by the pairing handshake
(peers.py is the client side, peer_api.py the server side).

The handshake: A -> B `pair-request` (carries A's freshly generated token);
a human confirms the 6-digit code on both screens; B -> A `pair-confirm`
(carries B's token). The confirm is a *claim* from whoever can reach A's port,
so it has to prove it came from the machine that received the request: `proof`
is an HMAC, keyed by the token A sent, over B's id and B's new token. Only B
(or someone who captured the request in transit) can compute it -- another
host on the LAN that merely guesses A's peer_id cannot plant a token of its
own. (This is not a defense against an on-path attacker on plain HTTP; the
confirmation code and Tailscale/WireGuard are what cover that.)
"""
from __future__ import annotations

import hashlib
import hmac

PENDING_TTL_SECONDS = 10 * 60   # an unanswered request/in-progress pairing expires
MAX_PENDING_TOTAL = 10          # incoming requests waiting for a human, all sources
MAX_PENDING_PER_SOURCE = 3      # ... from any one IP


def pairing_proof(initiator_token: str, responder_peer_id: str, responder_token: str) -> str:
    msg = f"{responder_peer_id}\n{responder_token}".encode()
    return hmac.new(initiator_token.encode(), msg, hashlib.sha256).hexdigest()


def proof_ok(initiator_token: str, responder_peer_id: str, responder_token: str, proof: str) -> bool:
    return hmac.compare_digest(pairing_proof(initiator_token, responder_peer_id, responder_token), proof)
