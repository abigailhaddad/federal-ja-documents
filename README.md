# federal-ja-documents

Collects the Justification & Approval (J&A) documents federal agencies post on SAM.gov when they award a contract without full and open competition, and publishes them with their text and notice metadata.

## Links

- Dataset: [abigailhaddad/federal-ja-documents](https://huggingface.co/datasets/abigailhaddad/federal-ja-documents) on Hugging Face
- Website and email archive (WOAH! J&As): [woah-jas.abigailhaddad.com](https://woah-jas.abigailhaddad.com)
- How this is made: [woah-jas.abigailhaddad.com/about](https://woah-jas.abigailhaddad.com/about)
- Sign up: [woah-jas.abigailhaddad.com/#subscribe](https://woah-jas.abigailhaddad.com/#subscribe)
- Data dictionary: [docs/data_dictionary.md](docs/data_dictionary.md); SQLite copy: [latest-data release](https://github.com/abigailhaddad/federal-ja-documents/releases/tag/latest-data)

## How it works

- Collection: [src/federal_ja/collect](src/federal_ja/collect), run by [daily.yml](.github/workflows/daily.yml)
- AI-extracted fields (experimental): [src/federal_ja/analysis](src/federal_ja/analysis), run by [extract-daily.yml](.github/workflows/extract-daily.yml); see [docs/field_extraction.md](docs/field_extraction.md)
- Email digest: [src/federal_ja/digest](src/federal_ja/digest), run by [digest.yml](.github/workflows/digest.yml); see [docs/digest.md](docs/digest.md)
- Website: [site.py](src/federal_ja/digest/site.py), deployed by [deploy-site.yml](.github/workflows/deploy-site.yml)
- Sources, tables and every collection command in detail: [docs/collection.md](docs/collection.md)
- Handing the project to a new owner: [HANDOFF.md](HANDOFF.md)

## Running locally

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run pytest
uv run ja-collect --help
```

## License

- **Code:** [Apache-2.0](LICENSE). See [NOTICE](NOTICE) for attribution.
- **Dataset on Hugging Face:** [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/). The J&A documents and USAspending figures are U.S. government records and generally not copyrighted (17 U.S.C. 105). Material a contractor supplied may be an exception.
