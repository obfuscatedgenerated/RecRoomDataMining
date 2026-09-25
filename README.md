# Rec Room data

Tools and reference data for reading Rec Room room exports, preserved after the game shut down on June 1, 2026.

## Room Inspector

`index.html` is a browser tool for exploring the `.binpb` files in a Rec Room data export. It lets you:

- Browse and search everything in a room: objects, settings, circuits and nested data.
- List every repeated item, such as all room objects, and filter them by the components they have.
- See Circuits V1 and V2 boards laid out as diagrams, with each chip's settings and wires.
- Decode raw bytes fields as any message type, or without a schema.

Files are read in your browser and aren't uploaded anywhere.

### Using it

Open the hosted version [here](https://ollieg.codes/RecRoomDataMining), then open `persisted_room_data.binpb` from your export. The schema and prefab names load automatically from this repo.

To use it offline, download `index.html` and open it in a browser. Browsers block the automatic loading for local files, so open these together instead:

- `descriptor_set.binpb` from your export (in every Rec Room export, all the same file), or from `schema/`
- `persisted_room_data.binpb` from your export
- `data/prefab_registry.csv`, optional, for prefab names (Rec Room export also includes PrefabIds.csv which contains just those in your subroom)

URL options for the hosted version:

| Option | Example | Effect |
|---|---|---|
| `data` | `?data=rooms/example.binpb` | Opens a room file from the repo straight away |
| `schema` | `?schema=schema/other.binpb` | Uses a different schema file |
| `prefabs` | `?prefabs=data/other.csv` | Uses a different prefab table |

## Data

### `data/prefab_registry.csv`

639 prefab IDs with their names, in the same format as the `PrefabIds.csv` in an export.

The IDs and names come from the prefab definition objects in the final Steam client's asset bundles. They were checked against the names in an export's `PrefabIds.csv`: all 39 matched either exactly or as the same prefab under a different name. For example, `SKYDOME_NODE` in an export is the `SkyBoxCircuit` prefab in the game files.

Where a name came from an export, the `Type` column uses it. Otherwise `Type` is the prefab's name in the game files. `Asset Name` always holds the name from the game files.

### `schema/`

`descriptor_set.binpb` is the protobuf schema shipped in every Rec Room data export. It describes the format of all the export's `.binpb` files. `schema.proto` holds the same schema as a textual `.proto` file, generated with `tools/descriptor_to_proto.py`.

## Tools

The Python tools need Python 3.10 or newer.

| Tool | What it does |
|---|---|
| `tools/harvest_prefab_registry.py` | Rebuilds `prefab_registry.csv` from a Rec Room install, using an export's `PrefabIds.csv` to find and check the registry |
| `tools/harvest_prefab_ids.py` | Merges the `PrefabIds.csv` files from any number of exports, folders or zips, and lists prefabs that appear in rooms without a name |
| `tools/descriptor_to_proto.py` | Turns `descriptor_set.binpb` into readable `.proto` files |

Install their dependencies with:

```bash
pip install -r requirements.txt
```

Rebuilding the prefab registry from a Rec Room install:

```bash
python tools/harvest_prefab_registry.py --anchors PrefabIds.csv --install "path/to/RecRoom"
```

It assumes Unity 6000.0.27f1, the engine version of the final Steam client. Pass `--unity-version` to override it.

Regenerating the readable schema:

```bash
python tools/descriptor_to_proto.py schema/descriptor_set.binpb schema/proto
```

## License

The code in this repository is licensed under the terms in `LICENSE`.

The contents of `data/` and `schema/` are derived from Rec Room's game files and data exports. They aren't covered by that license, and are provided as-is for preservation and interoperability.

## Disclaimer

This project isn't affiliated with or endorsed by Rec Room Inc. It's non-commercial and contains no game assets. Rights holders can request removal by opening an issue.
