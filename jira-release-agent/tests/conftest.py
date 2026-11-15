import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from fake_jira import FakeJira  # noqa: E402
from jra.importer import load_dataset, run_import  # noqa: E402


@pytest.fixture(scope="session")
def dataset():
    return load_dataset(ROOT / "data" / "dataset.json")


@pytest.fixture(scope="session")
def expected():
    return json.loads((Path(__file__).parent / "expected_decisions.json").read_text())


@pytest.fixture()
def jira(dataset):
    j = FakeJira("TYN")
    run_import(j, dataset, "TYN", log=lambda *_: None)
    return j


def cid(jira, dataset, source_id, needle):
    """Jira comment id of the comment on `source_id` containing `needle`."""
    key = next(k for k, i in jira.issues.items() if f"src-{source_id}" in i["labels"])
    return next(c["id"] for c in jira.comments[key] if needle in c["text"])
