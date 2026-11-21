import tempfile
import unittest
from pathlib import Path
from garmentcare.__main__ import run_import, audit
from garmentcare.store import connect


class EnergyCase(unittest.TestCase):
    def test_meter_usage_and_replay(self):
        raw = (Path(__file__).resolve().parents[1] / 'fixtures' / 'meter_ok.json').read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / 'state.sqlite')
            try:
                result = run_import(db, 'meter-import', raw)
                self.assertEqual(result[0]['units'], 200)
                self.assertEqual(audit(db)['status'], 'matched')
            finally:
                db.close()
