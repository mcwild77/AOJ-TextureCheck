"""Tkinter front end. All checking and resizing logic lives in cabinet.py / resize.py."""

import io
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk

import numpy as np
from PIL import Image, ImageOps, ImageTk

from . import cabinet, preview3d, resize, rules, sources

PREVIEW_MAX = 320  # longest edge, in pixels, that a preview image is scaled to fit
MODEL_MAX = 1440   # cap on the 3D render's longest edge; big enough to fill most panes
UV_FLASH_COLOR = (255, 0, 0)  # flash color for a texture's unused UV space in the Base preview

SEVERITY_LABEL = {rules.ERROR: "Fix", rules.WARNING: "Warning", rules.INFO: "Note", None: "OK"}


def human_bytes(n: int) -> str:
    """Byte count as a short human string, e.g. 512 B, 48 KB, 12.0 MB."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Age of Joy Texture Checker")
        self.geometry("900x520")
        self.minsize(700, 360)
        self.cabinet_path: str | None = None  # the open cabinet: a .zip file or a folder
        self.reports: list[cabinet.TextureReport] = []
        self.model: preview3d.CabinetModel | None = None
        self.renderer: preview3d.Renderer | None = None
        self._renderer_failed = False  # GL context couldn't be created on this machine
        self.azimuth, self.elevation = 0.6, 0.15
        self.cam_center = None          # look-at point; None until a model loads
        self.cam_radius = 1.0           # framing radius (smaller = zoomed in)
        self._cam_anim_after = None     # pending camera-animation callback
        self._highlight_name = None     # texture currently flashing on the 3D model
        self._highlight = 0.0           # flash intensity, 0..1
        self._uv_coverage = {}          # texture -> percent of it the UVs cover (from the model)
        self._uv_tris = {}              # texture -> UV triangles, for textures with unused UV space
        self._uv_flash_name = None      # texture whose unused UV space is flashing red in Base
        self._uv_flash = 0.0            # that flash's intensity, 0..1
        self._uv_mask = None            # (texture, size, mask): last unused-UV mask drawn
        self._base_fit = None           # (source, box, image): Base texture scaled to its panel
        self._model_photo = None       # keep a ref so Tk doesn't drop the 3D image
        self._load_gen = 0             # bumps each open, so stale loads are ignored
        self._selected_report = None   # the texture whose resize preview is showing
        self._resized_cache = {}       # (texture name, size) -> resized PIL image, in memory
        self._resize_overrides = {}    # texture name (lower) -> (w, h) the user picked in the dropdown
        self._base_src = None          # current base/resized PIL images, kept so the
        self._resized_src = None       # previews can re-scale when the panel resizes
        self._resized_no_change = False  # chosen size == original: grey the resized preview out
        self._preview_sig = None       # last-rendered (boxes, state): skip no-op re-renders
        self._apply_theme()
        self._build()
        self._maximize()
        self._take_focus()

    def _take_focus(self):
        # Python launched from a terminal doesn't come forward on macOS. Briefly forcing
        # topmost pops the window to the front, then we drop it so it behaves normally.
        self.lift()
        self.attributes("-topmost", True)
        self.after(300, lambda: self.attributes("-topmost", False))
        self.focus_force()

    def _maximize(self):
        # "zoomed" works on Windows (the target platform) and many Linux WMs;
        # macOS Tk supports neither, so fall back to filling the screen.
        for attempt in (lambda: self.state("zoomed"), lambda: self.attributes("-zoomed", True)):
            try:
                attempt()
                return
            except tk.TclError:
                pass
        self.update_idletasks()
        self.geometry(f"{self.winfo_screenwidth()}x{self.winfo_screenheight()}+0+0")

    def _apply_theme(self):
        # The severity colors are dark tones chosen for a light background. macOS's
        # default "aqua" theme follows the system appearance, so in dark mode the
        # backgrounds go dark and those colors become unreadable. Pin a stock light
        # theme with explicit light backgrounds so the app looks the same everywhere.
        BG, FIELD, FG = "#ffffff", "#ffffff", "#000000"
        self.configure(background=BG)
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure(".", background=BG, foreground=FG)
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("TButton", background=BG, foreground=FG)
        style.configure("Treeview", background=FIELD, fieldbackground=FIELD, foreground=FG)
        style.configure("Treeview.Heading", background=BG, foreground=FG)

    def _build(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")
        ttk.Button(top, text="Open cabinet (.zip)...", command=self.open_zip).pack(side="left")
        ttk.Button(top, text="Open cabinet (folder)...", command=self.open_folder).pack(
            side="left", padx=(6, 0))
        self.path_label = ttk.Label(top, text="No cabinet loaded")
        self.path_label.pack(side="left", padx=10)

        # Export controls, aligned to the right of the toolbar: a name field (defaults
        # to <cabinet>_optimized) and the two nondestructive export buttons.
        export = ttk.Frame(top)
        export.pack(side="right")
        ttk.Label(export, text="Save as:").pack(side="left")
        self.export_name = ttk.Entry(export, width=26)
        self.export_name.pack(side="left", padx=(4, 8))
        self.export_folder_btn = ttk.Button(
            export, text="Export to New Folder", command=self.export_to_folder, state="disabled")
        self.export_folder_btn.pack(side="left")
        self.export_zip_btn = ttk.Button(
            export, text="Export to Zip", command=self.export_to_zip, state="disabled")
        self.export_zip_btn.pack(side="left", padx=(6, 0))

        self.summary = ttk.Label(self, padding=(10, 0), font=("TkDefaultFont", 11, "bold"))
        self.summary.pack(fill="x")

        # The byte totals sit on their own row below the summary line.
        self.stats = ttk.Label(self, padding=(10, 0), justify="left", font=("TkDefaultFont", 11, "bold"))
        self.stats.pack(fill="x")

        # The cabinet's total polygon count, below the byte totals. Turns into an orange
        # (warning) or red (error) alert past the rules.POLY_TIERS thresholds. Filled in
        # once the 3D model loads.
        self.poly_label = ttk.Label(
            self, padding=(10, 0), justify="left", font=("TkDefaultFont", 11, "bold"))
        self.poly_label.pack(fill="x")

        # Custom-screen 4:3 warning, right below the totals. Red and bold when a custom
        # CRT screen mesh isn't 4:3; blank otherwise. Filled in once the 3D model loads.
        self.screen_label = ttk.Label(
            self, padding=(10, 0), justify="left",
            font=("TkDefaultFont", 11, "bold"), foreground="#c00000")
        self.screen_label.pack(fill="x")

        # Age of Joy cache-file (.aojv1) warning, under the screen warning. Orange; only
        # packed while the open cabinet has cache files, so it takes no space otherwise.
        self.cache_label = ttk.Label(
            self, padding=(10, 0), justify="left",
            font=("TkDefaultFont", 11, "bold"), foreground="#c06000")
        self.cache_files: dict[str, int] = {}  # the open cabinet's .aojv1 files -> bytes

        # Top half: the 3D cabinet on the left (~1/3), then the selected texture with
        # its resize-target dropdowns. Bottom half: the texture list. Sashes drag.
        split = ttk.PanedWindow(self, orient="vertical")
        split.pack(fill="both", expand=True, padx=10, pady=(6, 10))

        previews = ttk.PanedWindow(split, orient="horizontal")
        split.add(previews, weight=3)

        model = ttk.Frame(previews, padding=10)
        previews.add(model, weight=1)
        header = ttk.Frame(model)
        header.pack(fill="x")
        ttk.Label(header, text="Cabinet  ·  drag to rotate", font=("TkDefaultFont", 11, "bold")).pack(side="left")
        self.auto_focus = tk.BooleanVar(value=True)
        ttk.Checkbutton(header, text="Auto Focus", variable=self.auto_focus).pack(side="right")
        self.show_resized = tk.BooleanVar(value=True)  # 3D shows resized textures by default
        ttk.Checkbutton(
            header, text="Show resized textures", variable=self.show_resized,
            command=self._apply_3d_overrides).pack(side="right", padx=(0, 12))
        self.model_view = tk.Label(
            model, background="#808080", anchor="center",
            text="Open a cabinet to see its 3D preview", foreground="#dddddd")
        self.model_view.pack(fill="both", expand=True, pady=6)
        self.model_view.bind("<ButtonPress-1>", self._drag_start)
        self.model_view.bind("<B1-Motion>", self._drag_move)
        self.model_view.bind("<Configure>", self._on_model_resize)
        self.model_view.bind("<MouseWheel>", self._on_wheel)   # Windows / macOS
        self.model_view.bind("<Button-4>", self._on_wheel)     # Linux scroll up
        self.model_view.bind("<Button-5>", self._on_wheel)     # Linux scroll down
        self._rendered_size = None

        # Base texture on the left, the resized result immediately to its right. The
        # "Resize to" controls live in their own labeled panel, packed to the bottom
        # FIRST so it always stays visible no matter how tall the images make the panel.
        preview = ttk.Frame(previews, padding=10)
        previews.add(preview, weight=2)
        ttk.Label(preview, text="Texture", font=("TkDefaultFont", 11, "bold")).pack(anchor="w")

        pow2 = [str(n) for n in rules.power_of_two_options()]
        # Title and dropdowns centered: the dropdowns sit in an inner row that pack
        # centers across the panel's width.
        panel = ttk.LabelFrame(preview, text="Resize to", labelanchor="n", padding=(10, 8))
        panel.pack(side="bottom", fill="x", pady=(10, 0))
        controls = ttk.Frame(panel)
        controls.pack()
        self.resize_w = ttk.Combobox(controls, values=pow2, width=6, state="readonly")
        self.resize_w.pack(side="left", padx=(0, 2))
        ttk.Label(controls, text="×").pack(side="left")
        self.resize_h = ttk.Combobox(controls, values=pow2, width=6, state="readonly")
        self.resize_h.pack(side="left", padx=(2, 0))
        self.resize_w.bind("<<ComboboxSelected>>", self._on_resize_change)
        self.resize_h.bind("<<ComboboxSelected>>", self._on_resize_change)

        images = ttk.Frame(preview)
        images.pack(side="top", fill="both", expand=True)
        images.rowconfigure(0, weight=1)
        images.columnconfigure(0, weight=1, uniform="tex")  # equal-width columns
        images.columnconfigure(1, weight=1, uniform="tex")
        self.base_image, self.base_caption = self._texture_column(images, "Base", 0)
        self.resized_image, self.resized_caption = self._texture_column(images, "Resized", 1)
        self._base_photo = self._resized_photo = None  # keep refs so Tk doesn't drop them
        # UV usage warnings, under the Base texture only. They get their own grid row so
        # a long warning never squeezes the Base image smaller than the Resized one.
        # Hidden (grid_remove) when there's nothing to say, so it takes no space.
        self.uv_warning = ttk.Label(
            images, text="", justify="center", anchor="center", wraplength=PREVIEW_MAX,
            font=("TkDefaultFont", 11, "bold"))
        self.uv_warning.grid(row=1, column=0, sticky="ew", padx=6, pady=(6, 0))
        self.uv_warning.grid_remove()
        self.uv_warning.bind("<Configure>", self._wrap_uv_warning)
        # Re-fit the preview images whenever their area changes (window / sash resize).
        self.base_image.bind("<Configure>", lambda e: self._render_previews())
        self.resized_image.bind("<Configure>", lambda e: self._render_previews())

        cols = ("status", "size", "texbytes", "gpubytes", "uvusage", "polys", "recommended", "note")
        frame = ttk.Frame(split)
        split.add(frame, weight=2)
        self.tree = ttk.Treeview(frame, columns=cols, show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="Texture")
        self.tree.heading("status", text="Status")
        self.tree.heading("size", text="Dimensions")
        self.tree.heading("texbytes", text="Texture size")
        self.tree.heading("gpubytes", text="In-game size")
        self.tree.heading("uvusage", text="UV usage")
        self.tree.heading("polys", text="Polygons")
        self.tree.heading("recommended", text="Recommended size")
        self.tree.heading("note", text="Issues")
        self.tree.column("#0", width=180)
        self.tree.column("status", width=64, anchor="center")
        self.tree.column("size", width=90, anchor="center")
        self.tree.column("texbytes", width=90, anchor="e")
        self.tree.column("gpubytes", width=90, anchor="e")
        self.tree.column("uvusage", width=90, anchor="e")
        self.tree.column("polys", width=80, anchor="e")
        self.tree.column("recommended", width=110, anchor="center")
        self.tree.column("note", width=300)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.row_report: dict[str, cabinet.TextureReport] = {}
        self._flash_after = None  # pending flash callback, so re-selection can cancel it

        # Resize UI is shelved for now (revisit later). The button is still created so
        # _populate can toggle it, but it and its caption are not packed / shown.
        bottom = ttk.Frame(self, padding=10)
        # bottom.pack(fill="x")
        self.resize_btn = ttk.Button(
            bottom, text="Resize textures that need it...", command=self.resize_all, state="disabled")
        # self.resize_btn.pack(side="left")
        # ttk.Label(bottom, text="Your original cabinet is never changed. A new zip is saved.").pack(
        #     side="left", padx=10)

    def _texture_column(self, parent, title, col):
        """One equal-width texture column (Base or Resized): sub-title, image, caption.

        The caption is packed to the bottom first so it always stays visible, even
        when a tall image would otherwise push it off the panel.

        The image sits in a holder with geometry propagation OFF. Without that, the
        image label sizes itself to whatever PhotoImage we set, which changes its
        allocated area, which fires <Configure>, which re-fits the image to the new
        area -- a loop that, for some aspect ratios (e.g. a 1024x1024 texture resized
        to 8x16), never settles and freezes the app. With propagation off, the holder's
        size is driven top-down by the panel, so the image can never resize its own box.
        """
        frame = ttk.Frame(parent)
        frame.grid(row=0, column=col, sticky="nsew", padx=6)
        ttk.Label(frame, text=title, foreground="#555555").pack(side="top")
        # anchor centers the line in the column's width; justify centers wrapped lines.
        caption = ttk.Label(frame, text="", wraplength=PREVIEW_MAX, justify="center", anchor="center")
        caption.pack(side="bottom", fill="x", pady=(4, 0))
        holder = ttk.Frame(frame)
        holder.pack(side="top", fill="both", expand=True, pady=6)
        holder.pack_propagate(False)
        image = tk.Label(holder, background="#ffffff", anchor="center")
        image.pack(fill="both", expand=True)
        return image, caption

    def open_zip(self):
        path = filedialog.askopenfilename(
            title="Choose a cabinet", filetypes=[("Cabinet zip", "*.zip"), ("All files", "*.*")])
        if path:
            self._load_cabinet(path)

    def open_folder(self):
        path = filedialog.askdirectory(title="Choose a cabinet folder", mustexist=True)
        if path:
            self._load_cabinet(path)

    def _load_cabinet(self, path):
        """Load a cabinet from a .zip or a folder into the whole UI."""
        try:
            reports = cabinet.check_cabinet(path)
        except Exception as exc:
            messagebox.showerror("Could not open cabinet", f"{Path(path).name} could not be read.\n\n{exc}")
            return
        self.cabinet_path, self.reports = path, reports
        self.path_label.config(text=Path(path).name)
        self.export_folder_btn.config(state="normal")
        self.export_zip_btn.config(state="normal")
        self.export_name.delete(0, "end")
        self.export_name.insert(0, sources.default_export_name(path))
        self._update_cache_warning(path)
        self._populate()
        self._load_model(path)

    def _update_cache_warning(self, path):
        # Show the .aojv1 line when the cabinet carries Age of Joy cache files (usually a
        # folder opened straight out of the game's cabinetsdb); hide it otherwise.
        try:
            self.cache_files = cabinet.aoj_cache_files(path)
        except Exception:
            self.cache_files = {}
        n = len(self.cache_files)
        if not n:
            self.cache_label.pack_forget()
            return
        self.cache_label.config(
            text=f"Warning: Found {n} Age of Joy cache file{'s' if n != 1 else ''} "
                 f"(.aojv1, {human_bytes(sum(self.cache_files.values()))}). The game makes "
                 f"these itself, so don't share them. Exports leave them out automatically.")
        self.cache_label.pack(fill="x", after=self.screen_label)

    def _export_targets(self) -> dict[str, tuple[int, int]]:
        """The textures the export should re-encode: name -> new size, for every texture
        whose effective size (the user's dropdown pick, else its recommendation) differs
        from its current size. Textures left at their original size are copied unchanged."""
        targets = {}
        for r in self.reports:
            if r.unreadable:
                continue
            size = self._resized_size_for(r)
            if size is not None and size != (r.width, r.height):
                targets[r.name] = size
        return targets

    def _export_base_name(self) -> str:
        name = self.export_name.get().strip()
        return name or sources.default_export_name(self.cabinet_path)

    def _footprint_message(self) -> str:
        """A sentence about how the export changed the cabinet's in-game memory footprint,
        comparing the original total against the chosen resizes. Blank when there's nothing
        to compare (no readable textures) or the footprint is unchanged."""
        original = sum(r.ingame_bytes for r in self.reports if not r.unreadable)
        resized = sum(self._resized_ingame_bytes(r) for r in self.reports)
        if original <= 0 or resized == original:
            return ""
        pct = abs(resized - original) / original * 100
        if resized < original:
            return f"This cabinet's in-game memory footprint has been reduced a total of {pct:.0f} percent!"
        return (f"This cabinet's in-game memory footprint has been increased a total of "
                f"{pct:.0f} percent. Are you sure you meant to do that?")

    def export_to_folder(self):
        """Duplicate the open cabinet into a new folder, applying the chosen resizes."""
        if not self.cabinet_path:
            return
        parent = filedialog.askdirectory(
            title="Choose where to save the new folder", mustexist=True,
            initialdir=self._export_dialog_dir())
        if not parent:
            return
        dest = Path(parent) / self._export_base_name()
        try:
            out = resize.export_folder(self.cabinet_path, dest, self._export_targets())
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))
            return
        self._show_export_done(out)

    def export_to_zip(self):
        """Duplicate the open cabinet into a new .zip, applying the chosen resizes."""
        if not self.cabinet_path:
            return
        name = self._export_base_name()
        if name.lower().endswith(".zip"):
            name = name[:-4]
        dest = filedialog.asksaveasfilename(
            title="Export cabinet as a zip", defaultextension=".zip",
            initialfile=f"{name}.zip", initialdir=self._export_dialog_dir(),
            filetypes=[("Cabinet zip", "*.zip")])
        if not dest:
            return
        try:
            out = resize.export_zip(self.cabinet_path, dest, self._export_targets())
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))
            return
        self._show_export_done(out)

    def _export_dialog_dir(self) -> str:
        # Start the export dialogs NEXT TO the cabinet, not inside it. Left to itself
        # Windows opens the last folder used, which after "Open cabinet (folder)" is the
        # cabinet folder -- and an export saved there lands inside the cabinet.
        return str(Path(self.cabinet_path).parent)

    def _show_export_done(self, out):
        footprint = self._footprint_message()
        lines = [f"Saved to:\n{out}", "Your original cabinet is untouched."]
        if self.cache_files:
            n = len(self.cache_files)
            lines.append(f"Left out {n} Age of Joy cache file{'s' if n != 1 else ''} (.aojv1).")
        if footprint:
            lines.append(footprint)
        messagebox.showinfo("Exported", "\n\n".join(lines))

    def _populate(self):
        self.tree.delete(*self.tree.get_children())
        self.row_report.clear()
        self._resize_overrides.clear()  # a new cabinet: drop the previous one's dropdown picks
        self._selected_report = None
        self._set_resize_targets(None)
        self._refresh_texture_preview()
        for r in self.reports:
            size = "?" if r.unreadable else f"{r.width}x{r.height}"
            texbytes = human_bytes(r.file_bytes)
            gpubytes = "?" if r.unreadable else human_bytes(r.ingame_bytes)
            recommended = self._recommended_text(r)
            note = self._issues_text(r)
            # UV usage and Polygons are filled in once the 3D model finishes loading
            # (see _update_model_columns).
            item = self.tree.insert(
                "", "end", text=r.name,
                values=(SEVERITY_LABEL[r.severity], size, texbytes, gpubytes, "…", "…",
                        recommended, note))
            self.row_report[item] = r
        self._update_totals()
        n_resize = sum(1 for r in self.reports if r.needs_resize)
        # The pass/fail summary line is intentionally gone for now; a different way to
        # flag textures that need fixing will come later.
        self.summary.config(text="No textures found in this zip." if not self.reports else "")
        self.resize_btn.config(state="normal" if n_resize else "disabled")
        self._autosize_columns()

    def _issues_text(self, report) -> str:
        # The "Issues" cell: each issue's message joined into one string, led by
        # "Huge texture." and then the "Unreferenced in description.yaml." note.
        messages = [i.message for i in report.issues]
        messages.sort(key=lambda m: 0 if m == rules.HUGE_MESSAGE
                      else 1 if m.startswith("Unreferenced in description.yaml") else 2)
        # When we suggest a smaller size (a non-blank "Recommended size"), nudge the user.
        if self._recommended_text(report):
            messages.append("Consider resizing.")
        return " ".join(messages)

    def _recommended_text(self, report) -> str:
        # The "Recommended size" column: the same value the dropdowns default to, so the
        # column and the applied resize never disagree. Blank when nothing is recommended.
        rec = self._recommended_default(report)
        return "%dx%d" % rec if rec else ""

    def _recommended_default(self, report) -> "tuple[int, int] | None":
        """The size the dropdowns default to (and the export applies): the detail-based
        recommendation when one is offered (e.g. 512x512); otherwise, for a texture that
        is not a power of two, the nearest smaller power of two so it complies with the
        standard. None means no change -- the texture is already compliant and its detail
        justifies its size, so the dropdowns stay blank unless the author picks something."""
        if report is None or report.unreadable:
            return None
        rec = report.recommended_size
        if rec is None and not (
                rules.is_power_of_two(report.width) and rules.is_power_of_two(report.height)):
            rec = rules.suggested_size(report.width, report.height)
        if rec is None or rec == (report.width, report.height):
            return None
        return rec

    def _resized_size_for(self, report) -> "tuple[int, int] | None":
        """The size this texture will end up at: the author's dropdown pick if they've made
        one, else its detail-based recommendation, else its own current size."""
        if report.unreadable:
            return None
        override = self._resize_overrides.get(report.name.lower())
        if override is not None:
            return override
        rec = self._recommended_default(report)
        return rec if rec is not None else (report.width, report.height)

    def _resized_ingame_bytes(self, report) -> int:
        size = self._resized_size_for(report)
        if size is None:
            return 0
        w, h = size
        return w * h * (4 if report.has_alpha else 3)

    def _update_totals(self):
        """Refresh the top-bar byte totals. The Resized total reflects the user's dropdown
        picks, so it updates live as they retarget individual textures."""
        if not self.reports:
            self.stats.config(text="")
            return
        total_file = sum(r.file_bytes for r in self.reports)
        total_ingame = sum(r.ingame_bytes for r in self.reports)
        total_resized = sum(self._resized_ingame_bytes(r) for r in self.reports)
        self.stats.config(
            text=f"Total texture size: {human_bytes(total_file)}     "
                 f"Total in-game size (Original): {human_bytes(total_ingame)}     "
                 f"Total in-game size (Resized): {human_bytes(total_resized)}")
        self._sort_rows()

    def _sort_rows(self):
        """Order the table rows by in-game size, largest first. Keyed on each texture's
        effective (resized) in-game footprint, so the order tracks the Resized total and
        re-settles whenever the user retargets a texture. Unreadable rows sort to the end."""
        items = sorted(
            self.tree.get_children(),
            key=lambda i: self._resized_ingame_bytes(self.row_report[i]), reverse=True)
        for index, item in enumerate(items):
            self.tree.move(item, "", index)

    def _autosize_columns(self):
        """Size each column to its widest cell or heading, so nothing is padded out."""
        cell_font = tkfont.nametofont("TkDefaultFont")
        head_font = tkfont.nametofont("TkHeadingFont")
        PAD = 20  # left/right cell padding, plus room for the heading's sort arrow
        for col in ("#0", *self.tree["columns"]):
            width = head_font.measure(self.tree.heading(col, "text"))
            for item in self.tree.get_children():
                value = self.tree.item(item, "text") if col == "#0" else self.tree.set(item, col)
                width = max(width, cell_font.measure(str(value)))
            # stretch=False so the column keeps exactly this width; otherwise ttk shares
            # the table's spare width across all columns, padding them well past content.
            self.tree.column(col, width=width + PAD, stretch=False)

    def _on_select(self, _event=None):
        selection = self.tree.selection()
        report = self.row_report.get(selection[0]) if selection else None
        self._selected_report = report
        self._set_resize_targets(report)   # seed the dropdowns before we read them
        self._refresh_texture_preview()
        if report is not None:
            self._flash_texture(report)
            self._focus_on(report)

    def _focus_on(self, report: "cabinet.TextureReport"):
        # With Auto Focus on, glide the camera to frame the part this texture is
        # applied to. Textures with no matching mesh (or when off) leave the view.
        if self.model is None or self.renderer is None or not self.auto_focus.get():
            return
        target = self.model.focus_targets.get(report.name.lower())
        if target is None:
            center, radius = self.model.center, self.model.radius  # no mesh: pull back to full view
        else:
            center, radius = target
            radius = max(radius, self.model.radius * 0.12)  # don't zoom in so far it clips
        self._animate_camera(center, radius)

    def _animate_camera(self, target_center, target_radius, duration_ms=250, step_ms=16):
        if self.model is None or self.renderer is None:
            return
        if self._cam_anim_after is not None:
            self.after_cancel(self._cam_anim_after)
            self._cam_anim_after = None
        start_c = np.array(self.cam_center, dtype=np.float32)
        start_r = float(self.cam_radius)
        end_c = np.array(target_center, dtype=np.float32)
        end_r = float(target_radius)
        steps = max(1, duration_ms // step_ms)

        def step(i=1):
            t = i / steps
            t = t * t * (3 - 2 * t)  # smoothstep, so it eases in and out
            self.cam_center = start_c + (end_c - start_c) * t
            self.cam_radius = start_r + (end_r - start_r) * t
            self._render_model()
            self._cam_anim_after = self.after(step_ms, lambda: step(i + 1)) if i < steps else None

        step()

    def _flash_texture(self, report: "cabinet.TextureReport"):
        # Flash the selected texture three times within one second: yellow on the 3D
        # model, so the eye is drawn to which part uses it, and red over the Base
        # preview's unused UV space, to call out texture area the model wastes. Both
        # share one timer: six ~166ms steps (on/off x3), the last clearing the tints.
        if self._flash_after is not None:
            self.after_cancel(self._flash_after)
            self._flash_after = None
        name = report.name.lower()
        # Only textures applied to a mesh can flash on the model, and only ones whose
        # UVs leave part of the map unused have any red to show.
        on_model = (self.model is not None and self.renderer is not None
                    and name in self.model.focus_targets)
        on_uvs = name in self._uv_tris
        self._highlight_name = name if on_model else None
        self._uv_flash_name = name if on_uvs else None
        self._highlight = self._uv_flash = 0.0
        if not (on_model or on_uvs):
            return
        amounts = [0.8, 0.0, 0.8, 0.0, 0.8, 0.0]

        def step(i=0):
            if on_model:
                self._highlight = amounts[i]
                self._render_model()
            if on_uvs:
                self._uv_flash = amounts[i]
                self._render_previews()
            if i + 1 < len(amounts):
                self._flash_after = self.after(166, lambda: step(i + 1))
            else:
                self._highlight, self._highlight_name = 0.0, None
                self._uv_flash, self._uv_flash_name = 0.0, None
                self._flash_after = None

        step()

    def _on_resize_change(self, _event=None):
        # Remember this texture's chosen size and refresh the Resized total so it tracks
        # what the user actually picked. Nothing is auto-resized: a texture only counts
        # as resized once the user picks a size for it here.
        report = self._selected_report
        size = self._resize_target()
        if report is not None and size is not None:
            self._resize_overrides[report.name.lower()] = size
            self._update_totals()
        self._refresh_texture_preview()

    def _refresh_texture_preview(self):
        """Show the base texture and its resized result side by side (2D box)."""
        self._preview_sig = None  # content changed: force the next _render_previews to redraw
        self._update_uv_warning()
        report = self._selected_report
        if report is None:
            self._base_src = self._resized_src = None
            self.base_caption.config(text="Select a texture")
            self.resized_caption.config(text="")
            self._render_previews()
            self._apply_3d_overrides()
            return
        source = self._texture_source_image(report.name)
        if source is None:
            self._base_src = self._resized_src = None
            self.base_caption.config(text=f"{report.name}\n(cannot preview this image)")
            self.resized_caption.config(text="")
            self._render_previews()
            self._apply_3d_overrides()
            return
        self._base_src = source
        self.base_caption.config(text=self._texture_info(report, (report.width, report.height)))
        size = self._resize_target()
        if size is None:
            # Nothing is auto-resized: until the user picks a size there is no resized
            # result to show, so leave the Resized panel empty with a prompt.
            self._resized_src = None
            self._resized_no_change = False
            self.resized_caption.config(text="Choose a size to preview a resize")
        else:
            # When the chosen size equals the original, nothing actually changes, so grey
            # the resized preview out to say "no resize needed". A different size
            # (e.g. 1024 -> 128) lights up in its new, resampled form.
            self._resized_no_change = size == (report.width, report.height)
            self._resized_src = self._resized_texture(report.name.lower(), source, size)
            self.resized_caption.config(
                text="No resize needed" if self._resized_no_change else self._texture_info(report, size))
        self._render_previews()
        self._apply_3d_overrides()

    def _update_uv_warning(self):
        # Warn under the Base texture when the selected texture's UVs leave too much of
        # it unused (tiers in rules.UV_USAGE_TIERS). Red for the errors, orange for a
        # plain warning; hidden when the usage is fine or not measured (tiled, no model).
        report = self._selected_report
        usage = None if report is None or report.unreadable else self._uv_coverage.get(report.name.lower())
        issues = [] if usage is None else rules.uv_usage_issues(usage, report.width, report.height)
        if not issues:
            self.uv_warning.config(text="")
            self.uv_warning.grid_remove()
            return
        color = "#c00000" if any(i.severity == rules.ERROR for i in issues) else "#c06000"
        self.uv_warning.config(text="\n".join(i.message for i in issues), foreground=color)
        self.uv_warning.grid()

    def _wrap_uv_warning(self, event):
        # Wrap the warning to its column's width (grid sets the label's width), so it
        # breaks into lines instead of pushing the column wider. Only on a real change,
        # since re-wrapping resizes the label and fires <Configure> again.
        width = max(100, event.width - 4)
        if str(self.uv_warning.cget("wraplength")) != str(width):
            self.uv_warning.configure(wraplength=width)

    def _render_previews(self):
        """(Re)build the base/resized preview images sized to the panel's current area.

        Setting an image can nudge the layout and fire the labels' <Configure>, which
        calls back here; for some aspect ratios that never settles and spins forever.
        So skip when nothing that affects the drawing has changed: the boxes are the
        same and the content is the same (_refresh_texture_preview clears the signature
        on a real change). This makes a no-op <Configure> a cheap early return.
        """
        base_box = self._preview_box(self.base_image) if self._base_src is not None else None
        resized_box = self._preview_box(self.resized_image) if self._resized_src is not None else None
        report = self._selected_report
        # The unused-UV flash belongs to one texture; never tint a different one.
        flash = (self._uv_flash if report is not None and report.name.lower() == self._uv_flash_name
                 else 0.0)
        sig = (id(self._base_src), base_box, id(self._resized_src), resized_box,
               self._resized_no_change, flash)
        if sig == self._preview_sig:
            return
        self._preview_sig = sig
        self._base_photo = self._resized_photo = None
        if self._base_src is None:
            self._base_fit = None
            self.base_image.config(image="")
            self.resized_image.config(image="")
            return
        self._base_photo = self._base_preview_photo(base_box, flash)
        self.base_image.config(image=self._base_photo)
        if self._resized_src is not None:
            self._resized_photo = self._display_photo(
                self._resized_src, resized_box,
                sharp=not self._resized_no_change, dim=self._resized_no_change)
            self.resized_image.config(image=self._resized_photo)
        else:
            self.resized_image.config(image="")

    def _preview_box(self, label) -> "tuple[int, int]":
        # Space available for the image inside its label (a small margin so the 1px
        # border never spills). Falls back before the panel has been laid out.
        w, h = label.winfo_width(), label.winfo_height()
        if w <= 1 or h <= 1:
            return PREVIEW_MAX, PREVIEW_MAX
        return max(16, w - 6), max(16, h - 6)

    def _resize_target(self) -> "tuple[int, int] | None":
        try:
            return int(self.resize_w.get()), int(self.resize_h.get())
        except ValueError:
            return None

    def _texture_source_image(self, name: str) -> "Image.Image | None":
        # Prefer the model's own texture (alpha preserved) so the 3D preview matches;
        # fall back to reading it flat for textures that aren't on the mesh.
        if self.model is not None:
            for part in self.model.parts:
                if part.texture_name == name.lower() and part.texture is not None:
                    return part.texture
        return self._load_image(name)

    def _base_preview_photo(self, box, flash: float) -> "ImageTk.PhotoImage":
        # The Base texture, with its unused UV space tinted red by `flash` (0..1). The
        # scaled image and the mask are cached, so each flash step is just a blend:
        # scaling a big texture or rasterizing a dense mesh's UVs can take ~100ms,
        # which would throw off the flash's rhythm.
        src = self._base_src
        if self._base_fit is None or self._base_fit[0] is not src or self._base_fit[1] != box:
            self._base_fit = (src, box, self._fit_image(src, box))
        img = self._base_fit[2]
        if flash > 0:
            name = self._uv_flash_name
            if self._uv_mask is None or self._uv_mask[:2] != (name, img.size):
                self._uv_mask = (name, img.size, preview3d.uv_mask(self._uv_tris[name], img.size))
            red = Image.blend(img, Image.new("RGB", img.size, UV_FLASH_COLOR), flash)
            img = Image.composite(img, red, self._uv_mask[2])  # used UV space keeps its art
        return self._framed_photo(img)

    def _display_photo(self, img: "Image.Image", box, sharp: bool = False,
                       dim: bool = False) -> "ImageTk.PhotoImage":
        img = self._fit_image(img, box, sharp)
        if dim:
            # Wash it toward light grey so it reads as inactive ("nothing to change here").
            img = Image.blend(img, Image.new("RGB", img.size, (235, 235, 235)), 0.6)
        return self._framed_photo(img)

    def _framed_photo(self, img: "Image.Image") -> "ImageTk.PhotoImage":
        # A 1px grey frame so the texture's edges show even when it is white/transparent.
        return ImageTk.PhotoImage(ImageOps.expand(img, border=1, fill=(180, 180, 180)))

    def _fit_image(self, img: "Image.Image", box, sharp: bool = False) -> "Image.Image":
        # Composite alpha onto white for the panel.
        if img.mode == "RGBA":
            bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
            img = Image.alpha_composite(bg, img).convert("RGB")
        elif img.mode != "RGB":
            img = img.convert("RGB")
        # Scale to fit inside `box`, preserving aspect (so a square texture stays square
        # and is never cropped), enlarging small textures too so a tiny resized texture
        # (e.g. 8x8) isn't microscopic. `sharp` enlarges with NEAREST so the resized
        # pixels stay crisp/blocky and the resolution drop is visible; otherwise LANCZOS.
        bw, bh = box
        w, h = img.size
        scale = min(bw / w, bh / h)
        if scale != 1:
            resample = Image.NEAREST if (scale > 1 and sharp) else Image.LANCZOS
            img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), resample)
        return img

    def _resized_texture(self, name, source, size):
        """Resize `source` to `size`, cached in memory by (name, size)."""
        key = (name, size)
        img = self._resized_cache.get(key)
        if img is None:
            img = resize.resize_image(source, size)
            self._resized_cache[key] = img
        return img

    def _apply_3d_overrides(self):
        """Push resized textures onto the 3D model per the 'Show resized textures' toggle.

        Each texture is shown at its effective size -- the author's dropdown pick if any,
        otherwise its detail-based recommendation -- and the selected texture follows its
        live dropdown so you can tune one and see it. Textures with no reduction stay at
        their original size. All resized images are cached in memory.
        """
        if self.model is None or self.renderer is None:
            return
        overrides = {}
        if self.show_resized.get():
            reports_by_name = {r.name.lower(): r for r in self.reports}
            for part in self.model.parts:
                name = part.texture_name
                if not name or part.texture is None or name in overrides:
                    continue
                rep = reports_by_name.get(name)
                if rep is None:
                    continue  # embedded GLB texture with no report: nothing to recommend
                size = self._resized_size_for(rep)
                if size is not None and size != part.texture.size:
                    overrides[name] = self._resized_texture(name, part.texture, size)
            report = self._selected_report
            size = self._resize_target()
            if report is not None and size is not None:
                source = self._texture_source_image(report.name)
                if source is not None:
                    overrides[report.name.lower()] = self._resized_texture(
                        report.name.lower(), source, size)
        self.renderer.set_overrides(overrides)
        self._render_model()

    def _texture_info(self, report, size) -> str:
        # Centered info line under the (resized) texture: name, resolution, mode, in-game size.
        w, h = size
        mode = "RGBA" if report.has_alpha else "RGB"
        ingame = w * h * (4 if report.has_alpha else 3)
        return f"{report.name}      {w}×{h}      {mode}      {human_bytes(ingame)} in-game"

    def _set_resize_targets(self, report):
        # Default the dropdowns to the detail-based recommendation (e.g. 512x512), so the
        # guesswork is done for the author and they only need to override it. When no
        # reduction is recommended the dropdowns stay blank (keep the current size). An
        # earlier explicit pick by the author wins. Only offer sizes up to each source
        # dimension, so a texture can never be accidentally upscaled (adds cost, no detail).
        if report is None or report.unreadable:
            self.resize_w["values"] = self.resize_h["values"] = []
            self.resize_w.set("")
            self.resize_h.set("")
            return
        self.resize_w["values"] = [str(n) for n in rules.power_of_two_options() if n <= report.width]
        self.resize_h["values"] = [str(n) for n in rules.power_of_two_options() if n <= report.height]
        choice = self._resize_overrides.get(report.name.lower()) or self._recommended_default(report)
        if choice is not None:
            self.resize_w.set(str(choice[0]))
            self.resize_h.set(str(choice[1]))
        else:
            self.resize_w.set("")
            self.resize_h.set("")

    def _load_image(self, name: str) -> "Image.Image | None":
        if not self.cabinet_path:
            return None
        try:
            with sources.open_source(self.cabinet_path) as src:
                data = src.read(name)
            img = Image.open(io.BytesIO(data))
            img.load()
            # Composite onto white so transparent textures read clearly on the light panel.
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGBA")
                bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
                img = Image.alpha_composite(bg, img).convert("RGB")
            else:
                img = img.convert("RGB")
            return img
        except Exception:
            return None

    # --- 3D cabinet preview ------------------------------------------------
    # Loading (trimesh parse) is CPU work and runs on a worker thread. The GL
    # renderer owns an OpenGL context, which is single-threaded, so uploading and
    # drawing happen on the main thread. GPU frames are ~1-2ms, so drag renders
    # synchronously with no threading or coalescing.

    def _ensure_renderer(self):
        if self.renderer is None and not self._renderer_failed:
            try:
                self.renderer = preview3d.Renderer()
            except Exception:
                self._renderer_failed = True
        return self.renderer

    def _load_model(self, path: str):
        self.model = None
        self.azimuth, self.elevation = 0.6, 0.15
        if self._cam_anim_after is not None:
            self.after_cancel(self._cam_anim_after)
            self._cam_anim_after = None
        if self._flash_after is not None:
            self.after_cancel(self._flash_after)
            self._flash_after = None
        self._highlight_name, self._highlight = None, 0.0
        self._uv_coverage, self._uv_tris, self._uv_mask = {}, {}, None
        self._uv_flash_name, self._uv_flash = None, 0.0
        self._model_photo = None
        self._rendered_size = None
        self.model_view.config(image="", text="Building 3D preview...", foreground="#999999")
        self.screen_label.config(text="")  # clear any previous cabinet's screen warning
        self.poly_label.config(text="")    # ...and its polygon total
        self._load_gen += 1
        gen = self._load_gen

        def work():
            try:
                model = preview3d.build_model(path)
                err = None
            except Exception as exc:  # trimesh failure, unreadable GLB, etc.
                model, err = None, exc
            self.after(0, lambda: self._on_model_ready(gen, model, err))

        threading.Thread(target=work, daemon=True).start()

    def _on_model_ready(self, gen, model, err):
        if gen != self._load_gen:
            return  # a newer cabinet was opened while this one was loading
        if err is not None or model is None or model.empty:
            if isinstance(err, ImportError):
                # The 3D libraries aren't installed in the Python running the app (the
                # checker itself only needs Pillow), which is not the cabinet's fault.
                text = ("3D preview unavailable: this Python is missing\n"
                        f"'{err.name or err}'. Run: pip install -r requirements.txt")
            elif err is not None:
                text = f"Could not build the 3D preview:\n{err}"
            else:
                text = "No 3D model found in this cabinet."
            self.model_view.config(image="", text=text)
            self._update_model_columns(None)
            self._update_screen_status(None)
            self._update_poly_status(None)
            return
        # UV coverage and polygon counts come from the CPU model build, so fill them in
        # even if GL fails below.
        self._update_model_columns(model)
        self._update_screen_status(model)
        self._update_poly_status(model)
        # Also CPU-side, so the unused-UV flash and warnings work without GL. Textures
        # whose UVs cover the whole map have nothing to flash.
        self._uv_coverage = model.uv_coverage
        self._uv_tris = {name: tris for name, tris in model.uv_tris.items()
                         if model.uv_coverage.get(name, 100.0) < 100.0}
        self._update_uv_warning()  # a texture may have been selected while this loaded
        renderer = self._ensure_renderer()
        if renderer is None:
            self.model_view.config(image="", text="3D preview unavailable\n(no OpenGL on this machine).")
            return
        try:
            renderer.upload(model)
        except Exception:
            self.model_view.config(image="", text="Could not load this model for preview.")
            return
        self.model = model
        self.cam_center = model.center
        self.cam_radius = model.radius
        self._resized_cache = {}  # resized textures from a previous model no longer apply
        self._render_model()
        self._apply_3d_overrides()  # honor the "Show resized textures" toggle for this model
        # If a texture was selected while the model was still loading, apply its resize
        # preview and focus now -- otherwise that first click never focuses (model was None).
        if self._selected_report is not None:
            self._refresh_texture_preview()
            self._focus_on(self._selected_report)

    def _update_model_columns(self, model):
        # Fill the UV usage and Polygons cells for each row. UV usage is a percentage
        # when we could measure it; "Tiled" when the texture repeats across its mesh (so
        # all of it is used); "No UVs" when it is on a mesh that carries no UVs. Polygons
        # is the triangle count of the meshes the texture is applied to. Both read "—"
        # when the texture isn't mapped onto any mesh.
        coverage = model.uv_coverage if model is not None else {}
        tiled = model.uv_tiled if model is not None else set()
        on_mesh = model.focus_targets if model is not None else {}
        polys = model.poly_counts if model is not None else {}
        for item, report in self.row_report.items():
            name = report.name.lower()
            if name in coverage:
                text = f"{coverage[name]:.0f}%"
            elif name in tiled:
                text = "Tiled"
            elif name in on_mesh:
                text = "No UVs"
                self._add_no_uv_issue(report)  # note it in the Issues column too
            else:
                text = "—"
            self.tree.set(item, "status", SEVERITY_LABEL[report.severity])  # may have changed
            self.tree.set(item, "uvusage", text)
            self.tree.set(item, "polys", f"{polys[name]:,}" if name in polys else "—")
            self.tree.set(item, "note", self._issues_text(report))
        self._autosize_columns()  # UV usage / Polygons / Issues text changed, re-fit

    def _update_screen_status(self, model):
        # Show the red 4:3 warning only when the cabinet ships a custom CRT screen mesh
        # whose measured aspect isn't 4:3. Non-custom cabinets, a compliant custom screen,
        # or a mesh we couldn't find leave the line blank.
        screen = model.screen if model is not None else None
        if screen is not None and screen.found and not screen.ok:
            self.screen_label.config(text="Warning: Custom Screentype may not be 4:3 aspect ratio!")
        else:
            self.screen_label.config(text="")

    def _update_poly_status(self, model):
        # The total polygon count, or the tiered alert when it's over budget. Blank when
        # there's no model to count.
        if model is None:
            self.poly_label.config(text="")
            return
        # Name any extra models (e.g. a lightgun) so the builder knows where polygons are.
        extra = ", ".join(f"{t:,} from {n}" for n, t in model.other_model_polys.items())
        issue = rules.poly_count_issue(model.total_polys)
        if issue is None:
            text = f"Total polygons: {model.total_polys:,}" + (f" (includes {extra})" if extra else "")
            self.poly_label.config(text=text, foreground="")
        else:
            color = "#c00000" if issue.severity == rules.ERROR else "#c06000"
            text = issue.message + (f" Includes {extra}." if extra else "")
            self.poly_label.config(text=text, foreground=color)

    def _add_no_uv_issue(self, report):
        msg = "Target mesh has no UVs, so will be flat color."
        if not any(i.message == msg for i in report.issues):
            report.issues.append(rules.Issue(rules.INFO, msg))

    def _model_size(self) -> tuple[int, int]:
        # The 3D pane's pixel size, so the render matches its aspect ratio (no square
        # crop). Scaled down so the longest edge stays within MODEL_MAX for speed.
        w, h = self.model_view.winfo_width(), self.model_view.winfo_height()
        if w <= 1 or h <= 1:
            return 480, 480
        scale = min(1.0, MODEL_MAX / max(w, h))
        return max(1, round(w * scale)), max(1, round(h * scale))

    def _render_model(self):
        if self.model is None or self.renderer is None:
            return
        w, h = self._model_size()
        try:
            img = self.renderer.render(
                self.azimuth, self.elevation, w, h, self.cam_center, self.cam_radius,
                highlight_names={self._highlight_name} if self._highlight_name else None,
                highlight=self._highlight)
        except Exception:
            return
        self._rendered_size = (w, h)
        self._model_photo = ImageTk.PhotoImage(img)
        self.model_view.config(image=self._model_photo, text="")

    def _drag_start(self, event):
        self._drag_xy = (event.x, event.y)

    def _drag_move(self, event):
        if self.model is None or not hasattr(self, "_drag_xy"):
            return
        dx, dy = event.x - self._drag_xy[0], event.y - self._drag_xy[1]
        self._drag_xy = (event.x, event.y)
        self.azimuth += dx * 0.01
        self.elevation = max(-1.4, min(1.4, self.elevation + dy * 0.01))
        self._render_model()

    def _on_wheel(self, event):
        # Zoom by moving the camera in/out (cam_radius is the framing distance:
        # smaller = closer). Wheel up zooms in. Clamped to the model's extent.
        if self.model is None or self.renderer is None:
            return
        if event.num == 4:      # Linux scroll up
            direction = 1
        elif event.num == 5:    # Linux scroll down
            direction = -1
        elif event.delta:       # Windows / macOS
            direction = 1 if event.delta > 0 else -1
        else:
            return
        if self._cam_anim_after is not None:  # let the zoom win over any focus glide
            self.after_cancel(self._cam_anim_after)
            self._cam_anim_after = None
        factor = 0.88 if direction > 0 else 1 / 0.88
        lo, hi = self.model.radius * 0.08, self.model.radius * 6
        self.cam_radius = max(lo, min(hi, self.cam_radius * factor))
        self._render_model()

    def _on_model_resize(self, event):
        # Re-render to the new pane size, but only when it actually changed.
        if self.model is not None and self._model_size() != self._rendered_size:
            self._render_model()

    def resize_all(self):
        targets = resize.targets_for(self.reports)
        if not targets:
            return
        dest = filedialog.asksaveasfilename(
            title="Save resized cabinet as", defaultextension=".zip",
            initialfile=resize.default_output_path(self.cabinet_path).name,
            initialdir=str(Path(self.cabinet_path).parent), filetypes=[("Cabinet zip", "*.zip")])
        if not dest:
            return
        try:
            done = resize.write_resized_cabinet(self.cabinet_path, dest, targets)
        except Exception as exc:
            messagebox.showerror("Resize failed", str(exc))
            return
        messagebox.showinfo("Done", f"Resized {len(done)} textures.\nSaved to {dest}")


def main():
    App().mainloop()
