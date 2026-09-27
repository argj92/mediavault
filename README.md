# MediaVault

A personal movie/TV library manager: keeps backup folders (including
storage-limited ones like iCloud Drive) in sync, finds duplicates, guesses
title/year from filenames (optionally confirmed via TMDB), and flags media
worth filing into the library. Meant to be cloned onto every machine that
holds part of your library — the code and this README are the only things
shared between machines; each machine keeps its own local index and config.

## How it's organized

The web/desktop UI is a single page with a collapsible section for each of
these (click a section to expand/collapse it; the top nav just jumps to one):

- **Library** — browse every tracked root's video files by folder and play
  them inline (streamed as-is, no transcoding, so playback depends on your
  browser's own codec support — mp4/mov generally work, mkv/avi/wmv often
  won't). A folder is left out of this view (but still fully scanned,
  synced, and deduped like any other) if its name starts with `.`, or if it
  contains a `.mediavault-hide` marker file — drop an empty file by that
  name in any folder you want backed up/deduped but not shown here.
- **Roots** — any folder a machine tracks. Each has a `role` (primary,
  backup, icloud, inbox) and a `mode`. **Exactly one root should be
  `primary`** — that's the single basis every other root is compared
  against automatically (no manual pairing to set up):
  - `mirror` — a full backup copy. Missing files here are real deletion
    candidates (always confirmed manually, never auto-applied) and this root
    is always kept filled with everything from the primary.
  - `subset` — intentionally holds only *part* of the library (e.g. an
    iCloud Drive folder you've deliberately kept smaller than the full
    library to save space). Never auto-filled, never a deletion source —
    "not here" is just its normal state.
  - `watch` — scanned for duplicates/suggestions but never synced at all
    (e.g. a Downloads folder).
  When adding a root, use the **Browse…** button (macOS/Linux) to pick the
  folder in a native dialog instead of typing the path.
- **Sync** — every non-primary `mirror`/`subset` root, shown against the
  primary automatically. The "easy sync" button copies whatever's missing on
  the mirror side(s); actual deletions and content conflicts always need a
  separate, explicit confirmation shown inline under that root.
- **Protection** — for every file (by content hash), how many independent
  `mirror`-mode copies exist — locally, and on any other machine whose
  portable catalog you've imported (`mediavault catalog export`/`import`, or
  the Export/Import controls in this section). A `subset`/`watch` copy never
  counts toward protection. Files backed by only one copy are listed,
  largest first. Catalogs are a small JSON file (hash/size/path/role — never
  the media itself) you move between machines yourself (AirDrop, a synced
  folder, a USB drive) — no network access between machines required, so
  this works even for a second machine that isn't mounted or on the same
  network right now.
- **Duplicates** — identical content (by hash) found anywhere across every
  tracked root. Removing a copy quarantines it (see Recycle Bin) rather than
  deleting it outright.
- **Metadata** — guesses a movie/episode's title/year from its filename
  (using `guessit`, with a regex fallback), optionally confirmed against TMDB
  if you set an API key, with a one-click rename to the suggested name.
- **iCloud Storage** — for any `icloud`-role root: how much is downloaded
  locally vs. left as cloud-only placeholders, and the largest local files —
  safe candidates to evict (Finder → Remove Download) since the content
  stays safe in the cloud copy.
- **Recycle Bin** — MediaVault's own quarantine (from duplicate cleanup) plus
  a look at your OS's actual Trash/Recycle Bin, since large forgotten files
  often hide there too. Restoring is one click; permanently deleting always
  needs an explicit confirmation.

All of this runs automatically once the app is up: a full rescan happens at
startup and then on an interval (`scan_interval_minutes` in config.yaml),
recomputing duplicates and sync status and firing a desktop notification
whenever something *changes* (new duplicates, a root drifting out of sync
with the primary, new untracked media) — not on every tick.

## Setup on a new machine

```bash
git clone <your-private-repo-url> mediavault
cd mediavault
./scripts/setup.sh
```

That one script finds (or installs, via Homebrew) a Python 3.10+ interpreter,
creates the venv, installs mediavault, and runs `mediavault init` if this is
the first time on this machine. It's safe to re-run any time. Then:

```bash
.venv/bin/mediavault root add primary-library /path/to/your/movies --role primary --mode mirror
.venv/bin/mediavault web    # local web UI, http://127.0.0.1:8420 by default
.venv/bin/mediavault gui    # the same UI in a native desktop window
```

(`source .venv/bin/activate` first if you'd rather type `mediavault` without
the `.venv/bin/` prefix.) From here on, add more roots either with
`mediavault root add ...` or from the Roots section once the UI is running —
the Add Root form's **Browse…** button opens a native folder picker.

Everything (root management, sync, duplicates, metadata renames, the
recycle bin) is available from either UI, and the same operations are also
available from the CLI for scripting — run `mediavault --help`.

### Keeping it running in the background

By default mediavault only scans/syncs/notifies while `mediavault web` or
`gui` is actually running. To have it run continuously (survives reboots,
restarts itself if it crashes):

```bash
.venv/bin/mediavault service install   # launchd on macOS, systemd --user on Linux
.venv/bin/mediavault service status
.venv/bin/mediavault service uninstall # fully reverses it
```

This is a real, persistent system change (an auto-start-at-login background
process) — it's opt-in and not part of the base setup.

### TMDB lookups (optional)

Set `TMDB_API_KEY` in your environment to confirm filename guesses against
[TMDB](https://www.themoviedb.org/settings/api) and pull in the canonical
title/year. Without it, MediaVault still guesses from the filename alone.

### What's per-machine vs. shared via git

Only the code, `config.example.yaml`, and this README are shared. Each
machine's `config.yaml`, its SQLite index (`db_path`), and its quarantine
folder are git-ignored and never leave that machine — paths differ per
machine anyway (a different drive letter, a different iCloud container),
so there'd be nothing meaningful to share.

### Keep the GitHub repo private

This manages a personal media library and (optionally) a TMDB API key via
environment variable — keep the remote repository **private**:

```bash
gh repo create mediavault --private --source=. --remote=origin
```

## Running the tests

```bash
pip install -e ".[dev]"
pytest
```
