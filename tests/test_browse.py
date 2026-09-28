from mediavault import browse


def make_file(rel_path, size=100, missing=0, is_placeholder=0, file_id=None):
    return {"id": file_id or rel_path, "rel_path": rel_path, "size": size, "missing": missing, "is_placeholder": is_placeholder}


def test_has_hidden_component_detects_dot_prefixed_folder():
    assert browse.has_hidden_component(".Private/Movie.mkv")
    assert not browse.has_hidden_component("Movies/Action/Movie.mkv")


def test_has_hidden_component_detects_dot_prefixed_file():
    assert browse.has_hidden_component(".Movie.mkv")


def test_marker_folders_finds_marker_at_nested_path():
    hidden = browse.marker_folders(["Family/Private/.mediavault-hide", "Family/Public/Movie.mkv"])
    assert hidden == {"Family/Private"}


def test_marker_folders_finds_marker_at_root():
    hidden = browse.marker_folders([".mediavault-hide", "Movie.mkv"])
    assert hidden == {""}


def test_is_under_marker_hides_descendants_not_siblings():
    hidden = {"Family/Private"}
    assert browse.is_under_marker("Family/Private/Movie.mkv", hidden)
    assert browse.is_under_marker("Family/Private/Sub/Movie.mkv", hidden)
    assert not browse.is_under_marker("Family/Public/Movie.mkv", hidden)


def test_is_under_marker_at_root_hides_everything():
    assert browse.is_under_marker("Anything/Movie.mkv", {""})


def test_build_tree_groups_by_folder():
    files = [
        make_file("Action/Movie1.mkv"),
        make_file("Action/Sub/Movie2.mkv"),
        make_file("Comedy/Movie3.mp4"),
        make_file("Root.mkv"),
    ]
    tree = browse.build_tree(files, [".mkv", ".mp4"])

    assert len(tree.files) == 1
    assert tree.files[0]["rel_path"] == "Root.mkv"
    assert {f.name for f in tree.sorted_folders()} == {"Action", "Comedy"}

    action = tree.folders["Action"]
    assert len(action.files) == 1
    assert "Sub" in action.folders
    assert action.folders["Sub"].files[0]["rel_path"] == "Action/Sub/Movie2.mkv"

    assert tree.total_videos() == 4


def test_build_tree_skips_missing_and_placeholder_files():
    files = [
        make_file("Gone.mkv", missing=1),
        make_file("NotDownloaded.mkv", is_placeholder=1),
        make_file("Present.mkv"),
    ]
    tree = browse.build_tree(files, [".mkv"])
    assert tree.total_videos() == 1
    assert tree.files[0]["rel_path"] == "Present.mkv"


def test_build_tree_skips_non_video_extensions():
    files = [make_file("readme.txt"), make_file("Movie.mkv")]
    tree = browse.build_tree(files, [".mkv"])
    assert tree.total_videos() == 1


def test_build_tree_skips_dot_prefixed_folder():
    files = [
        make_file(".Private/Movie.mkv"),
        make_file("Public/Movie.mkv"),
    ]
    tree = browse.build_tree(files, [".mkv"])
    assert ".Private" not in tree.folders
    assert "Public" in tree.folders
    assert tree.total_videos() == 1


def test_build_tree_skips_folder_with_hide_marker():
    files = [
        make_file("Family/.mediavault-hide", size=0),
        make_file("Family/Private.mkv"),
        make_file("Family/Sub/AlsoHidden.mkv"),
        make_file("Public.mkv"),
    ]
    tree = browse.build_tree(files, [".mkv"])
    # The marker file itself is never a video, and the whole Family folder
    # (including nested Sub) is left out of the tree.
    assert "Family" not in tree.folders
    assert tree.total_videos() == 1
    assert tree.files[0]["rel_path"] == "Public.mkv"


def test_build_tree_marker_at_root_hides_all():
    files = [make_file(".mediavault-hide", size=0), make_file("Movie.mkv")]
    tree = browse.build_tree(files, [".mkv"])
    assert tree.total_videos() == 0
