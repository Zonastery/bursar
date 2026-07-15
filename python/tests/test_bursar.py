from dataclasses import dataclass

from bursar.bursar import Bursar


@dataclass
class FakeCredits:
    def setup(self):
        return "setup"

    def load_pricing_from_store(self):
        return None

    def get_active_pricing(self):
        return {"version": 1}

    def publish_pricing_draft(self, config, label=None):
        return "draft-1"

    def activate_pricing(self, version):
        return str(version)

    def publish_pricing(self, config, label=None):
        return None


def test_bursar_create_owns_catalog_and_delegates():
    credits = FakeCredits()
    bursar = Bursar.create(credit_store=object(), credit_manager=credits)

    assert bursar.credits is credits
    assert bursar.billing is None
    assert bursar.catalog.active() == {"version": 1}
    assert bursar.setup() == "setup"
