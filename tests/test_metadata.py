from mediavault import db, metadata


def test_guess_movie_release_name():
    g = metadata.guess_from_filename("The.Matrix.1999.1080p.BluRay.x264-GROUP.mkv")
    assert g.title == "The Matrix"
    assert g.year == 1999
    assert g.media_type == "movie"


def test_guess_movie_with_parens_year():
    g = metadata.guess_from_filename("Inception (2010).mkv")
    assert g.title == "Inception"
    assert g.year == 2010


def test_guess_tv_episode():
    g = metadata.guess_from_filename("Breaking.Bad.S01E02.Cats.in.the.Bag.720p.mkv")
    assert g.media_type == "episode"
    assert g.season == 1
    assert g.episode == 2


def test_suggest_filename_movie():
    g = metadata.Guess(title="Inception", year=2010, media_type="movie")
    assert metadata.suggest_filename("Inception", 2010, g, "some/weird.name.mkv") == "Inception (2010).mkv"


def test_suggest_filename_episode():
    g = metadata.Guess(title="Breaking Bad", year=None, media_type="episode", season=1, episode=2)
    assert metadata.suggest_filename("Breaking Bad", None, g, "bb.s01e02.mkv") == "Breaking Bad - S01E02.mkv"


def test_enrich_file_without_tmdb_key_uses_filename_guess(conn):
    from mediavault import db

    db.upsert_root(conn, "primary", "/tmp/primary", "primary", "mirror")
    db.upsert_file(conn, "primary", "Inception (2010).mkv", 100, 1.0, "sha256:abc", False)
    row = db.get_file(conn, "primary", "Inception (2010).mkv")

    result = metadata.enrich_file(conn, row, api_key=None)

    assert result["title"] == "Inception"
    assert result["year"] == 2010
    assert result["from_tmdb"] is False


def test_files_needing_guess_excludes_non_video_junk(conn):
    """Scanning indexes every file (macOS .DS_Store / AppleDouble "._*"
    sidecar files included) -- the Metadata section should only ever offer
    a title/year guess for something that's actually a video file."""
    db.upsert_root(conn, "primary", "/tmp/primary", "primary", "mirror")
    video_extensions = [".mkv", ".mp4"]

    db.upsert_file(conn, "primary", "Movie.mkv", 100, 1.0, "sha256:a", False)
    db.upsert_file(conn, "primary", ".DS_Store", 10, 1.0, "sha256:b", False)
    db.upsert_file(conn, "primary", "._Movie.mkv", 10, 1.0, "sha256:c", False)
    db.upsert_file(conn, "primary", "Notes.txt", 10, 1.0, "sha256:d", False)
    db.upsert_file(conn, "primary", "Show.mp4", 100, 1.0, "sha256:e", False)

    results = metadata.files_needing_guess(conn, video_extensions)

    assert {r["rel_path"] for r in results} == {"Movie.mkv", "Show.mp4"}


def test_files_needing_guess_skips_files_already_guessed(conn):
    db.upsert_root(conn, "primary", "/tmp/primary", "primary", "mirror")
    db.upsert_file(conn, "primary", "Movie.mkv", 100, 1.0, "sha256:a", False)
    row = db.get_file(conn, "primary", "Movie.mkv")
    db.update_file_guess(conn, row["id"], title="Movie", year=2020, media_type="movie")

    results = metadata.files_needing_guess(conn, [".mkv"])

    assert results == []
