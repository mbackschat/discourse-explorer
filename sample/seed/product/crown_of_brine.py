"""Crown of Brine — the default fictional product universe.

A four-game comedic pirate-themed point-and-click adventure series by the
in-fiction studio "Brackwater Interactive". Full lore + naming guarantees
in `sample/docs/analysis/seeder-internals.md`.
"""

from __future__ import annotations

# Three categories every Crown-of-Brine forum has, regardless of seed — they
# give every generated forum the basic Q&A skeleton.
CORE_CATEGORIES: list[str] = [
    "Announcements",
    "Help & Hints",
    "Bug Reports",
]

# Optional categories the seeder draws from per seed (2–4 picked) on top of
# the core list. Drawn from a pool of ~10 to give different seeds visibly
# different forum shapes.
CATEGORY_POOL: list[str] = CORE_CATEGORIES + [
    "Modding & Fan Projects",
    "Lore & Theories",
    "Show & Tell",
    "Off-Topic Tavern",
    "Speedruns & Challenges",
    "Translations & Localization",
    "Voice Cast Talk",
]


# Tag axes — the full ~60-tag pool the seeder draws from, grouped by axis so
# the generator can guarantee each axis contributes ≥1 tag and so the design
# doc's pain-point clusters can reserve specific values across axes.
#
# Hardware/OS platforms use real names (Amiga, Steam Deck, Windows, …) per the
# subtree-local invariant in `sample/CLAUDE.md` — fan forums of classic games
# really do discuss running them on real hardware. Engine + mod-tool names
# (jester-engine, tide-engine, compass-editor, doubloon-sdk) are fabricated.
TAG_POOL_BY_AXIS: dict[str, list[str]] = {
    "status": [
        "spoiler",
        "hint-needed",
        "solved",
        "won't-fix",
        "duplicate",
        "abandoned",
    ],
    "game-version": ["game-1", "game-2", "game-3", "remaster"],
    "type": [
        "bug",
        "feature-request",
        "lore",
        "fan-art",
        "fan-fiction",
        "modded",
        "theory",
        "walkthrough",
        "speedrun",
    ],
    "subject": [
        "voice-acting",
        "localization",
        "music",
        "art-style",
        "puzzle-design",
        "dialogue",
        "characters",
        "locations",
    ],
    "platform": [
        "amiga",
        "atari-st",
        "c64",
        "dos",
        "classic-mac",
        "fm-towns",
        "windows",
        "linux",
        "steam-deck",
        "switch",
        "ios",
        "android",
    ],
    "engine": ["jester-engine", "tide-engine"],
    "mod-tool": ["compass-editor", "doubloon-sdk"],
}


# Tags that are always drawn for each axis, regardless of seed. An empty list
# for an axis means "no specific reservation, but the generator still draws at
# least one tag from that axis's optional pool". The `game-version` axis is
# fully reserved because the four-game series IS the universe — every forum
# must surface all four versions.
CORE_TAGS_BY_AXIS: dict[str, list[str]] = {
    "status": ["spoiler", "hint-needed", "solved"],
    "game-version": ["game-1", "game-2", "game-3", "remaster"],
    "type": ["bug", "feature-request"],
    "subject": [],
    "platform": [],
    "engine": [],
    "mod-tool": [],
}


# Pain-point cluster combinations — each entry is a set of tags that all must
# co-occur in the final draw. These mirror the design doc's recurring complaint
# themes (TIDE-engine on Phantom-Galleon ch.3, Compass-Editor archive format,
# voice-compression in the remaster, insult-duel localization, vintage-platform
# regression). The generator reserves the union of these tags before any
# random axis sampling so cluster signals survive the subset draw.
CLUSTER_TAG_COMBINATIONS: list[list[str]] = [
    ["bug", "tide-engine", "remaster", "game-2"],
    ["bug", "compass-editor", "modded", "remaster"],
    ["voice-acting", "remaster", "localization"],
    ["puzzle-design", "localization", "game-2"],
    ["bug", "amiga", "game-1"],
]


# Username-construction pools used by `generators/users.py`. Two lists combine
# as `<adjective>_<noun>` (snake_case for username, Title Case for display
# name). All vocabulary is common-domain nautical / swashbuckling — no proper
# names from real games, studios, or persons (per the subtree-local invariant
# in `sample/CLAUDE.md`). 20 × 30 = 600 unique combos, comfortably above the
# `large` scale's 200-user target so collision-free draw is the common path.
USERNAME_PARTS: dict[str, list[str]] = {
    "adjectives": [
        "salty",
        "briny",
        "scurvy",
        "weathered",
        "lucky",
        "landlubber",
        "drunken",
        "barnacled",
        "rusty",
        "fearless",
        "grumpy",
        "tipsy",
        "wayward",
        "soggy",
        "stormy",
        "cranky",
        "shifty",
        "haggard",
        "jolly",
        "ragged",
    ],
    "nouns": [
        "gull",
        "mariner",
        "swabbie",
        "helmsman",
        "bilge-rat",
        "lookout",
        "quartermaster",
        "parrot",
        "deckhand",
        "boatswain",
        "stowaway",
        "cabin-boy",
        "shipmate",
        "kraken",
        "barnacle",
        "anchor",
        "rigger",
        "navigator",
        "harpooner",
        "cooper",
        "mate",
        "skipper",
        "buccaneer",
        "corsair",
        "privateer",
        "pirate",
        "shark",
        "octopus",
        "albatross",
        "marlin",
    ],
}


# In-fiction release events used by `generators/timeline.py` to inject burst
# windows of topic-creation activity. Each entry is `(offset_days_from_base,
# label)`, where `base_epoch` is today − 365 days and offsets must lie in
# `[0, 365]`. The four events spread across the corpus's 12-month window with
# reasonable spacing (~100 days apart) so each burst is independently visible
# in the time-series view rather than smearing into one mega-spike.
#
# All labels reference the existing universe (Reborn remaster, JESTER/TIDE
# engines, the four games, the modding tools) — no real franchises, studios,
# or persons. Dates are anchored to fictional in-universe events: the patch
# cadence of the Reborn remaster, an anniversary fan event for game III, and
# a fan-driven ARG hyping a hypothetical sequel.
RELEASE_EVENTS: list[tuple[int, str]] = [
    (28, "Reborn 1.1 patch released"),
    (123, "Crown of Brine III anniversary fan event"),
    (218, "Reborn 1.2 + new mod-tool features"),
    (315, "v2.0-beta announcement (fan ARG)"),
]


# The four in-fiction game titles in the Crown-of-Brine series. Used by the
# topic-title generator (Sit 5) for `{game}` slot fills, and reused by later
# sits for post-body and announcement templates. All names fabricated, per the
# subtree-local invariant in `sample/CLAUDE.md`.
GAME_TITLES: list[str] = [
    "Crown of Brine",
    "Crown of Brine II: The Phantom Galleon",
    "Crown of Brine III: Tides of Forgetting",
    "Crown of Brine: Reborn",
]


# In-fiction modding tools — fabricated per the subtree-local naming rule
# (`sample/CLAUDE.md`). Reused for both the `mod-tool` tag axis and the
# `{tool}` slot in title templates.
MOD_TOOLS: list[str] = [
    "Compass Editor",
    "Doubloon SDK",
]


# Display-name overrides for platform tags that don't title-case naturally.
# The topic-title generator uses these when filling `{platform}` slots — most
# tags (`amiga`, `linux`, `switch`) become `Amiga`, `Linux`, `Switch` via a
# plain `.title()`, but multi-segment tags (`steam-deck`, `atari-st`) need a
# hand-tuned rendering so they don't end up as `Steam-Deck` or `Atari-St`.
PLATFORM_DISPLAY_NAMES: dict[str, str] = {
    "amiga": "Amiga",
    "atari-st": "Atari ST",
    "c64": "C64",
    "dos": "DOS",
    "classic-mac": "Classic Mac",
    "fm-towns": "FM Towns",
    "windows": "Windows",
    "linux": "Linux",
    "steam-deck": "Steam Deck",
    "switch": "Nintendo Switch",
    "ios": "iOS",
    "android": "Android",
}


# Slot-fill vocabulary pools used by `TITLE_TEMPLATES_BY_CATEGORY`. Every key
# corresponds to a `{slot}` placeholder used somewhere in the templates; the
# topic generator (Sit 5) draws from these per-topic to slot-fill a title.
#
# All names fabricated — no proper names from real games, studios, characters,
# or persons. Lore anchors mirror the design doc's "Lore anchors" row
# (pirate-hopeful protagonist, spectral antagonist, harbor-master,
# cartographer's guild, lost map of the Brine Crown, …).
LORE_VOCAB: dict[str, list[str]] = {
    "puzzles": [
        "the cartographer's riddle",
        "the brine-locked chest",
        "the parrot's verse",
        "the lighthouse cipher",
        "the doubloon riddle",
        "the tide-clock puzzle",
        "the harbor-master's ledger",
        "the spectral compass dial",
        "the smuggler's knot",
        "the gallows-tree puzzle",
        "the buried-bell sequence",
        "the pirate-king's epitaph",
    ],
    "chapters": [
        "Chapter 1: Salt and Sorrow",
        "Chapter 2: The Phantom's Wake",
        "Chapter 3: Below the Brine",
        "Chapter 4: A Cartographer's Folly",
        "Chapter 5: The Gallows Finale",
        "the harbor sequence",
        "the swamp interlude",
        "the lighthouse arc",
        "the brig escape",
        "the lost-map epilogue",
    ],
    "locations": [
        "Bilgewater Tavern",
        "the Forgotten Lighthouse",
        "Skull Cove",
        "the Cartographer's Loft",
        "the Drowned Market",
        "Gallows Pier",
        "the Salt-Bone Reef",
        "Brackwater Harbor",
        "the Phantom Galleon",
        "the Brine Crown vault",
        "the Smuggler's Hollow",
        "the Cooper's Cellar",
    ],
    "character_archetypes": [
        "the pirate-hopeful protagonist",
        "the spectral antagonist",
        "the harbor-master",
        "the cartographer's apprentice",
        "the disgraced quartermaster",
        "the parrot oracle",
        "the gallows-keeper",
        "the smuggler-queen",
        "the lighthouse hermit",
        "the cooper's daughter",
    ],
    "items": [
        "the lost map",
        "the brine compass",
        "the cursed doubloon",
        "the spectral cutlass",
        "the harbor-master's seal",
        "the cartographer's astrolabe",
        "the parrot's whistle",
        "the gallows lantern",
        "the smuggler's ledger",
        "the salt-bone amulet",
    ],
    "verbs": [
        "open",
        "decipher",
        "outwit",
        "pacify",
        "parley with",
        "bluff past",
        "appease",
        "translate",
        "unlock",
        "navigate",
        "decode",
        "barter with",
    ],
    "asset_types": [
        "sprites",
        "voice clips",
        "save files",
        "cutscenes",
        "dialogue trees",
        "music stems",
    ],
}


# Title templates keyed by category. Every entry of `CATEGORY_POOL` MUST have
# a non-empty list here (the topic generator validates this on input). Each
# template is a Python f-string-style placeholder string; the slots below are
# all supported:
#
#   {puzzle}              <- LORE_VOCAB["puzzles"]
#   {chapter}             <- LORE_VOCAB["chapters"]
#   {location}            <- LORE_VOCAB["locations"]
#   {character_archetype} <- LORE_VOCAB["character_archetypes"]
#   {item}                <- LORE_VOCAB["items"]
#   {verb}                <- LORE_VOCAB["verbs"]
#   {asset_type}          <- LORE_VOCAB["asset_types"]
#   {game}                <- GAME_TITLES
#   {platform}            <- TAG_POOL_BY_AXIS["platform"] (rendered via PLATFORM_DISPLAY_NAMES)
#   {tool}                <- MOD_TOOLS
#   {tag}                 <- the generated tag list (passed in at gen time)
#
# Templates are intentionally varied per category so the resulting forum has
# visibly different "tones" across sub-forums — bug reports read like bug
# reports, lore threads read like lore threads, etc.
TITLE_TEMPLATES_BY_CATEGORY: dict[str, list[str]] = {
    "Announcements": [
        "PSA: {game} on {platform} — known issues",
        "{tool} update — what's new",
        "Reminder: tagging convention for `{tag}`",
        "Mark your calendars: fan event for {game}",
    ],
    "Help & Hints": [
        "Stuck on {puzzle} in {chapter}",
        "How do I {verb} {item}?",
        "Hint needed: {puzzle}",
        "Walkthrough request — {chapter}",
        "Can't get past {location} — any tips?",
    ],
    "Bug Reports": [
        "{game} crashes on {platform} during {chapter}",
        "Save corruption after {verb}ing {item}",
        "{tool} fails to load {asset_type}",
        "Audio dropout in {location} on {platform}",
        "Softlock when interacting with {character_archetype}",
    ],
    "Modding & Fan Projects": [
        "Mod release: alternate {asset_type} for {game}",
        "WIP: replacing {item} sprite in {chapter}",
        "{tool} workflow for editing {asset_type}",
        "Fan project — reimagining {location}",
        "Looking for collaborators: {game} total conversion",
    ],
    "Lore & Theories": [
        "Who is {character_archetype}, really?",
        "The {item} — speculations",
        "Connecting {chapter} to {chapter}",
        "Theory: {character_archetype} and {location}",
        "Re-reading {chapter} after the remaster",
    ],
    "Show & Tell": [
        "My fan-art of {character_archetype}",
        "Cosplay: {character_archetype} at a convention",
        "Painted miniature of {location}",
        "Shadow-box diorama — {chapter}",
        "Embroidered {item} — finished piece",
    ],
    "Off-Topic Tavern": [
        "What are you {verb}ing this weekend?",
        "Favorite {asset_type} from {game}?",
        "If you could visit {location}, would you?",
        "Tavern thread: introduce yourselves",
        "Music recs that fit {chapter}",
    ],
    "Speedruns & Challenges": [
        "Any% route through {chapter}",
        "No-{item} challenge — feasible?",
        "100% run on {platform} — splits inside",
        "Glitchless category for {game}",
        "Race: who can {verb} {item} fastest?",
    ],
    "Translations & Localization": [
        "Localization notes for {chapter}",
        "Untranslated {asset_type} in {game}",
        "Fan translation patch for {game} — progress",
        "Idiom check: how does {character_archetype} speak in your language?",
        "Subtitle timing issues in {location}",
    ],
    "Voice Cast Talk": [
        "Voice direction for {character_archetype}",
        "Recasting in the remaster — thoughts on {character_archetype}",
        "Best line readings in {chapter}",
        "Voice clips comparison: {game} vs Reborn",
        "Behind-the-scenes: recording {asset_type}",
    ],
}


# Per-category tag-axis affinity. Drives the weighted draw of tags for a
# topic in a given category — values are KEYS of `TAG_POOL_BY_AXIS`, not
# literal tags. The generator turns a category into the union of those axes,
# intersects with the actual generated tag list, and draws 1–4 from the
# result. Categories with no entry here would default to all axes (the
# generator validates that every CATEGORY_POOL entry has a key).
TAG_AFFINITY_BY_CATEGORY: dict[str, list[str]] = {
    "Announcements":              ["status", "game-version", "type"],
    "Help & Hints":               ["status", "game-version", "subject", "type"],
    "Bug Reports":                ["type", "platform", "engine", "game-version", "subject", "status"],
    "Modding & Fan Projects":     ["type", "mod-tool", "engine", "game-version"],
    "Lore & Theories":            ["type", "subject", "game-version"],
    "Show & Tell":                ["type", "subject", "game-version"],
    "Off-Topic Tavern":           ["type", "subject"],
    "Speedruns & Challenges":     ["type", "platform", "game-version", "status"],
    "Translations & Localization": ["subject", "game-version", "type"],
    "Voice Cast Talk":            ["subject", "game-version", "type"],
}
