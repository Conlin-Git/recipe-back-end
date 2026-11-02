"""The offline deployment archive must refuse an altered image/page before DB access."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from scripts.bundle_sichuan_image_run import FMT, unpack
from scripts.import_sichuan_image_batches import sha


class OfflineImageBundleTests(unittest.TestCase):
    def check_invalid_archive(self, filename, digest, contents, error):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            archive=root/"bad.zip"
            hashes={filename:digest}
            with ZipFile(archive,"w") as z:
                z.writestr(filename,contents)
                z.writestr("manifest.json",json.dumps({"format":FMT,"members":hashes,"content_id":sha(hashes)}))
            with self.assertRaisesRegex(ValueError,error):
                unpack(archive,root/"unpack")
            self.assertFalse((root/"escape").exists())

    def test_rejects_modified_original(self):
        self.check_invalid_archive("history/images/640.webp","0"*64,b"changed","SHA-256")

    def test_rejects_parent_path(self):
        self.check_invalid_archive("../escape",sha(b"text"),b"text","路径")
