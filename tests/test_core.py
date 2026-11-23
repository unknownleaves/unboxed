"""Run with:  python -m unittest discover tests"""
import gzip
import io
import os
import sys
import tarfile
import tempfile
import threading
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unboxed_core as core  # noqa: E402


def make_tree(root):
    os.makedirs(os.path.join(root, "proj", "sub"))
    with open(os.path.join(root, "proj", "a.txt"), "w") as f:
        f.write("hello" * 1000)
    with open(os.path.join(root, "proj", "sub", "b.bin"), "wb") as f:
        f.write(os.urandom(300_000))
    with open(os.path.join(root, "proj", "sub", "ünïcødé.txt"), "w", encoding="utf-8") as f:
        f.write("x")


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.src = os.path.join(self.d, "src")
        make_tree(self.src)

    def tearDown(self):
        self.tmp.cleanup()

    def roundtrip(self, fmt):
        out = os.path.join(self.d, f"out.{fmt}")
        seen = []
        opts = core.Options(progress=lambda d, t, n: seen.append((d, t)))
        core.create_archive(out, [os.path.join(self.src, "proj")], fmt, opts)
        self.assertTrue(seen and seen[-1][0] == seen[-1][1])
        arc = core.open_archive(out)
        paths = {e.path.rstrip("/") for e in arc.entries}
        self.assertIn("proj/sub/b.bin", paths)
        self.assertIn("proj/sub/ünïcødé.txt", paths)
        dest = core.smart_destination(arc, os.path.join(self.d, "x"))
        self.assertEqual(dest, os.path.join(self.d, "x"))  # single top folder: no nesting
        arc.extract(dest, None, core.Options())
        with open(os.path.join(dest, "proj", "sub", "b.bin"), "rb") as a, \
             open(os.path.join(self.src, "proj", "sub", "b.bin"), "rb") as b:
            self.assertEqual(a.read(), b.read())
        self.assertEqual(arc.test(core.Options()), [])
        # extract selected subfolder -> parent folders stripped
        sel = os.path.join(self.d, "sel")
        arc.extract(sel, ["proj/sub"], core.Options())
        self.assertTrue(os.path.isfile(os.path.join(sel, "sub", "b.bin")))
        self.assertFalse(os.path.exists(os.path.join(sel, "proj")))
        # rename policy on second extract
        arc.extract(sel, ["proj/a.txt"], core.Options())
        arc.extract(sel, ["proj/a.txt"], core.Options(overwrite="rename"))
        self.assertTrue(os.path.isfile(os.path.join(sel, "a (2).txt")))
        arc.extract(sel, ["proj/a.txt"], core.Options(overwrite="skip"))
        self.assertFalse(os.path.exists(os.path.join(sel, "a (3).txt")))
        arc.close()

    def test_zip(self):
        self.roundtrip("zip")

    def test_targz(self):
        self.roundtrip("tar.gz")

    def test_tarxz(self):
        self.roundtrip("tar.xz")

    def test_zip_slip_blocked(self):
        p = os.path.join(self.d, "evil.zip")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("../../evil.txt", "bad")
        arc = core.open_archive(p)
        with self.assertRaises(core.ArchiveError):
            arc.extract(os.path.join(self.d, "o"), None, core.Options())
        self.assertFalse(os.path.exists(os.path.join(self.d, "..", "evil.txt")))

    def test_tar_absolute_blocked(self):
        p = os.path.join(self.d, "evil.tar")
        with tarfile.open(p, "w") as t:
            data = b"bad"
            ti = tarfile.TarInfo("/../../etc/evil")
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
        arc = core.open_archive(p)
        with self.assertRaises(core.ArchiveError):
            arc.extract(os.path.join(self.d, "o"), None, core.Options())

    def test_gz_single(self):
        p = os.path.join(self.d, "notes.txt.gz")
        with gzip.open(p, "wb") as f:
            f.write(b"abc" * 100)
        arc = core.open_archive(p)
        self.assertEqual(arc.entries[0].path, "notes.txt")
        self.assertEqual(arc.entries[0].size, 300)
        arc.extract(self.d, None, core.Options())
        with open(os.path.join(self.d, "notes.txt"), "rb") as f:
            self.assertEqual(f.read(), b"abc" * 100)

    def test_loose_files_get_folder(self):
        p = os.path.join(self.d, "loose.zip")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("one.txt", "1")
            z.writestr("two.txt", "2")
        arc = core.open_archive(p)
        self.assertEqual(core.smart_destination(arc), os.path.join(self.d, "loose"))

    def test_cancel(self):
        out = os.path.join(self.d, "c.zip")
        core.create_archive(out, [self.src], "zip", core.Options())
        arc = core.open_archive(out)
        ev = threading.Event()
        ev.set()
        with self.assertRaises(core.Cancelled):
            arc.extract(os.path.join(self.d, "c"), None, core.Options(cancel=ev))

    def test_zip_password(self):
        p = os.path.join(self.d, "enc.zip")
        # build a ZipCrypto archive by hand is complex; just verify detection path
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("a.txt", "x")
        arc = core.open_archive(p)
        self.assertFalse(arc.encrypted)

    def test_not_archive(self):
        p = os.path.join(self.d, "plain.txt")
        with open(p, "w") as f:
            f.write("not an archive")
        with self.assertRaises(core.ArchiveError):
            core.open_archive(p)

    def test_human_size(self):
        self.assertEqual(core.human_size(512), "512 B")
        self.assertEqual(core.human_size(1536), "1.5 KB")


if __name__ == "__main__":
    unittest.main()
