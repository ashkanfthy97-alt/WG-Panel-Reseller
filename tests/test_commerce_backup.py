import json,sqlite3,sys,tempfile,unittest,zipfile
from contextlib import closing
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from portal_commerce_backup import backup

class Backup(unittest.TestCase):
    def test_database_receipts_and_existing_output_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);data=root/'instance/telegram-commerce';data.mkdir(parents=True)
            with closing(sqlite3.connect(data/'commerce.sqlite3')) as db:
                db.execute('CREATE TABLE fixture(value TEXT)');db.execute("INSERT INTO fixture VALUES('snapshot')")
                db.commit()
            (data/'receipts').mkdir();(data/'receipts/fixture.png').write_bytes(b'private-fixture')
            output=root/'backup.zip';backup(root/'instance',output)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.read('telegram-commerce/receipts/fixture.png'),b'private-fixture')
                self.assertTrue(json.loads(archive.read('manifest.json'))['requires_original_panel_secret'])
                copy=root/'copy.sqlite3';copy.write_bytes(archive.read('telegram-commerce/commerce.sqlite3'))
                with closing(sqlite3.connect(copy)) as db:self.assertEqual(db.execute('SELECT value FROM fixture').fetchone()[0],'snapshot')
            original=output.read_bytes()
            with self.assertRaises(FileExistsError):backup(root/'instance',output)
            self.assertEqual(output.read_bytes(),original)

if __name__=='__main__':unittest.main()
