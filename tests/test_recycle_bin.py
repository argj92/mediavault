from mediavault import recycle_bin


def test_scan_trash_reports_access_error_instead_of_pretending_empty(tmp_path, monkeypatch):
    """A permission-denied Trash location must never look identical to an
    actually-empty one — that's the exact bug that made a real ~12GB+ of
    trashed movies show up as '0 B' / 'empty'."""
    fake_trash = tmp_path / "Trash"
    fake_trash.mkdir()

    monkeypatch.setattr(recycle_bin, "trash_locations", lambda: [fake_trash])

    def deny_iterdir(self):
        raise PermissionError("Operation not permitted")

    monkeypatch.setattr(type(fake_trash), "iterdir", deny_iterdir)

    scan = recycle_bin.scan_trash()

    assert scan["items"] == []
    assert scan["accurate"] is False
    assert len(scan["access_errors"]) == 1
    assert scan["access_errors"][0].location == fake_trash


def test_scan_trash_accurate_when_readable(tmp_path):
    trash = tmp_path / "Trash"
    trash.mkdir()
    (trash / "old_movie.mkv").write_bytes(b"x" * 5000)

    # Point at a controlled location rather than relying on the real OS
    # trash being readable in CI.
    import mediavault.recycle_bin as rb

    original = rb.trash_locations
    rb.trash_locations = lambda: [trash]
    try:
        scan = rb.scan_trash()
    finally:
        rb.trash_locations = original

    assert scan["accurate"] is True
    assert scan["access_errors"] == []
    assert scan["total_bytes"] == 5000
    assert len(scan["items"]) == 1
