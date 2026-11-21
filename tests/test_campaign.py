import tempfile
import unittest
from pathlib import Path
from garmentcare.__main__ import run_import, audit
from garmentcare.store import connect


class CampaignCase(unittest.TestCase):
    def test_campaign_attribution_replay(self):
        raw = (Path(__file__).resolve().parents[1] / 'fixtures' / 'campaign_ok.json').read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / 'state.sqlite')
            try:
                result = run_import(db, 'campaign-import', raw)
                self.assertEqual(result[0]['campaign'], 'autumn-care')
                self.assertEqual(audit(db)['status'], 'matched')
            finally:
                db.close()
