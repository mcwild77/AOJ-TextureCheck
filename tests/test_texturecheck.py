import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image, ImageFilter

from texturecheck import cabinet, resize, rules, sources

REPO = Path(__file__).resolve().parent.parent
SAMPLES = REPO / "Cabinets"

try:
    import trimesh  # noqa: F401  (only needed by the 3D-preview / screen-geometry tests)
    HAS_TRIMESH = True
except Exception:
    HAS_TRIMESH = False


def png_bytes(size, color=(200, 30, 30)):
    img = Image.new("RGB", size, color)
    # Add noise-free variety so it is not detected as a flat color.
    img.putpixel((0, 0), (0, 0, 0))
    img.putpixel((1, 0), (255, 255, 255))
    img.putpixel((2, 0), (0, 255, 0))
    img.putpixel((3, 0), (0, 0, 255))
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def make_zip(path, files):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)


class RulesTest(unittest.TestCase):
    def test_power_of_two(self):
        for n in (1, 2, 8, 512, 1024, 2048):
            self.assertTrue(rules.is_power_of_two(n))
        for n in (0, 3, 60, 510, 1030, 1698):
            self.assertFalse(rules.is_power_of_two(n))

    def test_docs_examples(self):
        for size in ((512, 512), (1024, 256), (64, 512)):
            self.assertEqual(rules.check_dimensions(*size), [])
        for size in ((510, 510), (1030, 300), (60, 400)):
            self.assertEqual(rules.check_dimensions(*size)[0].severity, rules.ERROR)

    def test_oversize_is_warning_only_when_power_of_two(self):
        self.assertEqual(rules.check_dimensions(4096, 4096), [])  # 4096 is the cap, not oversize
        issues = rules.check_dimensions(8192, 8192)
        self.assertEqual([i.severity for i in issues], [rules.WARNING])

    def test_suggested_size(self):
        # Round DOWN to the largest power of two that fits, so the tool only shrinks.
        self.assertEqual(rules.suggested_size(773, 262), (512, 256))
        self.assertEqual(rules.suggested_size(3000, 1026), (2048, 1024))
        self.assertEqual(rules.suggested_size(512, 512), (512, 512))

    def test_screen_aspect_ok(self):
        # 4:3 (and, since it is a longer/shorter ratio, 3:4 portrait too) passes.
        self.assertTrue(rules.screen_aspect_ok(4 / 3))
        self.assertTrue(rules.screen_aspect_ok(1.355))  # a real 4:3 with modeling slop
        # The common mistakes people ship are all rejected.
        for ratio in (1.0, 5 / 4, 3 / 2, 16 / 10, 16 / 9):
            self.assertFalse(rules.screen_aspect_ok(ratio))


class CabinetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.zip = os.path.join(self.tmp.name, "cab.zip")

    def test_thumbnail_is_skipped_and_unreferenced_noted(self):
        make_zip(self.zip, {
            "description.yaml": "parts:\n  - name: a\n    art:\n      file: good.png\n",
            "good.png": png_bytes((512, 512)),
            "extra.png": png_bytes((512, 512)),
            "game.mp4.png": png_bytes((498, 380)),
        })
        reports = {r.name: r for r in cabinet.check_cabinet(self.zip)}
        self.assertEqual(set(reports), {"good.png", "extra.png"})
        self.assertTrue(reports["good.png"].referenced)
        self.assertFalse(reports["extra.png"].referenced)

    def test_missing_reference_does_not_crash(self):
        make_zip(self.zip, {
            "description.yaml": "art:\n  file: missing.png\n",
            "a.png": png_bytes((100, 100)),
        })
        reports = cabinet.check_cabinet(self.zip)
        self.assertEqual(reports[0].severity, rules.ERROR)

    def test_corrupt_image_is_reported(self):
        make_zip(self.zip, {"bad.png": b"not a png"})
        [r] = cabinet.check_cabinet(self.zip)
        self.assertTrue(r.unreadable)
        self.assertFalse(r.needs_resize)

    def test_flat_color_note(self):
        img = Image.new("RGB", (1024, 1024), (10, 20, 30))
        out = io.BytesIO()
        img.save(out, "PNG")
        make_zip(self.zip, {"flat.png": out.getvalue()})
        [r] = cabinet.check_cabinet(self.zip)
        self.assertEqual(r.severity, rules.INFO)
        self.assertTrue(r.flat_color)
        self.assertTrue(r.needs_resize)
        self.assertEqual(r.target_size, (8, 8))  # a flat color only needs 8x8
        self.assertEqual(r.recommended_size, (8, 8))

    def test_recommended_size_keeps_detailed_texture(self):
        # Full random noise has real detail at every scale: nothing smaller is faithful.
        noise = Image.frombytes("RGB", (256, 256), os.urandom(256 * 256 * 3))
        out = io.BytesIO()
        noise.save(out, "PNG")
        make_zip(self.zip, {"noise.png": out.getvalue()})
        [r] = cabinet.check_cabinet(self.zip)
        self.assertIsNone(r.recommended_size)

    def test_recommended_size_flags_low_detail_texture(self):
        # A tiny pattern blown up to 512 carries only low-frequency content, so the
        # probe should recommend a much smaller power-of-two size than it ships at.
        seed = Image.frombytes("RGB", (8, 8), os.urandom(8 * 8 * 3))
        blown_up = seed.resize((512, 512), Image.BILINEAR)
        out = io.BytesIO()
        blown_up.save(out, "PNG")
        make_zip(self.zip, {"soft.png": out.getvalue()})
        [r] = cabinet.check_cabinet(self.zip)
        self.assertIsNotNone(r.recommended_size)
        self.assertLess(max(r.recommended_size), 512)
        self.assertTrue(rules.is_power_of_two(r.recommended_size[0]))
        self.assertTrue(rules.is_power_of_two(r.recommended_size[1]))

    def test_recommended_size_flags_low_color_shape(self):
        # A hard-edged 2-color shape with an anti-aliased alpha edge at 1024 (like
        # "start button 2.png"): too many RGBA colors to be flagged flat, and its sharp
        # edges defeat the exact round-trip -- but it's still only two colors, so the
        # low-color path must flag it as wildly reducible.
        from PIL import ImageDraw
        # RGB layer: two hard flat colors (black field, orange disc).
        rgb = Image.new("RGB", (1024, 1024), (2, 2, 2))
        ImageDraw.Draw(rgb).ellipse((150, 150, 870, 870), fill=(239, 130, 24))
        # Alpha layer: the disc as a soft-edged mask, so RGBA has hundreds of distinct
        # values (defeating the flat check) while RGB is still just two colors.
        mask = Image.new("L", (1024, 1024), 0)
        ImageDraw.Draw(mask).ellipse((150, 150, 870, 870), fill=255)
        img = rgb.copy()
        img.putalpha(mask.filter(ImageFilter.GaussianBlur(3)))
        out = io.BytesIO()
        img.save(out, "PNG")
        make_zip(self.zip, {"button.png": out.getvalue()})
        [r] = cabinet.check_cabinet(self.zip)
        self.assertFalse(r.flat_color)                 # not caught by the flat check
        self.assertIsNotNone(r.recommended_size)       # but still flagged
        self.assertLessEqual(max(r.recommended_size), 512)


class ResizeTest(unittest.TestCase):
    def test_resize_writes_new_zip_and_leaves_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dest = os.path.join(tmp, "a.zip"), os.path.join(tmp, "b.zip")
            make_zip(src, {
                "bad.png": png_bytes((773, 262)),
                "ok.png": png_bytes((256, 256)),
                "notes.bas": b"print 1",
            })
            before = Path(src).read_bytes()
            reports = cabinet.check_cabinet(src)
            done = resize.write_resized_cabinet(src, dest, resize.targets_for(reports))
            self.assertEqual(done, ["bad.png"])
            self.assertEqual(Path(src).read_bytes(), before)
            self.assertEqual([r.severity for r in cabinet.check_cabinet(dest)], [None, None])
            with zipfile.ZipFile(dest) as zf:
                self.assertEqual(zf.read("notes.bas"), b"print 1")
                self.assertEqual(Image.open(io.BytesIO(zf.read("bad.png"))).size, (512, 256))

    def test_refuses_to_overwrite_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "a.zip")
            make_zip(src, {"a.png": png_bytes((100, 100))})
            with self.assertRaises(ValueError):
                resize.write_resized_cabinet(src, src, {"a.png": (128, 128)})


class ExportTest(unittest.TestCase):
    FILES = {
        "big.png": png_bytes((773, 262)),
        "ok.png": png_bytes((256, 256)),
        "notes.bas": b"print 1",
    }

    def test_default_export_name(self):
        self.assertEqual(sources.default_export_name("/x/foo.zip"), "foo_optimized")
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "MyCab"
            folder.mkdir()
            self.assertEqual(sources.default_export_name(str(folder)), "MyCab_optimized")

    def test_export_zip_from_zip_resizes_and_leaves_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "a.zip")
            make_zip(src, self.FILES)
            before = Path(src).read_bytes()
            out = resize.export_zip(src, os.path.join(tmp, "out.zip"), {"big.png": (512, 256)})
            self.assertEqual(Path(src).read_bytes(), before)  # source untouched
            with zipfile.ZipFile(out) as zf:
                self.assertEqual(zf.read("notes.bas"), b"print 1")  # copied byte-for-byte
                self.assertEqual(Image.open(io.BytesIO(zf.read("big.png"))).size, (512, 256))
                self.assertEqual(Image.open(io.BytesIO(zf.read("ok.png"))).size, (256, 256))

    def test_export_folder_from_folder_resizes_and_leaves_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "cab"
            src.mkdir()
            for name, data in self.FILES.items():
                (src / name).write_bytes(data)
            out = resize.export_folder(str(src), Path(tmp) / "cab_optimized", {"big.png": (512, 256)})
            self.assertEqual((src / "big.png").read_bytes(), self.FILES["big.png"])  # source untouched
            self.assertEqual((out / "notes.bas").read_bytes(), b"print 1")
            with Image.open(out / "big.png") as im:
                self.assertEqual(im.size, (512, 256))
            with Image.open(out / "ok.png") as im:
                self.assertEqual(im.size, (256, 256))

    def test_export_folder_from_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "a.zip")
            make_zip(src, self.FILES)
            out = resize.export_folder(src, Path(tmp) / "out", {"big.png": (512, 256)})
            with Image.open(out / "big.png") as im:
                self.assertEqual(im.size, (512, 256))
            self.assertEqual((out / "notes.bas").read_bytes(), b"print 1")

    def test_export_folder_refuses_existing_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "a.zip")
            make_zip(src, {"a.png": png_bytes((100, 100))})
            dest = Path(tmp) / "taken"
            dest.mkdir()
            with self.assertRaises(ValueError):
                resize.export_folder(src, dest, {})

    def test_export_zip_refuses_to_overwrite_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "a.zip")
            make_zip(src, {"a.png": png_bytes((100, 100))})
            with self.assertRaises(ValueError):
                resize.export_zip(src, src, {})


@unittest.skipUnless(SAMPLES.exists(), "sample cabinets not present")
class SampleCabinetsTest(unittest.TestCase):
    def report(self, name):
        return {r.name: r for r in cabinet.check_cabinet(str(SAMPLES / name))}

    def test_ridge_racer(self):
        r = self.report("Ridge Racer (SIT DOWN).zip")
        self.assertEqual((r["gauge.png"].width, r["gauge.png"].height), (773, 262))
        self.assertEqual(r["gauge.png"].severity, rules.ERROR)
        self.assertEqual(r["bezel.png"].severity, None)
        self.assertNotIn("ridge.mp4.png", r)

    def test_virtua_cop_2(self):
        r = self.report("virtua cop 2.zip")
        # main.png is a 2290x640 texture that is a single flat color, so it is
        # flagged as a flat color (shrink to 8x8) rather than a power-of-two error.
        self.assertTrue(r["main.png"].flat_color)
        self.assertEqual(r["main.png"].target_size, (8, 8))
        self.assertNotIn("vcop2.mp4.png", r)


class CrtMeshParseTest(unittest.TestCase):
    """crt_custom_mesh reads description.yaml as text -- no trimesh needed."""

    def _mesh(self, crt_yaml):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cab.zip"
            make_zip(path, {"description.yaml": ("name: x\n" + crt_yaml).encode()})
            with sources.open_source(str(path)) as src:
                return cabinet.crt_custom_mesh(src)

    def test_custom_returns_mesh_name(self):
        self.assertEqual(
            self._mesh("crt:\n  type: custom\n  mesh: my-screen\n  screen:\n    shader: crt\n"),
            "my-screen")

    def test_builtin_type_returns_none(self):
        # A built-in screen type uses the engine's own screen; nothing to check.
        self.assertIsNone(self._mesh("crt:\n  type: 19i\n  orientation: horizontal\n"))

    def test_no_crt_block_returns_none(self):
        self.assertIsNone(self._mesh("parts:\n  - name: main\n"))

    def test_crt_block_last_in_file(self):
        # The block regex must still terminate when crt: is the final section.
        self.assertEqual(self._mesh("crt:\n  type: custom\n  mesh: scr"), "scr")


@unittest.skipUnless(HAS_TRIMESH, "trimesh not installed (3D-preview dependency)")
class ScreenGeometryTest(unittest.TestCase):
    """The 4:3 geometry check over a synthetic GLB screen mesh."""

    def _cabinet(self, quads, screen_type="custom", mesh_name="my-screen"):
        """A cabinet zip whose `mesh_name` node holds `quads` flat rectangles.

        Each quad is (width, height); several quads model a TWIN's two screens.
        """
        import numpy as np

        verts, faces = [], []
        x = 0.0
        for w, h in quads:
            base = len(verts)
            verts += [[x, 0, 0], [x + w, 0, 0], [x + w, h, 0], [x, h, 0]]
            faces += [[base, base + 1, base + 2], [base, base + 2, base + 3]]
            x += w + 1.0  # a gap, so the quads are separate connected components
        screen = trimesh.Trimesh(vertices=np.array(verts, float), faces=np.array(faces), process=False)
        scene = trimesh.Scene()
        scene.add_geometry(screen, node_name=mesh_name, geom_name=mesh_name)
        scene.add_geometry(trimesh.creation.box(extents=[2, 2, 2]), node_name="main", geom_name="main")
        yaml = (f"name: test\nparts:\n  - name: main\n    art:\n      file: main.png\n"
                f"crt:\n  type: {screen_type}\n  mesh: {mesh_name}\n  screen:\n    shader: crt\n")
        d = tempfile.mkdtemp()
        path = Path(d) / "cab.zip"
        make_zip(path, {"description.yaml": yaml.encode(), "cab.glb": scene.export(file_type="glb")})
        return str(path)

    def screen(self, *args, **kwargs):
        from texturecheck import preview3d
        return preview3d.build_model(self._cabinet(*args, **kwargs)).screen

    def test_four_three_passes(self):
        s = self.screen([(4, 3)])
        self.assertTrue(s.found and s.ok)

    def test_sixteen_nine_flagged(self):
        s = self.screen([(16, 9)])
        self.assertTrue(s.found)
        self.assertFalse(s.ok)

    def test_square_flagged(self):
        self.assertFalse(self.screen([(1, 1)]).ok)

    def test_portrait_four_three_passes(self):
        # A vertical 3:4 screen has the same longer/shorter ratio as 4:3, so it passes.
        self.assertTrue(self.screen([(3, 4)]).ok)

    def test_twin_two_quads_measured_separately(self):
        # Two 4:3 quads in one node must each read 4:3, not ~2.7:1 for the pair.
        s = self.screen([(4, 3), (4, 3)])
        self.assertEqual(len(s.aspects), 2)
        self.assertTrue(s.ok)

    def test_builtin_type_not_checked(self):
        self.assertIsNone(self.screen([(16, 9)], screen_type="19i"))

    def test_missing_mesh_reported_not_flagged(self):
        # A custom screen naming a mesh the GLB lacks reports found=False -- never a
        # 4:3 alarm (so the GUI stays quiet rather than crying wolf over a typo).
        from texturecheck import preview3d
        path = self._cabinet([(16, 9)], mesh_name="present")
        with zipfile.ZipFile(path) as z:
            data = {n: z.read(n) for n in z.namelist()}
        data["description.yaml"] = data["description.yaml"].replace(b"mesh: present", b"mesh: nope")
        make_zip(path, data)
        screen = preview3d.build_model(path).screen
        self.assertFalse(screen.found)
        self.assertTrue(screen.ok)  # not found is not a failure


if __name__ == "__main__":
    unittest.main()
