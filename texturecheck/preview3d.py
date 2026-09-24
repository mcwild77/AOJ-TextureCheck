"""GPU 3D preview of a cabinet GLB with description.yaml textures applied.

trimesh loads the GLB (geometry, UVs, embedded textures) on the CPU; moderngl
renders it on the GPU through an offscreen OpenGL context. moderngl was chosen
over pyrender because pyrender is unmaintained and breaks against current
numpy/PyOpenGL, whereas moderngl is current and packages cleanly.

Two halves, split by thread affinity:
  * `build_model()` is pure CPU/Pillow work (trimesh parse, resolve the yaml
    texture overrides) and is safe to run on a worker thread.
  * `Renderer` owns the GL context and MUST be created and used on one thread
    (the GUI's main thread). Uploading and drawing are cheap, so the GUI renders
    synchronously on each drag instead of threading it.

description.yaml's `parts:` assignments override the mesh texture; the GLB's own
embedded textures render only as the fallback and are never listed or edited.
"""

import base64
import io
import json
import struct
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

from . import cabinet, rules, sources


@dataclass
class ScreenCheck:
    """Result of the 4:3 aspect check on a `crt: type: custom` screen mesh.

    `aspects` is the longer/shorter side ratio of each screen quad (a TWIN cabinet
    packs two quads under one node, hence a list). `ok` is False when any quad
    strays from 4:3; `found` is False when the named mesh isn't in any GLB.
    """
    mesh_name: str
    found: bool
    ok: bool
    aspects: list = field(default_factory=list)


@dataclass
class Part:
    """One drawable node: interleaved local geometry, its world transform, its texture."""
    interleaved: np.ndarray   # (nv, 8) float32: position(3), normal(3), uv(2)
    indices: np.ndarray       # (nf*3,) uint32
    transform: np.ndarray     # (4, 4) float32, local -> world
    texture: Image.Image | None  # RGB or RGBA; None = untextured (drawn flat grey)
    texture_name: str | None = None  # lowercased basename of the yaml-assigned texture
    has_alpha: bool = False   # texture carries real transparency (draw in the blended pass)


@dataclass
class CabinetModel:
    """CPU-side result of loading a cabinet: everything the GPU needs, no GL objects."""
    parts: list = field(default_factory=list)
    center: np.ndarray = field(default_factory=lambda: np.zeros(3, np.float32))
    radius: float = 1.0
    matched: list = field(default_factory=list)    # yaml parts textured onto a mesh
    unmatched: list = field(default_factory=list)   # yaml parts with no mesh / missing png
    # texture filename (lowercased basename) -> (world center, world radius) of the
    # mesh(es) it is applied to, so the preview can zoom in on a selected texture.
    focus_targets: dict = field(default_factory=dict)
    # texture filename (lowercased basename) -> percent of the texture map its UVs cover.
    uv_coverage: dict = field(default_factory=dict)
    # texture filename (lowercased basename) -> triangle count of the visible mesh(es)
    # it is applied to (summed when several parts share one texture).
    poly_counts: dict = field(default_factory=dict)
    # Triangles across every visible mesh of the cabinet model, textured or not, plus
    # every other model in the zip (other_model_polys: file name -> its triangles).
    total_polys: int = 0
    other_model_polys: dict = field(default_factory=dict)
    # 4:3 check on a custom CRT screen mesh, or None when the cabinet has no custom screen.
    screen: "ScreenCheck | None" = None

    @property
    def empty(self) -> bool:
        return not self.parts


def _texture_image(img: Image.Image) -> Image.Image:
    """Return the texture as RGBA when it has transparency, else RGB.

    The 3D preview blends alpha for real, so (unlike the flat 2D thumbnail) we keep
    the alpha channel instead of compositing it onto white.
    """
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        return img.convert("RGBA")
    return img.convert("RGB")


def _has_real_alpha(img: Image.Image) -> bool:
    """True only when an RGBA image actually has some non-opaque pixels."""
    return img.mode == "RGBA" and img.getchannel("A").getextrema()[0] < 255


def _embedded_texture(geom) -> Image.Image | None:
    material = getattr(getattr(geom, "visual", None), "material", None)
    for attr in ("baseColorTexture", "image"):
        img = getattr(material, attr, None)
        if img is not None:
            try:
                return _texture_image(img)
            except Exception:
                return None
    return None


def _pick_cabinet_glb(zf, part_names: set[str]):
    """Choose the GLB that is the cabinet body.

    description.yaml's model.file is unreliable (it can name a file the zip does
    not contain), so score every GLB by how many scene-graph node names match
    yaml part names and take the best; ties break on file size.
    """
    import trimesh

    best = None
    for name in (n for n in zf.namelist() if n.lower().endswith(".glb")):
        data = zf.read(name)
        try:
            scene = trimesh.load(io.BytesIO(data), file_type="glb", process=False)
        except Exception:
            continue
        if not isinstance(scene, trimesh.Scene):
            continue
        nodes = {n.lower() for n in scene.graph.nodes}
        key = (len(nodes & part_names), len(data))
        if best is None or key > best[0]:
            best = (key, scene, data, name)
    return best[1:] if best else (None, None, None)


def _planar_aspect(verts: np.ndarray) -> float | None:
    """Longer/shorter in-plane side of a roughly planar vertex set, via PCA.

    A screen quad is flat, so PCA's two largest principal axes span its face and the
    smallest is its thickness/curvature (dropped). The ratio of the face extents is the
    aspect, independent of how the screen is oriented in the cabinet. None if degenerate.
    """
    if len(verts) < 3:
        return None
    centered = verts - verts.mean(0)
    try:
        _, _, vt = np.linalg.svd(centered, full_matrices=False)
    except Exception:
        return None
    proj = centered @ vt.T
    ext = np.sort(proj.max(0) - proj.min(0))[::-1]  # [major, minor, thickness]
    if ext[1] <= 1e-9:
        return None
    return float(ext[0] / ext[1])


def _screen_aspects(scene, mesh_name: str) -> list[float]:
    """Aspect ratio of each screen quad in `scene`'s node named `mesh_name`.

    The named node is split into connected components first, because a TWIN cabinet
    holds two separate screen quads under one node -- measuring them together would
    read ~2.7:1 and false-alarm. Each component is measured on its own.
    """
    import trimesh

    node = {n.lower(): n for n in scene.graph.nodes}.get(mesh_name.lower())
    if node is None:
        return []
    try:
        transform, geom_name = scene.graph[node]
    except Exception:
        return []
    geom = scene.geometry.get(geom_name)
    if geom is None or len(getattr(geom, "faces", [])) == 0:
        return []
    world = trimesh.transformations.transform_points(
        np.asarray(geom.vertices, np.float64), transform)
    aspects = []
    for comp in _connected_vertex_sets(len(world), np.asarray(geom.faces)):
        aspect = _planar_aspect(world[comp])
        if aspect is not None:
            aspects.append(aspect)
    return aspects


def _connected_vertex_sets(n_verts: int, faces: np.ndarray) -> list[np.ndarray]:
    """Vertex indices of each face-connected piece of a mesh.

    Done in numpy rather than trimesh's `mesh.split()`, which needs scipy or networkx:
    neither is a dependency, so in the packaged exe split() silently fails and a TWIN's
    two screens get measured as one wide quad. Labels spread across each face (every
    vertex takes its face's smallest label) with pointer jumping until nothing changes.
    """
    faces = faces.reshape(len(faces), -1)
    labels = np.arange(n_verts)
    while True:
        face_min = labels[faces].min(1)
        new = labels.copy()
        np.minimum.at(new, faces, face_min[:, None])
        new = new[new]  # pointer jumping: follow a label to its own label
        if np.array_equal(new, labels):
            break
        labels = new
    used = np.unique(faces)
    return [used[labels[used] == root] for root in np.unique(labels[used])]


def _check_screen(zf, scene, mesh_name: str | None) -> "ScreenCheck | None":
    """Aspect-check a custom CRT screen mesh, or None when the cabinet has no custom one.

    Tries the already-loaded cabinet `scene` first; if the mesh lives in a different
    GLB, scans the rest of the zip for it. A missing mesh is reported (found=False),
    never treated as a 4:3 failure.
    """
    import trimesh

    if not mesh_name:
        return None
    aspects = _screen_aspects(scene, mesh_name)
    if not aspects:
        for name in (n for n in zf.namelist() if n.lower().endswith(".glb")):
            try:
                other = trimesh.load(io.BytesIO(zf.read(name)), file_type="glb", process=False)
            except Exception:
                continue
            if isinstance(other, trimesh.Scene):
                aspects = _screen_aspects(other, mesh_name)
                if aspects:
                    break
    if not aspects:
        return ScreenCheck(mesh_name, found=False, ok=True, aspects=[])
    ok = all(rules.screen_aspect_ok(a) for a in aspects)
    return ScreenCheck(mesh_name, found=True, ok=ok, aspects=aspects)


def build_model(zip_path: str) -> CabinetModel:
    """Load the cabinet GLB and resolve description.yaml texture overrides (CPU only).

    `zip_path` may be a .zip file or a folder of loose cabinet files.
    """
    import trimesh

    model = CabinetModel()
    with sources.open_source(zip_path) as zf:
        art = cabinet.part_art_map(zf)  # part name -> texture filename
        styles = cabinet.part_style_map(zf)  # part name -> {'color', 'visible'}
        scene, glb_bytes, glb_name = _pick_cabinet_glb(zf, set(art))
        if scene is None:
            return model
        # 4:3 check on the author's own screen mesh, when the cabinet ships a custom one.
        model.screen = _check_screen(zf, scene, cabinet.crt_custom_mesh(zf))
        zip_names = {n.rsplit("/", 1)[-1].lower(): n for n in zf.namelist()}
        # trimesh only exposes UVs for primitives that carry an embedded texture, so
        # read TEXCOORD_0 straight from the GLB for every named node (see _glb_node_uvs).
        raw_uvs = _glb_node_uvs(glb_bytes)
        node_tris = _glb_node_triangles(glb_bytes)
        # Other models in the zip (e.g. a lightgun's `gun: model:`) are drawn in game
        # too, so they count toward the cabinet's total. Counted whole, since the yaml's
        # `visible:` flags only describe the cabinet body.
        for name in zf.namelist():
            if name.lower().endswith(".glb") and name != glb_name:
                tris = sum(t for _, t in _glb_node_triangles(zf.read(name)))
                if tris:
                    model.other_model_polys[name.rsplit("/", 1)[-1]] = tris

        lo = np.full(3, np.inf)
        hi = np.full(3, -np.inf)
        tex_cache: dict[str, Image.Image | None] = {}
        matched, unmatched = set(), []
        focus: dict[str, list] = {}  # texture basename -> [lo, hi] world bounds

        for node in scene.graph.nodes_geometry:
            transform, geom_name = scene.graph[node]
            geom = scene.geometry.get(geom_name)
            if geom is None or len(getattr(geom, "faces", [])) == 0:
                continue
            style = styles.get(node.lower(), {})
            if style.get("visible") is False:
                continue  # description.yaml hides this part (e.g. `visible: false`)

            v = np.asarray(geom.vertices, dtype=np.float32)
            try:
                n = np.asarray(geom.vertex_normals, dtype=np.float32)
            except Exception:
                n = np.zeros_like(v)
            if len(n) != len(v):
                n = np.zeros_like(v)
            tm_uv = getattr(getattr(geom, "visual", None), "uv", None)
            if tm_uv is not None and len(tm_uv) == len(v):
                uv = np.asarray(tm_uv, dtype=np.float32)
            else:
                # trimesh dropped the UVs; fall back to the ones read from the GLB when
                # the node is a single primitive whose vertex count lines up with trimesh's.
                # trimesh flips V on load and the shader is written for that convention, so
                # flip V here too (raw GLB UVs are unflipped). Coverage uses the raw UVs
                # separately, and a flip doesn't change area, so it is unaffected.
                prims = raw_uvs.get(node.lower())
                if prims and len(prims) == 1 and len(prims[0][0]) == len(v):
                    uv = prims[0][0].copy()
                    uv[:, 1] = 1.0 - uv[:, 1]
                else:
                    uv = np.zeros((len(v), 2), np.float32)

            # yaml override wins over the embedded texture.
            texture = None
            override = art.get(node.lower())
            if override is not None:
                key = f"o:{override.lower()}"
                if key in tex_cache:
                    texture = tex_cache[key]
                    if texture is not None:
                        matched.add(node)
                elif override.lower() not in zip_names:
                    unmatched.append(f"{node} -> {override} (not in zip)")
                    tex_cache[key] = None
                else:
                    try:
                        texture = _texture_image(Image.open(io.BytesIO(zf.read(zip_names[override.lower()]))))
                        matched.add(node)
                    except Exception:
                        unmatched.append(f"{node} -> {override} (unreadable)")
                    tex_cache[key] = texture
            # A yaml `color:` (no texture) paints the part a flat color instead of grey.
            if texture is None and style.get("color") is not None:
                texture = Image.new("RGB", (1, 1), style["color"])
            if texture is None:
                key = f"e:{geom_name}"
                if key not in tex_cache:
                    tex_cache[key] = _embedded_texture(geom)
                texture = tex_cache[key]

            interleaved = np.hstack([v, n, uv]).astype(np.float32)
            indices = np.asarray(geom.faces, dtype=np.uint32).ravel()
            t = np.asarray(transform, dtype=np.float32)
            tname = override.rsplit("/", 1)[-1].lower() if override is not None else None
            has_alpha = texture is not None and _has_real_alpha(texture)
            model.parts.append(Part(interleaved, indices, t, texture, tname, has_alpha))

            world = trimesh.transformations.transform_points(v.astype(np.float64), transform)
            lo = np.minimum(lo, world.min(0))
            hi = np.maximum(hi, world.max(0))

            # Track world-space bounds per assigned texture, so a selected texture
            # can be framed. Several parts may share one texture; union their bounds.
            if override is not None:
                key = override.rsplit("/", 1)[-1].lower()
                p_lo, p_hi = world.min(0), world.max(0)
                b = focus.get(key)
                if b is None:
                    focus[key] = [p_lo, p_hi]
                else:
                    b[0], b[1] = np.minimum(b[0], p_lo), np.maximum(b[1], p_hi)

    if not model.parts:
        return model
    model.center = ((lo + hi) / 2).astype(np.float32)
    model.radius = float(np.linalg.norm(hi - lo) / 2) or 1.0

    node_names = {n.lower() for n in scene.graph.nodes}
    for part in art:
        if part not in node_names:
            unmatched.append(f"{part} (no mesh named this)")
    model.matched = sorted(matched)
    model.unmatched = sorted(unmatched)
    model.focus_targets = {
        k: (((lo + hi) / 2).astype(np.float32), float(np.linalg.norm(hi - lo) / 2) or model.radius)
        for k, (lo, hi) in focus.items()
    }
    # UV coverage per texture, gathered from the GLB's own TEXCOORD_0 (grouped by the
    # texture each node is assigned in the yaml), so it works even where trimesh hid UVs.
    cov_tris: dict[str, list] = {}
    for node_lower, prims in raw_uvs.items():
        override = art.get(node_lower)
        if override is None:
            continue
        key = override.rsplit("/", 1)[-1].lower()
        cov_tris.setdefault(key, []).extend(prims)
    model.uv_coverage = {name: _uv_coverage(tris) for name, tris in cov_tris.items()}
    # Triangles for the whole cabinet and per texture, from the same GLB node names.
    # Hidden parts don't draw in game, so they don't count.
    model.total_polys = sum(model.other_model_polys.values())
    for node_lower, tris in node_tris:
        if styles.get(node_lower, {}).get("visible") is False:
            continue
        model.total_polys += tris
        override = art.get(node_lower)
        if override is not None:
            key = override.rsplit("/", 1)[-1].lower()
            model.poly_counts[key] = model.poly_counts.get(key, 0) + tris
    return model


def _uv_coverage(tris, grid: int = 256) -> float:
    """Percent of the 0..1 texture map covered by the mesh's UV triangles.

    The UV triangles are rasterized into a `grid`x`grid` mask and the filled
    fraction is returned. UVs outside 0..1 are clipped to the map, so a low
    number means much of the texture is unused and could be shrunk or repacked.
    """
    from PIL import ImageDraw

    mask = Image.new("L", (grid, grid), 0)
    draw = ImageDraw.Draw(mask)
    for uv, faces in tris:
        pts = uv * grid
        for f in faces:
            draw.polygon([(float(pts[i, 0]), float(pts[i, 1])) for i in f], fill=255)
    covered = int(np.count_nonzero(np.asarray(mask)))
    return 100.0 * covered / (grid * grid)


# glTF component/type tables: componentType -> (numpy dtype, byte size); type -> n components.
_GLTF_DTYPE = {5120: "i1", 5121: "u1", 5122: "i2", 5123: "u2", 5125: "u4", 5126: "f4"}
_GLTF_SIZE = {"i1": 1, "u1": 1, "i2": 2, "u2": 2, "u4": 4, "f4": 4}
_GLTF_NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}
_GLTF_NORM_MAX = {"u1": 255.0, "u2": 65535.0, "i1": 127.0, "i2": 32767.0}


def _parse_glb(data: bytes):
    """Split a .glb into its glTF JSON and the list of binary buffers, or None."""
    if len(data) < 12 or struct.unpack_from("<I", data, 0)[0] != 0x46546C67:  # 'glTF'
        return None
    length = struct.unpack_from("<I", data, 8)[0]
    off, gltf_json, bin_chunk = 12, None, None
    while off + 8 <= length:
        clen, ctype = struct.unpack_from("<II", data, off)
        off += 8
        chunk = data[off:off + clen]
        off += clen
        if ctype == 0x4E4F534A:      # 'JSON'
            gltf_json = chunk
        elif ctype == 0x004E4942:    # 'BIN\0'
            bin_chunk = chunk
    if gltf_json is None:
        return None
    gltf = json.loads(gltf_json)
    buffers = []
    for b in gltf.get("buffers", []):
        uri = b.get("uri")
        if uri is None:
            buffers.append(bin_chunk)
        elif uri.startswith("data:"):
            buffers.append(base64.b64decode(uri.split(",", 1)[1]))
        else:
            buffers.append(None)  # external file: not available from the zip
    return gltf, buffers


def _read_accessor(gltf, buffers, idx: int) -> np.ndarray:
    """Read a glTF accessor into an (count, ncomp) float array, honoring stride/normalize."""
    acc = gltf["accessors"][idx]
    bv = gltf["bufferViews"][acc["bufferView"]]
    buf = buffers[bv["buffer"]]
    if buf is None:
        raise ValueError("buffer not available")
    dt = _GLTF_DTYPE[acc["componentType"]]
    elem = _GLTF_SIZE[dt]
    ncomp = _GLTF_NCOMP[acc["type"]]
    count = acc["count"]
    base = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = bv.get("byteStride") or elem * ncomp
    # Gather each element's contiguous bytes with a strided view (works for packed too).
    raw = np.frombuffer(buf, dtype=np.uint8)
    view = np.lib.stride_tricks.as_strided(
        raw[base:], shape=(count, elem * ncomp), strides=(stride, 1))
    out = view.copy().view(f"<{dt}").reshape(count, ncomp).astype(np.float32)
    if acc.get("normalized") and dt in _GLTF_NORM_MAX:
        out = out / _GLTF_NORM_MAX[dt]
    return out


def _glb_node_uvs(data: bytes) -> dict:
    """node name (lowercased) -> [(uv (nv,2) float32, faces (nf,3) int), ...] from the GLB.

    Reads TEXCOORD_0 directly, which trimesh only surfaces for textured primitives, so
    yaml-textured flat panels still yield UVs. Any parse trouble degrades to {}.
    """
    try:
        parsed = _parse_glb(data)
    except Exception:
        return {}
    if parsed is None:
        return {}
    gltf, buffers = parsed
    meshes = gltf.get("meshes", [])
    out: dict[str, list] = {}
    for node in gltf.get("nodes", []):
        name, mesh_idx = node.get("name"), node.get("mesh")
        if name is None or mesh_idx is None or mesh_idx >= len(meshes):
            continue
        for prim in meshes[mesh_idx].get("primitives", []):
            if prim.get("mode", 4) != 4:  # 4 = triangles; skip strips/fans/lines
                continue
            tc = prim.get("attributes", {}).get("TEXCOORD_0")
            if tc is None:
                continue
            try:
                uv = _read_accessor(gltf, buffers, tc)
                if "indices" in prim:
                    idx = _read_accessor(gltf, buffers, prim["indices"]).ravel().astype(np.int64)
                else:
                    idx = np.arange(len(uv), dtype=np.int64)
            except Exception:
                continue
            faces = idx[: len(idx) - len(idx) % 3].reshape(-1, 3)
            out.setdefault(name.lower(), []).append((uv, faces))
    return out


def _glb_node_triangles(data: bytes) -> list:
    """[(node name lowercased, or "" if unnamed; triangle count), ...] per mesh node.

    Read from the GLB's glTF JSON per node rather than per trimesh geometry, because
    trimesh splits a multi-primitive mesh into `<name>_<hash>` child nodes that no longer
    match the yaml part name. Only accessor counts are read, no buffers. Any parse
    trouble degrades to [].
    """
    try:
        parsed = _parse_glb(data)
    except Exception:
        return []
    if parsed is None:
        return []
    gltf = parsed[0]
    meshes, accessors = gltf.get("meshes", []), gltf.get("accessors", [])
    out: list[tuple[str, int]] = []
    for node in gltf.get("nodes", []):
        name, mesh_idx = node.get("name") or "", node.get("mesh")
        if mesh_idx is None or mesh_idx >= len(meshes):
            continue
        tris = 0
        for prim in meshes[mesh_idx].get("primitives", []):
            acc = prim.get("indices", prim.get("attributes", {}).get("POSITION"))
            if acc is None or acc >= len(accessors):
                continue
            n = accessors[acc].get("count", 0)
            mode = prim.get("mode", 4)
            if mode == 4:  # triangles
                tris += n // 3
            elif mode in (5, 6):  # triangle strip / fan
                tris += max(n - 2, 0)
        out.append((name.lower(), tris))
    return out


def _look_at(eye, target, up):
    f = target - eye
    f /= np.linalg.norm(f)
    s = np.cross(f, up)
    s /= np.linalg.norm(s)
    u = np.cross(s, f)
    m = np.eye(4, dtype=np.float32)
    m[0, :3], m[1, :3], m[2, :3] = s, u, -f
    m[:3, 3] = [-s @ eye, -u @ eye, f @ eye]
    return m


def _perspective(fovy, aspect, near, far):
    t = 1.0 / np.tan(fovy / 2)
    m = np.zeros((4, 4), dtype=np.float32)
    m[0, 0] = t / aspect
    m[1, 1] = t
    m[2, 2] = (far + near) / (near - far)
    m[2, 3] = 2 * far * near / (near - far)
    m[3, 2] = -1.0
    return m


_VERT = """
#version 330
uniform mat4 mvp;
uniform mat3 nmat;
in vec3 in_pos;
in vec3 in_norm;
in vec2 in_uv;
out vec3 v_norm;
out vec2 v_uv;
void main() {
    v_norm = nmat * in_norm;
    v_uv = vec2(in_uv.x, 1.0 - in_uv.y);
    gl_Position = mvp * vec4(in_pos, 1.0);
}
"""

_FRAG = """
#version 330
uniform sampler2D tex;
uniform vec3 light_dir;   // world space, points toward the light
uniform float highlight;  // 0 = normal, up to 1 = blended fully to highlight_color
uniform vec3 highlight_color;
in vec3 v_norm;
in vec2 v_uv;
out vec4 frag;
void main() {
    vec3 n = normalize(v_norm);
    float d = abs(dot(n, light_dir));       // two-sided
    float shade = 0.45 + 0.55 * d;
    vec4 texel = texture(tex, v_uv);        // RGB textures read back alpha = 1
    vec3 base = texel.rgb * shade;
    frag = vec4(mix(base, highlight_color, highlight), texel.a);
}
"""


@dataclass
class _Drawable:
    """One uploaded part: its GL objects, transform, and which texture it shows.

    `tex` is what renders now; `orig_tex` is the model's own texture, so a resize
    preview can swap `tex` and later restore it.
    """
    vao: object
    tex: object
    orig_tex: object
    transform: np.ndarray
    tname: str | None
    has_alpha: bool
    centroid: np.ndarray


class Renderer:
    """Owns the offscreen GL context. Create and use on the GUI main thread only."""

    def __init__(self):
        import moderngl

        self.ctx = moderngl.create_standalone_context(require=330)
        self.ctx.enable(moderngl.DEPTH_TEST)
        # Anisotropic filtering keeps textures sharp on surfaces seen at a grazing angle
        # (cabinet sides, marquee). Clamp our request to what the GPU actually supports.
        self._aniso = float(getattr(self.ctx, "max_anisotropy", 1.0) or 1.0)
        self.prog = self.ctx.program(vertex_shader=_VERT, fragment_shader=_FRAG)
        self.prog["highlight_color"].value = (1.0, 0.9, 0.0)  # flash color for a selected texture
        self._flat = self.ctx.texture((1, 1), 3, bytes((170, 170, 170)))  # untextured grey
        self._fbo = None
        self._size = None
        self._drawables = []      # list of _Drawable for the current model
        self._owned = []          # GL objects to release when the model changes
        self._preview_owned = []  # GL textures for the current resize preview
        self.center = np.zeros(3, np.float32)
        self.radius = 1.0

    def _make_texture(self, img):
        import moderngl

        mode = "RGBA" if img.mode == "RGBA" else "RGB"
        if img.mode != mode:
            img = img.convert(mode)
        tex = self.ctx.texture(img.size, 4 if mode == "RGBA" else 3, img.tobytes())
        tex.build_mipmaps()
        tex.filter = (moderngl.LINEAR_MIPMAP_LINEAR, moderngl.LINEAR)  # trilinear base
        tex.anisotropy = self._aniso  # + anisotropic, clamped to the GPU max
        return tex

    def upload(self, model: CabinetModel):
        for obj in self._owned + self._preview_owned:
            obj.release()
        self._owned, self._preview_owned, self._drawables = [], [], []
        self.center, self.radius = model.center, model.radius

        for part in model.parts:
            vbo = self.ctx.buffer(part.interleaved.tobytes())
            ibo = self.ctx.buffer(part.indices.tobytes())
            vao = self.ctx.vertex_array(
                self.prog, [(vbo, "3f 3f 2f", "in_pos", "in_norm", "in_uv")], ibo)
            self._owned += [vbo, ibo, vao]
            if part.texture is not None:
                img = part.texture if part.has_alpha else part.texture.convert("RGB")
                tex = self._make_texture(img)
                self._owned.append(tex)
            else:
                tex = self._flat
            # World-space centroid, for back-to-front sorting of the transparent parts.
            local_center = part.interleaved[:, :3].mean(axis=0)
            centroid = (part.transform @ np.append(local_center, 1.0))[:3].astype(np.float32)
            self._drawables.append(_Drawable(
                vao, tex, tex, part.transform.astype(np.float32),
                part.texture_name, part.has_alpha, centroid))

    def set_overrides(self, overrides):
        """Swap in resized textures for a preview: {texture_name: PIL image}.

        Every part using a listed name shows the given image; all others revert to
        the model's own texture. Passing None or {} restores everything. Replaces
        any previous override set (only one is active at a time).
        """
        for tex in self._preview_owned:
            tex.release()
        self._preview_owned = []
        for d in self._drawables:
            d.tex = d.orig_tex
        if not overrides:
            return
        built = {}  # texture_name -> GL texture, so a shared texture uploads once
        for d in self._drawables:
            if d.tname in overrides:
                if d.tname not in built:
                    built[d.tname] = self._make_texture(overrides[d.tname])
                    self._preview_owned.append(built[d.tname])
                d.tex = built[d.tname]

    def _ensure_fbo(self, width, height):
        if self._size != (width, height):
            if self._fbo is not None:
                # Grab the attachments before releasing the framebuffer -- after
                # release its .color_attachments/.depth_attachment read back as None.
                attachments = list(self._fbo.color_attachments)
                if self._fbo.depth_attachment:
                    attachments.append(self._fbo.depth_attachment)
                self._fbo.release()
                for a in attachments:
                    a.release()
            color = self.ctx.texture((width, height), 3)
            depth = self.ctx.depth_texture((width, height))
            self._fbo = self.ctx.framebuffer(color_attachments=[color], depth_attachment=depth)
            self._size = (width, height)

    def render(self, azimuth: float, elevation: float, width: int, height: int,
               center=None, radius=None, highlight_names=None, highlight=0.0) -> Image.Image:
        """Render `width`x`height`, orbiting `center` at framing distance set by `radius`.

        The image matches the pane's aspect ratio so the cabinet is not cropped to a
        square. center/radius default to the whole model; pass a part's center and
        radius to zoom in on it. Near/far clipping always uses the model radius, so the
        rest of the cabinet still renders when zoomed in on a small part.

        Parts whose texture name is in `highlight_names` are tinted toward the
        flash color by `highlight` (0..1); used to flash a selected texture.
        """
        self._ensure_fbo(width, height)
        self._fbo.use()
        self._fbo.clear(0.5, 0.5, 0.5, 1.0)  # 50% grey backdrop

        c = self.center if center is None else np.asarray(center, dtype=np.float32)
        r = self.radius if radius is None else float(radius)
        aspect = width / height
        ce, se, ca, sa = np.cos(elevation), np.sin(elevation), np.cos(azimuth), np.sin(azimuth)
        direction = np.array([ce * sa, se, ce * ca], dtype=np.float32)
        fovy = np.radians(40.0)
        # Frame by the vertical field of view; pull back further on a portrait pane
        # (aspect < 1) so the model still fits across the narrower width.
        fit = r / np.tan(fovy / 2) * 1.25 / min(1.0, aspect)
        eye = c + direction * fit
        view = _look_at(eye, c.astype(np.float32), np.array([0, 1, 0], np.float32))
        proj = _perspective(fovy, aspect, self.radius * 0.05, self.radius * 20 + fit)
        vp = proj @ view
        light = direction / np.linalg.norm(direction)  # headlamp: light sits at the camera
        self.prog["light_dir"].value = tuple(float(x) for x in light)

        def draw(d):
            mvp = (vp @ d.transform).astype(np.float32)
            nmat = np.linalg.inv(d.transform[:3, :3]).T.astype(np.float32)
            # numpy is row-major, GLSL expects column-major, hence the transpose.
            self.prog["mvp"].write(np.ascontiguousarray(mvp.T).tobytes())
            self.prog["nmat"].write(np.ascontiguousarray(nmat.T).tobytes())
            self.prog["highlight"].value = (
                highlight if highlight_names and d.tname in highlight_names else 0.0)
            d.tex.use(0)
            self.prog["tex"].value = 0
            d.vao.render()

        # Opaque parts first (writing depth), then transparent parts blended over them,
        # sorted far-to-near with depth writes off so they layer correctly.
        opaque = [d for d in self._drawables if not d.has_alpha]
        transparent = [d for d in self._drawables if d.has_alpha]
        for d in opaque:
            draw(d)
        if transparent:
            import moderngl
            self.ctx.enable(moderngl.BLEND)
            self.ctx.blend_func = moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA
            self.ctx.depth_mask = False
            transparent.sort(key=lambda d: -float(np.linalg.norm(d.centroid - eye)))
            for d in transparent:
                draw(d)
            self.ctx.depth_mask = True
            self.ctx.disable(moderngl.BLEND)

        data = self._fbo.read(components=3)
        img = Image.frombytes("RGB", (width, height), data)
        return img.transpose(Image.FLIP_TOP_BOTTOM)
