"""2026-11-28: README.md stays short and states nothing that drifts (no model ids, dollar amounts, counts,
thresholds or dates outside links, code and the license). It must link the site, the methods page, the sign-up
and the Hugging Face dataset, and every relative link must point at a file in the repo."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
MUST_LINK = ["https://huggingface.co/datasets/abigailhaddad/federal-ja-documents", "https://woah-jas.abigailhaddad.com",
             "https://woah-jas.abigailhaddad.com/about", "https://woah-jas.abigailhaddad.com/#subscribe"]


def _prose(text: str) -> str:
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.split(r"^## License", text, flags=re.M)[0]
    text = re.sub(r"`[^`]*`", "", text)
    text = re.sub(r"\]\([^)]*\)", "]", text)
    return re.sub(r"https?://\S+", "", text)


def test_readme_hard_codes_nothing_that_drifts():
    assert "claude-" not in README.lower()
    assert not re.search(r"\$\s?\d", README)
    found = re.findall(r".{0,30}\d.{0,30}", _prose(README))
    assert not found, found


def test_readme_links_the_site_and_its_relative_links_exist():
    for url in MUST_LINK:
        assert f"({url})" in README, url
    for rel in re.findall(r"\]\((?!https?:|#|mailto:)([^)#]+)", README):
        assert (ROOT / rel).exists(), rel
