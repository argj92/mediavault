from mediavault import db


def test_peer_id_is_stable_across_calls(conn):
    first = db.get_or_create_peer_id(conn)
    second = db.get_or_create_peer_id(conn)
    assert first == second
    assert len(first) == 32  # uuid4().hex


def test_pairing_token_directions_cross_match(conn):
    """The exact bug caught by manual testing: after a full pairing
    handshake, each side's outgoing_token must equal the OTHER side's
    incoming_token -- not its own. Simulated with two separate in-memory
    connections standing in for two machines, driving the same db
    functions peer_api.py's routes call."""
    import sqlite3

    conn_a = sqlite3.connect(":memory:")
    conn_a.row_factory = sqlite3.Row
    db.init_db(conn_a)
    conn_b = sqlite3.connect(":memory:")
    conn_b.row_factory = sqlite3.Row
    db.init_db(conn_b)

    peer_id_a = db.get_or_create_peer_id(conn_a)
    peer_id_b = db.get_or_create_peer_id(conn_b)

    # A initiates: generates its own incoming_token (token_a) and sends it to B.
    token_a = "token-a-generated-by-a"
    db.upsert_pairing_peer(conn_a, peer_id_b, "machine-b", "10.0.0.2", 8421, incoming_token=token_a, paired=False)

    # B receives the pair-request carrying token_a, stores it as pending.
    db.add_pending_pairing(conn_b, peer_id_a, "machine-a", "10.0.0.1", 8421, code="123456", outgoing_token=token_a)

    # B accepts: generates its own token_b, becomes paired immediately.
    token_b = "token-b-generated-by-b"
    pending = db.get_pending_pairing(conn_b, peer_id_a)
    db.complete_pairing_as_acceptor(
        conn_b, peer_id_a, pending["machine_name"], pending["address"], pending["port"],
        outgoing_token=pending["outgoing_token"], incoming_token=token_b,
    )
    db.remove_pending_pairing(conn_b, peer_id_a)

    # B "calls back" A's pair-confirm with token_b -- A finalizes.
    db.finalize_pairing(conn_a, peer_id_b, outgoing_token=token_b)

    a_view_of_b = dict(db.get_known_peer(conn_a, peer_id_b))
    b_view_of_a = dict(db.get_known_peer(conn_b, peer_id_a))

    assert a_view_of_b["paired"] == 1
    assert b_view_of_a["paired"] == 1
    # The critical property: what A presents to B must be what B checks for.
    assert a_view_of_b["outgoing_token"] == b_view_of_a["incoming_token"] == token_b
    # And what B presents to A must be what A checks for.
    assert b_view_of_a["outgoing_token"] == a_view_of_b["incoming_token"] == token_a

    # And the auth lookup a real /peer/catalog request would do must resolve:
    assert db.find_peer_by_incoming_token(conn_a, token_a)["peer_id"] == peer_id_b
    assert db.find_peer_by_incoming_token(conn_b, token_b)["peer_id"] == peer_id_a


def test_unpaired_peer_never_authenticates(conn):
    db.upsert_pairing_peer(conn, "peer-x", "machine-x", "10.0.0.5", 8421, incoming_token="some-token", paired=False)
    # Pairing in progress (not yet confirmed) must not authenticate.
    assert db.find_peer_by_incoming_token(conn, "some-token") is None


def test_remove_known_peer(conn):
    db.complete_pairing_as_acceptor(conn, "peer-y", "machine-y", "10.0.0.6", 8421, outgoing_token="o", incoming_token="i")
    assert db.get_known_peer(conn, "peer-y") is not None
    db.remove_known_peer(conn, "peer-y")
    assert db.get_known_peer(conn, "peer-y") is None


def test_pending_pairing_upsert_refreshes_not_duplicates(conn):
    db.add_pending_pairing(conn, "peer-z", "machine-z", "10.0.0.7", 8421, code="111111", outgoing_token="t1")
    db.add_pending_pairing(conn, "peer-z", "machine-z", "10.0.0.7", 8421, code="222222", outgoing_token="t2")
    rows = db.list_pending_pairings(conn)
    assert len(rows) == 1
    assert rows[0]["code"] == "222222"
