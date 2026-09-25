from mediavault.hashing import hash_file, quick_signature


def test_identical_content_same_hash(tmp_path):
    a = tmp_path / "a.mkv"
    b = tmp_path / "b.mkv"
    a.write_bytes(b"same content" * 1000)
    b.write_bytes(b"same content" * 1000)
    assert hash_file(a) == hash_file(b)


def test_different_content_different_hash(tmp_path):
    a = tmp_path / "a.mkv"
    b = tmp_path / "b.mkv"
    a.write_bytes(b"one")
    b.write_bytes(b"two")
    assert hash_file(a) != hash_file(b)


def test_quick_signature_changes_with_content(tmp_path):
    p = tmp_path / "f.mkv"
    p.write_bytes(b"x" * 100)
    size1, _ = quick_signature(p)
    p.write_bytes(b"x" * 200)
    size2, _ = quick_signature(p)
    assert size1 != size2
