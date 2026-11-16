"""Build one SQLite file from the published parquet files, one SQLite table per parquet file.
Dates and timestamps are stored as ISO text, lists and structs as JSON text.

    python -m federal_ja.collect.build_sqlite hf_data/ federal-ja-documents.sqlite

Tables, files and keys come from docs/tables.toml. Each table gets a unique index on its primary key and
an index on each column it joins on. A small `_source` table says which Hugging Face commit the data came
from and when the file was built. The script stops if a table's row count differs from its parquet file's,
or if the file is over GitHub's 2 GiB release asset limit.
"""
import json, os, sqlite3, sys
from datetime import datetime, timezone
import pyarrow as pa
import pyarrow.parquet as pq

from . import data_docs

GITHUB_ASSET_LIMIT = 2 * 1024 ** 3


def sql_type(arrow_type):
    t = str(arrow_type)
    if t.startswith(("int", "uint")) or t == "bool":
        return "INTEGER"
    if t in ("double", "float", "halffloat"):
        return "REAL"
    return "TEXT"


def values(col):
    """A parquet column as Python values SQLite can store: dates and timestamps as ISO text, lists and
    structs as JSON text."""
    t = str(col.type)
    if t.startswith(("date", "timestamp", "time")):
        return col.cast(pa.string()).to_pylist()
    if t.startswith(("list", "large_list", "struct", "map")):
        return [None if v is None else json.dumps(v, default=str) for v in col.to_pylist()]
    return col.to_pylist()


def build(data_dir, out, cfg=None):
    cfg = cfg or data_docs.load_config()
    data_docs.check(data_dir, cfg)
    if os.path.exists(out):
        os.remove(out)
    con = sqlite3.connect(out)
    counts = {}
    for name, t in cfg["tables"].items():
        pf = pq.ParquetFile(os.path.join(data_dir, t["file"]))
        cols = pf.schema_arrow
        con.execute(f'CREATE TABLE "{name}" (' + ", ".join(f'"{f.name}" {sql_type(f.type)}' for f in cols) + ")")
        insert = f'INSERT INTO "{name}" VALUES (' + ", ".join("?" * len(cols)) + ")"
        for batch in pf.iter_batches(batch_size=10_000):
            con.executemany(insert, zip(*(values(c) for c in batch.columns)))
        n = con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
        if n != pf.metadata.num_rows:
            sys.exit(f"STOP: {name} has {n} rows in SQLite but {pf.metadata.num_rows} in {t['file']}")
        counts[name] = n
        keys = data_docs.key_columns(t)
        if keys:
            con.execute(f'CREATE UNIQUE INDEX "{name}_{"_".join(keys)}" ON "{name}" ('
                        + ", ".join(f'"{k}"' for k in keys) + ")")
    for j in cfg.get("joins", []):
        for side in (j["left"], j["right"]):
            tbl, col = side.split(".")
            if data_docs.key_columns(cfg["tables"][tbl]) != [col]:
                con.execute(f'CREATE INDEX IF NOT EXISTS "{tbl}_{col}" ON "{tbl}" ("{col}")')
    con.execute("CREATE TABLE _source (key TEXT, value TEXT)")
    con.executemany("INSERT INTO _source VALUES (?, ?)", [
        ("dataset", f"https://huggingface.co/datasets/{cfg['dataset']}"),
        ("hugging_face_commit", data_docs.revision(data_dir) or "unknown"),
        ("built_utc", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
    ] + [(f"rows_{k}", str(v)) for k, v in counts.items()])
    con.commit()
    con.execute("VACUUM")
    con.close()
    size = os.path.getsize(out)
    print(f"wrote {out}: {size / 1024 ** 2:.1f} MiB,", ", ".join(f"{k} {v:,} rows" for k, v in counts.items()))
    if size >= GITHUB_ASSET_LIMIT:
        sys.exit(f"STOP: {out} is {size:,} bytes, over GitHub's 2 GiB release asset limit")
    return counts


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    build(sys.argv[1], sys.argv[2])
