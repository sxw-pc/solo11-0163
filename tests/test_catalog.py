import json
import tempfile
import unittest
from pathlib import Path
from garmentcare.__main__ import run_import, audit
from garmentcare.store import connect


class CatalogCase(unittest.TestCase):
    def test_label_collection_and_replay(self):
        raw = (Path(__file__).resolve().parents[1] / 'fixtures' / 'label_page_ok.json').read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / 'state.sqlite')
            try:
                first = run_import(db, 'label-import', raw)
                self.assertEqual(first[0]['care'], 'dry')
                self.assertEqual(run_import(db, 'label-import', raw), first)
                self.assertEqual(audit(db)['status'], 'matched')
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()
