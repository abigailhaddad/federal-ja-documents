"""Data dictionary and table diagram, generated from the published parquet files and docs/tables.toml.

    python -m federal_ja.collect.data_docs fetch hf_data/  # download the parquet tables (not files/) from Hugging Face into hf_data/
    python -m federal_ja.collect.data_docs check hf_data/  # stop with an error if the columns differ from docs/tables.toml
    python -m federal_ja.collect.data_docs write hf_data/  # write docs/data_dictionary.md

Every column's type, non-null share and example value are read from the parquet files; descriptions come
from docs/tables.toml. `check` (and `write`) fail if a parquet column has no description or a described
column is not in the parquet, so the dictionary can't drift from the schema. If docs/tables.toml names a
`card` file (a dataset card kept in the repo), `write` also puts the same text in it between two markers.

Copied from abigailhaddad/nlrb-decisions; nothing here is specific to one dataset apart from
docs/tables.toml. With no [[joins]] in the config (one table), the join diagram is left out.
"""
import os, sys, tomllib
from datetime import date
import pyarrow.compute as pc
import pyarrow.parquet as pq

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))  # the repo: src/federal_ja/collect/ is three down
CONFIG = os.path.join(ROOT, "docs", "tables.toml")
OUT = os.path.join(ROOT, "docs", "data_dictionary.md")


def load_config(path=CONFIG):
    with open(path, "rb") as f:
        return tomllib.load(f)


def fetch(dest, cfg=None):
    """Download every table's file from the Hugging Face dataset at its current commit. Returns the commit."""
    from huggingface_hub import HfApi, hf_hub_download
    cfg = cfg or load_config()
    sha = HfApi().dataset_info(cfg["dataset"]).sha
    os.makedirs(dest, exist_ok=True)
    for t in cfg["tables"].values():
        hf_hub_download(cfg["dataset"], t["file"], repo_type="dataset", revision=sha, local_dir=dest)
    with open(os.path.join(dest, "REVISION"), "w") as f:
        f.write(sha + "\n")
    print(f"fetched {cfg['dataset']} at {sha} into {dest}")
    return sha


def revision(data_dir):
    p = os.path.join(data_dir, "REVISION")
    return open(p).read().strip() if os.path.exists(p) else None


def key_columns(t):
    """A table's primary key as a list: `primary_key` may be one column or a list (a composite key)."""
    k = t.get("primary_key")
    return [] if not k else [k] if isinstance(k, str) else list(k)


def path(data_dir, t, flat=False):
    """Where a table's file is: its path under data_dir as on Hugging Face, or with flat=True just its file name
    (for a local build that writes every file to one folder)."""
    return os.path.join(data_dir, os.path.basename(t["file"]) if flat else t["file"])


def check(data_dir, cfg=None, flat=False):
    """Columns in each parquet file must be exactly the columns described, in the same order."""
    cfg = cfg or load_config()
    problems = []
    for name, t in cfg["tables"].items():
        cols = pq.read_schema(path(data_dir, t, flat)).names
        described = list(t["columns"])
        missing = [c for c in cols if c not in described]
        extra = [c for c in described if c not in cols]
        if missing:
            problems.append(f"{name}: columns in {t['file']} with no description in docs/tables.toml: {missing}")
        if extra:
            problems.append(f"{name}: described in docs/tables.toml but not in {t['file']}: {extra}")
        if not missing and not extra and cols != described:
            problems.append(f"{name}: column order differs from {t['file']}; reorder docs/tables.toml")
        for c in key_columns(t):
            if c not in described:
                problems.append(f"{name}: primary key column {c} is not described")
        for c in t.get("unconfirmed", []):
            if c not in described:
                problems.append(f"{name}: unconfirmed column {c} is not described")
    for j in cfg.get("joins", []):
        for side in (j["left"], j["right"]):
            tbl, col = side.split(".")
            if col not in cfg["tables"].get(tbl, {}).get("columns", {}):
                problems.append(f"join {side}: no such table or column")
    if problems:
        sys.exit("SCHEMA DRIFT:\n  " + "\n  ".join(problems))
    print("schema matches docs/tables.toml:", ", ".join(f"{n} ({len(t['columns'])} columns)" for n, t in cfg["tables"].items()))


def simple_type(arrow_type):
    s = str(arrow_type)
    return {"large_string": "string", "double": "float64", "float": "float32"}.get(s, s)


def cell(v, width=60):
    s = str(v).replace("\n", " ").replace("|", "\\|")
    return (s[:width] + "...") if len(s) > width else s


def profile(path):
    """Per column: type, non-null count, non-blank count (non-null and not ''), first non-blank value."""
    table = pq.read_table(path)
    out = {}
    for name in table.column_names:
        col = table.column(name)
        nonnull = len(col) - col.null_count
        nonblank, example = nonnull, None
        if simple_type(col.type) == "string":
            nonblank = nonnull - pc.sum(pc.equal(col, "")).as_py() if nonnull else 0
            filled = col.filter(pc.and_(pc.is_valid(col), pc.not_equal(col, "")))
        else:
            filled = col.drop_null()
        if len(filled):
            example = filled[0].as_py()
        out[name] = {"type": simple_type(col.type), "nonnull": nonnull, "nonblank": nonblank, "example": example}
    return table.num_rows, out


def mermaid(cfg, profiles):
    """ER diagram showing each table's primary key and join columns. PK = primary key; FK = the right-hand
    side of an exact join (a join whose `kind` has no dotted line)."""
    joins = cfg.get("joins", [])
    lines = ["```mermaid", "erDiagram"]
    for name, t in cfg["tables"].items():
        block = []
        for col in t["columns"]:
            marks = []
            if col in key_columns(t):
                marks.append("PK")
            if any(j["right"] == f"{name}.{col}" and ".." not in j["kind"] for j in joins):
                marks.append("FK")
            in_join = any(f"{name}.{col}" in (j["left"], j["right"]) for j in joins)
            if marks or in_join:
                block.append(f"        {profiles[name][1][col]['type']} {col} {','.join(marks)}".rstrip())
        if block:  # a table with no key and no join column is left out of the diagram (an empty {} block)
            lines += [f"    {name} {{"] + block + ["    }"]
    for j in cfg.get("joins", []):
        lt, rt = j["left"].split(".")[0], j["right"].split(".")[0]
        lines.append(f'    {lt} {j["kind"]} {rt} : "{j["label"]}"')
    lines.append("```")
    return "\n".join(lines)


def markdown(data_dir, cfg=None, level=1, source=None, flat=False):
    """The data dictionary as markdown. `level` is the heading level of its title."""
    cfg = cfg or load_config()
    check(data_dir, cfg, flat)
    h = "#" * level
    profiles = {n: profile(path(data_dir, t, flat)) for n, t in cfg["tables"].items()}
    rev = revision(data_dir)
    src = source or (f"Hugging Face `{cfg['dataset']}` at commit `{rev[:8]}`" if rev else f"`{data_dir}`")
    out = [f"{h} Data dictionary", "",
           f"Generated by `src/federal_ja/collect/data_docs.py` on {date.today().isoformat()} from {src}. Types, row counts, "
           f"non-null shares and examples are read from the parquet files; descriptions are in `docs/tables.toml` in the "
           f"GitHub repo. Columns marked **(meaning not confirmed)** are ones whose meaning the source does not define.", "",
           "| table | file | Hugging Face config | rows | one row is |", "|---|---|---|---|---|"]
    for n, t in cfg["tables"].items():
        out.append(f"| `{n}` | `{t['file']}` | `{t['config']}` | {profiles[n][0]:,} | {t['grain']} |")
    if cfg.get("joins"):
        out += ["", f"{h}# How the tables join", "", mermaid(cfg, profiles), ""]
        for j in cfg["joins"]:
            out.append(f"- `{j['left']}` to `{j['right']}`: {j['note']}")
    for n, t in cfg["tables"].items():
        rows, prof = profiles[n]
        out += ["", f"{h}# `{n}` (`{t['file']}`, config `{t['config']}`)", "", t["grain"], "",
                "Non-null and non-blank are shares of the table's rows; non-blank also leaves out empty strings.", "",
                "| column | type | description | non-null | non-blank | example |", "|---|---|---|---|---|---|"]
        for col, desc in t["columns"].items():
            p = prof[col]
            mark = " **(meaning not confirmed)**" if col in t.get("unconfirmed", []) else ""
            keys = key_columns(t)
            pk = (" Primary key." if len(keys) == 1 else " Part of the primary key (" + ", ".join(keys) + ").") \
                if col in keys else ""
            pct = lambda x: f"{100 * x / rows:.1f}%" if rows else "n/a"
            ex = f"`{cell(p['example'])}`" if p["example"] is not None else ""
            desc = desc.replace("|", "\\|")  # a bare | would end the table cell
            out.append(f"| `{col}` | {p['type']} | {desc}{pk}{mark} | {pct(p['nonnull'])} | {pct(p['nonblank'])} | {ex} |")
    return "\n".join(out) + "\n"


CARD_START, CARD_END = "<!-- data dictionary: start -->", "<!-- data dictionary: end -->"


def update_card(data_dir, cfg=None):
    """For a repo whose dataset card is a committed file (`card` in docs/tables.toml), replace the text
    between the two markers in it with the dictionary. Doesn't upload anything."""
    cfg = cfg or load_config()
    path = os.path.join(ROOT, cfg["card"])
    text = open(path, encoding="utf-8").read()
    if text.count(CARD_START) != 1 or text.count(CARD_END) != 1:
        sys.exit(f"{cfg['card']}: needs one {CARD_START} and one {CARD_END}")
    head, rest = text.split(CARD_START)
    tail = rest.split(CARD_END)[1]
    body = markdown(data_dir, cfg, level=2, source="the file in this dataset")
    with open(path, "w", encoding="utf-8") as f:
        f.write(head + CARD_START + "\n" + body + CARD_END + tail)
    print("updated", cfg["card"])


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ("fetch", "check", "write"):
        sys.exit(__doc__)
    what, data_dir = sys.argv[1], sys.argv[2]
    if what == "fetch":
        fetch(data_dir)
    elif what == "check":
        check(data_dir)
    else:
        md = "<!-- Generated by src/federal_ja/collect/data_docs.py; edit docs/tables.toml instead. -->\n" + markdown(data_dir)
        with open(OUT, "w") as f:
            f.write(md)
        print("wrote", os.path.relpath(OUT))
        if load_config().get("card"):
            update_card(data_dir)


if __name__ == "__main__":
    main()
